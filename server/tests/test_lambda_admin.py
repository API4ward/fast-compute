"""Lambda nodes configured from /admin-ui: storage (keys encrypted, never returned), hot add /
replace / delete in the node pool, and the step-by-step diagnostics (with fake AWS clients)."""
import json

import pytest
from fastapi.testclient import TestClient

from conftest import auth
from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.config import NodeConfig, Settings
from dockerserver.lambda_admin import LambdaConfigError, clean_node
from dockerserver.models import User
from dockerserver.scheduler import NodePool

pytest.importorskip("boto3")

NODE = {"function": "fast-compute-job", "bucket": "fc-jobs-123-eu-west-2", "region": "eu-west-2",
        "max_concurrency": 4, "network": "none"}
KEY_ID, SECRET = "AKIAABCDEFGHIJKL1234", "s3cr3t/abcdefghijklmnopqrstuvwxyz0123"


def make(tmp_path, nodes=None):
    s = Settings(data_dir=tmp_path, local_execution=False, node_heartbeat=0)
    if nodes:
        s.nodes = nodes
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s, recover=False, enable_mcp=False, pool=NodePool(s, nodes=[], local=None))
    from sqlalchemy import select
    with app.state.Session() as db:
        adm = db.scalar(select(User).where(User.name == "root"))
        usr = db.scalar(select(User).where(User.name == "bob"))
        if adm is None:
            adm, usr = User(name="root", role="admin"), User(name="bob", role="user")
            db.add_all([adm, usr])
            db.commit()
        app.state.tok = issue_token(db, adm)[1]
        app.state.user_tok = issue_token(db, usr)[1]
    return app


def test_clean_node_validation():
    n = clean_node("aws", dict(NODE, images="python:3.12-slim, python:3.11-slim", prefix="x"))
    assert n["images"] == ["python:3.12-slim", "python:3.11-slim"] and n["prefix"] == "x/"
    for bad in ({"bucket": "b-1"}, dict(NODE, network="vpc"), dict(NODE, region="London"),
                dict(NODE, prefix="../etc"), dict(NODE, profile="x"), dict(NODE, max_concurrency=0),
                dict(NODE, s3_endpoint_url="file:///etc")):
        with pytest.raises(LambdaConfigError):
            clean_node("aws", bad)
    with pytest.raises(LambdaConfigError):
        clean_node("local", NODE)


def test_keys_nodes_lifecycle(tmp_path):
    app = make(tmp_path)
    pool = app.state.runner.pool
    with TestClient(app) as c:
        h = auth(app.state.tok)
        assert c.get("/admin/lambda", headers=auth(app.state.user_tok)).status_code == 403
        st = c.get("/admin/lambda", headers=h).json()
        assert st["boto3"] and not st["credentials"]["set"] and st["nodes"] == []

        r = c.put("/admin/lambda/credentials", headers=h,
                  json={"access_key_id": KEY_ID, "secret_access_key": SECRET})
        assert r.status_code == 200, r.text
        assert r.json()["credentials"]["key_id_hint"] == "…1234"
        raw = (tmp_path / "lambda.json").read_text()
        assert KEY_ID not in raw and SECRET not in raw and SECRET not in r.text
        assert oct((tmp_path / "lambda.json").stat().st_mode & 0o777) == "0o600"

        r = c.put("/admin/lambda/nodes/aws", headers=h, json=NODE)
        assert r.status_code == 200, r.text
        b = pool.by_name("aws")
        assert b.kind == "lambda" and b.credential_source == "admin" and b.max_concurrency == 4
        assert any(n["name"] == "aws" for n in c.get("/admin/nodes?probe=false", headers=h).json()["nodes"])

        # replace keeps slot accounting (jobs running on the old object still count)
        b._inflight.add("ds1-1")
        c.put("/admin/lambda/nodes/aws", headers=h, json=dict(NODE, max_concurrency=6))
        b2 = pool.by_name("aws")
        assert b2 is not b and b2.max_concurrency == 6 and "ds1-1" in b2._inflight
        b2._inflight.clear()

        assert c.put("/admin/lambda/nodes/aws", headers=h, json={"function": "x"}).status_code == 400
        assert c.delete("/admin/lambda/nodes/aws", headers=h).status_code == 200
        assert pool.by_name("aws") is None
        assert c.delete("/admin/lambda/nodes/aws", headers=h).status_code == 404
        st = c.delete("/admin/lambda/credentials", headers=h).json()
        assert not st["credentials"]["set"]


def test_saved_nodes_load_on_start_and_env_wins(tmp_path):
    app = make(tmp_path)
    with TestClient(app) as c:
        c.put("/admin/lambda/nodes/aws", headers=auth(app.state.tok), json=NODE)
    app2 = make(tmp_path)
    assert app2.state.runner.pool.by_name("aws").function == "fast-compute-job"
    # a node of the same name in .env: not overridden, not editable here
    env = [NodeConfig("aws", "lambda://x/f", "", kind="lambda", options={"function": "f", "bucket": "bucket-1"})]
    app3 = make(tmp_path, nodes=env)
    app3.state.runner.pool.put_node(app3.state.lambda_admin._backend({"name": "aws", "function": "f",
                                                                      "bucket": "bucket-1"}))
    app3.state.lambda_admin.install_all()
    assert app3.state.runner.pool.by_name("aws").function == "f"
    with TestClient(app3) as c:
        assert c.put("/admin/lambda/nodes/aws", headers=auth(app3.state.tok), json=NODE).status_code == 400


class FakeErr(Exception):
    def __init__(self, code, msg="nope"):
        super().__init__(msg)
        self.response = {"Error": {"Code": code, "Message": msg}}


class FakeAWS:
    def __init__(self, sts_ok=True, in_vpc=True, retries=0):
        self.sts_ok, self.in_vpc, self.retries, self.objects = sts_ok, in_vpc, retries, {}

    # sts
    def get_caller_identity(self):
        if not self.sts_ok:
            raise FakeErr("InvalidClientTokenId", "The security token included in the request is invalid.")
        return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/fast-compute-server"}

    # lambda
    def get_function_configuration(self, FunctionName):
        return {"MemorySize": 2048, "Timeout": 900, "State": "Active", "LastUpdateStatus": "Successful",
                "PackageType": "Image", "Architectures": ["x86_64"], "LastModified": "2026-11-28T00:00:00",
                "VpcConfig": {"SubnetIds": ["subnet-1"] if self.in_vpc else []}}

    def get_function_event_invoke_config(self, FunctionName):
        return {"MaximumRetryAttempts": self.retries, "MaximumEventAgeInSeconds": 3600}

    # s3
    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def test_diagnose_steps(tmp_path, monkeypatch):
    app = make(tmp_path)
    lam = app.state.lambda_admin
    fake = FakeAWS(in_vpc=False, retries=2)
    real = lam._backend

    def backend(node, **extra):
        b = real(node, lambda_client=fake, s3_client=fake, **extra)
        b.client = lambda service: fake
        return b
    monkeypatch.setattr(lam, "_backend", backend)
    with TestClient(app) as c:
        h = auth(app.state.tok)
        r = c.post("/admin/lambda/test", headers=h, json={"node": dict(NODE, name="draft")}).json()
        steps = {s["step"].split(" ")[0]: s for s in r["steps"]}
        assert steps["credentials"]["ok"] and "123456789012" in steps["credentials"]["detail"]
        assert steps["function"]["ok"] and "WARNING" in steps["function"]["detail"]
        assert not steps["async"]["ok"] and "MaximumRetryAttempts=2" in steps["async"]["detail"]
        assert steps["S3"]["ok"] and fake.objects == {}
        assert r["ok"] is False and r["target"] == "draft"

        fake.sts_ok = False
        c.put("/admin/lambda/nodes/aws", headers=h, json=NODE)
        r = c.post("/admin/lambda/test", headers=h, json={"name": "aws"}).json()
        cred = next(s for s in r["steps"] if s["step"].startswith("credentials"))
        assert not cred["ok"] and "InvalidClientTokenId" in cred["detail"]
        assert c.post("/admin/lambda/test", headers=h, json={"name": "nope"}).json()["ok"] is False
        assert c.post("/admin/lambda/test", headers=h, json={}).status_code == 400
        bad = c.post("/admin/lambda/test", headers=h, json={"node": {"function": "f"}}).json()
        assert bad["steps"][-1]["step"] == "settings" and not bad["ok"]


def test_diagnose_test_job(tmp_path, monkeypatch):
    """run_job runs a real job through the backend (invoker stub writes the final status)."""
    app = make(tmp_path)
    lam = app.state.lambda_admin
    fake = FakeAWS()
    real = lam._backend

    def invoker(event):
        job = event["job"]
        fake.objects[f"fast-compute/jobs/{job}/status.json"] = json.dumps(
            {"status": "succeeded", "exit_code": 0}).encode()
        fake.objects[f"fast-compute/jobs/{job}/output.log"] = b"hello from lambda\n"

    def backend(node, **extra):
        b = real(node, lambda_client=fake, s3_client=fake, invoker=invoker, **extra)
        b.client = lambda service: fake
        b.s3.generate_presigned_url = lambda *a, **k: "https://s3.example/x"
        return b
    fake.generate_presigned_url = lambda *a, **k: "https://s3.example/x"

    def get_object(Bucket, Key):
        import io
        if Key not in fake.objects:
            raise FakeErr("NoSuchKey")
        return {"Body": io.BytesIO(fake.objects[Key]), "ETag": "1"}
    fake.get_object = get_object
    monkeypatch.setattr(lam, "_backend", backend)
    with TestClient(app) as c:
        h = auth(app.state.tok)
        c.put("/admin/lambda/nodes/aws", headers=h, json=NODE)
        r = c.post("/admin/lambda/test", headers=h, json={"name": "aws", "run_job": True,
                                                          "command": "echo hi"}).json()
        job = r["steps"][-1]
        assert job["step"] == "test job" and job["ok"], job
        assert "hello from lambda" in job["logs"] and job["command"] == "echo hi"
        assert not [k for k in fake.objects if "/ds0-" in k]       # cleaned up
        assert not app.state.runner.pool.by_name("aws")._inflight
