"""Web pages are served, and the REST calls they make work for the right roles."""
import re

from conftest import auth, make_user


def test_pages_and_assets_served(client):
    for path, marker in (("/admin-ui", "DockerServer admin"), ("/ui", "My jobs")):
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        # every asset the page references exists
        for asset in re.findall(r'(?:src|href)="(/static/[^"]+)"', r.text):
            assert client.get(asset).status_code == 200, asset
    assert client.get("/static/common.js").text.count("sessionStorage") >= 1
    assert client.get("/", follow_redirects=False).headers["location"] == "/ui"


def test_no_token_in_pages(client, admin_token):
    # pages are static: no data or secrets are rendered server-side
    assert admin_token not in client.get("/admin-ui").text
    assert client.get("/admin-ui").status_code == 200  # no auth needed to load the shell


def test_admin_ui_api_calls(client, admin_token):
    h = auth(admin_token)
    u = make_user(client, admin_token, "ivy")
    uid = u["id"]
    assert client.get("/admin/users", headers=h).status_code == 200
    assert client.get(f"/admin/users/{uid}", headers=h).json()["name"] == "ivy"
    assert client.patch(f"/admin/users/{uid}", headers=h, json={
        "max_cpus": 0.5, "max_mem_mb": 128, "max_run_seconds": 30, "max_concurrent_jobs": 1,
        "allow_network": False, "role": "user"}).status_code == 200
    assert client.post(f"/admin/users/{uid}/credits", headers=h,
                       json={"hours": 5, "reason": "topup", "note": None}).status_code == 201
    assert client.get(f"/admin/users/{uid}/ledger?limit=100", headers=h).status_code == 200
    assert client.post(f"/admin/users/{uid}/tokens", headers=h, json={"name": "admin-ui"}).status_code == 201
    toks = client.get(f"/admin/users/{uid}/tokens", headers=h).json()
    assert client.delete(f"/admin/tokens/{toks[0]['id']}", headers=h).status_code == 200
    assert client.post(f"/admin/users/{uid}/disable", headers=h).json()["active"] is False
    assert client.post(f"/admin/users/{uid}/enable", headers=h).json()["active"] is True
    assert client.get("/jobs?all=true&limit=100&status=running", headers=h).status_code == 200
    r = client.get("/admin/usage?start=2026-01-01T00:00:00&end=2030-01-01T00:00:00", headers=h)
    assert r.status_code == 200 and "total" in r.json()


def test_user_ui_api_calls(client, admin_token):
    u = make_user(client, admin_token, "jay")
    h = auth(u["token"])
    me = client.get("/me", headers=h).json()
    assert me["role"] == "user"
    usage = client.get("/me/usage", headers=h).json()
    assert {"balance_hours", "available_hours", "total", "per_day", "jobs"} <= usage.keys()
    assert client.get("/jobs?limit=50", headers=h).json() == []
    # the admin page rejects non-admin tokens: its first admin call is 403
    assert client.get("/admin/users", headers=h).status_code == 403
