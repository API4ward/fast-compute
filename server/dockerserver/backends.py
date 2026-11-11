"""Execution backends: where a job's container actually runs.

* LocalDockerBackend  - the Docker daemon on this host (the original behaviour).
* RemoteNodeBackend   - a compute node agent (computenode) reached over the tailnet:
                         GET  /v1/info
                         POST /v1/jobs            multipart: spec (JSON) + optional workspace (tar)
                         GET  /v1/jobs/{id}       status, exit_code, oom, stats, started/finished
                         GET  /v1/jobs/{id}/logs?tail=N
                         POST /v1/jobs/{id}/kill
                         DELETE /v1/jobs/{id}
  Isolation (limits, cap_drop, read-only fs, nobody, no mounts, network) is enforced by the
  node itself; this side only chooses image/command/cpus/mem/timeout/network.

The runner (runner.py) drives a backend through: start -> wait/poll -> kill? -> final status ->
logs -> cleanup. Both backends take the same workspace tar (top-level "ws/" directory, entries
owned by the in-container user), produced by workspace.build_workspace.
"""
import json
import logging
import time
from dataclasses import dataclass, field

import docker
import httpx
import requests
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import LogConfig, Mount

log = logging.getLogger("dockerserver.backends")
LABEL = "dockerserver.job"


class BackendError(Exception):
    """The backend could not do what was asked (node down, bad answer, ...)."""


class NoCapacity(BackendError):
    """The node refused the job for lack of room (HTTP 409)."""


@dataclass
class JobSpec:
    job_id: int
    image: str
    command: str
    cpus: float
    mem_mb: int
    timeout_seconds: int
    network: bool


@dataclass
class ExecStatus:
    finished: bool
    exit_code: int | None = None
    oom: bool = False
    node_status: str | None = None     # backend's own status word
    error: str | None = None
    duration_seconds: float | None = None   # run time as measured by the backend
    stat_samples: int = 0
    cpu_avg_pct: float | None = None
    cpu_peak_pct: float | None = None
    mem_avg_mb: float | None = None
    mem_peak_mb: float | None = None


@dataclass
class Handle:
    backend: "Backend"
    id: str                          # container id (local) or node job id (remote)
    node: str
    extra: dict = field(default_factory=dict)
    last_status: ExecStatus | None = None
    last_seen: float = field(default_factory=time.time)        # wall clock of last good contact
    unreachable_since: float | None = None


class Backend:
    name: str = "?"

    def start(self, spec: JobSpec, payload: bytes) -> Handle: ...
    def wait(self, h: Handle, timeout: float) -> bool: ...      # True once the job has ended
    def status(self, h: Handle) -> ExecStatus: ...               # latest known status/stats
    def kill(self, h: Handle) -> None: ...
    def logs(self, h: Handle, tail: int | None = None) -> bytes: ...
    def cleanup(self, h: Handle) -> None: ...

    def info(self, max_age: float = 0) -> dict | None:
        """{"capacity": {"cpus", "mem_mb"}, "free": {...}} or None if unknown/unreachable."""
        return None


# =====================================================================================
class LocalDockerBackend(Backend):
    """Runs the container on this host's Docker daemon."""

    def __init__(self, settings, client: docker.DockerClient | None = None):
        self.settings = settings
        self._client = client
        self.name = "local"

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    def start(self, spec: JobSpec, payload: bytes) -> Handle:
        s = self.settings
        labels = {LABEL: str(spec.job_id)}
        self._ensure_image(spec.image)
        vol = self.client.volumes.create(name=f"dockerserver-job-{spec.job_id}", labels=labels)
        h = Handle(self, "", self.name, extra={"volume": vol})
        try:
            # Populate the volume through a never-started helper container.
            helper = self.client.containers.create(
                spec.image, "true", mounts=[Mount("/vol", vol.name, type="volume")],
                network_mode="none", labels=labels)
            try:
                helper.put_archive("/vol", payload)
            finally:
                helper.remove(force=True)
            ws = Mount("/workspace", vol.name, type="volume")
            ws["VolumeOptions"] = {"Subpath": "ws"}
            mem = f"{spec.mem_mb}m"
            c = self.client.containers.create(
                spec.image, ["sh", "-c", spec.command],
                name=f"dockerserver-job-{spec.job_id}", labels=labels,
                user=s.container_user, working_dir="/workspace",
                environment={"HOME": "/workspace", "PYTHONUNBUFFERED": "1"},
                mounts=[ws],
                tmpfs={"/tmp": f"rw,noexec,nosuid,nodev,size={s.tmp_size}"},
                read_only=True, privileged=False, cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"], pids_limit=s.pids_limit,
                nano_cpus=int(spec.cpus * 1e9), mem_limit=mem, memswap_limit=mem,
                network_mode="bridge" if spec.network else "none", ipc_mode="private",
                log_config=LogConfig(type="json-file", config={"max-size": "10m", "max-file": "1"}),
            )
            h.id, h.extra["container"] = c.id, c
            c.start()
        except Exception:
            self.cleanup(h)
            raise
        h.extra["stats"] = ExecStatus(finished=False)
        return h

    def wait(self, h: Handle, timeout: float) -> bool:
        c = h.extra["container"]
        try:
            res = c.wait(timeout=max(timeout, 0.01))
            h.extra["exit_code"] = res.get("StatusCode")
            return True
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError):
            pass
        c.reload()
        return c.status not in ("running", "created", "restarting")

    def status(self, h: Handle) -> ExecStatus:
        c = h.extra["container"]
        st: ExecStatus = h.extra["stats"]
        try:
            c.reload()
        except DockerException as e:
            raise BackendError(str(e))
        state = c.attrs.get("State", {})
        st.finished = c.status not in ("running", "created", "restarting")
        if st.finished:
            st.exit_code = h.extra.get("exit_code", state.get("ExitCode"))
            st.oom = bool(state.get("OOMKilled", False))
        else:
            self._sample(c, st)
        return st

    def _sample(self, c, st: ExecStatus):
        try:
            raw = c.stats(stream=False)
        except DockerException:
            return
        cpu, mem = cpu_percent(raw), mem_mb(raw)
        n = st.stat_samples
        if cpu is not None:
            st.cpu_avg_pct = ((st.cpu_avg_pct or 0) * n + cpu) / (n + 1)
            st.cpu_peak_pct = max(st.cpu_peak_pct or 0, cpu)
        if mem is not None:
            st.mem_avg_mb = ((st.mem_avg_mb or 0) * n + mem) / (n + 1)
            st.mem_peak_mb = max(st.mem_peak_mb or 0, mem)
        if cpu is not None or mem is not None:
            st.stat_samples = n + 1

    def kill(self, h: Handle) -> None:
        c = h.extra.get("container")
        if c is not None:
            try:
                c.kill()
            except (NotFound, DockerException):
                pass

    def logs(self, h: Handle, tail: int | None = None) -> bytes:
        c = h.extra.get("container")
        if c is None:
            return b""
        try:
            return c.logs(stdout=True, stderr=True, tail=tail if tail else "all")
        except (NotFound, DockerException):
            return b""

    def cleanup(self, h: Handle) -> None:
        for key in ("container", "volume"):
            obj = h.extra.get(key)
            if obj is not None:
                try:
                    obj.remove(force=True)
                except DockerException:
                    pass

    def cleanup_leftovers(self):
        try:
            for c in self.client.containers.list(all=True, filters={"label": LABEL}):
                c.remove(force=True)
            for v in self.client.volumes.list(filters={"label": LABEL}):
                v.remove(force=True)
        except DockerException as e:
            log.warning("local cleanup: %s", e)

    def _ensure_image(self, image):
        try:
            self.client.images.get(image)
        except ImageNotFound:
            self.client.images.pull(image)


# =====================================================================================
# Node status words -> finished?
_NODE_ACTIVE = {"preparing", "running"}


class RemoteNodeBackend(Backend):
    """Talks to one compute node agent over HTTP (tailnet), Bearer-token authenticated."""

    def __init__(self, name: str, url: str, token: str, *, poll_interval: float = 2.0,
                 connect_timeout: float = 5.0, read_timeout: float = 30.0, retries: int = 3,
                 transport: httpx.BaseTransport | None = None):
        self.name, self.url = name, url.rstrip("/")
        self.poll_interval, self.retries = poll_interval, retries
        self._http = httpx.Client(
            base_url=self.url, headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout), transport=transport)
        self._info: dict | None = None
        self._info_at = 0.0

    def __repr__(self):  # never show the token
        return f"RemoteNodeBackend({self.name!r}, {self.url!r})"

    # ---- HTTP with retries on connection errors (the request never reached the node) ----
    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        delay = 0.5
        for attempt in range(self.retries):
            try:
                return self._http.request(method, path, **kw)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
                if attempt == self.retries - 1:
                    raise BackendError(f"node {self.name} unreachable: {type(e).__name__}") from e
                time.sleep(delay)
                delay *= 2
            except httpx.HTTPError as e:
                raise BackendError(f"node {self.name}: {type(e).__name__}: {e}") from e
        raise BackendError(f"node {self.name} unreachable")

    @staticmethod
    def _detail(r: httpx.Response) -> str:
        try:
            d = r.json()
            return str(d.get("detail", d)) if isinstance(d, dict) else str(d)
        except ValueError:
            return r.text[:300]

    # ---- capacity ----
    def info(self, max_age: float = 0) -> dict | None:
        if self._info is not None and time.monotonic() - self._info_at <= max_age:
            return self._info
        try:
            r = self._req("GET", "/v1/info")
            if r.status_code != 200:
                raise BackendError(f"info: HTTP {r.status_code}: {self._detail(r)}")
            self._info, self._info_at = r.json(), time.monotonic()
        except (BackendError, ValueError) as e:
            log.warning("node %s info failed: %s", self.name, e)
            self._info, self._info_at = None, time.monotonic()
        return self._info

    def invalidate(self):
        self._info_at = 0.0

    # ---- jobs ----
    def start(self, spec: JobSpec, payload: bytes) -> Handle:
        node_id = f"ds{spec.job_id}-{int(time.time())}"
        body = {"id": node_id, "image": spec.image, "command": spec.command, "cpus": spec.cpus,
                "mem_mb": spec.mem_mb, "timeout_seconds": spec.timeout_seconds,
                "network": spec.network}
        files = {"spec": (None, json.dumps(body), "application/json")}
        if payload:
            files["workspace"] = ("workspace.tar", payload, "application/x-tar")
        try:
            r = self._req("POST", "/v1/jobs", files=files,
                          timeout=httpx.Timeout(120.0, connect=5.0))
        except BackendError:
            # The upload may have reached the node; if the job exists there, adopt it.
            if self._exists(node_id):
                return Handle(self, node_id, self.name)
            raise
        self.invalidate()
        if r.status_code == 409:
            raise NoCapacity(f"node {self.name}: {self._detail(r)}")
        if r.status_code not in (200, 201):
            raise BackendError(f"node {self.name} refused job: HTTP {r.status_code}: {self._detail(r)}")
        return Handle(self, node_id, self.name)

    def _exists(self, node_id: str) -> bool:
        try:
            return self._req("GET", f"/v1/jobs/{node_id}").status_code == 200
        except BackendError:
            return False

    def _poll(self, h: Handle) -> ExecStatus | None:
        """One status request; updates h.last_status / reachability. None if unreachable."""
        try:
            r = self._req("GET", f"/v1/jobs/{h.id}")
        except BackendError as e:
            h.unreachable_since = h.unreachable_since or time.time()
            log.warning("job %s: %s", h.id, e)
            return None
        if r.status_code != 200:
            h.unreachable_since = h.unreachable_since or time.time()
            log.warning("job %s: status HTTP %s: %s", h.id, r.status_code, self._detail(r))
            return None
        d = r.json()
        h.unreachable_since, h.last_seen = None, time.time()
        started, finished = d.get("started_at"), d.get("finished_at")
        dur = (finished - started) if isinstance(started, (int, float)) and isinstance(finished, (int, float)) else None
        st = ExecStatus(
            finished=d.get("status") not in _NODE_ACTIVE, exit_code=d.get("exit_code"),
            oom=bool(d.get("oom")), node_status=d.get("status"), error=d.get("error"),
            duration_seconds=max(dur, 0.0) if dur is not None else None,
            stat_samples=d.get("stat_samples") or 0, cpu_avg_pct=d.get("cpu_avg_pct"),
            cpu_peak_pct=d.get("cpu_peak_pct"), mem_avg_mb=d.get("mem_avg_mb"),
            mem_peak_mb=d.get("mem_peak_mb"))
        h.last_status = st
        return st

    def wait(self, h: Handle, timeout: float) -> bool:
        end = time.monotonic() + max(timeout, 0)
        while True:
            st = self._poll(h)
            if st is not None and st.finished:
                return True
            left = end - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(self.poll_interval, left))

    def status(self, h: Handle) -> ExecStatus:
        st = self._poll(h) or h.last_status
        if st is None:
            raise BackendError(f"node {self.name} unreachable")
        return st

    def kill(self, h: Handle) -> None:
        try:
            self._req("POST", f"/v1/jobs/{h.id}/kill")
        except BackendError as e:
            log.warning("kill %s on %s: %s", h.id, self.name, e)

    def logs(self, h: Handle, tail: int | None = None) -> bytes:
        try:
            r = self._req("GET", f"/v1/jobs/{h.id}/logs", params={"tail": tail} if tail else None)
            return r.content if r.status_code == 200 else b""
        except BackendError:
            return b""

    def cleanup(self, h: Handle) -> None:
        try:
            self._req("DELETE", f"/v1/jobs/{h.id}")
        except BackendError as e:
            log.warning("delete %s on %s: %s", h.id, self.name, e)
        self.invalidate()


# =====================================================================================
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
