"""Caddy config management (/admin/caddy*), against a fake Caddy admin API."""
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import auth, make_user
from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.config import Settings
from dockerserver.models import User

ORIGINAL = "old.example.com {\n\treverse_proxy 127.0.0.1:8800\n}\n"


class FakeCaddy:
    """Minimal stand-in for Caddy's admin API: /adapt, /load, /config/."""

    def __init__(self):
        self.running = self.adapt(ORIGINAL)
        self.loads = 0
        self.up = True

    @staticmethod
    def adapt(text):
        routes = []
        for m in re.finditer(r"^(\S[^{\n]*)\{\s*\n\s*reverse_proxy (\S+)", text, re.M):
            hosts = [h.strip() for h in m.group(1).split(",") if h.strip()]
            routes.append({"match": [{"host": hosts}],
                           "handle": [{"handler": "subroute", "routes": [{"handle": [
                               {"handler": "reverse_proxy", "upstreams": [{"dial": m.group(2)}]}]}]}]})
        return {"apps": {"http": {"servers": {"srv0": {"listen": [":443"], "routes": routes}}}}}

    def handler(self, req: httpx.Request):
        if not self.up:
            raise httpx.ConnectError("refused")
        if req.url.path == "/adapt":
            text = req.content.decode()
            if "BROKEN" in text:
                return httpx.Response(400, json={"error": "Caddyfile:2: unrecognized directive: BROKEN"})
            return httpx.Response(200, json={"result": self.adapt(text)})
        if req.url.path == "/load":
            cfg = json.loads(req.content)
            if "fail.example.com" in json.dumps(cfg):
                return httpx.Response(400, json={"error": "loading new config: tls: bad thing"})
            self.running, self.loads = cfg, self.loads + 1
            return httpx.Response(200)
        if req.url.path == "/config/":
            return httpx.Response(200, json=self.running)
        return httpx.Response(404)


@pytest.fixture
def caddy_env(tmp_path):
    fake = FakeCaddy()
    cf = tmp_path / "Caddyfile"
    cf.write_text(ORIGINAL)
    cf.chmod(0o640)
    s = Settings(data_dir=tmp_path / "data", caddyfile=cf,
                 allowed_hosts=["127.0.0.1:8800", "old.example.com"])
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s, recover=False, enable_mcp=False, caddy_transport=httpx.MockTransport(fake.handler))
    with app.state.Session() as db:
        u = User(name="root", role="admin")
        db.add(u)
        db.commit()
        tok = issue_token(db, u)[1]
    with TestClient(app) as c:
        yield c, auth(tok), fake, cf, tok


def test_status_shows_file_and_running_sites(caddy_env):
    c, h, fake, cf, _ = caddy_env
    st = c.get("/admin/caddy", headers=h).json()
    assert st["content"] == ORIGINAL and st["reachable"] and st["in_sync"] is True
    assert st["running_sites"][0]["hosts"] == ["old.example.com"]
    assert st["running_sites"][0]["upstreams"] == ["127.0.0.1:8800"]
    assert st["running_sites"][0]["allowed_host"] == {"old.example.com": True}


def test_admin_only(caddy_env):
    c, h, fake, cf, admin_tok = caddy_env
    u = make_user(c, admin_tok, "eve")
    uh = auth(u["token"])
    assert c.get("/admin/caddy", headers=uh).status_code == 403
    assert c.put("/admin/caddy", headers=uh, json={"content": "x {\n}\n"}).status_code == 403
    assert c.get("/admin/caddy/backups", headers=uh).status_code == 403
    assert c.get("/admin/caddy").status_code == 401
    assert cf.read_text() == ORIGINAL


def test_validate_does_not_change_anything(caddy_env):
    c, h, fake, cf, _ = caddy_env
    r = c.post("/admin/caddy/validate", headers=h, json={"content": "new.example.com {\n\treverse_proxy 127.0.0.1:9000\n}\n"}).json()
    assert r["ok"] and r["sites"][0]["hosts"] == ["new.example.com"]
    r = c.post("/admin/caddy/validate", headers=h, json={"content": "a {\n\tBROKEN\n}\n"}).json()
    assert r["ok"] is False and "unrecognized directive" in r["error"]
    assert cf.read_text() == ORIGINAL and fake.loads == 0


def test_apply_writes_backs_up_and_rolls_back(caddy_env):
    c, h, fake, cf, _ = caddy_env
    base = c.get("/admin/caddy", headers=h).json()["sha256"]
    new = "new.example.com, old.example.com {\n\treverse_proxy 127.0.0.1:8800\n}"
    r = c.put("/admin/caddy", headers=h, json={"content": new, "base_sha256": base})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] and body["backup"].endswith("-root.Caddyfile")
    assert cf.read_text() == new + "\n" and oct(cf.stat().st_mode & 0o777) == "0o640"
    assert fake.loads == 1
    st = c.get("/admin/caddy", headers=h).json()
    assert st["in_sync"] is True
    assert st["running_sites"][0]["allowed_host"] == {"new.example.com": False, "old.example.com": True}

    # stale editor -> 409, nothing changes
    r = c.put("/admin/caddy", headers=h, json={"content": ORIGINAL, "base_sha256": base})
    assert r.status_code == 409 and fake.loads == 1

    # roll back by loading the backup into the editor and applying it
    bs = c.get("/admin/caddy/backups", headers=h).json()
    assert len(bs) == 1 and bs[0]["replaced_by"] == "root"
    old = c.get(f"/admin/caddy/backups/{bs[0]['name']}", headers=h).text
    assert old == ORIGINAL
    r = c.put("/admin/caddy", headers=h, json={"content": old, "base_sha256": st["sha256"]})
    assert r.status_code == 200 and cf.read_text() == ORIGINAL
    assert len(c.get("/admin/caddy/backups", headers=h).json()) == 2


def test_bad_config_is_not_written(caddy_env):
    c, h, fake, cf, _ = caddy_env
    r = c.put("/admin/caddy", headers=h, json={"content": "x {\n\tBROKEN\n}\n"})
    assert r.status_code == 422 and "unrecognized directive" in r.json()["detail"]
    # Caddy refuses at load time (e.g. provisioning error): file untouched, no backup
    r = c.put("/admin/caddy", headers=h, json={"content": "fail.example.com {\n\treverse_proxy 127.0.0.1:8800\n}\n"})
    assert r.status_code == 422 and "still running the old one" in r.json()["detail"]
    assert cf.read_text() == ORIGINAL and fake.loads == 0
    assert c.get("/admin/caddy/backups", headers=h).json() == []


def test_drift_and_reload_from_file(caddy_env):
    c, h, fake, cf, _ = caddy_env
    cf.write_text("edited.example.com {\n\treverse_proxy 127.0.0.1:8800\n}\n")   # hand edit on the host
    assert c.get("/admin/caddy", headers=h).json()["in_sync"] is False
    assert c.post("/admin/caddy/reload", headers=h).status_code == 200
    assert c.get("/admin/caddy", headers=h).json()["in_sync"] is True


def test_caddy_down_and_bad_backup_names(caddy_env):
    c, h, fake, cf, _ = caddy_env
    fake.up = False
    st = c.get("/admin/caddy", headers=h).json()
    assert st["reachable"] is False and "unreachable" in st["error"] and st["content"] == ORIGINAL
    assert c.put("/admin/caddy", headers=h, json={"content": ORIGINAL}).status_code == 502
    for name in ("..%2F..%2Fetc%2Fpasswd", "x.Caddyfile", "Caddyfile"):
        assert c.get(f"/admin/caddy/backups/{name}", headers=h).status_code == 404
    assert c.put("/admin/caddy", headers=h, json={"content": "a" * 300000}).status_code == 422


def test_management_can_be_turned_off(tmp_path):
    s = Settings(data_dir=tmp_path, caddyfile=tmp_path / "Caddyfile", caddy_manage=False)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s, recover=False, enable_mcp=False)
    with app.state.Session() as db:
        u = User(name="root", role="admin")
        db.add(u)
        db.commit()
        tok = issue_token(db, u)[1]
    with TestClient(app) as c:
        st = c.get("/admin/caddy", headers=auth(tok)).json()
        assert st["enabled"] is False and st["content"] == ""
        assert c.put("/admin/caddy", headers=auth(tok), json={"content": "a {\n}\n"}).status_code == 404


def test_admin_api_must_be_loopback():
    from dockerserver.caddy import CaddyManager
    with pytest.raises(SystemExit):
        CaddyManager("/tmp/x", "http://10.0.0.5:2019", "/tmp/b")
