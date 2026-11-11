"""Chooses a backend for a job.

Remote nodes: the node with the most free capacity that fits the job's cpus/mem (from
GET /v1/info, cached for node_info_ttl seconds) is tried first, then the next; a 409 from a
node moves on to the next. If nothing fits right now the job waits (status "queued") and is
retried. A job larger than every reachable node's total capacity is rejected.

Local execution (this host's Docker) is used only when enabled (default: when no node is
configured), and only as the last option.
"""
import threading

from .backends import Backend, LocalDockerBackend, RemoteNodeBackend


class NodePool:
    def __init__(self, settings, nodes: list[Backend] | None = None,
                 local: LocalDockerBackend | None = None):
        self.settings = settings
        self.nodes: list[Backend] = nodes if nodes is not None else [
            RemoteNodeBackend(n.name, n.url, n.token, poll_interval=settings.node_poll_interval)
            for n in settings.nodes]
        self.local = local
        self._lock = threading.Lock()
        # in-flight reservations not yet visible in a node's /v1/info (name -> [cpus, mem])
        self._pending: dict[str, list[float]] = {}

    @property
    def remote_enabled(self) -> bool:
        return bool(self.nodes)

    def _infos(self):
        out = []
        for b in self.nodes:
            info = b.info(max_age=self.settings.node_info_ttl)
            if info:
                out.append((b, info))
        return out

    def never_fits(self, cpus: float, mem_mb: int) -> str | None:
        """Reason string if no node (that we can see) could ever run this size, else None."""
        if not self.nodes:
            return None
        infos = self._infos()
        if not infos:
            return None  # nodes unreachable right now: can't tell, let it queue
        if any(i["capacity"]["cpus"] + 1e-9 >= cpus and i["capacity"]["mem_mb"] >= mem_mb
               for _, i in infos):
            return None
        biggest = max(infos, key=lambda bi: (bi[1]["capacity"]["cpus"], bi[1]["capacity"]["mem_mb"]))[1]
        return (f"job size {cpus} CPU / {mem_mb} MB is larger than any compute node "
                f"(largest: {biggest['capacity']['cpus']} CPU / {biggest['capacity']['mem_mb']} MB)")

    def candidates(self, cpus: float, mem_mb: int) -> list[Backend]:
        """Backends to try, best first."""
        ranked = []
        with self._lock:
            pending = {k: list(v) for k, v in self._pending.items()}
        for b, info in self._infos():
            free = info["free"]
            pc, pm = pending.get(b.name, (0, 0))
            fc, fm = free["cpus"] - pc, free["mem_mb"] - pm
            if fc + 1e-9 >= cpus and fm >= mem_mb:
                ranked.append(((fc, fm), b))       # most free CPUs first, then most free memory
        ranked.sort(key=lambda sb: sb[0], reverse=True)
        out = [b for _, b in ranked]
        if self.local is not None:
            out.append(self.local)
        return out

    def reserve(self, b: Backend, cpus: float, mem_mb: int):
        with self._lock:
            r = self._pending.setdefault(b.name, [0.0, 0])
            r[0] += cpus
            r[1] += mem_mb

    def release(self, b: Backend, cpus: float, mem_mb: int):
        with self._lock:
            r = self._pending.get(b.name)
            if r:
                r[0] = max(r[0] - cpus, 0.0)
                r[1] = max(r[1] - mem_mb, 0)
        if isinstance(b, RemoteNodeBackend):
            b.invalidate()

    def by_name(self, name: str | None) -> Backend | None:
        if name == "local":
            return self.local
        return next((b for b in self.nodes if b.name == name), None)
