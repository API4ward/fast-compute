"""Static web sites: upload, publish, serve through the proxy, sleep / wake, limits, cleanup.

Most tests use FakeRuntime (a Python HTTP server per site instead of an nginx container);
test_real_nginx_container runs the real container when Docker is reachable."""
import base64
import functools
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from conftest import auth, make_user, needs_docker
from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.config import Settings
from dockerserver.models import Site, User

GAME = {
    "index.html": '<!doctype html><meta charset="utf-8"><title>枪斗术</title>'
                  '<link href="https://fonts.googleapis.com/css2?family=Inter" rel="stylesheet">'
                  '<script src="engine.js"></script><script src="levels.js"></script>'
                  '<script src="game.js"></script><h1>枪斗术</h1>',
    "engine.js": "window.ENGINE = '引擎';\n",
    "game.js": "console.log('start', ENGINE, LEVELS.length);\n",
    "levels.js": "window.LEVELS = [{name: '第 1 关'}];\n",
}


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class FakeRuntime:
    """Stands in for DockerSiteRuntime: one threaded HTTP server per awake site."""

    def __init__(self):
        self.servers = {}
        self.starts = 0

    def start(self, site_id, user_id, site_dir):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(site_dir)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cid = f"fake-{site_id}-{self.starts}"
        self.starts += 1
        self.servers[cid] = srv
        return cid, f"127.0.0.1:{srv.server_address[1]}"

    def address(self, cid):
        srv = self.servers.get(cid)
        return f"127.0.0.1:{srv.server_address[1]}" if srv else None

    def stop(self, cid):
        srv = self.servers.pop(cid, None)
        if srv:
            srv.shutdown()
            srv.server_close()

    def list(self):
        return [(cid, int(cid.split("-")[1])) for cid in self.servers]


def _make(tmp_path, **kw):
    s = Settings(data_dir=tmp_path, **kw)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    rt = FakeRuntime()
    return create_app(s, recover=False, site_runtime=rt), rt


@pytest.fixture
def env(tmp_path):
    app, rt = _make(tmp_path)
    with TestClient(app) as c:
        with app.state.Session() as db:
            adm = User(name="root", role="admin", max_cpus=1.0, max_mem_mb=256, max_run_seconds=60)
            db.add(adm)
            db.commit()
            atok = issue_token(db, adm)[1]
        u = make_user(c, atok, "jian")
        yield {"c": c, "app": app, "rt": rt, "admin": atok, "tok": u["token"], "uid": u["id"]}


def upload_game(c, tok, site="gun-fu"):
    for path, text in GAME.items():
        r = c.put(f"/me/sites/{site}/files/{path}", json={"content": text}, headers=auth(tok))
        assert r.status_code == 200, r.text


def test_upload_publish_serve(env):
    c, tok = env["c"], env["tok"]
    upload_game(c, tok)
    # not yet published -> 404
    assert c.get("/sites/jian/gun-fu/").status_code == 404
    r = c.post("/me/sites/gun-fu/publish", headers=auth(tok))
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    assert url.endswith("/sites/jian/gun-fu/")
    # without trailing slash -> redirect (relative links need it)
    r = c.get("/sites/jian/gun-fu", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/sites/jian/gun-fu/"
    r = c.get("/sites/jian/gun-fu/")
    assert r.status_code == 200 and "枪斗术" in r.text and "text/html" in r.headers["content-type"]
    assert "content-security-policy" not in {k.lower() for k in r.headers}
    for name in ("engine.js", "levels.js", "game.js"):
        r = c.get(f"/sites/jian/gun-fu/{name}")
        assert r.status_code == 200 and r.content == GAME[name].encode()
    assert c.get("/sites/jian/gun-fu/missing.js").status_code == 404
    assert c.get("/sites/jian/nope/").status_code == 404
    assert env["rt"].starts == 1      # one wake-up served all requests
    sites = c.get("/me/sites", headers=auth(tok)).json()
    assert len(sites) == 1
    s = sites[0]
    assert s["published"] and s["awake"] and s["file_count"] == 4
    assert s["requests"] == 5 and s["bytes_served"] > 0 and s["wake_count"] == 1
    files = c.get("/me/sites/gun-fu/files", headers=auth(tok)).json()
    assert sorted(f["path"] for f in files) == sorted(GAME)


def test_sleep_after_idle_and_wake_on_request(env):
    c, tok, app, rt = env["c"], env["tok"], env["app"], env["rt"]
    upload_game(c, tok)
    c.post("/me/sites/gun-fu/publish", headers=auth(tok))
    assert c.get("/sites/jian/gun-fu/").status_code == 200
    mgr = app.state.services.sites
    mgr.tick()                       # recently used: stays awake
    assert rt.servers
    app.state.settings.site_idle_seconds = 0
    mgr.tick()                       # idle: container stopped
    assert not rt.servers
    s = c.get("/me/sites", headers=auth(tok)).json()[0]
    assert not s["awake"] and s["awake_hours"] >= 0
    r = c.get("/sites/jian/gun-fu/game.js")   # wakes it again
    assert r.status_code == 200 and rt.starts == 2
    assert c.get("/me/sites", headers=auth(tok)).json()[0]["wake_count"] == 2


def test_container_killed_behind_our_back_is_restarted(env):
    c, tok, rt = env["c"], env["tok"], env["rt"]
    upload_game(c, tok)
    c.post("/me/sites/gun-fu/publish", headers=auth(tok))
    assert c.get("/sites/jian/gun-fu/").status_code == 200
    for cid in list(rt.servers):
        rt.stop(cid)
    assert c.get("/sites/jian/gun-fu/").status_code == 200
    assert rt.starts == 2


def test_max_awake_evicts_least_recently_used(env):
    c, tok, app, rt = env["c"], env["tok"], env["app"], env["rt"]
    app.state.settings.site_max_awake = 1
    for site in ("a", "b"):
        upload_game(c, tok, site)
        c.post(f"/me/sites/{site}/publish", headers=auth(tok))
    assert c.get("/sites/jian/a/").status_code == 200
    assert c.get("/sites/jian/b/").status_code == 200
    assert len(rt.servers) == 1
    awake = {s["site"]: s["awake"] for s in c.get("/me/sites", headers=auth(tok)).json()}
    assert awake == {"a": False, "b": True}


@pytest.mark.parametrize("path", ["../x.html", "/etc/passwd", "a/../../b", "a\\b.js", "a//b",
                                  "./a", "a/./b", "", "x\x00y"])
def test_bad_paths_rejected(env, path):
    svc, c = env["app"].state.services, env["c"]
    with env["app"].state.Session() as db:
        u = db.get(User, env["uid"])
        from dockerserver.services import ServiceError
        with pytest.raises(ServiceError) as e:
            svc.put_site_file(db, u, "s", path, "x")
        assert e.value.status == 400


def test_bad_site_names_rejected(env):
    c, tok = env["c"], env["tok"]
    for name in ("UPPER", "-x", "a_b", "x" * 41):
        r = c.put(f"/me/sites/{name}/files/index.html", json={"content": "x"}, headers=auth(tok))
        assert r.status_code == 400, name


def test_limits(tmp_path):
    app, _ = _make(tmp_path, site_max_file_bytes=100, site_max_bytes=250)
    with TestClient(app) as c:
        with app.state.Session() as db:
            adm = User(name="root", role="admin")
            db.add(adm)
            db.commit()
            atok = issue_token(db, adm)[1]
        u = make_user(c, atok, "lim", max_sites=1)
        h = auth(u["token"])
        assert c.put("/me/sites/s/files/big.txt", json={"content": "x" * 101}, headers=h).status_code == 413
        for i in range(2):
            assert c.put(f"/me/sites/s/files/f{i}.txt", json={"content": "x" * 100}, headers=h).status_code == 200
        assert c.put("/me/sites/s/files/f2.txt", json={"content": "x" * 100}, headers=h).status_code == 413
        # replacing an existing file counts only the difference
        assert c.put("/me/sites/s/files/f1.txt", json={"content": "y" * 100}, headers=h).status_code == 200
        # second site over max_sites
        assert c.put("/me/sites/t/files/a.txt", json={"content": "x"}, headers=h).status_code == 403
        # a file where a directory is needed
        assert c.put("/me/sites/s/files/f0.txt/x", json={"content": "x"}, headers=h).status_code == 409


def test_base64_and_delete_file(env):
    c, tok = env["c"], env["tok"]
    png = b"\x89PNG\r\n\x1a\n\x00binary"
    r = c.put("/me/sites/s/files/img/logo.png", headers=auth(tok),
              json={"content": base64.b64encode(png).decode(), "encoding": "base64"})
    assert r.status_code == 200 and r.json()["bytes"] == len(png)
    c.put("/me/sites/s/files/index.html", json={"content": "<p>hi"}, headers=auth(tok))
    c.post("/me/sites/s/publish", headers=auth(tok))
    assert c.get("/sites/jian/s/img/logo.png").content == png
    assert c.put("/me/sites/s/files/x.bin", headers=auth(tok),
                 json={"content": "not base64!", "encoding": "base64"}).status_code == 400
    assert c.delete("/me/sites/s/files/img/logo.png", headers=auth(tok)).status_code == 200
    assert c.get("/sites/jian/s/img/logo.png").status_code == 404
    assert c.get("/me/sites", headers=auth(tok)).json()[0]["file_count"] == 1


def test_publish_requires_files_and_unpublish(env):
    c, tok, rt = env["c"], env["tok"], env["rt"]
    assert c.post("/me/sites/nothing/publish", headers=auth(tok)).status_code == 404
    upload_game(c, tok)
    c.post("/me/sites/gun-fu/publish", headers=auth(tok))
    assert c.get("/sites/jian/gun-fu/").status_code == 200
    r = c.post("/me/sites/gun-fu/unpublish", headers=auth(tok))
    assert r.status_code == 200 and not r.json()["published"]
    assert not rt.servers and c.get("/sites/jian/gun-fu/").status_code == 404


def test_users_are_separated_and_delete_site(env):
    c, tok, app = env["c"], env["tok"], env["app"]
    other = make_user(c, env["admin"], "eve")
    upload_game(c, tok)
    c.post("/me/sites/gun-fu/publish", headers=auth(tok))
    # eve's namespace is her own: same site name is a different (empty) site
    assert c.post("/me/sites/gun-fu/publish", headers=auth(other["token"])).status_code == 404
    assert c.delete("/me/sites/gun-fu", headers=auth(other["token"])).status_code == 404
    assert c.get("/sites/jian/gun-fu/").status_code == 200
    d = app.state.settings.sites_dir / f"u{env['uid']}" / "gun-fu"
    assert d.is_dir()
    assert c.delete("/me/sites/gun-fu", headers=auth(tok)).status_code == 200
    assert not d.exists() and not env["rt"].servers
    assert c.get("/sites/jian/gun-fu/").status_code == 404
    assert c.get("/me/sites", headers=auth(tok)).json() == []


def test_admin_sees_sites_and_delete_user_cleans_up(env):
    c, tok, app, rt, atok = env["c"], env["tok"], env["app"], env["rt"], env["admin"]
    upload_game(c, tok)
    c.post("/me/sites/gun-fu/publish", headers=auth(tok))
    c.get("/sites/jian/gun-fu/")
    assert c.get("/admin/sites", headers=auth(tok)).status_code == 403
    rows = c.get("/admin/sites", headers=auth(atok)).json()
    assert len(rows) == 1 and rows[0]["user"] == "jian" and rows[0]["awake"]
    assert rows[0]["requests"] == 1 and rows[0]["container"].startswith("fc-site-")
    r = c.delete(f"/admin/users/{env['uid']}", headers=auth(atok))
    assert r.status_code == 200, r.text
    assert r.json()["deleted_sites"] == ["gun-fu"]
    assert not rt.servers
    assert not (app.state.settings.sites_dir / f"u{env['uid']}").exists()
    assert c.get("/admin/sites", headers=auth(atok)).json() == []
    assert c.get("/me", headers=auth(tok)).status_code == 401
    assert "jian" not in [u["name"] for u in c.get("/admin/users", headers=auth(atok)).json()]
    assert c.get("/sites/jian/gun-fu/").status_code == 404
    # the name is free again
    assert make_user(c, atok, "jian")["name"] == "jian"
    # admins cannot delete themselves
    me = c.get("/me", headers=auth(atok)).json()
    assert c.delete(f"/admin/users/{me['id']}", headers=auth(atok)).status_code == 400


def test_separate_sites_host(tmp_path):
    app, _ = _make(tmp_path, public_url="https://compute.example.com",
                   sites_url="https://sites.example.com")
    with TestClient(app, base_url="https://compute.example.com") as c:
        r = c.get("/sites/a/b/x.js?v=1", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "https://sites.example.com/sites/a/b/x.js?v=1"
        r = c.get("/ui", headers={"host": "sites.example.com"})
        assert r.status_code == 404
        r = c.get("/sites/a/b/", headers={"host": "sites.example.com"})
        assert r.status_code == 404 and r.text == "site not found"


def test_idle_sleep_charges_when_rate_set(env):
    c, tok, app = env["c"], env["tok"], env["app"]
    app.state.settings.site_credit_per_hour = 1.0
    upload_game(c, tok)
    c.post("/me/sites/gun-fu/publish", headers=auth(tok))
    c.get("/sites/jian/gun-fu/")
    with app.state.Session() as db:
        site = db.query(Site).one()
        from datetime import timedelta
        site.awake_since = site.awake_since - timedelta(hours=2)
        db.commit()
    before = c.get("/me", headers=auth(tok)).json()["balance_hours"]
    app.state.settings.site_idle_seconds = 0
    app.state.services.sites.tick()
    after = c.get("/me", headers=auth(tok)).json()["balance_hours"]
    assert 1.99 < before - after < 2.01
    s = c.get("/me/sites", headers=auth(tok)).json()[0]
    assert 1.99 < s["cost_hours"] < 2.01


@needs_docker
def test_real_nginx_container(tmp_path):
    """The real thing: nginx container on the internal network, content types, sleep."""
    import docker
    s = Settings(data_dir=tmp_path, site_network="fc-sites-test")
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(s, recover=False)
    client = docker.from_env()
    try:
        with TestClient(app) as c:
            with app.state.Session() as db:
                adm = User(name="root", role="admin")
                db.add(adm)
                db.commit()
                atok = issue_token(db, adm)[1]
            tok = make_user(c, atok, "jian")["token"]
            upload_game(c, tok)
            c.put("/me/sites/gun-fu/files/data/info.json", json={"content": '{"a": "中"}'}, headers=auth(tok))
            c.put("/me/sites/gun-fu/files/sub/index.html", json={"content": "<p>sub"}, headers=auth(tok))
            c.post("/me/sites/gun-fu/publish", headers=auth(tok))
            r = c.get("/sites/jian/gun-fu/")
            assert r.status_code == 200, r.text
            assert r.headers["content-type"] == "text/html; charset=utf-8"
            assert "枪斗术" in r.text
            for name in ("engine.js", "game.js", "levels.js"):
                r = c.get(f"/sites/jian/gun-fu/{name}")
                assert r.status_code == 200
                assert r.headers["content-type"] == "application/javascript; charset=utf-8"
                assert r.text == GAME[name]
            assert c.get("/sites/jian/gun-fu/data/info.json").headers["content-type"] == \
                "application/json; charset=utf-8"
            r = c.get("/sites/jian/gun-fu/sub", follow_redirects=False)
            assert r.status_code == 301 and r.headers["location"] == "/sites/jian/gun-fu/sub/"
            assert c.get("/sites/jian/gun-fu/sub/").text == "<p>sub"
            etag = c.get("/sites/jian/gun-fu/game.js").headers["etag"]
            assert c.get("/sites/jian/gun-fu/game.js", headers={"If-None-Match": etag}).status_code == 304
            assert c.get("/sites/jian/gun-fu/nginx.conf").status_code == 404
            cs = client.containers.list(filters={"label": "fc.kind=site"})
            assert len(cs) == 1
            attrs = cs[0].attrs
            assert attrs["HostConfig"]["ReadonlyRootfs"] and attrs["Config"]["User"] == "101:101"
            assert attrs["HostConfig"]["Memory"] == 32 * 2**20
            # the container has no way out
            rc, out = cs[0].exec_run(["wget", "-q", "-T", "2", "-O-", "http://1.1.1.1/"])
            assert rc != 0
            app.state.settings.site_idle_seconds = 0
            app.state.services.sites.tick()
            assert client.containers.list(all=True, filters={"label": "fc.kind=site"}) == []
            t0 = time.monotonic()
            assert c.get("/sites/jian/gun-fu/game.js").status_code == 200
            assert time.monotonic() - t0 < 5
    finally:
        for ct in client.containers.list(all=True, filters={"label": "fc.kind=site"}):
            ct.remove(force=True)
        for n in client.networks.list(names=["fc-sites-test"]):
            n.remove()
