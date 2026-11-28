"""AWS Lambda nodes: the server-side backend and the real handler (lambda-node/handler.py),
against an in-process S3 (moto server). The "invoke" runs the handler in a thread instead of
in AWS, so the whole path - S3 workspace, pre-signed URLs, status heartbeats, logs, kill -
is exercised without an AWS account."""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

boto3 = pytest.importorskip("boto3")
moto_server = pytest.importorskip("moto.server")

from conftest import auth  # noqa: E402
from test_nodes import FakeNode, big_user, wait_job  # noqa: E402

from dockerserver.api import create_app  # noqa: E402
from dockerserver.auth import issue_token  # noqa: E402
from dockerserver.backends import RemoteNodeBackend  # noqa: E402
from dockerserver.config import Settings, parse_nodes  # noqa: E402
from dockerserver.lambda_backend import LambdaBackend  # noqa: E402
from dockerserver.models import User  # noqa: E402
from dockerserver.scheduler import NodePool, make_backend  # noqa: E402

import httpx  # noqa: E402

HANDLER = Path(__file__).resolve().parents[2] / "lambda-node" / "handler.py"
BUCKET = "fc-test"


def load_handler():
    spec = importlib.util.spec_from_file_location("fc_lambda_handler", HANDLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def s3_url():
    srv = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=0)
    srv.start()
    host, port = srv.get_host_and_port()
    yield f"http://{host}:{port}"
    srv.stop()


@pytest.fixture
def s3(s3_url, monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                 "AWS_DEFAULT_REGION": "eu-west-2"}.items():
        monkeypatch.setenv(k, v)
    from botocore.config import Config
    c = boto3.client("s3", endpoint_url=s3_url, region_name="eu-west-2",
                     config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    try:
        c.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "eu-west-2"})
    except c.exceptions.BucketAlreadyOwnedByYou:
        pass
    return c


class FakeLambda:
    """get_function_configuration + an async invoke that runs the real handler in a thread."""

    def __init__(self, memory=2048, timeout=900, state="Active"):
        self.memory, self.timeout, self.state = memory, timeout, state
        self.handler = load_handler()
        self.handler.HEARTBEAT_EVERY = 0.5
        self.handler.LOG_EVERY = 0.3
        self.handler.KILL_EVERY = 0.2
        self.handler.TICK = 0.05
        self.events: list[dict] = []
        self.threads: list[threading.Thread] = []
        self.hang = False           # simulate an invocation that dies without reporting

    def get_function_configuration(self, FunctionName):
        return {"MemorySize": self.memory, "Timeout": self.timeout, "State": self.state,
                "LastUpdateStatus": "Successful", "Architectures": ["x86_64"],
                "VpcConfig": {"SubnetIds": ["subnet-1"]}}

    def invoke_event(self, event):
        self.events.append(event)
        if self.hang:
            return
        # handler.run, not handler.handler: the latter ends the process (fresh_sandbox)
        t = threading.Thread(target=self.handler.run, args=(json.loads(json.dumps(event)),), daemon=True)
        t.start()
        self.threads.append(t)


def make_lambda(s3, fake, **kw):
    kw.setdefault("network", "none")
    return LambdaBackend("aws", function="fc-test", bucket=BUCKET, region="eu-west-2",
                         s3_client=s3, lambda_client=fake, invoker=fake.invoke_event,
                         poll_interval=0.05, max_concurrency=kw.pop("max_concurrency", 2), **kw)


def make_app(tmp_path, backends, **kw):
    s = Settings(data_dir=tmp_path, monitor_interval=0.2, queue_retry=0.2, node_info_ttl=0,
                 node_lost_grace=5, local_execution=False, min_start_seconds=1, **kw)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s, recover=False, enable_mcp=False, pool=NodePool(s, nodes=backends, local=None))
    with app.state.Session() as db:
        adm = User(name="root", role="admin", max_cpus=64, max_mem_mb=100000)
        db.add(adm)
        db.commit()
        app.state.admin_token = issue_token(db, adm)[1]
    return app


def keys(s3):
    return [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


def test_job_runs_on_lambda_with_workspace_env_logs(tmp_path, s3):
    fake = FakeLambda()
    app = make_app(tmp_path, [make_lambda(s3, fake)])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "lena")
        h = auth(u["token"])
        assert c.put("/me/env/GREETING", headers=h, json={"value": "hej"}).status_code in (200, 204)
        r = c.post("/jobs", headers=h, data={"command": "python3 main.py; echo $GREETING; "
                                                         "env | grep -c AWS_ || true; exit 4",
                                             "mem_mb": "512"},
                   files={"file": ("main.py", b"print('from lambda', open('main.py').read()[:5])")})
        assert r.status_code == 201, r.text
        j = wait_job(c, u["token"], r.json()["id"])
        assert j["node"] == "aws" and j["status"] == "failed" and j["exit_code"] == 4, j
        assert j["billed"] and j["duration_seconds"] is not None
        logs = c.get(f"/jobs/{j['id']}/logs", headers=h).text
        assert "from lambda print" in logs and "hej" in logs
        assert logs.strip().splitlines()[-1] == "0"            # no AWS_* credentials in the job
        ev = fake.events[0]
        assert ev["env"] == {"GREETING": "hej"} and ev["fresh_sandbox"] is True
        assert keys(s3) == []                                  # cleaned up after billing


def test_kill_and_timeout_on_lambda(tmp_path, s3):
    fake = FakeLambda()
    app = make_app(tmp_path, [make_lambda(s3, fake)])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "mo")
        h = auth(u["token"])
        jid = c.post("/jobs", headers=h, data={"command": "echo start; sleep 30"}).json()["id"]
        wait_job(c, u["token"], jid, want="running")
        time.sleep(0.5)
        assert "start" in c.get(f"/jobs/{jid}/logs", headers=h).text   # live log tail
        t0 = time.time()
        assert c.post(f"/jobs/{jid}/stop", headers=h).json()["status"] == "stopped"
        assert time.time() - t0 < 10
        j = c.get(f"/jobs/{jid}", headers=h).json()
        assert j["duration_seconds"] < 15
        j2 = wait_job(c, u["token"], c.post("/jobs", headers=h, data={
            "command": "sleep 30", "timeout_seconds": "1"}).json()["id"])
        assert j2["status"] == "timeout" and j2["node"] == "aws"


def test_dead_invocation_is_detected(tmp_path, s3):
    fake = FakeLambda()
    fake.hang = True
    be = make_lambda(s3, fake, start_timeout=1)
    app = make_app(tmp_path, [be])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "nils")
        j = wait_job(c, u["token"], c.post("/jobs", headers=auth(u["token"]),
                                           data={"command": "true"}).json()["id"])
        assert j["status"] == "error" and "did not start" in j["error"]
    assert not be._inflight


def test_rejects_image_timeout_network_and_size(tmp_path, s3):
    fake = FakeLambda(memory=1769, timeout=300)
    app = make_app(tmp_path, [make_lambda(s3, fake)], allowed_images=["python:3.12-slim", "node:22"])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "ola", allow_network=True)
        h = auth(u["token"])
        for data, msg in [({"timeout_seconds": "600"}, "longer than Lambda allows"),
                          ({"image": "node:22"}, "not available on Lambda"),
                          ({"network": "true"}, "need a Lambda node with internet"),
                          ({"cpus": "2"}, "larger than any compute node")]:
            r = c.post("/jobs", headers=h, data={"command": "true", "timeout_seconds": "60", **data})
            assert r.status_code == 400 and msg in r.json()["detail"], (data, r.text)


def test_overflow_from_docker_node_and_slots(tmp_path, s3):
    """Docker nodes go first (even with less free room); Lambda takes what does not fit."""
    fake = FakeLambda()
    node = FakeNode("mini", cpus=1)
    remote = RemoteNodeBackend("mini", "http://mini.test:8900", "node-secret-token-0123456789abcdef",
                               poll_interval=0.05, retries=1, transport=httpx.MockTransport(node.handler))
    lam = make_lambda(s3, fake, max_concurrency=1)
    app = make_app(tmp_path, [remote, lam])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "pam")
        h = auth(u["token"])
        j1 = c.post("/jobs", headers=h, data={"command": "sleep 2", "cpus": "1"}).json()["id"]
        assert wait_job(c, u["token"], j1, want="running")["node"] == "mini"
        j2 = c.post("/jobs", headers=h, data={"command": "sleep 1", "cpus": "1"}).json()["id"]
        assert wait_job(c, u["token"], j2, want="running")["node"] == "aws"
        d = {n["name"]: n for n in c.get("/admin/nodes", headers=auth(app.state.admin_token)).json()["nodes"]}
        assert d["aws"]["kind"] == "lambda" and d["mini"]["kind"] == "docker"
        assert d["aws"]["slots"] == {"max": 1, "used": 1} and d["aws"]["free"]["cpus"] == 0
        assert d["aws"]["capacity"] == {"cpus": 1.16, "mem_mb": 2048}
        assert d["aws"]["lambda"]["function"] == "fc-test" and d["aws"]["lambda"]["max_job_seconds"] == 870
        assert [x["id"] for x in d["aws"]["running_jobs"]] == [j2]
        assert wait_job(c, u["token"], j2)["status"] == "succeeded"
        assert wait_job(c, u["token"], j1)["status"] == "succeeded"


def test_offline_function_shows_in_nodes(tmp_path, s3):
    fake = FakeLambda(state="Failed")
    app = make_app(tmp_path, [make_lambda(s3, fake)])
    with TestClient(app) as c:
        d = c.get("/admin/nodes", headers=auth(app.state.admin_token)).json()["nodes"][0]
        assert d["status"] == "offline" and "Failed" in d["error"] and d["kind"] == "lambda"


def test_lambda_node_config(monkeypatch):
    nodes = parse_nodes(json.dumps([
        {"name": "aws", "type": "lambda", "function": "fast-compute-job", "bucket": "b",
         "region": "eu-west-2", "max_concurrency": 5, "network": "none"}]))
    assert nodes[0].kind == "lambda" and nodes[0].url == "lambda://eu-west-2/fast-compute-job"
    be = make_backend(nodes[0], Settings(nodes=nodes))
    assert isinstance(be, LambdaBackend) and be.max_concurrency == 5
    with pytest.raises(SystemExit):
        parse_nodes(json.dumps([{"name": "x", "type": "lambda", "function": "f"}]))       # no bucket
    with pytest.raises(SystemExit):
        parse_nodes(json.dumps([{"name": "x", "type": "lambda", "function": "f", "bucket": "b",
                                 "secret_key": "nope"}]))                                 # unknown key
    assert Settings(nodes=nodes).use_local is False


def test_handler_cleans_reused_environment(tmp_path, monkeypatch):
    """handler(): a second invocation in a reused environment kills leftover processes."""
    h = load_handler()
    import subprocess
    stray = subprocess.Popen(["sleep", "60"], start_new_session=True)
    monkeypatch.setattr(h, "USED_MARK", str(tmp_path / "used"))
    (tmp_path / "used").write_text("")
    called = {}
    monkeypatch.setattr(h, "run", lambda ev, ctx=None: called.setdefault("ok", True))
    monkeypatch.setattr(h.os, "_exit", lambda code: called.setdefault("exit", code))
    my_pid = __import__("os").getpid()
    real_kill = h.os.kill
    killed = []
    # only let it kill our stray, never anything else on the test machine
    monkeypatch.setattr(h.os, "kill", lambda pid, sig: (killed.append(pid), real_kill(pid, sig))
                        if pid == stray.pid else killed.append(pid))
    h.handler({"fresh_sandbox": True}, None)
    assert stray.wait(5) is not None and stray.pid in killed and my_pid not in killed
    assert called == {"ok": True, "exit": 0}


class NoListS3:
    """S3 as the server's IAM user sees it without s3:ListBucket (deploy.sh before Settings):
    reading a missing object is a 403 AccessDenied, not a 404."""

    def __init__(self, s3):
        self._s3, self.deny_all = s3, False

    def __getattr__(self, name):
        return getattr(self._s3, name)

    def _denied(self):
        from botocore.exceptions import ClientError
        return ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"},
                            "ResponseMetadata": {"HTTPStatusCode": 403}}, "GetObject")

    def get_object(self, **kw):
        if self.deny_all:
            raise self._denied()
        try:
            return self._s3.get_object(**kw)
        except self._s3.exceptions.NoSuchKey:
            raise self._denied() from None


def test_status_read_as_403_before_the_first_report_is_not_an_error(s3):
    from dockerserver.backends import Handle
    s3n = NoListS3(s3)
    be = make_lambda(s3n, FakeLambda(), start_timeout=30)
    now = time.time()
    h = Handle(be, "ds1-403", be.name, extra={"invoked": now, "etag": None, "changed": now})
    st = be._poll(h)                       # no status.json yet, read as 403: still starting
    assert st is not None and not st.finished and st.node_status == "preparing"
    assert h.unreachable_since is None
    s3.put_object(Bucket=BUCKET, Key="fast-compute/jobs/ds1-403/status.json",
                  Body=json.dumps({"status": "running"}).encode())
    assert be._poll(h).node_status == "running" and "first_seen" in h.extra
    s3n.deny_all = True                    # after the first report a 403 is a real error
    assert be._poll(h) is None and h.unreachable_since is not None


def test_recovered_job_that_never_reports_times_out(s3):
    from dockerserver.backends import Handle
    be = make_lambda(s3, FakeLambda(), start_timeout=30)
    be.start_timeout = 0.3
    h = Handle(be, "ds2-gone", be.name)    # as recover() builds it after a restart: no "invoked"
    assert not be._poll(h).finished
    time.sleep(0.4)
    st = be._poll(h)
    assert st.finished and "did not start the job within" in st.error
