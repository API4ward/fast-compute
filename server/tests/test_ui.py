"""Web pages are served, and the REST calls they make work for the right roles."""
import re
from pathlib import Path

from conftest import auth, make_user


def test_pages_and_assets_served(client):
    for path, marker in (("/admin-ui", 'data-tab="settings"'), ("/ui", "My jobs"), ("/", "fast-compute"),
                         ("/welcome", 'id="welcome"')):
        r = client.get(path)
        assert r.status_code == 200 and marker in r.text
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        # every asset the page references exists
        for asset in re.findall(r'(?:src|href)="(/static/[^"]+)"', r.text):
            assert client.get(asset).status_code == 200, asset
    assert client.get("/static/common.js").text.count("sessionStorage") >= 1


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
    # /ui uses jobsTable (common.js), /admin-ui uses adminJobsTable (admin.html); headers are translated
    js = client.get("/static/common.js").text
    i = js.index("function jobsTable")
    assert 't("col.image")' in js[i:i + 600] and "td(j.image)" in js[i:i + 1500]
    assert '"col.image": "Image"' in js and '"col.image": "镜像"' in js
    html = client.get("/admin-ui").text
    i = html.index("function adminJobsTable")
    assert 't("col.image")' in html[i:i + 600] and "td(j.image)" in html[i:i + 1200]


def test_welcome_page_public(client):
    # /welcome needs no token; it loads the guide script and builds URLs from the request origin.
    r = client.get("/welcome")
    assert r.status_code == 200 and 'id="welcome"' in r.text
    assert re.search(r'src="/static/welcome\.js\?v=[0-9a-f]{12}"', r.text)
    js = client.get("/static/welcome.js").text
    assert "location.origin" in js and "compute.llmfreedomforge.com" not in js
    assert "zh:" in js and "en:" in js and "/mcp" in js
    info = client.get("/info").json()
    assert info["allowed_images"] and "version" in info


def test_user_ui_has_welcome_tab(client):
    html = client.get("/ui").text
    assert 'data-tab="welcome"' in html and 'data-tab="dashboard"' in html
    assert "/static/welcome.js" in html and 'href="/welcome"' in html


def test_admin_has_nodes_tab(client):
    html = client.get("/admin-ui").text
    assert 'data-tab="nodes"' in html and "/admin/nodes" in html and "function loadNodes" in html


STATIC = Path(__file__).resolve().parent.parent / "dockerserver" / "static"


def _dict_blocks(text):
    """The addI18n({en: {...}, zh: {...}}) blocks of a file as (en_keys, zh_keys) lists."""
    out = []
    for m in re.finditer(r"addI18n\(\{", text):
        en_i = text.index("en: {", m.end())
        zh_i = text.index("zh: {", en_i)
        end = text.index("});", zh_i)
        out.append((re.findall(r'"([\w.-]+)":', text[en_i:zh_i]), re.findall(r'"([\w.-]+)":', text[zh_i:end])))
    return out


def _used_keys(text):
    keys = set(re.findall(r'\btr?\("([\w.-]+)"(?!\s*\+)', text))
    keys |= set(re.findall(r'data-i18n(?:-rich|-ph|-title|-aria)?="([\w.-]+)"', text))
    return keys


def test_i18n_dictionaries_complete():
    # every dictionary has the same keys in English and Chinese, without duplicates, and every
    # literal key a page uses (t("..."), data-i18n="...") is defined for that page
    common = (STATIC / "common.js").read_text()
    pages = {"admin.html": ["admin-i18n.js", "admin-settings.js"], "user.html": ["welcome.js"],
             "welcome.html": ["welcome.js"]}
    for page, extra in pages.items():
        texts = [(STATIC / page).read_text()] + [(STATIC / f).read_text() for f in extra]
        defined = set()
        for text in [common] + texts:
            for en, zh in _dict_blocks(text):
                assert len(en) == len(set(en)) and len(zh) == len(set(zh)), f"duplicate keys ({page})"
                assert set(en) == set(zh), (page, sorted(set(en) ^ set(zh)))
                defined |= set(en)
        used = set().union(*(_used_keys(x) for x in [common] + texts))
        assert not used - defined, (page, sorted(used - defined))
    # dynamic keys (status pills, diagnostics steps, ...) exist too
    admin = (STATIC / "admin-i18n.js").read_text()
    for k in ("status.succeeded", "status.out_of_credits", "status.killed_by_admin"):
        assert f'"{k}"' in common
    for k in ("node.online", "node.draining", "node.offline", "set.src.admin", "set.src.env", "set.src.none",
              "set.st.ok", "set.st.warn", "set.st.fail", "set.st.skip", "set.phase.finished", "set.phase.error",
              "set.logs.m15", "set.logs.m1440", "set.mode.llm", "set.mode.faq", "set.step.presigned"):
        assert f'"{k}"' in admin, k


def test_language_switch_and_brand(client):
    js = client.get("/static/common.js").text
    # the choice is shared with the homepage and the welcome guide
    assert 'LANG_KEY = "fastcompute_lang"' in js and "function t(" in js and "data-lang-switch" in js
    assert "document.documentElement.lang" in js
    assert "fastcompute_lang" in client.get("/static/home.js").text
    assert "LANG" in client.get("/static/welcome.js").text
    for page in ("/ui", "/admin-ui", "/welcome"):
        html = client.get(page).text
        assert "data-lang-switch" in html and "fast-compute" in html and "DockerServer" not in html
        assert 'href="/"' in html   # brand links back to the homepage
    admin = client.get("/admin-ui").text
    for tab in ("users", "jobs", "nodes", "usage", "sites", "caddy", "settings"):
        assert f'nav button[data-tab="{tab}"]' not in admin   # (setupTabs selects them generically)
        assert f'<button data-tab="{tab}"' in admin and f'<section data-tab="{tab}"' in admin


def test_admin_settings_tab_uses_the_api(client):
    html = client.get("/admin-ui").text
    assert re.search(r'src="/static/admin-settings\.js\?v=[0-9a-f]{12}"', html)
    assert re.search(r'src="/static/admin-i18n\.js\?v=[0-9a-f]{12}"', html)
    for id_ in ("set-aws", "set-lambda", "set-deploy", "set-advisor", "settings-error"):
        assert f'id="{id_}"' in html
    js = client.get("/static/admin-settings.js").text
    for path in ('"/admin/settings"', '"/admin/settings/aws"', '"/admin/settings/aws/check"',
                 '"/admin/settings/lambda/nodes"', '"/admin/settings/lambda/validate"',
                 "/admin/settings/lambda/nodes/${enc(name)}/diagnose", "/admin/settings/lambda/nodes/${enc(name)}/test-job",
                 "/admin/settings/lambda/tests/${enc(id)}", "/admin/settings/lambda/nodes/${enc(name)}/logs?minutes=",
                 '"/admin/settings/advisor"', '"/admin/settings/advisor/test"', "lambda-node/deploy.sh"):
        assert path in js, path
    # the secret fields are never pre-filled and every call shows a readable error
    assert 'name: "secret_access_key", type: "password"' in js and 'name: "api_key", type: "password"' in js
    assert "errText(e)" in js and "function errText" in client.get("/static/common.js").text


def test_dashboards_build_dom_without_innerhtml(client):
    # server and AWS data only ever goes into the page as text
    html_sink = re.compile(r"\.(?:inner|outer)HTML\b|insertAdjacentHTML|document\.write")
    for asset in ("common.js", "admin-settings.js", "admin-i18n.js", "welcome.js"):
        assert not html_sink.search(client.get(f"/static/{asset}").text), asset
    for page in ("/ui", "/admin-ui", "/welcome"):
        html = client.get(page).text
        assert not html_sink.search(html), page
        # CSP default-src 'self': no external scripts, styles, fonts or images
        assert not re.search(r'(?:src|href)="(?:https?:)?//', html), page


def test_settings_script_skips_null_children():
    # Element.append(null) would print "null" in the page: admin-settings.js builds its optional
    # parts with addKids / setKids (which skip null like el()) instead of calling append directly.
    js = (STATIC / "admin-settings.js").read_text()
    direct = [ln.strip() for ln in js.splitlines() if ".append(" in ln and "//" not in ln.split(".append(")[0]]
    assert direct == [
        "function addKids(node, ...kids) { for (const c of kids.flat()) if (c != null) node.append(c); return node; }",
        'document.body.append(el("datalist", { id: "aws-regions" }, AWS_REGIONS.map(r => el("option", { value: r }))));',
    ]
