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
import ipaddress
import logging
import re
import secrets
import tarfile
import threading
import time
from dataclasses import dataclass, field

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.types import IPAMConfig, IPAMPool, LogConfig, Mount

from .config import Settings
from .ingress import RELAY_CODE, RELAY_PORT, Forwarder

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
    # service jobs only: container port, this node's forward port, and whether the app accepts TCP
    service_port: int | None = None
    ingress_port: int | None = None
    service_ready: bool = False
    relay_id: str | None = None

    def public(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()
             if k not in ("container_id", "kill_reason", "env", "relay_id")}
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
        self._ingress_ports: set[int] = set()   # held from submit until the service is torn down
        self._forwarder: Forwarder | None = None
        self._ingress_host: str | None = None

    @property
    def services_enabled(self) -> bool:
        """Only with an explicit caller allowlist: the forward ports carry no token."""
        return bool(self.s.allowed_clients)

    @property
    def forwarder(self) -> Forwarder:
        with self._lock:
            if self._forwarder is None:
                self._forwarder = Forwarder(self.s.allowed_clients, self.s.service_max_conns)
            return self._forwarder

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
        if self._forwarder is not None:
            self._forwarder.shutdown()

    def _cleanup_labelled(self):
        try:
            for c in self.client.containers.list(all=True, filters={"label": LABEL}):
                c.remove(force=True)
            for v in self.client.volumes.list(filters={"label": LABEL}):
                v.remove(force=True)
            for n in self.client.networks.list(filters={"label": LABEL}):
                n.remove()
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
            "features": ["env"] + (["service"] if self.services_enabled else []),
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
        service_port = check_service_port(spec.get("service_port"))
        max_timeout = self.s.max_service_seconds if service_port else self.s.max_timeout_seconds
        if not (1 <= timeout <= max_timeout):
            raise Refused("timeout_seconds out of range")
        if service_port and not self.services_enabled:
            raise Refused("service jobs need COMPUTENODE_ALLOWED_CLIENTS on this node", 403)
        if service_port and spec.get("network"):
            raise Refused("service jobs have no outbound network", 400)
        if workspace_tar is not None and len(workspace_tar) > self.s.max_upload_bytes:
            raise Refused("workspace too large", 413)
        if workspace_tar:
            workspace_tar = clean_tar(workspace_tar)
        network = bool(spec.get("network", False))
        if network and not self.s.allow_network:
            raise Refused("network access is disabled on this node", 403)
        env = check_env(spec.get("env"))
        job = NodeJob(jid, image, command, cpus, mem_mb, timeout, network, env=env,
                      service_port=service_port)
        with self._lock:
            if jid in self.jobs:
                raise Refused("job id already exists", 409)
            act = [j for j in self.jobs.values() if j.status in ACTIVE]
            if (sum(j.cpus for j in act) + cpus > self.cap_cpus + 1e-9
                    or sum(j.mem_mb for j in act) + mem_mb > self.cap_mem_mb):
                raise Refused("node is full", 409)
            if service_port:
                job.ingress_port = next((p for p in range(self.s.ingress_port_min,
                                                          self.s.ingress_port_max + 1)
                                         if p not in self._ingress_ports), None)
                if job.ingress_port is None:
                    raise Refused("no free service port on this node", 409)
                self._ingress_ports.add(job.ingress_port)
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
            app_net, edge_net = self._create_service_networks(job) if job.service_port else (None, None)
            c = self.client.containers.create(
                job.image, ["sh", "-c", job.command], name=cname, labels=labels,
                user=s.container_user, working_dir="/workspace",
                environment={**job.env, "HOME": "/workspace", "PYTHONUNBUFFERED": "1"},
                mounts=[ws],
                tmpfs={"/tmp": f"rw,noexec,nosuid,nodev,size={s.tmp_size}"},
                read_only=True, privileged=False, cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"], pids_limit=s.pids_limit,
                nano_cpus=int(job.cpus * 1e9), mem_limit=mem, memswap_limit=mem,
                network_mode=app_net.name if app_net else ("bridge" if job.network else "none"),
                ipc_mode="private",
                log_config=LogConfig(type="json-file", config={"max-size": "10m", "max-file": "1"}),
            )
            job.container_id = c.id
            if job.kill_reason:
                raise _Killed()
            c.start()
            if app_net:
                self._start_ingress(job, c, app_net, edge_net)
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
                # poll faster until a service first accepts connections
                interval = 2 if job.service_port and not job.service_ready else s.stats_interval
                try:
                    res = c.wait(timeout=min(remaining, interval))
                    break
                except Exception:  # noqa: BLE001 - read timeout: still running
                    self._sample(job, c)
                    if job.service_port:
                        self._check_service(job, c)
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
        finally:
            if job.service_port:
                self._teardown_service(job)

    # ---------- service jobs (see ingress.py) ----------
    def _service_names(self, jid):
        return f"computenode-app-{jid}", f"computenode-edge-{jid}", f"computenode-relay-{jid}"

    def _service_subnets(self, ingress_port: int) -> tuple[str, str]:
        """Two /28s derived from the forward port, so concurrent services never overlap."""
        base = ipaddress.ip_network(self.s.service_subnet)
        idx = (ingress_port - self.s.ingress_port_min) * 2
        first = int(base.network_address) + idx * 16
        if first + 32 > int(base.broadcast_address) + 1:
            raise RuntimeError("COMPUTENODE_SERVICE_SUBNET is too small for the ingress port range")
        return (f"{ipaddress.ip_address(first)}/28", f"{ipaddress.ip_address(first + 16)}/28")

    def _create_service_networks(self, job: NodeJob):
        app_name, edge_name, _ = self._service_names(job.id)
        app_sub, edge_sub = self._service_subnets(job.ingress_port)
        labels = {LABEL: job.id}
        # internal + inhibit_ipv4: no route out and no Docker-host address the app could reach
        app_net = self.client.networks.create(
            app_name, driver="bridge", internal=True, labels=labels,
            options={"com.docker.network.bridge.inhibit_ipv4": "true"},
            ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=app_sub)]))
        edge_net = self.client.networks.create(
            edge_name, driver="bridge", labels=labels,
            options={"com.docker.network.bridge.enable_icc": "false",
                     "com.docker.network.bridge.enable_ip_masquerade": "false"},
            ipam=IPAMConfig(pool_configs=[IPAMPool(subnet=edge_sub)]))
        return app_net, edge_net

    def _start_ingress(self, job: NodeJob, app, app_net, edge_net):
        app.reload()
        target = app.attrs["NetworkSettings"]["Networks"][app_net.name]["IPAddress"]
        _, _, relay_name = self._service_names(job.id)
        self._ensure_image(self.s.service_relay_image)
        secret = secrets.token_hex(32)   # held only by the forwarder and the relay
        relay = self.client.containers.run(
            self.s.service_relay_image, ["python", "-c", RELAY_CODE], name=relay_name, detach=True,
            labels={LABEL: job.id}, user=self.s.container_user,
            environment={"TARGET_HOST": target, "TARGET_PORT": str(job.service_port),
                         "RELAY_SECRET": secret, "PYTHONUNBUFFERED": "1"},
            network=edge_net.name, ports={f"{RELAY_PORT}/tcp": ("127.0.0.1", None)},
            read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
            mem_limit="48m", memswap_limit="48m", pids_limit=self.s.service_max_conns * 2 + 16,
            nano_cpus=int(0.5 * 1e9),
            log_config=LogConfig(type="json-file", config={"max-size": "1m", "max-file": "1"}))
        job.relay_id = relay.id
        for _ in range(100):      # the listener must be bound before the app network is attached
            if b"ready" in relay.logs():
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("service relay did not start")
        app_net.connect(relay)
        relay.reload()
        published = relay.attrs["NetworkSettings"]["Ports"][f"{RELAY_PORT}/tcp"][0]["HostPort"]
        self.forwarder.open(self._listen_host(), job.ingress_port, int(published), secret.encode())

    def _listen_host(self) -> str:
        if self._ingress_host is None:
            self._ingress_host = self.s.bind_host()
        return self._ingress_host

    def _check_service(self, job: NodeJob, app):
        """Ask the relay (never the user's container) whether the app port accepts TCP."""
        if not job.relay_id:
            return
        try:
            app_name = self._service_names(job.id)[0]
            app.reload()
            ip = app.attrs["NetworkSettings"]["Networks"][app_name]["IPAddress"]
            code = (f"import socket; socket.create_connection(({ip!r}, {job.service_port}), 2).close()")
            res = self.client.containers.get(job.relay_id).exec_run(["python", "-c", code])
            job.service_ready = res.exit_code == 0
        except (DockerException, KeyError):
            job.service_ready = False

    def _teardown_service(self, job: NodeJob):
        job.service_ready = False
        app_name, edge_name, relay_name = self._service_names(job.id)
        if job.ingress_port is not None and self._forwarder is not None:
            try:
                self._forwarder.close(job.ingress_port)
            except Exception:  # noqa: BLE001
                log.exception("closing ingress for job %s", job.id)
        for name in (relay_name, self._names(job.id)[0]):
            try:
                c = self.client.containers.get(name)
                if name == relay_name:
                    c.remove(force=True)
                elif c.status == "running":   # a failed start must not leave the app running
                    c.kill()
            except (NotFound, DockerException):
                pass
        for name in (app_name, edge_name):
            try:
                self.client.networks.get(name).remove()
            except (NotFound, DockerException) as e:
                if not isinstance(e, NotFound):
                    log.warning("removing network %s: %s", name, e)
        with self._lock:
            self._ingress_ports.discard(job.ingress_port)

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


def check_service_port(port) -> int | None:
    """Container port of a service job. Below 1024 is impossible for the unprivileged job user."""
    if port is None:
        return None
    if isinstance(port, bool) or not isinstance(port, int) or not (1024 <= port <= 65535):
        raise Refused("service_port must be an integer from 1024 to 65535")
    return port


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
