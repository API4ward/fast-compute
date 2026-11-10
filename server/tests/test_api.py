"""API/auth tests that do not need Docker (runner is stubbed)."""
import io
import tarfile
import zipfile

from conftest import auth, make_user

from dockerserver.workspace import build_workspace


class StubRunner:
    def __init__(self):
        self.submitted = []

    def submit(self, job_id, filename, content):
        self.submitted.append((job_id, filename, content))

    def recover(self):
        pass


def test_requires_token(client):
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers=auth("ds_bogus")).status_code == 401


def test_admin_user_and_token_lifecycle(client, admin_token):
    u = make_user(client, admin_token, "alice", max_cpus=0.5, max_mem_mb=128)
    assert u["role"] == "user" and u["max_cpus"] == 0.5
    tok = u["token"]
    assert client.get("/me", headers=auth(tok)).json()["name"] == "alice"
    # non-admins cannot use admin endpoints
    assert client.get("/admin/users", headers=auth(tok)).status_code == 403
    # duplicate name
    r = client.post("/admin/users", json={"name": "alice"}, headers=auth(admin_token))
    assert r.status_code == 409
    # set limits
    r = client.patch(f"/admin/users/{u['id']}", json={"max_run_seconds": 30}, headers=auth(admin_token))
    assert r.json()["max_run_seconds"] == 30
    # second token, then revoke the first
    t2 = client.post(f"/admin/users/{u['id']}/tokens", json={"name": "laptop"}, headers=auth(admin_token)).json()
    toks = client.get(f"/admin/users/{u['id']}/tokens", headers=auth(admin_token)).json()
    assert len(toks) == 2 and all("token_hash" not in t for t in toks)
    first = [t for t in toks if t["id"] != t2["id"]][0]
    client.delete(f"/admin/tokens/{first['id']}", headers=auth(admin_token))
    assert client.get("/me", headers=auth(tok)).status_code == 401
    assert client.get("/me", headers=auth(t2["token"])).status_code == 200
    # disable user -> all tokens stop working
    client.post(f"/admin/users/{u['id']}/disable", headers=auth(admin_token))
    assert client.get("/me", headers=auth(t2["token"])).status_code == 401
    assert len(client.get("/admin/users", headers=auth(admin_token)).json()) == 2


def test_tokens_stored_hashed(app, client, admin_token):
    from dockerserver.models import Token
    with app.state.Session() as db:
        stored = [t.token_hash for t in db.query(Token)]
    assert admin_token not in stored and len(stored[0]) == 64


def test_job_limits_and_visibility(tmp_path):
    from fastapi.testclient import TestClient
    from dockerserver.api import create_app
    from dockerserver.config import Settings
    from dockerserver.auth import issue_token
    from dockerserver.models import User

    s = Settings(data_dir=tmp_path)
    s.logs_dir.mkdir(parents=True)
    stub = StubRunner()
    app = create_app(s, runner=stub, recover=False)
    with app.state.Session() as db:
        adm = User(name="root", role="admin")
        db.add(adm); db.commit()
        atok = issue_token(db, adm)[1]
    with TestClient(app) as c:
        a = make_user(c, atok, "a", max_cpus=1, max_mem_mb=256, max_run_seconds=60)
        b = make_user(c, atok, "b")
        h = auth(a["token"])
        assert c.post("/jobs", data={"command": "echo", "cpus": "2"}, headers=h).status_code == 400
        assert c.post("/jobs", data={"command": "echo", "mem_mb": "1024"}, headers=h).status_code == 400
        assert c.post("/jobs", data={"command": "echo", "timeout_seconds": "999"}, headers=h).status_code == 400
        assert c.post("/jobs", data={"command": "echo", "network": "true"}, headers=h).status_code == 403
        assert c.post("/jobs", data={"command": "echo", "image": "evil:latest"}, headers=h).status_code == 400
        assert c.post("/jobs", data={"command": "echo", "git_url": "file:///etc"}, headers=h).status_code == 400
        r = c.post("/jobs", data={"command": "python main.py"},
                   files={"file": ("main.py", b"print(1)")}, headers=h)
        assert r.status_code == 201, r.text
        job = r.json()
        assert job["cpus"] == 1 and job["mem_mb"] == 256 and job["image"] == "python:3.12-slim"
        assert stub.submitted[-1][1:] == ("main.py", b"print(1)")
        # b can't see a's job; admin can
        hb = auth(b["token"])
        assert c.get(f"/jobs/{job['id']}", headers=hb).status_code == 404
        assert c.get(f"/jobs/{job['id']}/logs", headers=hb).status_code == 404
        assert c.post(f"/jobs/{job['id']}/stop", headers=hb).status_code == 404
        assert c.get("/jobs", headers=hb).json() == []
        assert c.get(f"/jobs/{job['id']}", headers=auth(atok)).status_code == 200
        assert len(c.get("/jobs?all=true", headers=auth(atok)).json()) == 1
        assert c.get("/jobs?all=true", headers=hb).json() == []  # all ignored for non-admin
        # concurrency limit (default 2 active jobs)
        c.post("/jobs", data={"command": "x"}, headers=h)
        assert c.post("/jobs", data={"command": "x"}, headers=h).status_code == 429


def _names(tar_bytes):
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as t:
        return {m.name: m for m in t}


def test_workspace_sanitizes_archives():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name in ("ok/a.py", "../escape.txt", "/abs.txt"):
            ti = tarfile.TarInfo(name); ti.size = 1; t.addfile(ti, io.BytesIO(b"x"))
        ln = tarfile.TarInfo("link"); ln.type = tarfile.SYMTYPE; ln.linkname = "/etc/passwd"; t.addfile(ln)
    m = _names(build_workspace("code.tar", buf.getvalue(), None, 65534, 65534, 10**6))
    assert set(m) == {"ws", "ws/ok", "ws/ok/a.py", "ws/abs.txt"}
    assert all(x.uid == 65534 for x in m.values())

    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("pkg/main.py", "print(1)")
        z.writestr("../../evil", "x")
    assert set(_names(build_workspace("c.zip", zbuf.getvalue(), None, 1, 1, 10**6))) == {"ws", "ws/pkg", "ws/pkg/main.py"}
