"""Chooses a backend for a job.

Remote nodes: the node with the most free capacity that fits the job's cpus/mem (from
GET /v1/info, cached for node_info_ttl seconds) is tried first, then the next; a 409 from a
node moves on to the next. If nothing fits right now the job waits (status "queued") and is
retried. A job larger than every reachable node's total capacity is rejected.

AWS Lambda nodes (lambda_backend.py) take part the same way: their "capacity" is one
invocation's size and they are "free" while a concurrency slot is open. They are tried after
every Docker node that has room (Lambda costs money per use), so they take the overflow. A node may also refuse
a job for other reasons (Backend.rejects(): image, timeout, network), and is then skipped.

Local execution (this host's Docker) is used only when enabled (default: when no node is
configured), and only as the last option.

Monitoring: a heartbeat thread probes every node's /v1/info every node_heartbeat seconds so
the admin "Nodes" view knows when a node was last seen. An admin can mark a node "draining":
it gets no new jobs, running jobs finish normally. The draining set is kept in
<data_dir>/nodes.json so it survives a restart.
"""
import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from .backends import Backend, LocalDockerBackend, RemoteNodeBackend

log = logging.getLogger("dockerserver.scheduler")


class NodePool:
    def __init__(self, settings, nodes: list[Backend] | None = None,
                 local: LocalDockerBackend | None = None):
        self.settings = settings
        self.nodes: list[Backend] = nodes if nodes is not None else [
            make_backend(n, settings) for n in settings.nodes]
        self.local = local
        self._lock = threading.Lock()
        # in-flight reservations not yet visible in a node's /v1/info (name -> [cpus, mem])
        self._pending: dict[str, list[float]] = {}
        self._state_file = settings.data_dir / "nodes.json"
        self.draining: set[str] = self._load_draining()
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None

    # ---- draining (persisted) ----
    def _load_draining(self) -> set[str]:
        try:
            return set(json.loads(self._state_file.read_text()).get("draining", []))
        except FileNotFoundError:
            return set()
        except (OSError, ValueError, AttributeError) as e:
            log.warning("cannot read %s: %s", self._state_file, e)
            return set()

    def set_draining(self, name: str, draining: bool) -> bool:
        if not any(b.name == name for b in self.nodes):
            raise KeyError(name)
        with self._lock:
            (self.draining.add if draining else self.draining.discard)(name)
            data = {"draining": sorted(self.draining)}
        tmp = self._state_file.with_suffix(".tmp")
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=1) + "\n")
        os.replace(tmp, self._state_file)
        log.warning("node %s %s", name, "draining (no new jobs)" if draining else "accepting jobs again")
        return draining

    # ---- monitoring ----
    def probe_all(self, timeout: float = 4.0):
        """Probe every remote node once, in parallel."""
        remotes = [b for b in self.nodes if hasattr(b, "probe")]
        if not remotes:
            return
        with ThreadPoolExecutor(max_workers=min(len(remotes), 8)) as ex:
            list(ex.map(lambda b: b.probe(timeout), remotes))

    def health(self) -> list[dict]:
        out = []
        with self._lock:
            pending = {k: list(v) for k, v in self._pending.items()}
        for b in self.nodes:
            h = b.health() if hasattr(b, "health") else {"name": b.name, "online": True}
            h["draining"] = b.name in self.draining
            pc, pm = pending.get(b.name, (0, 0))
            h["pending"] = {"cpus": pc, "mem_mb": pm}
            out.append(h)
        return out

    def start_heartbeat(self):
        interval = getattr(self.settings, "node_heartbeat", 0)
        if not interval or interval <= 0 or not self.nodes or self._hb_thread is not None:
            return
        self._hb_stop.clear()

        def loop():
            while not self._hb_stop.is_set():
                try:
                    self.probe_all()
                except Exception:  # never let the heartbeat die
                    log.exception("node heartbeat")
                self._hb_stop.wait(interval)
        self._hb_thread = threading.Thread(target=loop, name="node-heartbeat", daemon=True)
        self._hb_thread.start()

    def stop_heartbeat(self):
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=5)
            self._hb_thread = None

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

    def never_fits(self, cpus: float, mem_mb: int, needs_env: bool = False, *,
                   image: str | None = None, timeout_seconds: int | None = None,
                   network: bool | None = None) -> str | None:
        """Reason string if no node (that we can see) could ever run this job, else None."""
        if not self.nodes:
            return None
        infos = self._infos()
        if not infos:
            return None  # nodes unreachable right now: can't tell, let it queue
        refusals = [why for b, _ in infos if (why := b.rejects(image, timeout_seconds, network))]
        infos = [(b, i) for b, i in infos if not b.rejects(image, timeout_seconds, network)]
        if not infos:
            if self.local is not None:
                return None
            return f"no compute node can run this job: {refusals[0]}"
        if needs_env:
            infos = [(b, i) for b, i in infos if "env" in (i.get("features") or [])]
            if not infos and self.local is None:
                return ("environment variables are not supported by the compute nodes yet "
                        "(their agent needs updating); remove env/user variables or ask the admin")
        if any(i["capacity"]["cpus"] + 1e-9 >= cpus and i["capacity"]["mem_mb"] >= mem_mb
               for _, i in infos):
            return None
        biggest = max(infos, key=lambda bi: (bi[1]["capacity"]["cpus"], bi[1]["capacity"]["mem_mb"]))[1]
        return (f"job size {cpus} CPU / {mem_mb} MB is larger than any compute node "
                f"(largest: {biggest['capacity']['cpus']} CPU / {biggest['capacity']['mem_mb']} MB)")

    def candidates(self, cpus: float, mem_mb: int, needs_env: bool = False, *,
                   image: str | None = None, timeout_seconds: int | None = None,
                   network: bool | None = None) -> list[Backend]:
        """Backends to try, best first (only nodes that support env when the job has any,
        and that don't reject the job's image/timeout/network)."""
        ranked = []
        with self._lock:
            pending = {k: list(v) for k, v in self._pending.items()}
        for b, info in self._infos():
            if b.name in self.draining:
                continue
            if needs_env and "env" not in (info.get("features") or []):
                continue
            if b.rejects(image, timeout_seconds, network):
                continue
            free = info["free"]
            pc, pm = pending.get(b.name, (0, 0))
            fc, fm = free["cpus"] - pc, free["mem_mb"] - pm
            if fc + 1e-9 >= cpus and fm >= mem_mb:
                # own Docker nodes before pay-per-use Lambda; then most free CPUs, then memory
                ranked.append(((getattr(b, "kind", "docker") != "lambda", fc, fm), b))
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
        if hasattr(b, "invalidate"):
            b.invalidate()

    def by_name(self, name: str | None) -> Backend | None:
        if name == "local":
            return self.local
        return next((b for b in self.nodes if b.name == name), None)


def make_backend(n, settings) -> Backend:
    if getattr(n, "kind", "docker") == "lambda":
        from .lambda_backend import LambdaBackend
        return LambdaBackend(n.name, poll_interval=settings.node_poll_interval, **n.options)
    return RemoteNodeBackend(n.name, n.url, n.token, poll_interval=settings.node_poll_interval)
