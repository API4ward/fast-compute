"""Business logic shared by the REST API and the MCP server.

Every function takes an explicit db session and the acting user, enforces
permissions/limits/credits, and raises ServiceError(status, message) on refusal.
"""
import io
import tarfile
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import AuthFailureLimiter, issue_token, lookup_user
from .billing import (accrued_uh, add_entry, available_uh, cost_uh, grant_initial, hours,
                      to_uh)
from .config import Settings
from .models import ACTIVE_STATUSES, TERMINAL_STATUSES, Job, LedgerEntry, Token, User, utcnow
from .runner import JobRunner
from .schemas import AdminJobOut, JobOut, UserOut, ledger_out, usage_totals


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Services:
    def __init__(self, settings: Settings, session_factory, runner: JobRunner):
        self.settings, self.Session, self.runner = settings, session_factory, runner
        self.auth_limiter = AuthFailureLimiter(settings.auth_fail_limit, settings.auth_fail_window)

    def authenticate(self, db: Session, token: str | None, client_ip: str) -> User:
        """Resolve a bearer token to an active user; rate-limits repeated failures per IP."""
        if self.auth_limiter.blocked(client_ip):
            raise ServiceError(429, "too many failed authentication attempts; try again later")
        user = lookup_user(db, token) if token else None
        if user is None:
            if token:
                self.auth_limiter.record_failure(client_ip)
            raise ServiceError(401, "missing, invalid or revoked bearer token")
        return user

    # ----- helpers -----
    @staticmethod
    def require_admin(user: User):
        if not user.is_admin:
            raise ServiceError(403, "admin only")

    @staticmethod
    def get_user(db: Session, user_id: int) -> User:
        u = db.get(User, user_id)
        if u is None:
            raise ServiceError(404, "user not found")
        return u

    @staticmethod
    def get_job(db: Session, user: User, job_id: int) -> Job:
        """A job of the caller's own. Nobody (not even an admin) can open another user's job;
        admins only see other users' job metadata via list_user_jobs."""
        job = db.get(Job, job_id)
        if job is None or job.user_id != user.id:
            raise ServiceError(404, "job not found")
        return job

    # ----- users / tokens (admin) -----
    def create_user(self, db: Session, admin: User, name: str, role: str = "user",
                    **limits) -> tuple[User, str]:
        self.require_admin(admin)
        u = User(name=name, role=role, **{k: v for k, v in limits.items() if v is not None})
        db.add(u)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise ServiceError(409, "user name already exists")
        grant_initial(db, u, self.settings.default_credit_hours, admin_id=admin.id)
        db.commit()
        db.refresh(u)
        _, raw = issue_token(db, u)
        return u, raw

    def update_user(self, db: Session, admin: User, user_id: int, changes: dict) -> User:
        self.require_admin(admin)
        u = self.get_user(db, user_id)
        if u.id == admin.id and (changes.get("active") is False or changes.get("role") == "user"):
            raise ServiceError(400, "admins cannot disable or demote themselves")
        for k, v in changes.items():
            setattr(u, k, v)
        db.commit()
        return u

    def suspend_user(self, db: Session, admin: User, user_id: int) -> dict:
        """Disable the account and kill all of the user's active jobs (status killed_by_admin,
        billed normally for the time they ran)."""
        self.require_admin(admin)
        u = self.get_user(db, user_id)
        if u.id == admin.id:
            raise ServiceError(400, "admins cannot suspend themselves")
        u.active = False
        db.commit()
        active = db.scalars(select(Job.id).where(Job.user_id == u.id,
                                                 Job.status.in_(ACTIVE_STATUSES))).all()
        for jid in active:
            self.runner.stop(jid, "killed_by_admin", f"killed by admin {admin.name}")
        for jid in active:
            self.runner.wait(jid, timeout=30)
        db.expire_all()
        return {"user": UserOut.model_validate(self.get_user(db, user_id)).model_dump(mode="json"),
                "killed_job_ids": list(active)}

    def list_user_jobs(self, db: Session, admin: User, user_id: int | None = None,
                       status: str | None = None, limit: int = 100) -> list[dict]:
        """Read-only job metadata for admins (no command, code, git URL or logs)."""
        self.require_admin(admin)
        q = select(Job).order_by(Job.id.desc()).limit(max(1, min(limit, 1000)))
        if user_id is not None:
            self.get_user(db, user_id)
            q = q.where(Job.user_id == user_id)
        if status:
            q = q.where(Job.status == status)
        return [AdminJobOut.model_validate(j).model_dump(mode="json") for j in db.scalars(q)]

    def list_users(self, db: Session, admin: User) -> list[User]:
        self.require_admin(admin)
        return db.scalars(select(User).order_by(User.id)).all()

    def issue_token(self, db: Session, admin: User, user_id: int, name: str):
        self.require_admin(admin)
        return issue_token(db, self.get_user(db, user_id), name)

    def revoke_token(self, db: Session, admin: User, token_id: int) -> Token:
        self.require_admin(admin)
        tok = db.get(Token, token_id)
        if tok is None:
            raise ServiceError(404, "token not found")
        if tok.revoked_at is None:
            tok.revoked_at = utcnow()
            db.commit()
        return tok

    # ----- credits -----
    def change_credits(self, db: Session, admin: User, user_id: int, hours_: float,
                       reason: str = "topup", note: str | None = None) -> LedgerEntry:
        self.require_admin(admin)
        self.get_user(db, user_id)
        if hours_ == 0:
            raise ServiceError(400, "hours must be non-zero")
        if reason not in ("topup", "adjust"):
            raise ServiceError(400, "reason must be topup or adjust")
        e = add_entry(db, user_id, to_uh(hours_), reason, admin_id=admin.id, note=note)
        db.commit()
        return e

    def ledger(self, db: Session, admin: User, user_id: int, limit: int = 200) -> list[dict]:
        self.require_admin(admin)
        self.get_user(db, user_id)
        q = select(LedgerEntry).where(LedgerEntry.user_id == user_id).order_by(LedgerEntry.id.desc())
        return [ledger_out(e) for e in db.scalars(q.limit(min(limit, 5000)))]

    @staticmethod
    def _jobs_in_range(db, start, end, user_id=None):
        q = select(Job).where(Job.started_at.is_not(None)).order_by(Job.id)
        if user_id is not None:
            q = q.where(Job.user_id == user_id)
        if start:
            q = q.where(Job.started_at >= start)
        if end:
            q = q.where(Job.started_at < end)
        return db.scalars(q).all()

    def usage_report(self, db: Session, admin: User, start: datetime | None = None,
                     end: datetime | None = None) -> dict:
        self.require_admin(admin)
        by_user: dict[int, list[Job]] = {}
        for j in self._jobs_in_range(db, start, end):
            by_user.setdefault(j.user_id, []).append(j)
        users = {u.id: u for u in db.scalars(select(User))}
        rows = [{"user_id": uid, "name": users[uid].name, "balance_hours": users[uid].balance_hours,
                 **usage_totals(js)} for uid, js in sorted(by_user.items())]
        return {"start": start, "end": end, "users": rows,
                "total": usage_totals([j for js in by_user.values() for j in js])}

    def my_usage(self, db: Session, user: User, start: datetime | None = None,
                 end: datetime | None = None, include_jobs: bool = True) -> dict:
        jobs = self._jobs_in_range(db, start, end, user.id)
        days: dict[str, list[Job]] = {}
        for j in jobs:
            days.setdefault(j.started_at.date().isoformat(), []).append(j)
        out = {
            "balance_hours": user.balance_hours,
            "available_hours": hours(available_uh(db, user.id)),
            "total": usage_totals(jobs),
            "per_day": [{"day": d, **usage_totals(js)} for d, js in sorted(days.items())],
        }
        if include_jobs:
            out["jobs"] = [{**JobOut.model_validate(j).model_dump(), "cost_hours": hours(accrued_uh(j))}
                           for j in reversed(jobs)]
        return out

    def balance(self, db: Session, user: User) -> dict:
        return {"balance_hours": user.balance_hours,
                "available_hours": hours(available_uh(db, user.id)),
                "limits": UserOut.model_validate(user).model_dump(
                    include={"max_cpus", "max_mem_mb", "max_run_seconds",
                             "max_concurrent_jobs", "allow_network"})}

    # ----- jobs -----
    def submit_job(self, db: Session, user: User, *, command: str, image: str | None = None,
                   cpus: float | None = None, mem_mb: int | None = None,
                   timeout_seconds: int | None = None, network: bool = False,
                   git_url: str | None = None, filename: str | None = None,
                   content: bytes | None = None) -> Job:
        s = self.settings
        if not command or not command.strip():
            raise ServiceError(400, "command is required")
        image = image or s.default_image
        if not s.image_allowed(image):
            raise ServiceError(400, f"image not allowed; allowed: {s.allowed_images}")
        cpus = cpus if cpus is not None else min(1.0, user.max_cpus)
        mem_mb = mem_mb if mem_mb is not None else min(256, user.max_mem_mb)
        timeout_seconds = timeout_seconds if timeout_seconds is not None else user.max_run_seconds
        if cpus <= 0 or mem_mb < 32 or timeout_seconds < 1:
            raise ServiceError(400, "cpus must be > 0, mem_mb >= 32, timeout_seconds >= 1")
        if cpus > user.max_cpus:
            raise ServiceError(400, f"cpus exceeds your limit ({user.max_cpus})")
        if mem_mb > user.max_mem_mb:
            raise ServiceError(400, f"mem_mb exceeds your limit ({user.max_mem_mb})")
        if timeout_seconds > user.max_run_seconds:
            raise ServiceError(400, f"timeout_seconds exceeds your limit ({user.max_run_seconds})")
        if network and not user.allow_network:
            raise ServiceError(403, "network access is not enabled for your account")
        if git_url and not git_url.startswith("https://"):
            raise ServiceError(400, "git_url must be an https:// URL")
        if content is not None and len(content) > s.max_upload_bytes:
            raise ServiceError(413, "upload too large")
        need = cost_uh(cpus, mem_mb, s.min_start_seconds)
        if available_uh(db, user.id) < need:
            raise ServiceError(402, f"not enough credits: need at least {hours(need):.4f} hours "
                                    f"for {s.min_start_seconds}s at this size")
        active = db.scalar(select(func.count()).select_from(Job).where(
            Job.user_id == user.id, Job.status.in_(ACTIVE_STATUSES)))
        if active >= user.max_concurrent_jobs:
            raise ServiceError(429, f"too many active jobs (limit {user.max_concurrent_jobs})")
        job = Job(user_id=user.id, image=image, command=command, git_url=git_url or None,
                  cpus=cpus, mem_mb=mem_mb, timeout_seconds=timeout_seconds, network=network)
        db.add(job)
        db.commit()
        self.runner.submit(job.id, filename, content)
        return job

    def files_to_tar(self, files: dict[str, str]) -> bytes:
        """Inline {path: text} files -> tar bytes (sanitized later by workspace.build_workspace)."""
        buf = io.BytesIO()
        total = 0
        with tarfile.open(fileobj=buf, mode="w") as t:
            for path, text in files.items():
                data = text.encode()
                total += len(data)
                if total > self.settings.max_upload_bytes:
                    raise ServiceError(413, "files too large")
                ti = tarfile.TarInfo(path)
                ti.size = len(data)
                ti.mode = 0o755 if data.startswith(b"#!") else 0o644
                t.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    def list_jobs(self, db: Session, user: User, status: str | None = None,
                  limit: int = 100) -> list[Job]:
        """The caller's own jobs (admins included)."""
        q = select(Job).order_by(Job.id.desc()).limit(max(1, min(limit, 1000)))
        q = q.where(Job.user_id == user.id)
        if status:
            q = q.where(Job.status == status)
        return db.scalars(q).all()

    def logs(self, db: Session, user: User, job_id: int, tail: int | None = None) -> str:
        job = self.get_job(db, user, job_id)
        if job.status not in TERMINAL_STATUSES:
            text = self.runner.live_logs(job, tail=tail or 2000) or ""
        else:
            p = self.runner.log_path(job.id)
            text = p.read_text(errors="replace") if p.exists() else ""
        if tail:
            text = "".join(text.splitlines(keepends=True)[-tail:])
        return text

    def stop_job(self, db: Session, user: User, job_id: int) -> Job:
        job = self.get_job(db, user, job_id)
        if job.status in TERMINAL_STATUSES:
            raise ServiceError(409, f"job already {job.status}")
        self.runner.stop(job.id)
        self.runner.wait(job.id, timeout=30)
        db.refresh(job)
        return job
