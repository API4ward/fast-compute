"""Remote compute nodes, against an in-process fake node (httpx.MockTransport, no network)."""
import json
import re
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_default

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import auth, make_user
from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.backends import RemoteNodeBackend
from dockerserver.config import Settings, parse_nodes
from dockerserver.models import TERMINAL_STATUSES, User
from dockerserver.scheduler import NodePool

TOKEN = "node-secret-token-0123456789abcdef"


class FakeNode:
    """Mimics the computenode agent API. Commands: 'sleep N', 'exit K', anything else -> exit 0."""

    def __init__(self, name, cpus=10.0, mem_mb=24000):
        self.name, self.cap_cpus, self.cap_mem = name, cpus, mem_mb
        self.jobs: dict[str, dict] = {}
        self.up = True
        self.force_409 = False
        self.features: list[str] = []          # e.g. ["env"] for an updated agent
        self.specs: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.lock = threading.Lock()

    # -- simulation --
    def _state(self, j):
        if j["status"] in ("killed",) or j["finished_at"]:
            return j
        now = time.time()
        if now >= j["started_at"] + min(j["duration"], j["timeout"]):
            j["finished_at"] = j["started_at"] + min(j["duration"], j["timeout"])
            if j["duration"] > j["timeout"]:
                j["status"], j["exit_code"] = "timeout", 137
            else:
                j["exit_code"] = j["exit"]
                j["status"] = "succeeded" if j["exit"] == 0 else "failed"
        return j

    def _active(self):
        return [j for j in self.jobs.values() if self._state(j)["status"] == "running"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not self.up:
            raise httpx.ConnectError("node down", request=request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "bad token"})
        path, m = request.url.path, request.method
        self.calls.append((m, path))
        with self.lock:
            if m == "GET" and path == "/v1/info":
                act = self._active()
                uc, um = sum(j["cpus"] for j in act), sum(j["mem_mb"] for j in act)
                return httpx.Response(200, json={
                    "node": self.name, "capacity": {"cpus": self.cap_cpus, "mem_mb": self.cap_mem},
                    "used": {"cpus": uc, "mem_mb": um},
                    "free": {"cpus": self.cap_cpus - uc, "mem_mb": self.cap_mem - um},
                    "active_jobs": len(act), "features": self.features})
            if m == "POST" and path == "/v1/jobs":
                msg = BytesParser(policy=email_default).parsebytes(
                    b"Content-Type: " + request.headers["content-type"].encode() + b"\r\n\r\n" + request.content)
                parts = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True)
                         for p in msg.iter_parts()}
                spec = json.loads(parts["spec"])
                self.specs.append(spec)
                assert spec["image"] == "python:3.12-slim"
                act = self._active()
                if (self.force_409 or spec["cpus"] > self.cap_cpus
                        or sum(j["cpus"] for j in act) + spec["cpus"] > self.cap_cpus):
                    return httpx.Response(409, json={"detail": "node is full"})
                cmd = spec["command"]
                sleep = re.match(r"sleep (\d+(\.\d+)?)", cmd)
                ex = re.match(r"exit (\d+)", cmd)
                self.jobs[spec["id"]] = {
                    "id": spec["id"], "cpus": spec["cpus"], "mem_mb": spec["mem_mb"],
                    "timeout": spec["timeout_seconds"], "status": "running", "exit_code": None,
                    "duration": float(sleep.group(1)) if sleep else 0.05,
                    "exit": int(ex.group(1)) if ex else 0, "started_at": time.time(),
                    "finished_at": None, "workspace": len(parts.get("workspace") or b""),
                    "log": f"ran: {cmd}\n"}
                return httpx.Response(201, json={"id": spec["id"], "status": "running"})
            mj = re.match(r"^/v1/jobs/([^/]+)(/logs|/kill)?$", path)
            if not mj or mj.group(1) not in self.jobs:
                return httpx.Response(404, json={"detail": "no such job"})
            j = self._state(self.jobs[mj.group(1)])
            if m == "GET" and not mj.group(2):
                return httpx.Response(200, json={
                    "id": j["id"], "status": j["status"], "exit_code": j["exit_code"], "oom": False,
                    "error": "killed by gateway" if j["status"] == "killed" else None,
                    "started_at": j["started_at"], "finished_at": j["finished_at"],
                    "cpu_avg_pct": 42.0, "cpu_peak_pct": 55.0, "mem_avg_mb": 10.0,
                    "mem_peak_mb": 12.0, "stat_samples": 3})
            if m == "GET" and mj.group(2) == "/logs":
                return httpx.Response(200, text=j["log"])
            if m == "POST" and mj.group(2) == "/kill":
                if j["status"] == "running":
                    j["status"], j["finished_at"], j["exit_code"] = "killed", time.time(), 137
                return httpx.Response(200, json={"id": j["id"], "status": j["status"]})
            if m == "DELETE":
                del self.jobs[j["id"]]
                return httpx.Response(204)
        return httpx.Response(405)


def make_app(tmp_path, nodes, **kw):
    s = Settings(data_dir=tmp_path, monitor_interval=0.2, queue_retry=0.2, node_info_ttl=0,
                 node_lost_grace=1, local_execution=False, min_start_seconds=1, **kw)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    backends = [RemoteNodeBackend(n.name, f"http://{n.name}.test:8900", TOKEN, poll_interval=0.05,
                                  retries=1, transport=httpx.MockTransport(n.handler)) for n in nodes]
    app = create_app(s, recover=False, enable_mcp=False, pool=NodePool(s, nodes=backends, local=None))
    with app.state.Session() as db:
        adm = User(name="root", role="admin", max_cpus=64, max_mem_mb=100000)
        db.add(adm)
        db.commit()
        app.state.admin_token = issue_token(db, adm)[1]
    return app


def wait_job(c, tok, jid, timeout=20, want=None):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/jobs/{jid}", headers=auth(tok)).json()
        if want and j["status"] == want:
            return j
        if not want and j["status"] in TERMINAL_STATUSES and j["billed"]:
            return j
        time.sleep(0.1)
    raise AssertionError(f"job {jid} stuck: {j}")


def big_user(c, atok, name, **kw):
    return make_user(c, atok, name, max_cpus=16, max_mem_mb=32000, max_run_seconds=600,
                     max_concurrent_jobs=5, **kw)


def test_dispatches_to_most_free_node_and_cleans_up(tmp_path):
    a, b = FakeNode("a", cpus=4), FakeNode("b", cpus=10)
    app = make_app(tmp_path, [a, b])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "nina")
        r = c.post("/jobs", headers=auth(u["token"]), data={"command": "python main.py", "cpus": "2"},
                   files={"file": ("main.py", b"print('hi')")})
        assert r.status_code == 201, r.text
        j = wait_job(c, u["token"], r.json()["id"])
        assert j["status"] == "succeeded" and j["node"] == "b"          # b has more free room
        assert j["billed"] and j["cost_hours"] is not None
        assert j["stat_samples"] == 3 and j["cpu_peak_pct"] == 55.0     # node-reported stats
        assert "ran: python main.py" in c.get(f"/jobs/{j['id']}/logs", headers=auth(u["token"])).text
        assert any(m == "DELETE" for m, _ in b.calls)
        assert not b.jobs                                                # deleted after billing
        assert not any(m == "POST" for m, _ in a.calls)


def test_409_moves_to_next_node_and_exit_codes(tmp_path):
    a, b = FakeNode("a", cpus=10), FakeNode("b", cpus=4)
    a.force_409 = True
    app = make_app(tmp_path, [a, b])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "omar")
        j = wait_job(c, u["token"], c.post("/jobs", headers=auth(u["token"]),
                                           data={"command": "exit 3"}).json()["id"])
        assert j["node"] == "b" and j["status"] == "failed" and j["exit_code"] == 3


def test_queues_until_capacity_frees(tmp_path):
    a = FakeNode("a", cpus=1)
    app = make_app(tmp_path, [a])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "pia")
        h = auth(u["token"])
        j1 = c.post("/jobs", headers=h, data={"command": "sleep 1.5", "cpus": "1"}).json()["id"]
        wait_job(c, u["token"], j1, want="running")
        j2 = c.post("/jobs", headers=h, data={"command": "echo second", "cpus": "1"}).json()["id"]
        q = wait_job(c, u["token"], j2, want="queued")
        assert "capacity" in (q["error"] or "")
        done = wait_job(c, u["token"], j2)
        assert done["status"] == "succeeded" and done["node"] == "a" and done["error"] is None


def test_rejects_job_larger_than_any_node(tmp_path):
    app = make_app(tmp_path, [FakeNode("a", cpus=4, mem_mb=8000)])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "quinn")
        r = c.post("/jobs", headers=auth(u["token"]), data={"command": "true", "cpus": "8"})
        assert r.status_code == 400 and "larger than any compute node" in r.json()["detail"]


def test_out_of_credits_kills_on_node(tmp_path):
    a = FakeNode("a")
    app = make_app(tmp_path, [a])
    with TestClient(app) as c:
        atok = app.state.admin_token
        u = big_user(c, atok, "rita")
        # leave ~2 s of credit at 1 CPU
        c.post(f"/admin/users/{u['id']}/credits", headers=auth(atok),
               json={"hours": -1000 + 2 / 3600, "reason": "adjust"})
        jid = c.post("/jobs", headers=auth(u["token"]), data={"command": "sleep 30"}).json()["id"]
        j = wait_job(c, u["token"], jid)
        assert j["status"] == "out_of_credits" and j["billed"]
        assert any(m == "POST" and p.endswith("/kill") for m, p in a.calls)
        assert j["duration_seconds"] < 10


def test_suspend_kills_on_node(tmp_path):
    a = FakeNode("a")
    app = make_app(tmp_path, [a])
    with TestClient(app) as c:
        atok = app.state.admin_token
        u = big_user(c, atok, "sam")
        jid = c.post("/jobs", headers=auth(u["token"]), data={"command": "sleep 30"}).json()["id"]
        wait_job(c, u["token"], jid, want="running")
        r = c.post(f"/admin/users/{u['id']}/suspend", headers=auth(atok)).json()
        assert r["killed_job_ids"] == [jid]
        meta = c.get(f"/admin/jobs?user_id={u['id']}", headers=auth(atok)).json()[0]
        assert meta["status"] == "killed_by_admin" and meta["billed"] and meta["node"] == "a"
        assert any(p.endswith("/kill") for _, p in a.calls)


def test_unreachable_node_marks_job_lost(tmp_path):
    a = FakeNode("a")
    app = make_app(tmp_path, [a])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "tom")
        jid = c.post("/jobs", headers=auth(u["token"]), data={"command": "sleep 60"}).json()["id"]
        wait_job(c, u["token"], jid, want="running")
        time.sleep(0.5)
        a.up = False
        j = wait_job(c, u["token"], jid, timeout=15)
        assert j["status"] == "lost" and "unreachable" in j["error"] and j["billed"]
        assert j["duration_seconds"] < 5          # billed up to when the node was last seen


def test_nodes_config(monkeypatch):
    monkeypatch.setenv("MINI_TOKEN", TOKEN)
    nodes = parse_nodes(json.dumps([
        {"name": "mini", "url": "http://100.126.20.127:8900", "token_env": "MINI_TOKEN"},
        {"name": "gone", "url": "http://10.0.0.9:8900", "token_env": "NOT_SET_TOKEN"}]))
    assert [n.name for n in nodes] == ["mini"] and nodes[0].token == TOKEN
    assert TOKEN not in repr(nodes[0])
    s = Settings(nodes=nodes)
    assert s.use_local is False                 # nodes configured -> no local execution by default
    assert Settings(nodes=[]).use_local is True
    assert Settings(nodes=nodes, local_execution=True).use_local is True
    b = RemoteNodeBackend("mini", "http://x", TOKEN)
    assert TOKEN not in repr(b)


def test_default_limits_configurable(tmp_path):
    app = make_app(tmp_path, [FakeNode("a")], default_max_cpus=4, default_max_mem_mb=4096)
    with TestClient(app) as c:
        u = make_user(c, app.state.admin_token, "uma")
        assert u["max_cpus"] == 4 and u["max_mem_mb"] == 4096
