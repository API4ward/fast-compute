"""Two MCP services (Streamable HTTP) served by the same FastAPI process.

* /mcp        user service: run and manage *your own* jobs. Every token (admins included)
               only ever sees and controls its own jobs here.
* /mcp-admin  admin service (admin tokens only; others get 403): manage users, credits and
               usage. Admins cannot open, stop or read the logs of an individual user's job;
               the only way to affect a user's running jobs is suspend_user (disable the
               account and kill all of that user's jobs).

Auth: the same access tokens as the REST API (`Authorization: Bearer <token>`). An ASGI
wrapper checks the token (with the shared per-IP failure rate limit) before the MCP app sees
the request; tools then resolve the caller from the request headers and go through the same
Services layer as the REST API. DNS-rebinding protection uses DOCKERSERVER_ALLOWED_HOSTS.
"""
import json
import time
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .auth import lookup_user
from .models import TERMINAL_STATUSES, Job
from .schemas import JobOut, TokenOut, UserOut, ledger_out
from .services import ServiceError, Services

PRICING = ("Pricing: 1 credit-hour = 1 CPU core + 256 MB RAM for 1 hour of wall-clock time; a job "
           "costs run_seconds/3600 * max(cpus, mem_mb/256) credit-hours (charged for the reserved "
           "size, not actual use).")

USER_INSTRUCTIONS = f"""DockerServer runs your code in an isolated, throw-away Docker container.
Each job: fresh container, read-only root filesystem, writable /workspace (your files / git
checkout, also the working directory), small /tmp, runs as an unprivileged user, no network
unless your account allows it and you ask for it, killed at its timeout.
Defaults: image python:3.12-slim, 1 CPU, 256 MB, your max run time. Call get_balance to see your
limits and remaining credits. {PRICING} A job is refused unless you can afford 60 s at its size,
and is stopped (status out_of_credits) if your credits run out while it runs.
These tools only ever act on your own jobs."""

ADMIN_INSTRUCTIONS = f"""DockerServer administration: users, limits, credits and usage.
Admins cannot open, stop or read the logs of an individual user's job. To stop a user's
activity use set_user_status(active=false) (blocks their tokens and new jobs; running jobs
continue) or suspend_user (disables the account AND kills all their running jobs, which end
with status killed_by_admin and are billed for the time they ran). {PRICING}"""


def _bearer(headers) -> str | None:
    if not headers:
        return None
    value = headers.get("authorization") or headers.get("Authorization") or ""
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class _BearerAuth:
    """ASGI wrapper: 401 unless the request carries a valid, active token (403 if the
    service needs an admin and the token isn't one); 429 when rate-limited."""

    def __init__(self, app, svc: Services, require_admin: bool):
        self.app, self.svc, self.require_admin = app, svc, require_admin

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            client = (scope.get("client") or ("?", 0))[0]
            err = None
            with self.svc.Session() as db:
                try:
                    user = self.svc.authenticate(db, _bearer(headers), client)
                    if self.require_admin and not user.is_admin:
                        err = ServiceError(403, "this MCP service is for admins only; use /mcp")
                except ServiceError as e:
                    err = e
            if err is not None:
                body = json.dumps({"detail": err.message}).encode()
                extra = {401: [(b"www-authenticate", b"Bearer")], 429: [(b"retry-after", b"60")]}
                await send({"type": "http.response.start", "status": err.status,
                            "headers": [(b"content-type", b"application/json")] + extra.get(err.status, [])})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o)))


def _caller(svc: Services, admin: bool):
    def call(ctx: Context, fn):
        """Resolve the caller from the request headers, open a session, run fn(db, user)."""
        token = _bearer(ctx.headers)
        with svc.Session() as db:
            user = lookup_user(db, token) if token else None
            if user is None:
                raise ToolError("unauthorized: missing, invalid or revoked token")
            if admin and not user.is_admin:
                raise ToolError("admin only")
            try:
                return fn(db, user)
            except ServiceError as e:
                raise ToolError(f"{e.status}: {e.message}")
    return call


RO = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _asgi(mcp: MCPServer, svc: Services, path: str, require_admin: bool):
    st = svc.settings
    app = mcp.streamable_http_app(
        streamable_http_path=path, stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=st.allowed_hosts, allowed_origins=st.allowed_origins))
    return _BearerAuth(app, svc, require_admin)


# ====================================================================================
def build_user_mcp(svc: Services, path: str = "/mcp"):
    """Returns (mcp_server, asgi_app). Caller runs mcp.session_manager.run() in its lifespan."""
    mcp = MCPServer(name="dockerserver", instructions=USER_INSTRUCTIONS)
    call = _caller(svc, admin=False)

    def job_dict(job) -> dict[str, Any]:
        return JobOut.model_validate(job).model_dump(mode="json")

    @mcp.tool(description=(
        "Run a shell command in a fresh isolated container and return the job. Provide code either "
        "as `files` (a {relative_path: text_content} map, for small inline code) or as `git_url` "
        "(public https repo, shallow-cloned into /workspace), or neither for a pure command. "
        "`command` runs via `sh -c` in /workspace, e.g. 'python main.py'. Optional: image "
        "(default python:3.12-slim; only allow-listed images), cpus (default 1), mem_mb (default "
        "256), timeout_seconds (default = your max run time), network (only if your account "
        "allows it). Requests above your limits are rejected. Set `wait` (0-60 s) to block until "
        "the job finishes and get its status and log tail back; otherwise poll get_job / "
        "get_job_logs. " + PRICING))
    def submit_job(ctx: Context, command: str, files: dict[str, str] | None = None,
                   git_url: str | None = None, image: str | None = None,
                   cpus: float | None = None, mem_mb: int | None = None,
                   timeout_seconds: int | None = None, network: bool = False,
                   wait: int = 0) -> dict[str, Any]:
        def run(db, user):
            filename = content = None
            if files:
                content, filename = svc.files_to_tar(files), "inline.tar"
            job = svc.submit_job(db, user, command=command, image=image, cpus=cpus, mem_mb=mem_mb,
                                 timeout_seconds=timeout_seconds, network=network, git_url=git_url,
                                 filename=filename, content=content)
            return job.id
        job_id = call(ctx, run)
        w = max(0, min(int(wait or 0), 60))
        if w:
            deadline = time.monotonic() + w
            svc.runner.wait(job_id, timeout=w)
            while time.monotonic() < deadline:
                with svc.Session() as db:
                    j = db.get(Job, job_id)
                    if j.status in TERMINAL_STATUSES and j.billed:
                        break
                time.sleep(0.3)

        def result(db, user):
            job = svc.get_job(db, user, job_id)
            out = {"job": job_dict(job)}
            if job.status in TERMINAL_STATUSES:
                out["logs_tail"] = svc.logs(db, user, job_id, tail=200)
            elif w:
                out["note"] = "still running; poll get_job / get_job_logs"
            return out
        return call(ctx, result)

    @mcp.tool(description="List my jobs, newest first. Optional status filter (queued, preparing, "
                          "running, succeeded, failed, timeout, stopped, out_of_credits, "
                          "killed_by_admin, error).", annotations=RO)
    def list_jobs(ctx: Context, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return call(ctx, lambda db, u: [job_dict(j) for j in svc.list_jobs(db, u, status, limit)])

    @mcp.tool(description="Get one of my jobs: status, exit code, times, cost_hours (once "
                          "finished), and CPU/memory usage samples.", annotations=RO)
    def get_job(ctx: Context, job_id: int) -> dict[str, Any]:
        return call(ctx, lambda db, u: job_dict(svc.get_job(db, u, job_id)))

    @mcp.tool(description="Get one of my jobs' combined stdout/stderr (live while running). "
                          "`tail` = last N lines (default 200, 0 = everything kept, max 1 MB).",
              annotations=RO)
    def get_job_logs(ctx: Context, job_id: int, tail: int = 200) -> str:
        return call(ctx, lambda db, u: svc.logs(db, u, job_id, tail if tail > 0 else None))

    @mcp.tool(description="Stop (kill) one of my running jobs. It is charged for the time it ran.",
              annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    def stop_job(ctx: Context, job_id: int) -> dict[str, Any]:
        return call(ctx, lambda db, u: job_dict(svc.stop_job(db, u, job_id)))

    @mcp.tool(description="My credit balance (credit-hours), available credit (balance minus what "
                          "my running jobs have used so far) and my per-job limits. " + PRICING,
              annotations=RO)
    def get_balance(ctx: Context) -> dict[str, Any]:
        return call(ctx, lambda db, u: {"user": u.name, **svc.balance(db, u)})

    @mcp.tool(description="My usage and spending: totals and per-day totals (jobs, run seconds, "
                          "CPU-hours, GB-hours, cost in credit-hours) for jobs started in "
                          "[start, end) (ISO dates/times, UTC, optional). include_jobs adds the "
                          "per-job list.", annotations=RO)
    def get_usage(ctx: Context, start: datetime | None = None, end: datetime | None = None,
                  include_jobs: bool = False) -> dict[str, Any]:
        return call(ctx, lambda db, u: _jsonable(svc.my_usage(db, u, start, end, include_jobs)))

    return mcp, _asgi(mcp, svc, path, require_admin=False)


# ====================================================================================
def build_admin_mcp(svc: Services, path: str = "/mcp-admin"):
    """Returns (mcp_server, asgi_app). Admin tokens only."""
    mcp = MCPServer(name="dockerserver-admin", instructions=ADMIN_INSTRUCTIONS)
    call = _caller(svc, admin=True)

    def user_dict(u) -> dict[str, Any]:
        return UserOut.model_validate(u).model_dump(mode="json")

    @mcp.tool(description="List all users with role, limits, active flag and balance (credit-hours).",
              annotations=RO)
    def list_users(ctx: Context) -> list[dict[str, Any]]:
        return call(ctx, lambda db, a: [user_dict(x) for x in svc.list_users(db, a)])

    @mcp.tool(description="Create a user and return its first access token (shown once). "
                          "role: 'user' or 'admin'. Optional limits: max_cpus, max_mem_mb, "
                          "max_run_seconds, max_concurrent_jobs, allow_network. New users get the "
                          "default credit grant.")
    def create_user(ctx: Context, name: str, role: str = "user", max_cpus: float | None = None,
                    max_mem_mb: int | None = None, max_run_seconds: int | None = None,
                    max_concurrent_jobs: int | None = None,
                    allow_network: bool | None = None) -> dict[str, Any]:
        if role not in ("user", "admin"):
            raise ToolError("role must be 'user' or 'admin'")

        def run(db, admin):
            u, raw = svc.create_user(db, admin, name, role, max_cpus=max_cpus, max_mem_mb=max_mem_mb,
                                     max_run_seconds=max_run_seconds,
                                     max_concurrent_jobs=max_concurrent_jobs,
                                     allow_network=allow_network)
            return {**user_dict(u), "token": raw}
        return call(ctx, run)

    @mcp.tool(description="Enable or disable a user's account. A disabled user's tokens stop "
                          "working, so they cannot submit or manage jobs; jobs already running "
                          "are NOT killed (use suspend_user for that).")
    def set_user_status(ctx: Context, user_id: int, active: bool) -> dict[str, Any]:
        return call(ctx, lambda db, a: user_dict(svc.update_user(db, a, user_id, {"active": active})))

    @mcp.tool(description="Change a user's per-job limits and/or role. Only the given fields "
                          "change: max_cpus, max_mem_mb, max_run_seconds, max_concurrent_jobs, "
                          "allow_network, role ('user'/'admin'). Applies to new jobs only.")
    def set_user_limits(ctx: Context, user_id: int, max_cpus: float | None = None,
                        max_mem_mb: int | None = None, max_run_seconds: int | None = None,
                        max_concurrent_jobs: int | None = None, allow_network: bool | None = None,
                        role: str | None = None) -> dict[str, Any]:
        changes = {k: v for k, v in {"max_cpus": max_cpus, "max_mem_mb": max_mem_mb,
                                     "max_run_seconds": max_run_seconds,
                                     "max_concurrent_jobs": max_concurrent_jobs,
                                     "allow_network": allow_network, "role": role}.items()
                   if v is not None}
        if not changes:
            raise ToolError("nothing to change")
        if role is not None and role not in ("user", "admin"):
            raise ToolError("role must be 'user' or 'admin'")
        if (max_cpus is not None and max_cpus <= 0) or (max_mem_mb is not None and max_mem_mb < 32) \
                or (max_run_seconds is not None and max_run_seconds < 1) \
                or (max_concurrent_jobs is not None and max_concurrent_jobs < 1):
            raise ToolError("limits must be positive (max_mem_mb >= 32)")
        return call(ctx, lambda db, a: user_dict(svc.update_user(db, a, user_id, changes)))

    @mcp.tool(description="Suspend a user: disable the account AND kill all of the user's "
                          "queued/running jobs (they end with status killed_by_admin and are "
                          "billed for the time they ran). Re-enable later with set_user_status.",
              annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    def suspend_user(ctx: Context, user_id: int) -> dict[str, Any]:
        return call(ctx, lambda db, a: svc.suspend_user(db, a, user_id))

    @mcp.tool(description="Read-only job metadata (id, user, status, size, times, cost, CPU/memory "
                          "stats) for one user (user_id) or all users. No commands, code or logs.",
              annotations=RO)
    def list_user_jobs(ctx: Context, user_id: int | None = None, status: str | None = None,
                       limit: int = 50) -> list[dict[str, Any]]:
        return call(ctx, lambda db, a: svc.list_user_jobs(db, a, user_id, status, limit))

    @mcp.tool(description="Add credit-hours to a user (negative to deduct). reason: 'topup' or "
                          "'adjust'. Returns the ledger entry with the new balance.")
    def add_credits(ctx: Context, user_id: int, hours: float, reason: str = "topup",
                    note: str | None = None) -> dict[str, Any]:
        return call(ctx, lambda db, a: _jsonable(ledger_out(
            svc.change_credits(db, a, user_id, hours, reason, note))))

    @mcp.tool(description="A user's credit ledger (top-ups, adjustments, job charges), newest first.",
              annotations=RO)
    def get_ledger(ctx: Context, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        return call(ctx, lambda db, a: _jsonable(svc.ledger(db, a, user_id, limit)))

    @mcp.tool(description="Per-user usage totals (jobs, run seconds, CPU-hours, GB-hours, cost) for "
                          "jobs started in [start, end) (UTC, optional).", annotations=RO)
    def usage_report(ctx: Context, start: datetime | None = None,
                     end: datetime | None = None) -> dict[str, Any]:
        return call(ctx, lambda db, a: _jsonable(svc.usage_report(db, a, start, end)))

    @mcp.tool(description="Issue a new access token for a user (shown once).")
    def issue_token(ctx: Context, user_id: int, name: str = "mcp") -> dict[str, Any]:
        def run(db, a):
            tok, raw = svc.issue_token(db, a, user_id, name)
            return {**TokenOut.model_validate(tok).model_dump(mode="json"), "token": raw}
        return call(ctx, run)

    @mcp.tool(description="Revoke an access token by its id (token ids are listed on the admin "
                          "page and by GET /admin/users/{id}/tokens).",
              annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    def revoke_token(ctx: Context, token_id: int) -> dict[str, Any]:
        return call(ctx, lambda db, a: TokenOut.model_validate(
            svc.revoke_token(db, a, token_id)).model_dump(mode="json"))

    return mcp, _asgi(mcp, svc, path, require_admin=True)
