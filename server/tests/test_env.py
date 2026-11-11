"""Per-user environment variables: validation, encryption at rest, masking, precedence,
privacy from admins, and delivery to the container (local Docker and compute nodes)."""
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import auth, make_user, needs_docker, wait_done
from dockerserver.config import ensure_secret_key
from dockerserver.models import Job, UserEnv
from dockerserver.userenv import EnvError, check_env, check_name, mask
from test_nodes import FakeNode, big_user, make_app, wait_job

SECRET = "sk-test-0123456789abcdefSECRET"


def test_validation_rules():
    for bad in ("1ABC", "A-B", "", "x" * 65, "PATH", "home", "LD_PRELOAD", "DOCKERSERVER_X"):
        with pytest.raises(EnvError):
            check_name(bad)
    assert check_name("OPENAI_API_KEY") == "OPENAI_API_KEY"
    with pytest.raises(EnvError):
        check_env({"A": "x" * 4097})
    with pytest.raises(EnvError):
        check_env({"A": "a\x00b"})
    with pytest.raises(EnvError):
        check_env({f"V{i}": "1" for i in range(51)})
    assert mask("short") == "••••" and mask(SECRET) == "••••CRET"


def test_secret_key_generated_into_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCKERSERVER_SECRET_KEY", "")
    env = tmp_path / ".env"
    ensure_secret_key(env)
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    key = os.environ["DOCKERSERVER_SECRET_KEY"]
    assert f"DOCKERSERVER_SECRET_KEY={key}" in env.read_text()


def test_rest_crud_masking_encryption_and_admin_privacy(app, client, admin_token):
    u = make_user(client, admin_token, "eva")
    h = auth(u["token"])
    assert client.put("/me/env/OPENAI_API_KEY", json={"value": SECRET}, headers=h).status_code == 204
    assert client.put("/me/env/MODE", json={"value": "fast"}, headers=h).status_code == 204
    assert client.put("/me/env/PATH", json={"value": "/x"}, headers=h).status_code == 400
    masked = {v["name"]: v for v in client.get("/me/env", headers=h).json()}
    assert masked["OPENAI_API_KEY"]["value"] == "••••CRET" and masked["OPENAI_API_KEY"]["length"] == len(SECRET)
    shown = {v["name"]: v["value"] for v in client.get("/me/env?reveal=true", headers=h).json()}
    assert shown == {"MODE": "fast", "OPENAI_API_KEY": SECRET}
    # encrypted at rest: the plaintext is nowhere in the database
    with app.state.Session() as db:
        blobs = [r.value_enc for r in db.scalars(select(UserEnv))]
    assert blobs and all(SECRET.encode() not in b and b"fast" not in b for b in blobs)
    raw = open(app.state.settings.data_dir / "dockerserver.db", "rb").read()
    assert SECRET.encode() not in raw
    # admins can't see it: their /me/env is their own; admin views never include values
    ah = auth(admin_token)
    assert client.get("/me/env?reveal=true", headers=ah).json() == []
    for path in (f"/admin/users/{u['id']}", "/admin/users", "/admin/jobs", "/admin/usage",
                 f"/admin/users/{u['id']}/ledger"):
        assert SECRET not in client.get(path, headers=ah).text
    assert client.delete("/me/env/MODE", headers=h).status_code == 204
    assert client.delete("/me/env/MODE", headers=h).status_code == 404
    assert [v["name"] for v in client.get("/me/env", headers=h).json()] == ["OPENAI_API_KEY"]


def test_precedence_and_names_only_on_job(tmp_path):
    from test_api import StubRunner
    from dockerserver.api import create_app
    from dockerserver.auth import issue_token
    from dockerserver.billing import grant_initial
    from dockerserver.config import Settings
    from dockerserver.models import User
    s = Settings(data_dir=tmp_path)
    s.logs_dir.mkdir(parents=True)
    stub = StubRunner()
    app = create_app(s, runner=stub, recover=False, enable_mcp=False)
    with app.state.Session() as db:
        usr = User(name="ivo")
        db.add(usr)
        db.flush()
        grant_initial(db, usr, 10)
        db.commit()
        tok = issue_token(db, usr)[1]
    with TestClient(app) as c:
        h = auth(tok)
        c.put("/me/env/GREETING", json={"value": "hello"}, headers=h)
        c.put("/me/env/API_KEY", json={"value": SECRET}, headers=h)
        r = c.post("/jobs", headers=h, data={"command": "env", "env": json.dumps({"GREETING": "override", "EXTRA": "1"})})
        assert r.status_code == 201, r.text
        assert stub.envs[-1] == {"GREETING": "override", "API_KEY": SECRET, "EXTRA": "1"}
        assert r.json()["env_names"] == "API_KEY,EXTRA,GREETING"
        assert c.post("/jobs", headers=h, data={"command": "env", "env": "{not json"}).status_code == 400
        assert c.post("/jobs", headers=h, data={"command": "env", "env": json.dumps({"HOME": "/"})}).status_code == 400
        with app.state.Session() as db:
            assert all(SECRET not in (j.env_names or "") and SECRET not in j.command for j in db.scalars(select(Job)))


@needs_docker
def test_env_reaches_local_container(client, admin_token):
    u = make_user(client, admin_token, "otto", max_cpus=0.5, max_mem_mb=64, max_run_seconds=30)
    h = auth(u["token"])
    client.put("/me/env/GREETING", json={"value": "hello"}, headers=h)
    client.put("/me/env/ONLY_USER", json={"value": "u1"}, headers=h)
    r = client.post("/jobs", headers=h, data={
        "command": 'echo "$GREETING $ONLY_USER $PER_JOB $HOME"', "cpus": "0.5", "mem_mb": "64",
        "env": json.dumps({"GREETING": "override", "PER_JOB": "j1"})})
    job = wait_done(client, u["token"], r.json()["id"])
    assert job["status"] == "succeeded", job
    assert client.get(f"/jobs/{job['id']}/logs", headers=h).text.strip() == "override u1 j1 /workspace"


def test_env_sent_only_to_nodes_that_support_it(tmp_path):
    old, new = FakeNode("old", cpus=10), FakeNode("new", cpus=4)
    new.features = ["env"]
    app = make_app(tmp_path, [old, new])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "pam")
        h = auth(u["token"])
        c.put("/me/env/TOKEN_X", json={"value": SECRET}, headers=h)
        j = wait_job(c, u["token"], c.post("/jobs", headers=h, data={"command": "echo"}).json()["id"])
        # "old" has more free CPUs but doesn't advertise env support -> job went to "new"
        assert j["status"] == "succeeded" and j["node"] == "new"
        assert new.specs[-1]["env"] == {"TOKEN_X": SECRET} and not old.specs


def test_env_job_refused_when_no_node_supports_it(tmp_path):
    app = make_app(tmp_path, [FakeNode("old")])
    with TestClient(app) as c:
        u = big_user(c, app.state.admin_token, "rex")
        h = auth(u["token"])
        assert c.post("/jobs", headers=h, data={"command": "true"}).status_code == 201   # no env: fine
        r = c.post("/jobs", headers=h, data={"command": "true", "env": json.dumps({"A": "1"})})
        assert r.status_code == 400 and "not supported by the compute nodes" in r.json()["detail"]
