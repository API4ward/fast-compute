"""OAuth 2.1 for MCP connectors: discovery -> register -> authorize (PKCE) -> consent -> token ->
MCP call -> refresh -> revocation on disable / ds_ token revoke. Real HTTP via uvicorn."""
import base64
import hashlib
import re
import secrets
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn

from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.config import Settings
from dockerserver.models import User

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture
def srv(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    s = Settings(data_dir=tmp_path, public_url=base, allowed_hosts=["127.0.0.1:*"],
                 auth_fail_limit=50)
    s.logs_dir.mkdir(parents=True)
    app = create_app(s, recover=False)
    with app.state.Session() as db:
        adm = User(name="root", role="admin")
        usr = User(name="ula", role="user")
        db.add_all([adm, usr])
        db.commit()
        atok, utok = issue_token(db, adm)[1], issue_token(db, usr)[1]
        uid = usr.id
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield {"base": base, "admin": atok, "user": utok, "uid": uid}
    server.should_exit = True
    t.join(10)


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def register(base, redirect=CALLBACK):
    return httpx.post(f"{base}/register", json={
        "redirect_uris": [redirect], "client_name": "Claude", "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})


def authorize(base, client_id, ds_token, resource_path="/mcp", approve=True):
    """Run /authorize + the consent page like a browser; returns the callback query (dict)."""
    verifier, challenge = pkce()
    state = secrets.token_urlsafe(8)
    with httpx.Client(base_url=base, follow_redirects=False) as browser:
        r = browser.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
            "scope": "mcp", "resource": f"{base}{resource_path}"})
        assert r.status_code == 302, r.text
        consent = r.headers["location"]
        assert consent.startswith(f"{base}/oauth/consent?request=")
        page = browser.get(consent)
        assert page.status_code == 200 and "form-action 'self' https://claude.ai" in page.headers["content-security-policy"]
        rid = re.search(r'name="request" value="([^"]+)"', page.text).group(1)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        r = browser.post("/oauth/consent", data={"request": rid, "csrf": csrf, "token": ds_token,
                                                  "action": "approve" if approve else "deny"})
    if r.status_code != 302:
        return {"_status": r.status_code, "_body": r.text}
    loc = r.headers["location"]
    assert loc.startswith(CALLBACK)
    q = {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}
    assert q["state"] == state
    q["_verifier"] = verifier
    return q


def token(base, client_id, **data):
    return httpx.post(f"{base}/token", data={"client_id": client_id, **data})


def mcp(base, path, bearer):
    return httpx.post(f"{base}{path}", json=LIST, headers={**ACCEPT, "Authorization": f"Bearer {bearer}"})


def test_discovery_and_401_hint(srv):
    base = srv["base"]
    r = httpx.post(f"{base}/mcp", json=LIST, headers=ACCEPT)
    assert r.status_code == 401
    assert f'resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    prm = httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{base}/mcp" and prm["authorization_servers"] == [base]
    assert httpx.get(f"{base}/.well-known/oauth-protected-resource/mcp-admin").json()["resource"] == f"{base}/mcp-admin"
    meta = httpx.get(f"{base}/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == base                       # exact, no trailing slash (RFC 8414)
    assert meta["code_challenge_methods_supported"] == ["S256"]
    for k in ("authorization_endpoint", "token_endpoint", "registration_endpoint", "revocation_endpoint"):
        assert meta[k].startswith(base)


def test_registration_rejects_foreign_redirects_and_bad_host(srv):
    base = srv["base"]
    assert register(base, "https://evil.example.com/cb").status_code == 400
    assert register(base, "http://claude.ai/cb").status_code == 400      # https required
    assert register(base, "http://localhost:33418/callback").status_code == 201
    assert register(base).status_code == 201
    r = httpx.post(f"{base}/register", json={"redirect_uris": [CALLBACK]}, headers={"Host": "evil.example.com"})
    assert r.status_code == 421


def test_full_flow_refresh_and_revocation(srv):
    base, uid = srv["base"], srv["uid"]
    cid = register(base).json()["client_id"]

    # consent page: wrong token is refused; deny returns access_denied
    bad = authorize(base, cid, "ds_wrong")
    assert bad["_status"] == 400 and "not valid" in bad["_body"]
    assert authorize(base, cid, srv["user"], approve=False)["error"] == "access_denied"

    q = authorize(base, cid, srv["user"])
    t = token(base, cid, grant_type="authorization_code", code=q["code"], redirect_uri=CALLBACK,
              code_verifier=q["_verifier"], resource=f"{base}/mcp")
    assert t.status_code == 200, t.text
    tok = t.json()
    assert tok["access_token"].startswith("dsa_") and tok["refresh_token"].startswith("dsr_")
    # code is single-use; PKCE verifier must match
    again = token(base, cid, grant_type="authorization_code", code=q["code"], redirect_uri=CALLBACK,
                  code_verifier=q["_verifier"])
    assert again.status_code == 400

    # the OAuth token works on /mcp only (not /mcp-admin, not the REST API)
    r = mcp(base, "/mcp", tok["access_token"])
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 7
    assert mcp(base, "/mcp-admin", tok["access_token"]).status_code == 401
    assert httpx.get(f"{base}/me", headers={"Authorization": f"Bearer {tok['access_token']}"}).status_code == 401
    # tools run as the approving user
    call = httpx.post(f"{base}/mcp", headers={**ACCEPT, "Authorization": f"Bearer {tok['access_token']}"},
                      json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "get_balance", "arguments": {}}})
    assert call.json()["result"]["structuredContent"]["user"] == "ula"

    # refresh rotates: new pair works, old access + old refresh are dead
    r2 = token(base, cid, grant_type="refresh_token", refresh_token=tok["refresh_token"])
    assert r2.status_code == 200, r2.text
    tok2 = r2.json()
    assert mcp(base, "/mcp", tok2["access_token"]).status_code == 200
    assert mcp(base, "/mcp", tok["access_token"]).status_code == 401
    assert token(base, cid, grant_type="refresh_token", refresh_token=tok["refresh_token"]).status_code == 400

    # disabling the user revokes the grant (and it stays revoked after re-enabling)
    adm = {"Authorization": f"Bearer {srv['admin']}"}
    assert httpx.post(f"{base}/admin/users/{uid}/disable", headers=adm).status_code == 200
    assert mcp(base, "/mcp", tok2["access_token"]).status_code == 401
    assert token(base, cid, grant_type="refresh_token", refresh_token=tok2["refresh_token"]).status_code == 400
    httpx.post(f"{base}/admin/users/{uid}/enable", headers=adm)
    assert mcp(base, "/mcp", tok2["access_token"]).status_code == 401

    # a fresh grant, then revoking the ds_ token it was approved with kills it
    q = authorize(base, cid, srv["user"])
    tok3 = token(base, cid, grant_type="authorization_code", code=q["code"], redirect_uri=CALLBACK,
                 code_verifier=q["_verifier"]).json()
    assert mcp(base, "/mcp", tok3["access_token"]).status_code == 200
    ds_id = [x for x in httpx.get(f"{base}/admin/users/{uid}/tokens", headers=adm).json()][0]["id"]
    assert httpx.delete(f"{base}/admin/tokens/{ds_id}", headers=adm).status_code == 200
    assert mcp(base, "/mcp", tok3["access_token"]).status_code == 401

    # RFC 7009 revocation endpoint
    q = authorize(base, cid, srv["admin"])
    tok4 = token(base, cid, grant_type="authorization_code", code=q["code"], redirect_uri=CALLBACK,
                 code_verifier=q["_verifier"]).json()
    assert mcp(base, "/mcp", tok4["access_token"]).status_code == 200
    # (the SDK requires the client_secret field to be present, empty for public clients)
    assert httpx.post(f"{base}/revoke", data={"client_id": cid, "client_secret": "",
                                              "token": tok4["refresh_token"]}).status_code == 200
    assert mcp(base, "/mcp", tok4["access_token"]).status_code == 401


def test_admin_resource_needs_admin_token(srv):
    base = srv["base"]
    cid = register(base).json()["client_id"]
    refused = authorize(base, cid, srv["user"], resource_path="/mcp-admin")
    assert refused["_status"] == 400 and "administrator" in refused["_body"]
    q = authorize(base, cid, srv["admin"], resource_path="/mcp-admin")
    t = token(base, cid, grant_type="authorization_code", code=q["code"], redirect_uri=CALLBACK,
              code_verifier=q["_verifier"], resource=f"{base}/mcp-admin").json()
    r = mcp(base, "/mcp-admin", t["access_token"])
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 11
    assert mcp(base, "/mcp", t["access_token"]).status_code == 401      # bound to /mcp-admin


def test_consent_csrf(srv):
    base = srv["base"]
    cid = register(base).json()["client_id"]
    _, challenge = pkce()
    r = httpx.get(f"{base}/authorize", follow_redirects=False, params={
        "response_type": "code", "client_id": cid, "redirect_uri": CALLBACK, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": "s", "resource": f"{base}/mcp"})
    page = httpx.get(r.headers["location"])
    rid = re.search(r'name="request" value="([^"]+)"', page.text).group(1)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    # no cookie (cross-site form post) -> refused
    r = httpx.post(f"{base}/oauth/consent", data={"request": rid, "csrf": csrf, "token": srv["user"],
                                                   "action": "approve"}, follow_redirects=False)
    assert r.status_code == 403
    # ds_ tokens keep working on /mcp exactly as before
    assert mcp(base, "/mcp", srv["user"]).status_code == 200
