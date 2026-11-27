"""Business logic shared by the REST API and the MCP server.

Every function takes an explicit db session and the acting user, enforces
permissions/limits/credits, and raises ServiceError(status, message) on refusal.
"""
import base64
import binascii
import io
import tarfile
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import AuthFailureLimiter, issue_token, lookup_user
from .oauth import revoke_for_ds_token, revoke_for_user
from .billing import (accrued_uh, add_entry, available_uh, cost_uh, grant_initial, hours,
                      to_uh)
from .config import Settings
from .models import ACTIVE_STATUSES, TERMINAL_STATUSES, Job, LedgerEntry, Site, Token, User, UserEnv, utcnow
from .runner import JobRunner
from .sites import SiteError, SiteManager, check_site_name, clean_path
from .userenv import EnvError, EnvStore
from .schemas import AdminJobOut, JobOut, UserOut, ledger_out, usage_totals


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Services:
    def __init__(self, settings: Settings, session_factory, runner: JobRunner):
        self.settings, self.Session, self.runner = settings, session_factory, runner
        self.auth_limiter = AuthFailureLimiter(settings.auth_fail_limit, settings.auth_fail_window)
        key = settings.secret_key
        if not key:   # e.g. tests constructing Settings directly; load_settings() always sets one
            from cryptography.fernet import Fernet
            key = Fernet.generate_key().decode()
        self.env = EnvStore(key)
        self.oauth = None  # DSOAuthProvider when OAuth is enabled (set by create_app)
        self.sites: SiteManager | None = None   # set by create_app

    def authenticate(self, db: Session, token: str | None, client_ip: str,
                     resource_path: str | None = None) -> User:
        """Resolve a bearer token to an active user; rate-limits repeated failures per IP.
        ds_ tokens work everywhere; OAuth access tokens (dsa_) only on the MCP endpoint
        (resource_path) they were issued for."""
        if self.auth_limiter.blocked(client_ip):
            raise ServiceError(429, "too many failed authentication attempts; try again later")
        user = self.resolve_user(db, token, resource_path)
        if user is None:
            if token:
                self.auth_limiter.record_failure(client_ip)
            raise ServiceError(401, "missing, invalid or revoked bearer token")
        return user

    # ----- helpers -----
    def resolve_user(self, db: Session, token: str | None, resource_path: str | None = None) -> User | None:
        """Token -> active user, without rate limiting (see authenticate)."""
        if not token:
            return None
        if token.startswith("dsa_"):
            if self.oauth is None or not resource_path:
                return None
            return self.oauth.user_for_access_token(db, token, resource_path)
        return lookup_user(db, token)

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
        st = self.settings
        defaults = {"max_cpus": st.default_max_cpus, "max_mem_mb": st.default_max_mem_mb,
                    "max_run_seconds": st.default_max_run_seconds,
                    "max_concurrent_jobs": st.default_max_concurrent_jobs,
                    "max_sites": st.default_max_sites}
        defaults.update({k: v for k, v in limits.items() if v is not None})
        u = User(name=name, role=role, **defaults)
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
        if changes.get("active") is False:
            revoke_for_user(db, u.id)
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
        revoke_for_user(db, u.id)
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

    def delete_user(self, db: Session, admin: User, user_id: int) -> dict:
        """Delete an account: kill its running jobs, delete its sites (containers and files),
        saved variables and tokens. The row stays (renamed, disabled, hidden) so job and ledger
        history keep their owner; the name becomes free again."""
        self.require_admin(admin)
        u = self.get_user(db, user_id)
        if u.id == admin.id:
            raise ServiceError(400, "admins cannot delete themselves")
        if u.deleted_at is not None:
            raise ServiceError(404, "user not found")
        name = u.name
        killed = self.suspend_user(db, admin, user_id)["killed_job_ids"]
        u = self.get_user(db, user_id)
        sites = db.scalars(select(Site).where(Site.user_id == u.id)).all()
        removed = []
        for site in sites:
            self._remove_site(db, site)
            removed.append(site.name)
        if self.sites is not None:
            self.sites.files.remove_user(u.id)
        db.query(UserEnv).filter(UserEnv.user_id == u.id).delete()
        db.query(Token).filter(Token.user_id == u.id, Token.revoked_at.is_(None)) \
            .update({Token.revoked_at: utcnow()})
        u.deleted_at = utcnow()
        u.active = False
        u.name = f"{name[:40]}~deleted~{u.id}"
        db.commit()
        return {"deleted_user": name, "user_id": u.id, "killed_job_ids": killed,
                "deleted_sites": removed}

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
        return db.scalars(select(User).where(User.deleted_at.is_(None)).order_by(User.id)).all()

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
            revoke_for_ds_token(db, tok.id)
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
                             "max_concurrent_jobs", "allow_network", "max_sites"})}

    # ----- static web sites (the caller's own) -----
    def site_url(self, user_name: str, site_name: str) -> str:
        return f"{self.settings.sites_base}/sites/{quote(user_name, safe='')}/{site_name}/"

    def _site_out(self, site: Site, user_name: str) -> dict:
        return {"site": site.name, "url": self.site_url(user_name, site.name),
                "published": site.published, "awake": site.container_id is not None,
                "file_count": site.file_count, "bytes_stored": site.bytes_stored,
                "requests": site.requests, "bytes_served": site.bytes_served,
                "wake_count": site.wake_count,
                "awake_hours": round(self._awake_seconds(site) / 3600, 4),
                "cost_hours": hours(site.cost_uh or 0),
                "last_access_at": site.last_access_at, "created_at": site.created_at,
                "updated_at": site.updated_at, "published_at": site.published_at}

    @staticmethod
    def _awake_seconds(site: Site) -> float:
        extra = (utcnow() - site.awake_since).total_seconds() if site.awake_since else 0.0
        return (site.awake_seconds or 0.0) + max(extra, 0.0)

    def _sites_mgr(self) -> SiteManager:
        if self.sites is None:
            raise ServiceError(503, "static sites are not enabled on this server")
        return self.sites

    def _get_site(self, db: Session, user: User, name: str) -> Site:
        try:
            check_site_name(name)
        except SiteError as e:
            raise ServiceError(e.status, e.message)
        site = db.scalar(select(Site).where(Site.user_id == user.id, Site.name == name))
        if site is None:
            raise ServiceError(404, f"no site named {name}")
        return site

    def put_site_file(self, db: Session, user: User, site_name: str, path: str, content: str,
                      encoding: str = "utf-8") -> dict:
        mgr, st = self._sites_mgr(), self.settings
        try:
            check_site_name(site_name)
            rel = clean_path(path)
        except SiteError as e:
            raise ServiceError(e.status, e.message)
        if encoding == "utf-8":
            data = (content or "").encode("utf-8")
        elif encoding == "base64":
            try:
                data = base64.b64decode(content or "", validate=True)
            except (binascii.Error, ValueError):
                raise ServiceError(400, "content is not valid base64")
        else:
            raise ServiceError(400, "encoding must be 'utf-8' or 'base64'")
        if len(data) > st.site_max_file_bytes:
            raise ServiceError(413, f"file too large (max {st.site_max_file_bytes // 2**20} MB)")
        site = db.scalar(select(Site).where(Site.user_id == user.id, Site.name == site_name))
        if site is None:
            count = db.scalar(select(func.count()).select_from(Site).where(Site.user_id == user.id))
            if count >= user.max_sites:
                raise ServiceError(403, f"site limit reached ({user.max_sites}); delete one first")
            site = Site(user_id=user.id, name=site_name)
            db.add(site)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                raise ServiceError(409, "site is being created by another request; retry")
        old = mgr.files.file_size(user.id, site_name, rel)
        if site.bytes_stored - old + len(data) > st.site_max_bytes:
            raise ServiceError(413, f"site too large (max {st.site_max_bytes // 2**20} MB)")
        if not old and site.file_count + 1 > st.site_max_files:
            raise ServiceError(413, f"too many files in the site (max {st.site_max_files})")
        try:
            mgr.files.write(user.id, site_name, rel, data)
        except SiteError as e:
            db.rollback()
            raise ServiceError(e.status, e.message)
        site.file_count, site.bytes_stored = mgr.files.usage(user.id, site_name)
        site.updated_at = utcnow()
        db.commit()
        return {"site": site_name, "path": rel, "bytes": len(data),
                "site_files": site.file_count, "site_bytes": site.bytes_stored,
                "published": site.published,
                "url": self.site_url(user.name, site_name) if site.published else None}

    def delete_site_file(self, db: Session, user: User, site_name: str, path: str) -> dict:
        mgr = self._sites_mgr()
        site = self._get_site(db, user, site_name)
        try:
            gone = mgr.files.delete_file(user.id, site.name, path)
        except SiteError as e:
            raise ServiceError(e.status, e.message)
        if not gone:
            raise ServiceError(404, f"no file {path} in site {site_name}")
        site.file_count, site.bytes_stored = mgr.files.usage(user.id, site.name)
        site.updated_at = utcnow()
        db.commit()
        return {"site": site.name, "path": path, "deleted": True}

    def list_site_files(self, db: Session, user: User, site_name: str) -> list[dict]:
        site = self._get_site(db, user, site_name)
        return self._sites_mgr().files.list(user.id, site.name)

    def publish_site(self, db: Session, user: User, site_name: str, published: bool = True) -> dict:
        self._sites_mgr()
        site = self._get_site(db, user, site_name)
        if published and site.file_count == 0:
            raise ServiceError(400, "the site has no files; upload some with put_site_file first")
        site.published = published
        if published and site.published_at is None:
            site.published_at = utcnow()
        db.commit()
        out = self._site_out(site, user.name)
        if published and not self.sites.files.site_dir(user.id, site.name).joinpath("index.html").is_file():
            out["warning"] = "no index.html at the site root; the URL itself will answer 404"
        if not published and site.container_id:
            self.sites.sleep(site.id, "unpublished")
        return out

    def list_sites(self, db: Session, user: User) -> list[dict]:
        if self.sites is not None:
            self.sites.flush()
        return [self._site_out(s, user.name) for s in
                db.scalars(select(Site).where(Site.user_id == user.id).order_by(Site.id))]

    def _remove_site(self, db: Session, site: Site):
        mgr = self._sites_mgr()
        mgr.sleep(site.id, "deleted")
        mgr.files.remove(site.user_id, site.name)
        db.refresh(site)
        db.delete(site)

    def delete_site(self, db: Session, user: User, site_name: str) -> dict:
        site = self._get_site(db, user, site_name)
        self._remove_site(db, site)
        db.commit()
        return {"site": site_name, "deleted": True}

    def admin_sites(self, db: Session, admin: User, user_id: int | None = None) -> list[dict]:
        """Every user's sites with their container state and usage (no file contents)."""
        self.require_admin(admin)
        if self.sites is not None:
            self.sites.flush()
        q = select(Site, User).join(User, User.id == Site.user_id).order_by(User.id, Site.id)
        if user_id is not None:
            q = q.where(Site.user_id == user_id)
        out = []
        for site, u in db.execute(q).all():
            row = {"user_id": u.id, "user": u.name, "container": f"fc-site-{site.id}",
                   **self._site_out(site, u.name)}
            out.append(row)
        return out

    # ----- compute nodes (monitoring, drain) -----
    def admin_nodes(self, db: Session, admin: User, probe: bool = True) -> dict:
        """Every configured compute node: reachability, capacity/usage as the node reports it,
        the jobs this server has running there, and 24 h counts. Job metadata only."""
        self.require_admin(admin)
        pool = self.runner.pool
        if probe:
            pool.probe_all()
        now = utcnow()
        since = now - timedelta(hours=24)
        users = dict(db.execute(select(User.id, User.name)).all())
        running = db.scalars(select(Job).where(Job.status.in_(("preparing", "running")))
                             .order_by(Job.id)).all()
        recent = db.execute(
            select(Job.node, Job.status, Job.cpus, Job.started_at, Job.finished_at)
            .where(Job.started_at >= since, Job.node.is_not(None))).all()
        stats: dict[str, dict] = {}
        for node, status, cpus, started, finished in recent:
            st = stats.setdefault(node, {"jobs": 0, "succeeded": 0, "failed": 0, "lost": 0,
                                         "cpu_hours": 0.0})
            n = 1
            st["jobs"] += n
            st["cpu_hours"] += max(((finished or now) - started).total_seconds(), 0) * cpus / 3600
            if status == "succeeded":
                st["succeeded"] += n
            elif status == "lost":
                st["lost"] += n
            elif status in TERMINAL_STATUSES:
                st["failed"] += n

        def job_row(j: Job) -> dict:
            return {"id": j.id, "user_id": j.user_id, "user": users.get(j.user_id), "image": j.image,
                    "status": j.status, "cpus": j.cpus, "mem_mb": j.mem_mb,
                    "started_at": j.started_at.isoformat() if j.started_at else None,
                    "timeout_seconds": j.timeout_seconds,
                    "run_seconds": (now - j.started_at).total_seconds() if j.started_at else None}

        def iso(t):
            return datetime.fromtimestamp(t, timezone.utc).replace(tzinfo=None).isoformat() if t else None

        nodes = []
        for h in pool.health():
            name = h["name"]
            mine = [job_row(j) for j in running if j.node == name]
            info = h.get("info") or None
            nodes.append({
                "name": name, "url": h.get("url"), "online": h["online"], "draining": h["draining"],
                "status": ("offline" if not h["online"] else "draining" if h["draining"] else "online"),
                "last_seen": iso(h.get("last_ok")), "last_check": iso(h.get("last_check")),
                "down_since": iso(h.get("down_since")), "error": h.get("last_error"),
                "latency_ms": h.get("latency_ms"),
                "capacity": info.get("capacity") if info else None,
                "used": info.get("used") if info else None,
                "free": info.get("free") if info else None,
                "node_active_jobs": info.get("active_jobs") if info else None,
                "docker_version": info.get("docker_version") if info else None,
                "features": info.get("features") if info else None,
                "allowed_images": info.get("allowed_images") if info else None,
                "allow_network": info.get("allow_network") if info else None,
                "reserved": {"cpus": round(sum(j["cpus"] for j in mine), 3),
                             "mem_mb": sum(j["mem_mb"] for j in mine)},
                "pending": h.get("pending"),
                "running_jobs": mine,
                "last_24h": stats.get(name, {"jobs": 0, "succeeded": 0, "failed": 0, "lost": 0,
                                             "cpu_hours": 0.0}),
            })
        local = pool.local is not None
        return {"nodes": nodes, "local_execution": local,
                "local_running_jobs": [job_row(j) for j in running if j.node == "local"] if local else [],
                "queued_jobs": db.scalar(select(func.count()).select_from(Job).where(Job.status == "queued")),
                "heartbeat_seconds": self.settings.node_heartbeat, "checked_at": now.isoformat()}

    def set_node_draining(self, admin: User, name: str, draining: bool) -> dict:
        self.require_admin(admin)
        try:
            self.runner.pool.set_draining(name, draining)
        except KeyError:
            raise ServiceError(404, f"no compute node named {name}")
        except OSError as e:
            raise ServiceError(500, f"could not save node state: {e}")
        return {"name": name, "draining": draining}

    # ----- the caller's own environment variables -----
    def list_env(self, db: Session, user: User, reveal: bool = False) -> list[dict]:
        return self.env.listing(db, user.id, reveal)

    def set_env(self, db: Session, user: User, name: str, value: str):
        try:
            self.env.set(db, user.id, name, value)
        except EnvError as e:
            raise ServiceError(400, str(e))

    def delete_env(self, db: Session, user: User, name: str):
        if not self.env.delete(db, user.id, name):
            raise ServiceError(404, f"no variable {name}")

    # ----- jobs -----
    def submit_job(self, db: Session, user: User, *, command: str, image: str | None = None,
                   cpus: float | None = None, mem_mb: int | None = None,
                   timeout_seconds: int | None = None, network: bool = False,
                   git_url: str | None = None, filename: str | None = None,
                   content: bytes | None = None, env: dict | None = None) -> Job:
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
        try:
            job_env = self.env.merged(db, user.id, env)   # user-global, overridden per job
        except EnvError as e:
            raise ServiceError(400, str(e))
        never = getattr(self.runner, "never_fits", lambda c, m, e=False: None)(cpus, mem_mb, bool(job_env))
        if never:
            raise ServiceError(400, never)
        need = cost_uh(cpus, mem_mb, s.min_start_seconds)
        if available_uh(db, user.id) < need:
            raise ServiceError(402, f"not enough credits: need at least {hours(need):.4f} hours "
                                    f"for {s.min_start_seconds}s at this size")
        active = db.scalar(select(func.count()).select_from(Job).where(
            Job.user_id == user.id, Job.status.in_(ACTIVE_STATUSES)))
        if active >= user.max_concurrent_jobs:
            raise ServiceError(429, f"too many active jobs (limit {user.max_concurrent_jobs})")
        job = Job(user_id=user.id, image=image, command=command, git_url=git_url or None,
                  cpus=cpus, mem_mb=mem_mb, timeout_seconds=timeout_seconds, network=network,
                  env_names=",".join(sorted(job_env)) or None)
        db.add(job)
        db.commit()
        self.runner.submit(job.id, filename, content, env=job_env)
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
