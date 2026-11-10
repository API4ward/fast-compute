"""FastAPI application: admin + job endpoints, bearer-token auth."""
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import __version__
from .auth import issue_token, lookup_user
from .config import Settings, load_settings
from .billing import (UH_PER_HOUR, accrued_uh, add_entry, available_uh, cost_uh, grant_initial,
                      grant_missing_initial, hours, job_run_seconds, to_uh)
from .models import (ACTIVE_STATUSES, TERMINAL_STATUSES, Job, LedgerEntry, Token, User,
                     make_session_factory, utcnow)
from .runner import JobRunner

bearer = HTTPBearer(auto_error=False)


# ---------- schemas ----------
class Limits(BaseModel):
    max_cpus: float | None = Field(None, gt=0, le=64)
    max_mem_mb: int | None = Field(None, ge=32, le=262144)
    max_run_seconds: int | None = Field(None, ge=1, le=7 * 24 * 3600)
    max_concurrent_jobs: int | None = Field(None, ge=1, le=100)
    allow_network: bool | None = None


class UserCreate(Limits):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    role: Literal["admin", "user"] = "user"


class UserUpdate(Limits):
    role: Literal["admin", "user"] | None = None
    active: bool | None = None


class UserOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    role: str
    active: bool
    max_cpus: float
    max_mem_mb: int
    max_run_seconds: int
    max_concurrent_jobs: int
    allow_network: bool
    balance_hours: float
    created_at: datetime


class TokenCreate(BaseModel):
    name: str = Field("default", max_length=64)


class TokenOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    user_id: int
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenIssued(TokenOut):
    token: str


class UserCreated(UserOut):
    token: str


class JobOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    user_id: int
    image: str
    command: str
    git_url: str | None
    cpus: float
    mem_mb: int
    timeout_seconds: int
    network: bool
    status: str
    exit_code: int | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    billed: bool
    cost_hours: float | None
    stat_samples: int
    cpu_avg_pct: float | None
    cpu_peak_pct: float | None
    mem_avg_mb: float | None
    mem_peak_mb: float | None


class CreditChange(BaseModel):
    hours: float = Field(description="credit-hours to add (negative to deduct)")
    reason: Literal["topup", "adjust"] = "topup"
    note: str | None = Field(None, max_length=500)


class LedgerOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    user_id: int
    delta_hours: float
    balance_after_hours: float
    reason: str
    job_id: int | None
    admin_id: int | None
    note: str | None
    created_at: datetime


def ledger_out(e: LedgerEntry) -> dict:
    return {"id": e.id, "user_id": e.user_id, "delta_hours": hours(e.delta_uh),
            "balance_after_hours": hours(e.balance_after_uh), "reason": e.reason,
            "job_id": e.job_id, "admin_id": e.admin_id, "note": e.note, "created_at": e.created_at}


def usage_totals(jobs: list[Job]) -> dict:
    """cpu_hours = cores x hours, mem_gb_hours = GB x hours, cost_hours = credits charged/accrued."""
    t = {"jobs": 0, "run_seconds": 0.0, "cpu_hours": 0.0, "mem_gb_hours": 0.0, "cost_hours": 0.0}
    for j in jobs:
        sec = job_run_seconds(j)
        t["jobs"] += 1
        t["run_seconds"] += sec
        t["cpu_hours"] += j.cpus * sec / 3600
        t["mem_gb_hours"] += j.mem_mb / 1024 * sec / 3600
        t["cost_hours"] += accrued_uh(j) / UH_PER_HOUR
    return {k: round(v, 6) if isinstance(v, float) else v for k, v in t.items()}


# ---------- app factory ----------
def create_app(settings: Settings | None = None, runner: JobRunner | None = None,
               recover: bool = True) -> FastAPI:
    settings = settings or load_settings()
    Session_ = make_session_factory(settings.db_url)
    runner = runner or JobRunner(settings, Session_)
    with Session_() as db:
        grant_missing_initial(db, settings.default_credit_hours)

    @asynccontextmanager
    async def lifespan(_app):
        if recover:
            runner.recover()
        yield

    app = FastAPI(title="DockerServer", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.Session, app.state.runner = settings, Session_, runner

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
        if not user.is_admin:
            raise HTTPException(403, "admin only")
        return user

    Admin = Annotated[User, Depends(admin_user)]

    def get_user_or_404(db, user_id):
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(404, "user not found")
        return u

    def get_job_for(db, user, job_id) -> Job:
        job = db.get(Job, job_id)
        if job is None or (job.user_id != user.id and not user.is_admin):
            raise HTTPException(404, "job not found")
        return job

    # ----- misc -----
    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/me", response_model=UserOut)
    def me(user: CurrentUser):
        return user

    # ----- admin -----
    @app.post("/admin/users", response_model=UserCreated, status_code=201)
    def create_user(body: UserCreate, db: DB, admin: Admin):
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        u = User(**fields)
        db.add(u)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "user name already exists")
        grant_initial(db, u, settings.default_credit_hours, admin_id=admin.id)
        db.commit()
        db.refresh(u)
        _, raw = issue_token(db, u)
        return {**UserOut.model_validate(u).model_dump(), "token": raw}

    @app.get("/admin/users", response_model=list[UserOut])
    def list_users(db: DB, _: Admin):
        return db.scalars(select(User).order_by(User.id)).all()

    @app.get("/admin/users/{user_id}", response_model=UserOut)
    def get_user(user_id: int, db: DB, _: Admin):
        return get_user_or_404(db, user_id)

    @app.patch("/admin/users/{user_id}", response_model=UserOut)
    def update_user(user_id: int, body: UserUpdate, db: DB, admin: Admin):
        u = get_user_or_404(db, user_id)
        changes = body.model_dump(exclude_unset=True, exclude_none=True)
        if u.id == admin.id and (changes.get("active") is False or changes.get("role") == "user"):
            raise HTTPException(400, "admins cannot disable or demote themselves")
        for k, v in changes.items():
            setattr(u, k, v)
        db.commit()
        return u

    @app.post("/admin/users/{user_id}/disable", response_model=UserOut)
    def disable_user(user_id: int, db: DB, admin: Admin):
        return update_user(user_id, UserUpdate(active=False), db, admin)

    @app.post("/admin/users/{user_id}/enable", response_model=UserOut)
    def enable_user(user_id: int, db: DB, admin: Admin):
        return update_user(user_id, UserUpdate(active=True), db, admin)

    @app.get("/admin/users/{user_id}/tokens", response_model=list[TokenOut])
    def list_tokens(user_id: int, db: DB, _: Admin):
        get_user_or_404(db, user_id)
        return db.scalars(select(Token).where(Token.user_id == user_id).order_by(Token.id)).all()

    @app.post("/admin/users/{user_id}/tokens", response_model=TokenIssued, status_code=201)
    def create_token(user_id: int, body: TokenCreate, db: DB, _: Admin):
        u = get_user_or_404(db, user_id)
        tok, raw = issue_token(db, u, body.name)
        return {**TokenOut.model_validate(tok).model_dump(), "token": raw}

    @app.delete("/admin/tokens/{token_id}", response_model=TokenOut)
    def revoke_token(token_id: int, db: DB, _: Admin):
        tok = db.get(Token, token_id)
        if tok is None:
            raise HTTPException(404, "token not found")
        if tok.revoked_at is None:
            tok.revoked_at = utcnow()
            db.commit()
        return tok

    @app.post("/admin/users/{user_id}/credits", response_model=LedgerOut, status_code=201)
    def change_credits(user_id: int, body: CreditChange, db: DB, admin: Admin):
        get_user_or_404(db, user_id)
        if body.hours == 0:
            raise HTTPException(400, "hours must be non-zero")
        e = add_entry(db, user_id, to_uh(body.hours), body.reason, admin_id=admin.id, note=body.note)
        db.commit()
        return ledger_out(e)

    @app.get("/admin/users/{user_id}/ledger", response_model=list[LedgerOut])
    def user_ledger(user_id: int, db: DB, _: Admin, limit: int = 200):
        get_user_or_404(db, user_id)
        q = select(LedgerEntry).where(LedgerEntry.user_id == user_id).order_by(LedgerEntry.id.desc())
        return [ledger_out(e) for e in db.scalars(q.limit(min(limit, 5000)))]

    def jobs_in_range(db, start, end, user_id=None):
        q = select(Job).where(Job.started_at.is_not(None)).order_by(Job.id)
        if user_id is not None:
            q = q.where(Job.user_id == user_id)
        if start:
            q = q.where(Job.started_at >= start)
        if end:
            q = q.where(Job.started_at < end)
        return db.scalars(q).all()

    @app.get("/admin/usage")
    def admin_usage(db: DB, _: Admin, start: datetime | None = None, end: datetime | None = None):
        """Per-user totals for jobs started in [start, end) (UTC)."""
        by_user: dict[int, list[Job]] = {}
        for j in jobs_in_range(db, start, end):
            by_user.setdefault(j.user_id, []).append(j)
        users = {u.id: u for u in db.scalars(select(User))}
        rows = [{"user_id": uid, "name": users[uid].name, "balance_hours": users[uid].balance_hours,
                 **usage_totals(js)} for uid, js in sorted(by_user.items())]
        return {"start": start, "end": end, "users": rows,
                "total": usage_totals([j for js in by_user.values() for j in js])}

    @app.get("/me/usage")
    def my_usage(db: DB, user: CurrentUser, start: datetime | None = None,
                 end: datetime | None = None):
        """My jobs (with cost and usage stats) and per-day totals, by job start day (UTC)."""
        jobs = jobs_in_range(db, start, end, user.id)
        days: dict[str, list[Job]] = {}
        for j in jobs:
            days.setdefault(j.started_at.date().isoformat(), []).append(j)
        return {
            "balance_hours": user.balance_hours,
            "available_hours": hours(available_uh(db, user.id)),
            "total": usage_totals(jobs),
            "per_day": [{"day": d, **usage_totals(js)} for d, js in sorted(days.items())],
            "jobs": [{**JobOut.model_validate(j).model_dump(), "cost_hours": hours(accrued_uh(j))}
                     for j in reversed(jobs)],
        }

    # ----- jobs -----
    @app.post("/jobs", response_model=JobOut, status_code=201)
    async def submit_job(
        request: Request,
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
        image = image or settings.default_image
        if not settings.image_allowed(image):
            raise HTTPException(400, f"image not allowed; allowed: {settings.allowed_images}")
        cpus = cpus if cpus is not None else min(1.0, user.max_cpus)
        mem_mb = mem_mb if mem_mb is not None else min(256, user.max_mem_mb)
        timeout_seconds = timeout_seconds if timeout_seconds is not None else user.max_run_seconds
        if cpus > user.max_cpus:
            raise HTTPException(400, f"cpus exceeds your limit ({user.max_cpus})")
        if mem_mb > user.max_mem_mb:
            raise HTTPException(400, f"mem_mb exceeds your limit ({user.max_mem_mb})")
        if timeout_seconds > user.max_run_seconds:
            raise HTTPException(400, f"timeout_seconds exceeds your limit ({user.max_run_seconds})")
        if network and not user.allow_network:
            raise HTTPException(403, "network access is not enabled for your account")
        if git_url and not git_url.startswith("https://"):
            raise HTTPException(400, "git_url must be an https:// URL")
        need = cost_uh(cpus, mem_mb, settings.min_start_seconds)
        if available_uh(db, user.id) < need:
            raise HTTPException(402, f"not enough credits: need at least {hours(need):.4f} hours "
                                     f"for {settings.min_start_seconds}s at this size")
        active = db.scalar(select(func.count()).select_from(Job).where(
            Job.user_id == user.id, Job.status.in_(ACTIVE_STATUSES)))
        if active >= user.max_concurrent_jobs:
            raise HTTPException(429, f"too many active jobs (limit {user.max_concurrent_jobs})")

        content = filename = None
        if file is not None and file.filename:
            content = await file.read(settings.max_upload_bytes + 1)
            if len(content) > settings.max_upload_bytes:
                raise HTTPException(413, "upload too large")
            filename = file.filename

        job = Job(user_id=user.id, image=image, command=command, git_url=git_url or None,
                  cpus=cpus, mem_mb=mem_mb, timeout_seconds=timeout_seconds, network=network)
        db.add(job)
        db.commit()
        runner.submit(job.id, filename, content)
        return job

    @app.get("/jobs", response_model=list[JobOut])
    def list_jobs(db: DB, user: CurrentUser, all: bool = False, status: str | None = None,
                  limit: int = 100):
        q = select(Job).order_by(Job.id.desc()).limit(min(limit, 1000))
        if not (all and user.is_admin):
            q = q.where(Job.user_id == user.id)
        if status:
            q = q.where(Job.status == status)
        return db.scalars(q).all()

    @app.get("/jobs/{job_id}", response_model=JobOut)
    def get_job(job_id: int, db: DB, user: CurrentUser):
        return get_job_for(db, user, job_id)

    @app.get("/jobs/{job_id}/logs", response_class=PlainTextResponse)
    def get_logs(job_id: int, db: DB, user: CurrentUser):
        job = get_job_for(db, user, job_id)
        if job.status not in TERMINAL_STATUSES:
            return runner.live_logs(job) or ""
        p = runner.log_path(job.id)
        return p.read_text(errors="replace") if p.exists() else ""

    @app.post("/jobs/{job_id}/stop", response_model=JobOut)
    def stop_job(job_id: int, db: DB, user: CurrentUser):
        job = get_job_for(db, user, job_id)
        if job.status in TERMINAL_STATUSES:
            raise HTTPException(409, f"job already {job.status}")
        runner.stop(job.id)
        runner.wait(job.id, timeout=30)
        db.refresh(job)
        return job

    return app
