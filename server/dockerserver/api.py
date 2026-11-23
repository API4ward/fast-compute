"""FastAPI application: admin + job endpoints, bearer-token auth."""
import hashlib
import json
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import anyio
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.routing import Route

from . import __version__
from .config import Settings, load_settings
from .billing import grant_missing_initial
from .models import Site, Token, User, make_session_factory
from .runner import JobRunner
from .schemas import (AdminJobOut, CreditChange, EnvValue, JobOut, LedgerOut, SiteFile, TokenCreate, TokenIssued, TokenOut,
                      UserCreate, UserCreated, UserOut, UserUpdate, ledger_out)
from .services import ServiceError, Services
from .sites import SiteError, SiteManager

bearer = HTTPBearer(auto_error=False)


class _NoCacheStatic(StaticFiles):
    """Static assets must always be revalidated (pages also reference them with ?v=<hash>)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


class _SitesHost:
    """ASGI middleware keeping user web sites on their own origin (DOCKERSERVER_SITES_URL):
    that host serves only /sites/..., and /sites/... on any other host redirects there."""

    def __init__(self, app, sites_host: str | None, sites_base: str):
        self.app, self.sites_host, self.sites_base = app, sites_host, sites_base

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.sites_host:
            host = dict(scope.get("headers", [])).get(b"host", b"").decode().lower()
            on_sites = scope["path"].startswith("/sites/")
            if host == self.sites_host and not on_sites:
                await PlainTextResponse("not found", status_code=404)(scope, receive, send)
                return
            if host != self.sites_host and on_sites:
                q = scope.get("query_string", b"").decode()
                url = self.sites_base + scope.get("raw_path", scope["path"].encode()).decode() + (f"?{q}" if q else "")
                await RedirectResponse(url, status_code=307)(scope, receive, send)
                return
        await self.app(scope, receive, send)


_SITE_REQ_HEADERS = ("if-none-match", "if-modified-since", "range", "if-range")
_SITE_RESP_HEADERS = ("content-type", "etag", "last-modified", "cache-control", "accept-ranges",
                      "content-range")


# ---------- app factory ----------
def create_app(settings: Settings | None = None, runner: JobRunner | None = None,
               recover: bool = True, enable_mcp: bool = True, pool=None,
               site_runtime=None) -> FastAPI:
    settings = settings or load_settings()
    Session_ = make_session_factory(settings.db_url)
    runner = runner or JobRunner(settings, Session_, pool=pool)
    svc = Services(settings, Session_, runner)
    svc.sites = SiteManager(settings, Session_, runtime=site_runtime)
    site_http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=3.0), trust_env=False)
    with Session_() as db:
        grant_missing_initial(db, settings.default_credit_hours)

    mcp_routes = {}
    if enable_mcp:
        from .mcp_server import build_admin_mcp, build_user_mcp
        mcp_routes["/mcp"] = build_user_mcp(svc, "/mcp")
        mcp_routes["/mcp-admin"] = build_admin_mcp(svc, "/mcp-admin")

    @asynccontextmanager
    async def lifespan(_app):
        if recover:
            runner.recover()
            await anyio.to_thread.run_sync(svc.sites.recover)
        svc.sites.start()
        try:
            async with AsyncExitStack() as stack:
                for server, _ in mcp_routes.values():
                    await stack.enter_async_context(server.session_manager.run())
                yield
        finally:
            svc.sites.shutdown()
            await site_http.aclose()

    app = FastAPI(title="DockerServer", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.Session, app.state.runner = settings, Session_, runner
    app.state.services = svc
    if settings.oauth_enabled:
        from .oauth import DSOAuthProvider, HostCheck, build_oauth_routes
        svc.oauth = DSOAuthProvider(svc)
        app.router.routes.extend(build_oauth_routes(svc, svc.oauth))
        app.add_middleware(HostCheck, allowed=settings.allowed_hosts)

    if settings.sites_host:
        app.add_middleware(_SitesHost, sites_host=settings.sites_host, sites_base=settings.sites_base)

    for path, (_, asgi) in mcp_routes.items():
        # Exact-path routes (no /mcp -> /mcp/ redirect); the MCP apps handle their own auth.
        app.router.routes.append(Route(path, endpoint=asgi))

    @app.exception_handler(ServiceError)
    async def _service_error(_req, exc: ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    def get_db():
        with Session_() as db:
            yield db

    DB = Annotated[Session, Depends(get_db)]

    def current_user(request: Request, db: DB,
                     cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
        token = cred.credentials if cred is not None and cred.scheme.lower() == "bearer" else None
        try:
            return svc.authenticate(db, token, request.client.host if request.client else "?")
        except ServiceError as e:
            headers = {"WWW-Authenticate": "Bearer"} if e.status == 401 else {"Retry-After": "60"}
            raise HTTPException(e.status, e.message, headers=headers)

    CurrentUser = Annotated[User, Depends(current_user)]

    def admin_user(user: CurrentUser) -> User:
        svc.require_admin(user)
        return user

    Admin = Annotated[User, Depends(admin_user)]

    # ----- web pages (static; they only call this REST API) -----
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", _NoCacheStatic(directory=static_dir), name="static")

    def render_page(name: str) -> HTMLResponse:
        # Asset URLs carry a content hash so browsers / Cloudflare never serve a stale copy.
        html = (static_dir / name).read_text()
        for asset in ("common.js", "common.css", "welcome.js"):
            digest = hashlib.sha256((static_dir / asset).read_bytes()).hexdigest()[:12]
            html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={digest}\"")
        return HTMLResponse(html, headers=page_headers)
    page_headers = {
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                   "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-cache",
    }

    @app.get("/admin-ui", include_in_schema=False)
    def admin_ui():
        return render_page("admin.html")

    @app.get("/ui", include_in_schema=False)
    def user_ui():
        return render_page("user.html")

    @app.get("/welcome", include_in_schema=False)
    def welcome_ui():
        return render_page("welcome.html")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui")

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin-ui/", include_in_schema=False)
    def admin_alias():
        return RedirectResponse("/admin-ui")

    @app.get("/ui/", include_in_schema=False)
    def ui_alias():
        return RedirectResponse("/ui")

    # ----- misc -----
    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/info")
    def info():
        # Public: what the welcome page needs to describe the service. No user data.
        return {"version": __version__, "allowed_images": settings.allowed_images,
                "oauth": settings.oauth_enabled}

    @app.get("/me", response_model=UserOut)
    def me(user: CurrentUser):
        return user

    @app.get("/me/usage")
    def my_usage(db: DB, user: CurrentUser, start: datetime | None = None,
                 end: datetime | None = None):
        """My jobs (with cost and usage stats) and per-day totals, by job start day (UTC)."""
        return svc.my_usage(db, user, start, end)

    # ----- my environment variables (owner only; admins have no access) -----
    @app.get("/me/env")
    def my_env(db: DB, user: CurrentUser, reveal: bool = False):
        """My variables; values masked unless reveal=true."""
        return svc.list_env(db, user, reveal)

    @app.put("/me/env/{name}", status_code=204)
    def put_env(name: str, body: EnvValue, db: DB, user: CurrentUser):
        svc.set_env(db, user, name, body.value)

    @app.delete("/me/env/{name}", status_code=204)
    def del_env(name: str, db: DB, user: CurrentUser):
        svc.delete_env(db, user, name)

    # ----- my static web sites -----
    @app.get("/me/sites")
    def my_sites(db: DB, user: CurrentUser):
        return svc.list_sites(db, user)

    @app.get("/me/sites/{site}/files")
    def my_site_files(site: str, db: DB, user: CurrentUser):
        return svc.list_site_files(db, user, site)

    @app.put("/me/sites/{site}/files/{path:path}")
    def put_site_file(site: str, path: str, body: SiteFile, db: DB, user: CurrentUser):
        return svc.put_site_file(db, user, site, path, body.content, body.encoding)

    @app.delete("/me/sites/{site}/files/{path:path}")
    def delete_site_file(site: str, path: str, db: DB, user: CurrentUser):
        return svc.delete_site_file(db, user, site, path)

    @app.post("/me/sites/{site}/publish")
    def publish_site(site: str, db: DB, user: CurrentUser):
        return svc.publish_site(db, user, site, True)

    @app.post("/me/sites/{site}/unpublish")
    def unpublish_site(site: str, db: DB, user: CurrentUser):
        return svc.publish_site(db, user, site, False)

    @app.delete("/me/sites/{site}")
    def delete_site(site: str, db: DB, user: CurrentUser):
        return svc.delete_site(db, user, site)

    # ----- public: serve published sites (wakes the site's container if it is asleep) -----
    def _site_404(msg="site not found"):
        return PlainTextResponse(msg, status_code=404, headers={"Cache-Control": "no-store"})

    @app.api_route("/sites/{user_name}/{site_name}", methods=["GET", "HEAD"], include_in_schema=False)
    def site_slash(user_name: str, site_name: str, request: Request):
        # relative links in index.html only work under a trailing slash
        q = request.url.query
        return RedirectResponse(request.url.path + "/" + (f"?{q}" if q else ""), status_code=308)

    @app.api_route("/sites/{user_name}/{site_name}/{rest:path}", methods=["GET", "HEAD"],
                   include_in_schema=False)
    async def serve_site(user_name: str, site_name: str, rest: str, request: Request):
        with Session_() as db:
            row = db.execute(select(Site.id, User.active, User.deleted_at, Site.published)
                             .join(User, User.id == Site.user_id)
                             .where(User.name == user_name, Site.name == site_name)).first()
        if row is None or not row.published or not row.active or row.deleted_at is not None:
            return _site_404()
        site_id = row.id
        raw = request.scope.get("raw_path", b"").decode("latin-1")
        raw_prefix = f"/sites/{user_name}/{site_name}/"
        upstream_path = raw.split("/", 4)[4] if raw.count("/") >= 4 else rest
        fwd = {k: v for k, v in request.headers.items() if k.lower() in _SITE_REQ_HEADERS}
        q = request.url.query
        resp = None
        for attempt in (1, 2):
            try:
                addr = await anyio.to_thread.run_sync(svc.sites.ensure_awake, site_id)
            except SiteError as e:
                return PlainTextResponse(e.message, status_code=e.status)
            try:
                resp = await site_http.request(
                    request.method, f"http://{addr}/{upstream_path}" + (f"?{q}" if q else ""),
                    headers=fwd)
                break
            except httpx.TransportError:
                svc.sites.forget(site_id)   # container gone (e.g. stopped by hand): restart it once
                if attempt == 2:
                    return PlainTextResponse("site temporarily unavailable", status_code=502)
        headers = {k: v for k, v in resp.headers.items() if k.lower() in _SITE_RESP_HEADERS}
        loc = resp.headers.get("location")
        if loc:
            headers["location"] = (raw_prefix + loc.lstrip("/")) if loc.startswith("/") else loc
        body = resp.content
        svc.sites.record(site_id, len(body))
        if request.method == "HEAD":
            if "content-length" in resp.headers:
                headers["content-length"] = resp.headers["content-length"]
            return Response(status_code=resp.status_code, headers=headers)
        return Response(body, status_code=resp.status_code, headers=headers)

    # ----- admin -----
    @app.get("/admin/sites")
    def admin_sites(db: DB, admin: Admin, user_id: int | None = None):
        """Every user's sites: container (awake/asleep) and usage. No file contents."""
        return svc.admin_sites(db, admin, user_id)

    @app.delete("/admin/users/{user_id}")
    def delete_user(user_id: int, db: DB, admin: Admin):
        """Delete a user: kill their jobs, remove their sites (containers + files), variables
        and tokens. Job and ledger history is kept."""
        return svc.delete_user(db, admin, user_id)

    @app.post("/admin/users", response_model=UserCreated, status_code=201)
    def create_user(body: UserCreate, db: DB, admin: Admin):
        data = body.model_dump()
        u, raw = svc.create_user(db, admin, data.pop("name"), data.pop("role"), **data)
        return {**UserOut.model_validate(u).model_dump(), "token": raw}

    @app.get("/admin/users", response_model=list[UserOut])
    def list_users(db: DB, admin: Admin):
        return svc.list_users(db, admin)

    @app.get("/admin/users/{user_id}", response_model=UserOut)
    def get_user(user_id: int, db: DB, _: Admin):
        return svc.get_user(db, user_id)

    @app.patch("/admin/users/{user_id}", response_model=UserOut)
    def update_user(user_id: int, body: UserUpdate, db: DB, admin: Admin):
        return svc.update_user(db, admin, user_id, body.model_dump(exclude_unset=True, exclude_none=True))

    @app.post("/admin/users/{user_id}/disable", response_model=UserOut)
    def disable_user(user_id: int, db: DB, admin: Admin):
        return svc.update_user(db, admin, user_id, {"active": False})

    @app.post("/admin/users/{user_id}/enable", response_model=UserOut)
    def enable_user(user_id: int, db: DB, admin: Admin):
        return svc.update_user(db, admin, user_id, {"active": True})

    @app.post("/admin/users/{user_id}/suspend")
    def suspend_user(user_id: int, db: DB, admin: Admin):
        """Disable the account and kill all of the user's running jobs (status killed_by_admin)."""
        return svc.suspend_user(db, admin, user_id)

    @app.get("/admin/jobs", response_model=list[AdminJobOut])
    def admin_jobs(db: DB, admin: Admin, user_id: int | None = None, status: str | None = None,
                   limit: int = 100):
        """Read-only job metadata across users (no command, code or logs)."""
        return svc.list_user_jobs(db, admin, user_id, status, limit)

    @app.get("/admin/users/{user_id}/tokens", response_model=list[TokenOut])
    def list_tokens(user_id: int, db: DB, _: Admin):
        svc.get_user(db, user_id)
        return db.scalars(select(Token).where(Token.user_id == user_id).order_by(Token.id)).all()

    @app.post("/admin/users/{user_id}/tokens", response_model=TokenIssued, status_code=201)
    def create_token(user_id: int, body: TokenCreate, db: DB, admin: Admin):
        tok, raw = svc.issue_token(db, admin, user_id, body.name)
        return {**TokenOut.model_validate(tok).model_dump(), "token": raw}

    @app.delete("/admin/tokens/{token_id}", response_model=TokenOut)
    def revoke_token(token_id: int, db: DB, admin: Admin):
        return svc.revoke_token(db, admin, token_id)

    @app.post("/admin/users/{user_id}/credits", response_model=LedgerOut, status_code=201)
    def change_credits(user_id: int, body: CreditChange, db: DB, admin: Admin):
        return ledger_out(svc.change_credits(db, admin, user_id, body.hours, body.reason, body.note))

    @app.get("/admin/users/{user_id}/ledger", response_model=list[LedgerOut])
    def user_ledger(user_id: int, db: DB, admin: Admin, limit: int = 200):
        return svc.ledger(db, admin, user_id, limit)

    @app.get("/admin/usage")
    def admin_usage(db: DB, admin: Admin, start: datetime | None = None, end: datetime | None = None):
        """Per-user totals for jobs started in [start, end) (UTC)."""
        return svc.usage_report(db, admin, start, end)

    # ----- jobs -----
    @app.post("/jobs", response_model=JobOut, status_code=201)
    async def submit_job(
        db: DB,
        user: CurrentUser,
        command: Annotated[str, Form(min_length=1, max_length=10000)],
        image: Annotated[str | None, Form()] = None,
        cpus: Annotated[float | None, Form(gt=0)] = None,
        mem_mb: Annotated[int | None, Form(ge=32)] = None,
        timeout_seconds: Annotated[int | None, Form(ge=1)] = None,
        network: Annotated[bool, Form()] = False,
        git_url: Annotated[str | None, Form(max_length=1024)] = None,
        env: Annotated[str | None, Form(description="JSON object NAME: value; overrides your saved variables for this job")] = None,
        file: Annotated[UploadFile | None, File()] = None,
    ):
        job_env = None
        if env:
            try:
                job_env = json.loads(env)
            except ValueError:
                raise HTTPException(400, "env must be a JSON object")
        content = filename = None
        if file is not None and file.filename:
            content = await file.read(settings.max_upload_bytes + 1)
            filename = file.filename
        return svc.submit_job(db, user, command=command, image=image, cpus=cpus, mem_mb=mem_mb,
                              timeout_seconds=timeout_seconds, network=network, git_url=git_url,
                              filename=filename, content=content, env=job_env)

    @app.get("/jobs", response_model=list[JobOut])
    def list_jobs(db: DB, user: CurrentUser, status: str | None = None, limit: int = 100):
        """My own jobs (for everyone, admins included)."""
        return svc.list_jobs(db, user, status, limit)

    @app.get("/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: int, db: DB, user: CurrentUser):
        return svc.get_job(db, user, job_id)

    @app.get("/jobs/{job_id}/logs", response_class=PlainTextResponse)
    def get_logs(job_id: int, db: DB, user: CurrentUser, tail: int | None = None):
        return svc.logs(db, user, job_id, tail)

    @app.post("/jobs/{job_id}/stop", response_model=JobOut)
    def stop_job(job_id: int, db: DB, user: CurrentUser):
        return svc.stop_job(db, user, job_id)

    return app
