"""Runs gateway-submitted jobs in the local Docker, one container + volume per job.

The gateway only chooses image, command, cpus, mem_mb, timeout and network on/off,
and ships the workspace as a tar. Every isolation setting is applied HERE, so a
compromised gateway still cannot get privileged containers or host mounts:
  * cpu / memory limits (memswap == mem), pids limit, cap_drop ALL,
    no-new-privileges, read-only root fs, small noexec tmpfs on /tmp
  * runs as nobody (65534); writable /workspace is a per-job named volume
  * no host bind mounts, no Docker socket; network "none" unless asked
  * wall-clock timeout enforced locally (works even if the gateway disappears)
Capacity is reserved per job; a job that does not fit is refused (HTTP 409).
"""
import io
import logging
import re
import tarfile
import threading
import time
from dataclasses import dataclass, field

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import LogConfig, Mount

from .config import Settings

log = logging.getLogger("computenode.runner")
LABEL = "computenode.job"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ACTIVE = ("preparing", "running")


class Refused(Exception):
    """Request is invalid or does not fit; message is safe to return."""

    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


@dataclass
class NodeJob:
    id: str
    image: str
    command: str
    cpus: float
    mem_mb: int
    timeout_seconds: int
    network: bool
    status: str = "preparing"  # preparing|running|succeeded|failed|timeout|killed|error
    exit_code: int | None = None
    oom: bool = False
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cpu_avg_pct: float | None = None
    cpu_peak_pct: float | None = None
    mem_avg_mb: float | None = None
    mem_peak_mb: float | None = None
    stat_samples: int = 0
    container_id: str | None = None
    kill_reason: str | None = None
    env: dict = field(default_factory=dict, repr=False)   # never returned by the API, never logged

    def public(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("container_id", "kill_reason", "env")}
        return d


class NodeRunner:
    def __init__(self, settings: Settings, client: docker.DockerClient | None = None):
        self.s = settings
        self._client = client
        self._lock = threading.Lock()
        self.jobs: dict[str, NodeJob] = {}
        self._stop = threading.Event()
        self.total_cpus = 0.0
        self.total_mem_mb = 0

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    # ---------- lifecycle ----------
    def start(self):
        info = self.client.info()
        self.total_cpus = float(info.get("NCPU") or 1)
        self.total_mem_mb = int((info.get("MemTotal") or 0) / (1024 * 1024))
        self.docker_version = info.get("ServerVersion")
        self._cleanup_labelled()
        threading.Thread(target=self._housekeeping, name="housekeeping", daemon=True).start()

    def shutdown(self):
        self._stop.set()
        for j in list(self.jobs.values()):
            if j.status in ACTIVE:
                self.kill(j.id, "node shutting down")

    def _cleanup_labelled(self):
        try:
            for c in self.client.containers.list(all=True, filters={"label": LABEL}):
                c.remove(force=True)
            for v in self.client.volumes.list(filters={"label": LABEL}):
                v.remove(force=True)
        except DockerException as e:
            log.warning("cleanup: %s", e)

    # ---------- capacity ----------
    @property
    def cap_cpus(self) -> float:
        return self.s.max_cpus or max(self.total_cpus - self.s.reserve_cpus, 0.5)

    @property
    def cap_mem_mb(self) -> int:
        return self.s.max_mem_mb or max(self.total_mem_mb - self.s.reserve_mem_mb, 256)

    def used(self) -> tuple[float, int]:
        with self._lock:
            act = [j for j in self.jobs.values() if j.status in ACTIVE]
        return sum(j.cpus for j in act), sum(j.mem_mb for j in act)

    def info(self) -> dict:
        uc, um = self.used()
        with self._lock:
            running = sum(1 for j in self.jobs.values() if j.status in ACTIVE)
        return {
            "node": self.s.node_name, "docker_version": getattr(self, "docker_version", None),
            "capacity": {"cpus": self.cap_cpus, "mem_mb": self.cap_mem_mb},
            "used": {"cpus": uc, "mem_mb": um},
            "free": {"cpus": round(self.cap_cpus - uc, 3), "mem_mb": self.cap_mem_mb - um},
            "active_jobs": running, "allowed_images": self.s.allowed_images,
            "allow_network": self.s.allow_network,
            # capabilities the bridge checks before sending a job that needs them
            "features": ["env"],
        }

    # ---------- jobs ----------
    def submit(self, spec: dict, workspace_tar: bytes | None) -> NodeJob:
        jid = str(spec.get("id", ""))
        if not JOB_ID_RE.match(jid):
            raise Refused("bad job id")
        image = str(spec.get("image", ""))
        if not self.s.image_allowed(image):
            raise Refused(f"image not allowed on this node: {image}", 403)
        command = str(spec.get("command", ""))
        if not command or len(command) > 10000:
            raise Refused("command missing or too long")
        try:
            cpus = float(spec.get("cpus", 1))
            mem_mb = int(spec.get("mem_mb", 512))
            timeout = int(spec.get("timeout_seconds", 3600))
        except (TypeError, ValueError):
            raise Refused("bad cpus / mem_mb / timeout_seconds")
        if not (0.05 <= cpus <= self.cap_cpus) or not (16 <= mem_mb <= self.cap_mem_mb):
            raise Refused("job is larger than this node", 409)
        if not (1 <= timeout <= self.s.max_timeout_seconds):
            raise Refused("timeout_seconds out of range")
        if workspace_tar is not None and len(workspace_tar) > self.s.max_upload_bytes:
            raise Refused("workspace too large", 413)
        if workspace_tar:
            workspace_tar = clean_tar(workspace_tar)
        network = bool(spec.get("network", False))
        if network and not self.s.allow_network:
            raise Refused("network access is disabled on this node", 403)
        env = check_env(spec.get("env"))
        job = NodeJob(jid, image, command, cpus, mem_mb, timeout, network, env=env)
        with self._lock:
            if jid in self.jobs:
                raise Refused("job id already exists", 409)
            act = [j for j in self.jobs.values() if j.status in ACTIVE]
            if (sum(j.cpus for j in act) + cpus > self.cap_cpus + 1e-9
                    or sum(j.mem_mb for j in act) + mem_mb > self.cap_mem_mb):
                raise Refused("node is full", 409)
            self.jobs[jid] = job
        threading.Thread(target=self._run, args=(job, workspace_tar), name=f"job-{jid}",
                         daemon=True).start()
        return job

    def get(self, jid: str) -> NodeJob:
        job = self.jobs.get(jid)
        if not job:
            raise Refused("no such job", 404)
        return job

    def kill(self, jid: str, reason: str = "killed") -> NodeJob:
        job = self.get(jid)
        if job.status in ACTIVE:
            job.kill_reason = job.kill_reason or reason
            if job.container_id:
                try:
                    self.client.containers.get(job.container_id).kill()
                except (NotFound, DockerException):
                    pass
        return job

    def logs(self, jid: str, tail: int | None = None) -> bytes:
        job = self.get(jid)
        if not job.container_id:
            return b""
        try:
            c = self.client.containers.get(job.container_id)
            return c.logs(stdout=True, stderr=True, tail=tail if tail else "all")
        except (NotFound, DockerException):
            return b""

    def remove(self, jid: str):
        job = self.get(jid)
        if job.status in ACTIVE:
            self.kill(jid, "removed")
            for _ in range(60):
                if job.status not in ACTIVE:
                    break
                time.sleep(0.5)
        self._remove_objects(jid)
        with self._lock:
            self.jobs.pop(jid, None)

    # ---------- internals ----------
    def _names(self, jid):
        return f"computenode-job-{jid}", f"computenode-job-{jid}"

    def _remove_objects(self, jid):
        cname, vname = self._names(jid)
        for getter, name in ((self.client.containers.get, cname), (self.client.volumes.get, vname)):
            try:
                getter(name).remove(force=True)
            except (NotFound, DockerException):
                pass

    def _run(self, job: NodeJob, payload: bytes | None):
        s = self.s
        cname, vname = self._names(job.id)
        labels = {LABEL: job.id}
        try:
            self._ensure_image(job.image)
            vol = self.client.volumes.create(name=vname, labels=labels)
            # Unpack the workspace (if any) and hand the volume to the unprivileged job user.
            helper = self.client.containers.create(
                job.image, ["chown", "-R", s.container_user, "/vol"],
                mounts=[Mount("/vol", vol.name, type="volume")],
                network_mode="none", labels=labels)
            try:
                if payload:
                    helper.put_archive("/vol", payload)
                helper.start()
                if helper.wait(timeout=120).get("StatusCode") != 0:
                    raise RuntimeError("could not prepare workspace volume")
            finally:
                helper.remove(force=True)
            ws = Mount("/workspace", vol.name, type="volume")
            mem = f"{job.mem_mb}m"
            c = self.client.containers.create(
                job.image, ["sh", "-c", job.command], name=cname, labels=labels,
                user=s.container_user, working_dir="/workspace",
                environment={**job.env, "HOME": "/workspace", "PYTHONUNBUFFERED": "1"},
                mounts=[ws],
                tmpfs={"/tmp": f"rw,noexec,nosuid,nodev,size={s.tmp_size}"},
                read_only=True, privileged=False, cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"], pids_limit=s.pids_limit,
                nano_cpus=int(job.cpus * 1e9), mem_limit=mem, memswap_limit=mem,
                network_mode="bridge" if job.network else "none", ipc_mode="private",
                log_config=LogConfig(type="json-file", config={"max-size": "10m", "max-file": "1"}),
            )
            job.container_id = c.id
            if job.kill_reason:
                raise _Killed()
            c.start()
            job.started_at, job.status = time.time(), "running"
            deadline = time.monotonic() + job.timeout_seconds
            timed_out = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    try:
                        c.kill()
                    except DockerException:
                        pass
                    res = c.wait(timeout=30)
                    break
                try:
                    res = c.wait(timeout=min(remaining, s.stats_interval))
                    break
                except Exception:  # noqa: BLE001 - read timeout: still running
                    self._sample(job, c)
            job.finished_at = time.time()
            job.exit_code = res.get("StatusCode")
            c.reload()
            job.oom = bool(c.attrs.get("State", {}).get("OOMKilled", False))
            if job.oom:
                job.error = "out of memory (OOM killed)"
            if job.kill_reason:
                job.status, job.error = "killed", job.error or job.kill_reason
            elif timed_out:
                job.status = "timeout"
            else:
                job.status = "succeeded" if job.exit_code == 0 else "failed"
        except _Killed:
            job.status, job.error, job.finished_at = "killed", job.kill_reason, time.time()
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job.id)
            job.status, job.error, job.finished_at = "error", f"{type(e).__name__}: {e}", time.time()

    def _sample(self, job: NodeJob, c):
        try:
            st = c.stats(stream=False)
        except DockerException:
            return
        cpu, mem = cpu_percent(st), mem_mb(st)
        n = job.stat_samples
        if cpu is not None:
            job.cpu_avg_pct = ((job.cpu_avg_pct or 0) * n + cpu) / (n + 1)
            job.cpu_peak_pct = max(job.cpu_peak_pct or 0, cpu)
        if mem is not None:
            job.mem_avg_mb = ((job.mem_avg_mb or 0) * n + mem) / (n + 1)
            job.mem_peak_mb = max(job.mem_peak_mb or 0, mem)
        if cpu is not None or mem is not None:
            job.stat_samples = n + 1

    def _ensure_image(self, image):
        try:
            self.client.images.get(image)
        except ImageNotFound:
            self.client.images.pull(image)

    def _housekeeping(self):
        """Drop finished jobs the gateway never collected."""
        while not self._stop.wait(60):
            now = time.time()
            stale = [j.id for j in list(self.jobs.values())
                     if j.finished_at and now - j.finished_at > self.s.retain_seconds]
            for jid in stale:
                log.info("removing uncollected job %s", jid)
                try:
                    self.remove(jid)
                except Refused:
                    pass


def clean_tar(payload: bytes) -> bytes:
    """Re-pack the workspace without extended attributes / pax headers (macOS tar adds
    com.apple.* xattrs, which Docker refuses) and without device files."""
    out = io.BytesIO()
    try:
        with tarfile.open(fileobj=io.BytesIO(payload)) as src, \
                tarfile.open(fileobj=out, mode="w", format=tarfile.GNU_FORMAT) as dst:
            for m in src:
                if not (m.isfile() or m.isdir() or m.issym()) or m.name.rsplit("/", 1)[-1].startswith("._"):
                    continue
                m.pax_headers = {}
                dst.addfile(m, src.extractfile(m) if m.isfile() else None)
    except tarfile.TarError as e:
        raise Refused(f"workspace is not a valid tar: {e}")
    return out.getvalue()


# Environment variables from the bridge: validated again here (the node never trusts the bridge
# for isolation). Same rules as the bridge: name syntax, reserved names, size limits.
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ENV_RESERVED = {"PATH", "HOME", "HOSTNAME", "PWD", "PYTHONHOME", "PYTHONUNBUFFERED"}
ENV_RESERVED_PREFIXES = ("LD_", "DOCKERSERVER_", "COMPUTENODE_")


def check_env(env) -> dict:
    if env is None:
        return {}
    if not isinstance(env, dict):
        raise Refused("env must be an object")
    if len(env) > 50:
        raise Refused("too many environment variables")
    total = 0
    for k, v in env.items():
        if not isinstance(k, str) or not ENV_NAME_RE.match(k):
            raise Refused(f"invalid environment variable name: {str(k)[:64]!r}")
        if k.upper() in ENV_RESERVED or k.upper().startswith(ENV_RESERVED_PREFIXES):
            raise Refused(f"environment variable {k} is reserved")
        if not isinstance(v, str) or len(v) > 4096 or "\x00" in v:
            raise Refused(f"invalid value for environment variable {k}")
        total += len(k) + len(v)
    if total > 32 * 1024:
        raise Refused("environment variables too large")
    return dict(env)


class _Killed(Exception):
    pass


def cpu_percent(st) -> float | None:
    """CPU use in % of one core (100 = one full core), as `docker stats` computes it."""
    try:
        cpu, pre = st["cpu_stats"], st["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu["system_cpu_usage"] - pre["system_cpu_usage"]
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1])
        if sys_delta <= 0 or cpu_delta < 0:
            return None
        return cpu_delta / sys_delta * ncpu * 100.0
    except (KeyError, TypeError):
        return None


def mem_mb(st) -> float | None:
    try:
        ms = st["memory_stats"]
        usage = ms["usage"] - ms.get("stats", {}).get("inactive_file", 0)
        return max(usage, 0) / (1024 * 1024)
    except (KeyError, TypeError):
        return None
