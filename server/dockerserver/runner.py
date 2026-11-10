"""Runs jobs in Docker containers, one fresh container + volume per job.

Isolation applied to every job container:
  * CPU / memory limits from the job (nano_cpus, mem_limit, memswap == mem)
  * pids limit, cap_drop ALL, no-new-privileges, not privileged
  * read-only root fs; writable /workspace (a per-job named volume, via subpath
    so it is owned by the unprivileged user) and a small noexec tmpfs on /tmp
  * runs as nobody (65534); no host bind mounts; no Docker socket
  * network_mode "none" unless the job was allowed network
  * wall-clock timeout, after which the container is killed
"""
import logging
import tempfile
import threading
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import LogConfig, Mount

from .config import Settings
from .models import ACTIVE_STATUSES, Job, utcnow
from .workspace import WorkspaceError, build_workspace, git_clone

log = logging.getLogger("dockerserver.runner")
LABEL = "dockerserver.job"


class JobRunner:
    def __init__(self, settings: Settings, session_factory, client: docker.DockerClient | None = None):
        self.settings = settings
        self.Session = session_factory
        self._client = client
        self._lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._stop_requested: set[int] = set()

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    # ---------- public API ----------
    def submit(self, job_id: int, filename: str | None, content: bytes | None):
        t = threading.Thread(target=self._run, args=(job_id, filename, content),
                             name=f"job-{job_id}", daemon=True)
        with self._lock:
            self._threads[job_id] = t
        t.start()

    def stop(self, job_id: int):
        with self._lock:
            self._stop_requested.add(job_id)
        with self.Session() as db:
            job = db.get(Job, job_id)
            cid = job.container_id if job else None
        if cid:
            try:
                self.client.containers.get(cid).kill()
            except (NotFound, DockerException):
                pass

    def wait(self, job_id: int, timeout: float | None = None):
        t = self._threads.get(job_id)
        if t:
            t.join(timeout)

    def live_logs(self, job: Job, tail: int = 2000) -> str | None:
        if not job.container_id:
            return None
        try:
            c = self.client.containers.get(job.container_id)
            return c.logs(stdout=True, stderr=True, tail=tail).decode(errors="replace")
        except (NotFound, DockerException):
            return None

    def log_path(self, job_id: int) -> Path:
        return self.settings.logs_dir / f"job-{job_id}.log"

    def recover(self):
        """On startup: clean up containers/jobs left over from a previous run."""
        with self.Session() as db:
            for job in db.query(Job).filter(Job.status.in_(ACTIVE_STATUSES)):
                job.status, job.error = "error", "server restarted while job was active"
                job.finished_at = job.finished_at or utcnow()
            db.commit()
        try:
            for c in self.client.containers.list(all=True, filters={"label": LABEL}):
                c.remove(force=True)
            for v in self.client.volumes.list(filters={"label": LABEL}):
                v.remove(force=True)
        except DockerException as e:
            log.warning("recover: %s", e)

    # ---------- internals ----------
    def _update(self, job_id: int, **fields):
        with self.Session() as db:
            job = db.get(Job, job_id)
            for k, v in fields.items():
                setattr(job, k, v)
            db.commit()

    def _stopping(self, job_id):
        with self._lock:
            return job_id in self._stop_requested

    def _run(self, job_id: int, filename, content):
        s = self.settings
        vol = container = None
        try:
            with self.Session() as db:
                job = db.get(Job, job_id)
                image, command, git_url = job.image, job.command, job.git_url
                cpus, mem_mb, timeout, network = job.cpus, job.mem_mb, job.timeout_seconds, job.network
            self._update(job_id, status="preparing")

            uid, gid = (int(x) for x in s.container_user.split(":"))
            if git_url:
                with tempfile.TemporaryDirectory() as td:
                    dest = Path(td) / "repo"
                    git_clone(git_url, dest, s.git_timeout)
                    payload = build_workspace(filename, content, dest, uid, gid, s.max_upload_bytes)
            else:
                payload = build_workspace(filename, content, None, uid, gid, s.max_upload_bytes)

            self._ensure_image(image)
            labels = {LABEL: str(job_id)}
            vol = self.client.volumes.create(name=f"dockerserver-job-{job_id}", labels=labels)

            # Populate the volume through a never-started helper container.
            helper = self.client.containers.create(
                image, "true", mounts=[Mount("/vol", vol.name, type="volume")],
                network_mode="none", labels=labels)
            try:
                helper.put_archive("/vol", payload)
            finally:
                helper.remove(force=True)

            ws = Mount("/workspace", vol.name, type="volume")
            ws["VolumeOptions"] = {"Subpath": "ws"}
            mem = f"{mem_mb}m"
            container = self.client.containers.create(
                image, ["sh", "-c", command],
                name=f"dockerserver-job-{job_id}",
                labels=labels,
                user=s.container_user,
                working_dir="/workspace",
                environment={"HOME": "/workspace", "PYTHONUNBUFFERED": "1"},
                mounts=[ws],
                tmpfs={"/tmp": f"rw,noexec,nosuid,nodev,size={s.tmp_size}"},
                read_only=True,
                privileged=False,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                pids_limit=s.pids_limit,
                nano_cpus=int(cpus * 1e9),
                mem_limit=mem,
                memswap_limit=mem,
                network_mode="bridge" if network else "none",
                ipc_mode="private",
                log_config=LogConfig(type="json-file", config={"max-size": "10m", "max-file": "1"}),
            )
            if self._stopping(job_id):
                raise _Stopped()
            self._update(job_id, container_id=container.id)
            container.start()
            self._update(job_id, status="running", started_at=utcnow())

            timed_out = False
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode")
            except Exception:  # read timeout from the Docker API
                timed_out = True
                try:
                    container.kill()
                except DockerException:
                    pass
                exit_code = container.wait(timeout=30).get("StatusCode")
            finished = utcnow()
            self._save_logs(job_id, container)
            container.reload()
            oom = container.attrs.get("State", {}).get("OOMKilled", False)
            if self._stopping(job_id):
                status = "stopped"
            elif timed_out:
                status = "timeout"
            else:
                status = "succeeded" if exit_code == 0 else "failed"
            self._update(job_id, status=status, exit_code=exit_code, finished_at=finished,
                         error="out of memory (OOM killed)" if oom else None)
        except _Stopped:
            self._update(job_id, status="stopped", finished_at=utcnow())
        except WorkspaceError as e:
            self._update(job_id, status="error", error=str(e), finished_at=utcnow())
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job_id)
            self._update(job_id, status="error", error=f"{type(e).__name__}: {e}", finished_at=utcnow())
        finally:
            for obj in (container, vol):
                if obj is not None:
                    try:
                        obj.remove(force=True)
                    except DockerException:
                        pass
            with self._lock:
                self._stop_requested.discard(job_id)

    def _ensure_image(self, image):
        try:
            self.client.images.get(image)
        except ImageNotFound:
            self.client.images.pull(image)

    def _save_logs(self, job_id, container):
        data = container.logs(stdout=True, stderr=True)
        limit = self.settings.max_log_bytes
        if len(data) > limit:
            data = b"[... log truncated ...]\n" + data[-limit:]
        self.log_path(job_id).write_bytes(data)


class _Stopped(Exception):
    pass
