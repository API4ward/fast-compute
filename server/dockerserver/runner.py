"""Job orchestration: prepare workspace -> schedule onto a backend -> monitor -> finish -> bill.

Backends (backends.py): a remote compute node (default once nodes are configured) or this
host's Docker. Isolation is applied by whichever side runs the container (see backends.py /
the computenode agent): limits, cap_drop ALL, no-new-privileges, read-only root fs, writable
/workspace only, nobody user, no host mounts / Docker socket, network off unless allowed.

While running, every monitor_interval seconds the job's status/usage is refreshed (docker
stats locally, the node's job status remotely) and the user's credit is checked; at zero the
job is killed (status out_of_credits). A job whose node stays unreachable for node_lost_grace
seconds is marked "lost" and billed up to the last time the node was seen. Each job is
charged once when it ends (billing.py), then its container/remote job is cleaned up.
"""
import logging
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

from .backends import (Backend, BackendError, Handle, JobSpec, LocalDockerBackend, NoCapacity,
                       RemoteNodeBackend)
from .billing import available_uh, bill_job, cost_uh
from .config import Settings
from .models import ACTIVE_STATUSES, TERMINAL_STATUSES, Job, utcnow
from .scheduler import NodePool
from .workspace import WorkspaceError, build_workspace, git_clone

log = logging.getLogger("dockerserver.runner")

# node status word -> our status (when we did not stop the job ourselves)
_NODE_FINAL = {"succeeded": "succeeded", "failed": "failed", "timeout": "timeout",
               "killed": "error", "error": "error"}


class JobRunner:
    def __init__(self, settings: Settings, session_factory, client=None,
                 pool: NodePool | None = None):
        self.settings = settings
        self.Session = session_factory
        local = LocalDockerBackend(settings, client) if settings.use_local else None
        self.pool = pool or NodePool(settings, local=local)
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._handles: dict[int, Handle] = {}
        # job_id -> (final status, error note) for jobs being stopped on request
        self._stop_requested: dict[int, tuple[str, str | None]] = {}

    # ---------- public API ----------
    def submit(self, job_id: int, filename: str | None, content: bytes | None):
        t = threading.Thread(target=self._run, args=(job_id, filename, content),
                             name=f"job-{job_id}", daemon=True)
        with self._lock:
            self._threads[job_id] = t
        t.start()

    def never_fits(self, cpus: float, mem_mb: int) -> str | None:
        if self.pool.local is not None:
            return None
        if not self.pool.remote_enabled:
            return "no compute node is configured and local execution is disabled"
        return self.pool.never_fits(cpus, mem_mb)

    def stop(self, job_id: int, status: str = "stopped", note: str | None = None):
        with self._lock:
            self._stop_requested.setdefault(job_id, (status, note))
            h = self._handles.get(job_id)
        if h is not None:
            h.backend.kill(h)

    def wait(self, job_id: int, timeout: float | None = None):
        t = self._threads.get(job_id)
        if t:
            t.join(timeout)

    def live_logs(self, job: Job, tail: int = 2000) -> str | None:
        with self._lock:
            h = self._handles.get(job.id)
        if h is None:
            return None
        return h.backend.logs(h, tail).decode(errors="replace")

    def log_path(self, job_id: int) -> Path:
        return self.settings.logs_dir / f"job-{job_id}.log"

    def recover(self):
        """On startup: finish jobs left active by a previous process, bill what's unbilled."""
        with self.Session() as db:
            for job in db.query(Job).filter(Job.status.in_(ACTIVE_STATUSES)):
                finished = utcnow()
                b = self.pool.by_name(job.node)
                if isinstance(b, RemoteNodeBackend) and job.container_id:
                    h = Handle(b, job.container_id, b.name)
                    try:
                        st = b.status(h)
                        if not st.finished:
                            b.kill(h)
                            b.wait(h, 15)
                            st = b.status(h)
                        if st.duration_seconds is not None and job.started_at:
                            finished = job.started_at + timedelta(seconds=st.duration_seconds)
                    except BackendError:
                        pass
                    b.cleanup(h)
                job.status, job.error = "error", "server restarted while job was active"
                job.finished_at = job.finished_at or finished
            db.commit()
            # Charge everything that finished but was never billed (incl. the above).
            for (jid,) in db.query(Job.id).filter(Job.billed.is_(False),
                                                  Job.status.in_(TERMINAL_STATUSES)).all():
                bill_job(db, jid)
        if self.pool.local is not None:
            self.pool.local.cleanup_leftovers()

    # ---------- internals ----------
    def _update(self, job_id: int, **fields):
        with self.Session() as db:
            job = db.get(Job, job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()

    def _stop_info(self, job_id):
        with self._lock:
            return self._stop_requested.get(job_id)

    def _has_credit_for_start(self, job_id, cpus, mem_mb) -> bool:
        with self.Session() as db:
            job = db.get(Job, job_id)
            return available_uh(db, job.user_id) >= cost_uh(cpus, mem_mb, self.settings.min_start_seconds)

    def _run(self, job_id: int, filename, content):
        s = self.settings
        h: Handle | None = None
        try:
            with self.Session() as db:
                job = db.get(Job, job_id)
                spec = JobSpec(job_id, job.image, job.command, job.cpus, job.mem_mb,
                               job.timeout_seconds, job.network)
                git_url = job.git_url
            self._update(job_id, status="preparing")
            uid, gid = (int(x) for x in s.container_user.split(":"))
            if git_url:
                with tempfile.TemporaryDirectory() as td:
                    dest = Path(td) / "repo"
                    git_clone(git_url, dest, s.git_timeout)
                    payload = build_workspace(filename, content, dest, uid, gid, s.max_upload_bytes)
            else:
                payload = build_workspace(filename, content, None, uid, gid, s.max_upload_bytes)

            h = self._schedule(job_id, spec, payload)
            if h is None:
                return  # final status already recorded
            with self._lock:
                self._handles[job_id] = h
            self._update(job_id, status="running", started_at=utcnow(), node=h.node,
                         container_id=h.id)
            if self._stop_info(job_id):       # stop raced with start
                h.backend.kill(h)
            self._monitor_and_finish(job_id, spec, h)
        except WorkspaceError as e:
            self._update(job_id, status="error", error=str(e), finished_at=utcnow())
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            self._update(job_id, status="error", error=f"{type(e).__name__}: {e}", finished_at=utcnow())
        finally:
            self._bill(job_id)  # status is final here; charge before the slower cleanup
            if h is not None:
                try:
                    h.backend.cleanup(h)
                except Exception:  # noqa: BLE001
                    log.exception("cleanup of job %s failed", job_id)
            with self._lock:
                self._handles.pop(job_id, None)
                self._stop_requested.pop(job_id, None)

    def _schedule(self, job_id: int, spec: JobSpec, payload: bytes) -> Handle | None:
        """Start the job on the best backend, waiting (status queued) while nothing fits.
        Returns the handle, or None after recording a final status."""
        s = self.settings
        waited_since = time.monotonic()
        queued = False
        while True:
            stop = self._stop_info(job_id)
            if stop:
                self._update(job_id, status=stop[0], error=stop[1], finished_at=utcnow())
                return None
            if not self._has_credit_for_start(job_id, spec.cpus, spec.mem_mb):
                self._update(job_id, status="out_of_credits", finished_at=utcnow(),
                             error="not enough credits to start this job")
                return None
            reason = self.never_fits(spec.cpus, spec.mem_mb)
            if reason:
                self._update(job_id, status="error", error=reason, finished_at=utcnow())
                return None
            errors = []
            for b in self.pool.candidates(spec.cpus, spec.mem_mb):
                self.pool.reserve(b, spec.cpus, spec.mem_mb)
                try:
                    return b.start(spec, payload)
                except NoCapacity as e:
                    errors.append(str(e))
                except BackendError as e:
                    log.warning("job %s: %s", job_id, e)
                    errors.append(str(e))
                finally:
                    self.pool.release(b, spec.cpus, spec.mem_mb)
            if time.monotonic() - waited_since > s.queue_timeout:
                self._update(job_id, status="error", finished_at=utcnow(),
                             error=f"no compute capacity within {int(s.queue_timeout)} s"
                                   + (f" ({errors[-1]})" if errors else ""))
                return None
            if not queued:
                self._update(job_id, status="queued",
                             error="waiting for compute capacity" + (f": {errors[-1]}" if errors else ""))
                queued = True
            time.sleep(s.queue_retry)

    def _monitor_and_finish(self, job_id: int, spec: JobSpec, h: Handle):
        s = self.settings
        b: Backend = h.backend
        deadline = time.monotonic() + spec.timeout_seconds
        reason = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "timeout"
                break
            if b.wait(h, min(remaining, s.monitor_interval)):
                break
            if self._stop_info(job_id):
                b.kill(h)  # idempotent; the node reports the end on a following wait()
            try:
                self._store_stats(job_id, b.status(h))
            except BackendError:
                pass
            if h.unreachable_since and time.time() - h.unreachable_since > s.node_lost_grace:
                reason = "lost"
                break
            with self.Session() as db:
                job = db.get(Job, job_id)
                if available_uh(db, job.user_id) <= 0:
                    reason = "out_of_credits"
                    break
        if reason in ("timeout", "out_of_credits"):
            b.kill(h)
            b.wait(h, 30)

        final = None
        if reason != "lost":
            try:
                final = b.status(h)
                self._store_stats(job_id, final)
            except BackendError:
                final = None
        self._save_logs(job_id, b.logs(h) if reason != "lost" else b"")

        with self.Session() as db:
            job = db.get(Job, job_id)
            started = job.started_at or utcnow()
        if reason == "lost":
            finished = started + timedelta(seconds=max(h.last_seen - started_epoch(started), 0))
        elif final is not None and final.duration_seconds is not None:
            finished = started + timedelta(seconds=final.duration_seconds)
        else:
            finished = utcnow()

        error = "out of memory (OOM killed)" if final is not None and final.oom else None
        exit_code = final.exit_code if final is not None else None
        stop = self._stop_info(job_id)
        if stop:
            status, error = stop[0], stop[1] or error
        elif reason == "lost":
            status, error = "lost", f"compute node {h.node} unreachable for {int(s.node_lost_grace)} s"
        elif reason:
            status = reason
        elif final is not None:
            status = _NODE_FINAL.get(final.node_status) if final.node_status else (
                "succeeded" if exit_code == 0 else "failed")
            if status is None:
                status = "succeeded" if exit_code == 0 else "failed"
            if final.node_status in ("killed", "error") and final.error:
                error = f"node: {final.error}"
        else:
            status, error = "error", "final status unavailable"
        self._update(job_id, status=status, exit_code=exit_code, finished_at=finished, error=error)

    def _store_stats(self, job_id, st):
        if st is None or not st.stat_samples:
            return
        self._update(job_id, stat_samples=st.stat_samples, cpu_avg_pct=st.cpu_avg_pct,
                     cpu_peak_pct=st.cpu_peak_pct, mem_avg_mb=st.mem_avg_mb,
                     mem_peak_mb=st.mem_peak_mb)

    def _bill(self, job_id):
        try:
            with self.Session() as db:
                bill_job(db, job_id)
        except Exception:  # noqa: BLE001 - retried by recover() on next start
            log.exception("billing job %s failed", job_id)

    def _save_logs(self, job_id, data: bytes):
        limit = self.settings.max_log_bytes
        if len(data) > limit:
            data = b"[... log truncated ...]\n" + data[-limit:]
        self.log_path(job_id).write_bytes(data)


def started_epoch(started_naive_utc) -> float:
    """Naive-UTC datetime (as stored) -> epoch seconds."""
    from datetime import timezone
    return started_naive_utc.replace(tzinfo=timezone.utc).timestamp()
