"""OAuth 2.1 for MCP custom connectors (claude.ai, Claude Desktop), per the MCP auth spec.

Endpoints (most from the MCP SDK's auth server, driven by DSOAuthProvider below):
  /.well-known/oauth-authorization-server         RFC 8414 metadata (issuer = PUBLIC_URL)
  /.well-known/oauth-protected-resource/mcp       RFC 9728 metadata for /mcp
  /.well-known/oauth-protected-resource/mcp-admin RFC 9728 metadata for /mcp-admin
  /register  dynamic client registration (redirect URIs restricted, see below)
  /authorize authorization code + PKCE S256 -> our consent page
  /oauth/consent  the user pastes their ds_ access token to approve (CSRF-protected,
                  rate-limited like other token checks)
  /token     authorization_code and refresh_token grants (refresh tokens rotate)
  /revoke    token revocation

Tokens: access tokens "dsa_..." (1 h), refresh tokens "dsr_..." (30 days), stored as SHA-256
hashes and bound to: the user, the ds_ token used to approve, the client and the resource
(/mcp or /mcp-admin). They stop working when the user is disabled/suspended or that ds_ token
is revoked. Approving /mcp-admin requires an admin's token. Existing "Bearer ds_..." access is
unchanged.
"""
import hashlib
import html
import secrets
import time
from urllib.parse import urlparse

from pydantic import AnyHttpUrl
from sqlalchemy import delete, select, update
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      RefreshToken, RegistrationError, TokenError,
                                      construct_redirect_uri)
from mcp.server.auth.routes import create_auth_routes, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .auth import lookup_user
from .models import OAuthClient, OAuthCode, OAuthPending, OAuthToken as TokenRow, Token, User, utcnow

SCOPE = "mcp"
CODE_TTL = 300
PENDING_TTL = 600
MCP_PATHS = ("/mcp", "/mcp-admin")
OAUTH_PATH_PREFIXES = ("/.well-known/oauth-", "/authorize", "/token", "/register", "/revoke",
                       "/oauth/")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _now() -> float:
    return time.time()


class DSOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider."""

    def __init__(self, svc):
        self.svc = svc
        self.settings = svc.settings
        self.Session = svc.Session

    # ---------- helpers ----------
    def resource_path(self, resource: str | None) -> str | None:
        """Map a resource indicator (URL) to /mcp or /mcp-admin; None if not ours."""
        if not resource:
            return "/mcp"
        u = urlparse(resource)
        base = urlparse(self.settings.public_url)
        if u.scheme != base.scheme or u.netloc != base.netloc:
            return None
        path = u.path.rstrip("/") or "/mcp"
        return path if path in MCP_PATHS else None

    def redirect_allowed(self, uri: str) -> bool:
        u = urlparse(uri)
        host = (u.hostname or "").lower()
        if u.scheme == "http" and host in ("localhost", "127.0.0.1"):
            return True
        if u.scheme != "https":
            return False
        return any(host == h or host.endswith("." + h) for h in self.settings.oauth_redirect_hosts)

    def _purge(self, db):
        now = _now()
        db.execute(delete(OAuthPending).where(OAuthPending.expires_at < now))
        db.execute(delete(OAuthCode).where(OAuthCode.expires_at < now - 3600))
        db.execute(delete(TokenRow).where(TokenRow.expires_at < now - 86400))

    # ---------- clients ----------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.Session() as db:
            row = db.get(OAuthClient, client_id)
            return OAuthClientInformationFull.model_validate_json(row.info_json) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            if not self.redirect_allowed(str(uri)):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description=f"redirect_uri not allowed: {uri} (allowed: https on "
                                      f"{', '.join(self.settings.oauth_redirect_hosts)}, or localhost)")
        with self.Session() as db:
            db.add(OAuthClient(client_id=client_info.client_id, info_json=client_info.model_dump_json()))
            db.commit()

    # ---------- authorization ----------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        from mcp.server.auth.provider import AuthorizeError
        if self.resource_path(params.resource) is None:
            raise AuthorizeError(error="invalid_target",
                                 error_description="unknown resource; use this server's /mcp or /mcp-admin URL")
        request_id = secrets.token_urlsafe(32)
        with self.Session() as db:
            self._purge(db)
            db.add(OAuthPending(id=_h(request_id), client_id=client.client_id,
                                params_json=params.model_dump_json(), csrf=secrets.token_urlsafe(32),
                                expires_at=_now() + PENDING_TTL))
            db.commit()
        return f"{self.settings.public_url}/oauth/consent?request={request_id}"

    def pending(self, db, request_id: str) -> OAuthPending | None:
        row = db.get(OAuthPending, _h(request_id or ""))
        return row if row is not None and row.expires_at >= _now() else None

    def issue_code(self, db, pending: OAuthPending, user: User, ds_token_id: int) -> tuple[str, AuthorizationParams]:
        params = AuthorizationParams.model_validate_json(pending.params_json)
        code = secrets.token_urlsafe(32)
        db.add(OAuthCode(code_hash=_h(code), client_id=pending.client_id, user_id=user.id,
                         ds_token_id=ds_token_id, params_json=pending.params_json,
                         expires_at=_now() + CODE_TTL))
        db.delete(pending)
        db.commit()
        return code, params

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        with self.Session() as db:
            row = db.get(OAuthCode, _h(authorization_code))
            if row is None or row.used or row.client_id != client.client_id or row.expires_at < _now():
                return None
            p = AuthorizationParams.model_validate_json(row.params_json)
            return AuthorizationCode(code=authorization_code, scopes=p.scopes or [SCOPE],
                                     expires_at=row.expires_at, client_id=row.client_id,
                                     code_challenge=p.code_challenge, redirect_uri=p.redirect_uri,
                                     redirect_uri_provided_explicitly=p.redirect_uri_provided_explicitly,
                                     resource=p.resource, subject=str(row.user_id))

    def _issue_pair(self, db, client_id, user_id, ds_token_id, resource, scopes, family=None) -> OAuthToken:
        s = self.settings
        family = family or secrets.token_hex(16)
        access, refresh = "dsa_" + secrets.token_urlsafe(32), "dsr_" + secrets.token_urlsafe(40)
        common = dict(family=family, client_id=client_id, user_id=user_id, ds_token_id=ds_token_id,
                      resource=resource, scopes=" ".join(scopes))
        db.add(TokenRow(token_hash=_h(access), kind="access", expires_at=_now() + s.oauth_access_ttl, **common))
        db.add(TokenRow(token_hash=_h(refresh), kind="refresh", expires_at=_now() + s.oauth_refresh_ttl, **common))
        db.commit()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=s.oauth_access_ttl,
                          refresh_token=refresh, scope=" ".join(scopes))

    def _owner_ok(self, db, user_id: int, ds_token_id: int, resource: str) -> bool:
        user, tok = db.get(User, user_id), db.get(Token, ds_token_id)
        if user is None or not user.active or tok is None or tok.revoked_at is not None:
            return False
        if self.resource_path(resource) == "/mcp-admin" and not user.is_admin:
            return False
        return True

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        with self.Session() as db:
            claimed = db.execute(update(OAuthCode).where(
                OAuthCode.code_hash == _h(authorization_code.code), OAuthCode.used.is_(False))
                .values(used=True)).rowcount
            db.commit()
            row = db.get(OAuthCode, _h(authorization_code.code))
            if not claimed or row is None:
                raise TokenError(error="invalid_grant", error_description="authorization code already used")
            p = AuthorizationParams.model_validate_json(row.params_json)
            resource = p.resource or f"{self.settings.public_url}/mcp"
            if not self._owner_ok(db, row.user_id, row.ds_token_id, resource):
                raise TokenError(error="invalid_grant", error_description="account or token no longer valid")
            return self._issue_pair(db, client.client_id, row.user_id, row.ds_token_id, resource,
                                    authorization_code.scopes or [SCOPE])

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        with self.Session() as db:
            row = db.scalar(select(TokenRow).where(TokenRow.token_hash == _h(refresh_token),
                                                   TokenRow.kind == "refresh"))
            if (row is None or row.revoked_at is not None or row.client_id != client.client_id
                    or row.expires_at < _now()):
                return None
            return RefreshToken(token=refresh_token, client_id=row.client_id,
                                scopes=row.scopes.split() or [SCOPE], expires_at=int(row.expires_at),
                                resource=row.resource, subject=str(row.user_id))

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with self.Session() as db:
            row = db.scalar(select(TokenRow).where(TokenRow.token_hash == _h(refresh_token.token)))
            if row is None or row.revoked_at is not None:
                raise TokenError(error="invalid_grant", error_description="refresh token revoked")
            if not self._owner_ok(db, row.user_id, row.ds_token_id, row.resource):
                self._revoke_family(db, row.family)
                raise TokenError(error="invalid_grant", error_description="account or token no longer valid")
            # rotate: retire the whole old pair, issue a new pair in the same family
            db.execute(update(TokenRow).where(TokenRow.family == row.family, TokenRow.revoked_at.is_(None))
                       .values(revoked_at=utcnow()))
            return self._issue_pair(db, client.client_id, row.user_id, row.ds_token_id, row.resource,
                                    scopes or row.scopes.split() or [SCOPE], family=row.family)

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self.Session() as db:
            row = self.valid_access_row(db, token)
            if row is None:
                return None
            return AccessToken(token=token, client_id=row.client_id, scopes=row.scopes.split(),
                               expires_at=int(row.expires_at), resource=row.resource,
                               subject=str(row.user_id))

    async def revoke_token(self, token) -> None:
        with self.Session() as db:
            row = db.scalar(select(TokenRow).where(TokenRow.token_hash == _h(token.token)))
            if row is not None:
                self._revoke_family(db, row.family)

    def _revoke_family(self, db, family: str):
        db.execute(update(TokenRow).where(TokenRow.family == family, TokenRow.revoked_at.is_(None))
                   .values(revoked_at=utcnow()))
        db.commit()

    # ---------- used by the MCP auth wrapper ----------
    def valid_access_row(self, db, token: str) -> TokenRow | None:
        row = db.scalar(select(TokenRow).where(TokenRow.token_hash == _h(token), TokenRow.kind == "access"))
        if row is None or row.revoked_at is not None or row.expires_at < _now():
            return None
        if not self._owner_ok(db, row.user_id, row.ds_token_id, row.resource):
            return None
        return row

    def user_for_access_token(self, db, token: str, path: str) -> User | None:
        row = self.valid_access_row(db, token)
        if row is None or self.resource_path(row.resource) != path:
            return None
        return db.get(User, row.user_id)


def revoke_for_user(db, user_id: int):
    db.execute(update(TokenRow).where(TokenRow.user_id == user_id, TokenRow.revoked_at.is_(None))
               .values(revoked_at=utcnow()))


def revoke_for_ds_token(db, token_id: int):
    db.execute(update(TokenRow).where(TokenRow.ds_token_id == token_id, TokenRow.revoked_at.is_(None))
               .values(revoked_at=utcnow()))


# ------------------------------- consent page -------------------------------
CSRF_COOKIE = "ds_oauth_csrf"


def _consent_html(client_name: str, redirect_host: str, admin: bool, request_id: str, csrf: str,
                  error: str | None = None) -> str:
    e = html.escape
    what = ("<b>administrator</b> access (users, credits, usage) via <code>/mcp-admin</code>"
            if admin else "running jobs as you via <code>/mcp</code>")
    err = f'<p class="err">{e(error)}</p>' if error else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Authorize · fast-compute</title>
<style>
:root{{--bg:#f6f7f9;--panel:#fff;--text:#1d2330;--muted:#667085;--border:#dfe3ea;--accent:#2f6fde;--bad:#c0392b}}
@media (prefers-color-scheme: dark){{:root{{--bg:#12151b;--panel:#1a1f28;--text:#e3e7ee;--muted:#98a2b3;--border:#2c3442;--accent:#5b8ff0;--bad:#f07167}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
.box{{max-width:480px;margin:8vh auto;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:24px}}
h1{{font-size:18px;margin:0 0 12px}} .muted{{color:var(--muted);font-size:13px}} .err{{color:var(--bad)}}
input{{width:100%;box-sizing:border-box;font:inherit;padding:8px;margin:8px 0 14px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text)}}
.row{{display:flex;gap:8px}} button{{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid var(--border);background:var(--panel);color:var(--text);cursor:pointer}}
button.primary{{background:var(--accent);border-color:var(--accent);color:#fff}}
</style></head><body><div class="box">
<h1>Connect {e(client_name)} to fast-compute</h1>
<p><b>{e(client_name)}</b> (returning to <b>{e(redirect_host)}</b>) asks for {what}.</p>
{err}
<form method="post" action="/oauth/consent">
<input type="hidden" name="request" value="{e(request_id)}"><input type="hidden" name="csrf" value="{e(csrf)}">
<label class="muted" for="t">Paste your access token (ds_…) to approve</label>
<input id="t" name="token" type="password" autocomplete="off" required autofocus>
<div class="row"><button class="primary" type="submit" name="action" value="approve">Approve</button>
<button type="submit" name="action" value="deny" formnovalidate>Deny</button></div>
</form>
<p class="muted">Only approve if you started this from {e(client_name)} yourself. The token is checked once
and never shared with the app; the app gets its own token that you can revoke by revoking yours.</p>
</div></body></html>"""


def build_oauth_routes(svc, provider: DSOAuthProvider) -> list[Route]:
    s = svc.settings
    # Built via AuthSettings so the issuer keeps its exact form (no trailing "/"): RFC 8414
    # requires it to equal the URL the metadata is fetched from.
    issuer = AuthSettings(issuer_url=s.public_url, resource_server_url=None).issuer_url
    routes = create_auth_routes(
        provider, issuer_url=issuer,
        client_registration_options=ClientRegistrationOptions(enabled=True, valid_scopes=[SCOPE],
                                                              default_scopes=[SCOPE]),
        revocation_options=RevocationOptions(enabled=True))
    for path in MCP_PATHS:
        routes += create_protected_resource_routes(
            resource_url=AnyHttpUrl(f"{s.public_url}{path}"), authorization_servers=[issuer],
            scopes_supported=[SCOPE], resource_name=f"fast-compute {path}")
    # some clients probe the root metadata document: describe the user endpoint there
    root_meta = {"resource": f"{s.public_url}/mcp", "authorization_servers": [s.public_url],
                 "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"]}
    routes.append(Route("/.well-known/oauth-protected-resource",
                        endpoint=lambda r: JSONResponse(root_meta), methods=["GET"]))

    def page(body: str, status: int, csrf_cookie: str | None, form_action_origin: str) -> Response:
        resp = HTMLResponse(body, status_code=status)
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
            f"form-action 'self' {form_action_origin}")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        if csrf_cookie:
            resp.set_cookie(CSRF_COOKIE, csrf_cookie, httponly=True, samesite="strict",
                            secure=s.public_url.startswith("https://"), path="/oauth/consent",
                            max_age=PENDING_TTL)
        return resp

    def context(db, pending):
        params = AuthorizationParams.model_validate_json(pending.params_json)
        client = db.get(OAuthClient, pending.client_id)
        info = OAuthClientInformationFull.model_validate_json(client.info_json) if client else None
        name = (info.client_name if info and info.client_name else "An application")[:80]
        ru = urlparse(str(params.redirect_uri))
        origin = f"{ru.scheme}://{ru.netloc}"
        admin = provider.resource_path(params.resource) == "/mcp-admin"
        return params, name, ru.netloc, origin, admin

    async def consent_get(request: Request):
        rid = request.query_params.get("request", "")
        with svc.Session() as db:
            pending = provider.pending(db, rid)
            if pending is None:
                return HTMLResponse("This authorization request has expired. Start again from the app.",
                                    status_code=400)
            _, name, host, origin, admin = context(db, pending)
            # double-submit CSRF: the same random value in a SameSite=Strict cookie and the form,
            # and it must match the value stored with this pending request
            return page(_consent_html(name, host, admin, rid, pending.csrf), 200, pending.csrf, origin)

    async def consent_post(request: Request):
        form = await request.form()
        rid, csrf = str(form.get("request", "")), str(form.get("csrf", ""))
        client_ip = request.client.host if request.client else "?"
        with svc.Session() as db:
            pending = provider.pending(db, rid)
            if pending is None:
                return HTMLResponse("This authorization request has expired. Start again from the app.",
                                    status_code=400)
            if not (csrf and secrets.compare_digest(csrf, pending.csrf)
                    and secrets.compare_digest(request.cookies.get(CSRF_COOKIE, ""), pending.csrf)):
                return HTMLResponse("Invalid form submission (CSRF check failed).", status_code=403)
            params, name, host, origin, admin = context(db, pending)
            if form.get("action") == "deny":
                db.delete(pending)
                db.commit()
                return RedirectResponse(construct_redirect_uri(
                    str(params.redirect_uri), error="access_denied", state=params.state), status_code=302)
            raw = str(form.get("token", "")).strip()
            if svc.auth_limiter.blocked(client_ip):
                return page(_consent_html(name, host, admin, rid, pending.csrf,
                                          "Too many failed attempts from your address; try again later."),
                            429, None, origin)
            user = lookup_user(db, raw) if raw.startswith("ds_") else None
            tok = db.scalar(select(Token).where(Token.token_hash == _h(raw))) if user else None
            error = None
            if user is None or tok is None:
                svc.auth_limiter.record_failure(client_ip)
                error = "That token is not valid (wrong, revoked, or the account is disabled)."
            elif admin and not user.is_admin:
                error = "This connector is for /mcp-admin and needs an administrator's token."
            if error:
                return page(_consent_html(name, host, admin, rid, pending.csrf, error), 400, None, origin)
            code, params = provider.issue_code(db, pending, user, tok.id)
            resp = RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code,
                                                           state=params.state), status_code=302)
            resp.delete_cookie(CSRF_COOKIE, path="/oauth/consent")
            return resp

    routes.append(Route("/oauth/consent", consent_get, methods=["GET"]))
    routes.append(Route("/oauth/consent", consent_post, methods=["POST"]))
    return routes


class HostCheck:
    """ASGI middleware: OAuth endpoints only answer for DOCKERSERVER_ALLOWED_HOSTS."""

    def __init__(self, app, allowed: list[str]):
        self.app, self.allowed = app, allowed

    def _ok(self, host: str) -> bool:
        for a in self.allowed:
            if host == a or (a.endswith(":*") and host.startswith(a[:-2] + ":")):
                return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(OAUTH_PATH_PREFIXES):
            host = dict(scope.get("headers", [])).get(b"host", b"").decode()
            if not self._ok(host):
                resp = JSONResponse({"detail": "invalid host"}, status_code=421)
                await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


def resource_metadata_url(public_url: str, path: str) -> str:
    return f"{public_url}/.well-known/oauth-protected-resource{path}"

