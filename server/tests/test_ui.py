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
    assert client.get("/admin/jobs?limit=100&status=running", headers=h).status_code == 200
    assert client.get(f"/admin/jobs?user_id={uid}&limit=20", headers=h).status_code == 200
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


def test_login_form_shown_without_token(client):
    # Regression: both panels start hidden; with no stored token the sign-in form must be
    # revealed (it used to stay hidden -> blank page on first visit).
    js = client.get("/static/common.js").text
    i = js.index("if (getToken())")
    assert 'login.classList.remove("hidden")' in js[i:i + 300]
    for page in ("/admin-ui", "/ui"):
        html = client.get(page).text
        assert 'id="login"' in html and 'id="token-input"' in html


def test_admin_aliases_redirect(client):
    for path in ("/admin", "/admin-ui/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/admin-ui"
    assert client.get("/ui/", follow_redirects=False).headers["location"] == "/ui"


def test_assets_are_cache_busted(client):
    import re
    html = client.get("/admin-ui").text
    js = re.search(r'src="(/static/common\.js\?v=[0-9a-f]{12})"', html)
    css = re.search(r'href="(/static/common\.css\?v=[0-9a-f]{12})"', html)
    assert js and css
    r = client.get(js.group(1))
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_job_tables_show_image(client):
    # /ui uses jobsTable (common.js), /admin-ui uses adminJobsTable (admin.html)
    js = client.get("/static/common.js").text
    i = js.index("function jobsTable")
    assert '"Image"' in js[i:i + 600] and "td(j.image)" in js[i:i + 1500]
    html = client.get("/admin-ui").text
    i = html.index("function adminJobsTable")
    assert '"Image"' in html[i:i + 600] and "td(j.image)" in html[i:i + 1200]
