"""FastAPI application: admin + job endpoints, bearer-token auth."""
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.routing import Route

from . import __version__
from .auth import lookup_user
from .config import Settings, load_settings
from .billing import grant_missing_initial
from .models import Token, User, make_session_factory
from .runner import JobRunner
from .schemas import (CreditChange, JobOut, LedgerOut, TokenCreate, TokenIssued, TokenOut,
                      UserCreate, UserCreated, UserOut, UserUpdate, ledger_out)
from .services import ServiceError, Services

bearer = HTTPBearer(auto_error=False)


# ---------- app factory ----------
def create_app(settings: Settings | None = None, runner: JobRunner | None = None,
               recover: bool = True, enable_mcp: bool = True) -> FastAPI:
    settings = settings or load_settings()
    Session_ = make_session_factory(settings.db_url)
    runner = runner or JobRunner(settings, Session_)
    svc = Services(settings, Session_, runner)
    with Session_() as db:
        grant_missing_initial(db, settings.default_credit_hours)

    mcp = None
    if enable_mcp:
        from .mcp_server import build_mcp
        mcp, mcp_asgi = build_mcp(svc)

    @asynccontextmanager
    async def lifespan(_app):
        if recover:
            runner.recover()
        if mcp is not None:
            async with mcp.session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="DockerServer", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.Session, app.state.runner = settings, Session_, runner
    app.state.services = svc
    if mcp is not None:
        # Exact-path route (no /mcp -> /mcp/ redirect); the MCP app handles its own auth.
        app.router.routes.append(Route("/mcp", endpoint=mcp_asgi))

    @app.exception_handler(ServiceError)
    async def _service_error(_req, exc: ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    def get_db():
        with Session_() as db:
            yield db

    DB = Annotated[Session, Depends(get_db)]

    def current_user(db: DB, cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
        if cred is None or cred.scheme.lower() != "bearer":
            raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        user = lookup_user(db, cred.credentials)
        if user is None:
            raise HTTPException(401, "invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
        return user

    CurrentUser = Annotated[User, Depends(current_user)]

    def admin_user(user: CurrentUser) -> User:
        svc.require_admin(user)
        return user

    Admin = Annotated[User, Depends(admin_user)]

    # ----- web pages (static; they only call this REST API) -----
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    page_headers = {
        "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                   "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-cache",
    }

    @app.get("/admin-ui", include_in_schema=False)
    def admin_ui():
        return FileResponse(static_dir / "admin.html", headers=page_headers)

    @app.get("/ui", include_in_schema=False)
    def user_ui():
        return FileResponse(static_dir / "user.html", headers=page_headers)

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui")

    # ----- misc -----
    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/me", response_model=UserOut)
    def me(user: CurrentUser):
        return user

    @app.get("/me/usage")
    def my_usage(db: DB, user: CurrentUser, start: datetime | None = None,
                 end: datetime | None = None):
        """My jobs (with cost and usage stats) and per-day totals, by job start day (UTC)."""
        return svc.my_usage(db, user, start, end)

    # ----- admin -----
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
        file: Annotated[UploadFile | None, File()] = None,
    ):
        content = filename = None
        if file is not None and file.filename:
            content = await file.read(settings.max_upload_bytes + 1)
            filename = file.filename
        return svc.submit_job(db, user, command=command, image=image, cpus=cpus, mem_mb=mem_mb,
                              timeout_seconds=timeout_seconds, network=network, git_url=git_url,
                              filename=filename, content=content)

    @app.get("/jobs", response_model=list[JobOut])
    def list_jobs(db: DB, user: CurrentUser, all: bool = False, status: str | None = None,
                  limit: int = 100):
        return svc.list_jobs(db, user, all, status, limit)

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
