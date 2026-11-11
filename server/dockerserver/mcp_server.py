"""MCP interface (Streamable HTTP) served by the same FastAPI process at /mcp.

Auth: the same access tokens as the REST API (`Authorization: Bearer <token>`).
An ASGI wrapper rejects missing/invalid/disabled tokens with 401 before the MCP
app sees the request; each tool then resolves the calling user from the request
headers and goes through the same Services layer as the REST API, so
permissions, limits and credit checks are identical. Admin tools are hidden
from non-admins in tools/list and refused if called anyway.
"""
import json
import time
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ListToolsResult, ToolAnnotations

from .auth import lookup_user
from .models import TERMINAL_STATUSES, Job
from .schemas import JobOut, UserOut, ledger_out
from .services import ServiceError, Services

ADMIN_TOOLS = {"list_users", "create_user", "add_credits", "usage_report"}

PRICING = ("Pricing: 1 credit-hour = 1 CPU core + 256 MB RAM for 1 hour of wall-clock time; a job "
           "costs run_seconds/3600 * max(cpus, mem_mb/256) credit-hours (charged for the reserved "
           "size, not actual use).")

INSTRUCTIONS = f"""DockerServer runs your code in an isolated, throw-away Docker container.
Each job: fresh container, read-only root filesystem, writable /workspace (your files / git
checkout, also the working directory), small /tmp, runs as an unprivileged user, no network
unless your account allows it and you ask for it, killed at its timeout.
Defaults: image python:3.12-slim, 1 CPU, 256 MB, your max run time. Call get_balance to see your
limits and remaining credits. {PRICING} A job is refused unless you can afford 60 s at its size,
and is stopped (status out_of_credits) if your credits run out while it runs."""


def _bearer(headers) -> str | None:
    if not headers:
        return None
    value = headers.get("authorization") or headers.get("Authorization") or ""
    scheme, _, token = value.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


class _DockerServerMCP(MCPServer):
    """Hides admin-only tools from non-admin callers in tools/list."""

    def __init__(self, svc: Services, **kw):
        super().__init__(**kw)
        self._svc = svc

    async def _handle_list_tools(self, ctx, params) -> ListToolsResult:
        result = await super()._handle_list_tools(ctx, params)
        token = _bearer(getattr(getattr(ctx, "request", None), "headers", None))
        is_admin = False
        if token:
            with self._svc.Session() as db:
                u = lookup_user(db, token)
                is_admin = bool(u and u.is_admin)
        if not is_admin:
            result.tools = [t for t in result.tools if t.name not in ADMIN_TOOLS]
        return result


class _BearerAuth:
    """ASGI wrapper: 401 unless the request carries a valid, active token."""

    def __init__(self, app, svc: Services):
        self.app, self.svc = app, svc

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            client = (scope.get("client") or ("?", 0))[0]
            err = None
            with self.svc.Session() as db:
                try:
                    self.svc.authenticate(db, _bearer(headers), client)
                except ServiceError as e:
                    err = e
            if err is not None:
                body = json.dumps({"detail": err.message}).encode()
                extra = [(b"www-authenticate", b"Bearer")] if err.status == 401 else [(b"retry-after", b"60")]
                await send({"type": "http.response.start", "status": err.status,
                            "headers": [(b"content-type", b"application/json")] + extra})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_mcp(svc: Services):
    """Returns (mcp_server, asgi_app). Caller must run mcp.session_manager.run() in its lifespan."""
    mcp = _DockerServerMCP(svc, name="dockerserver", instructions=INSTRUCTIONS)

    def call(ctx: Context, fn, admin: bool = False):
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

    def job_dict(job) -> dict[str, Any]:
        return JobOut.model_validate(job).model_dump(mode="json")

    ro = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

    # ---------------- user tools ----------------
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
            # runner.wait can return before the thread exists; poll status until terminal
            while time.monotonic() < deadline:
                with svc.Session() as db:
                    if db.get(Job, job_id).status in TERMINAL_STATUSES:
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
                          "running, succeeded, failed, timeout, stopped, out_of_credits, error). "
                          "Admins may set all_users=true.", annotations=ro)
    def list_jobs(ctx: Context, status: str | None = None, limit: int = 20,
                  all_users: bool = False) -> list[dict[str, Any]]:
        return call(ctx, lambda db, u: [job_dict(j) for j in svc.list_jobs(db, u, all_users, status, limit)])

    @mcp.tool(description="Get one job: status, exit code, times, cost_hours (once finished), "
                          "and CPU/memory usage samples.", annotations=ro)
    def get_job(ctx: Context, job_id: int) -> dict[str, Any]:
        return call(ctx, lambda db, u: job_dict(svc.get_job(db, u, job_id)))

    @mcp.tool(description="Get a job's combined stdout/stderr (live while running). "
                          "`tail` = last N lines (default 200, 0 = everything kept, max 1 MB).",
              annotations=ro)
    def get_job_logs(ctx: Context, job_id: int, tail: int = 200) -> str:
        return call(ctx, lambda db, u: svc.logs(db, u, job_id, tail if tail > 0 else None))

    @mcp.tool(description="Stop (kill) a running job. It is charged for the time it ran.",
              annotations=ToolAnnotations(destructiveHint=True, openWorldHint=False))
    def stop_job(ctx: Context, job_id: int) -> dict[str, Any]:
        return call(ctx, lambda db, u: job_dict(svc.stop_job(db, u, job_id)))

    @mcp.tool(description="My credit balance (credit-hours), available credit (balance minus what "
                          "my running jobs have used so far) and my per-job limits. " + PRICING,
              annotations=ro)
    def get_balance(ctx: Context) -> dict[str, Any]:
        return call(ctx, lambda db, u: {"user": u.name, **svc.balance(db, u)})

    @mcp.tool(description="My usage and spending: totals and per-day totals (jobs, run seconds, "
                          "CPU-hours, GB-hours, cost in credit-hours) for jobs started in "
                          "[start, end) (ISO dates/times, UTC, optional). include_jobs adds the "
                          "per-job list.", annotations=ro)
    def get_usage(ctx: Context, start: datetime | None = None, end: datetime | None = None,
                  include_jobs: bool = False) -> dict[str, Any]:
        return call(ctx, lambda db, u: _jsonable(svc.my_usage(db, u, start, end, include_jobs)))

    # ---------------- admin tools ----------------
    @mcp.tool(description="[admin] List all users with role, limits, active flag and balance.",
              annotations=ro)
    def list_users(ctx: Context) -> list[dict[str, Any]]:
        return call(ctx, lambda db, u: [UserOut.model_validate(x).model_dump(mode="json")
                                        for x in svc.list_users(db, u)], admin=True)

    @mcp.tool(description="[admin] Create a user and return its first access token (shown once). "
                          "Optional limits: max_cpus, max_mem_mb, max_run_seconds, "
                          "max_concurrent_jobs, allow_network. New users get the default credit "
                          "grant.")
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
            return {**UserOut.model_validate(u).model_dump(mode="json"), "token": raw}
        return call(ctx, run, admin=True)

    @mcp.tool(description="[admin] Add credit-hours to a user (negative to deduct). reason: "
                          "'topup' or 'adjust'. Returns the ledger entry with the new balance.")
    def add_credits(ctx: Context, user_id: int, hours: float, reason: str = "topup",
                    note: str | None = None) -> dict[str, Any]:
        return call(ctx, lambda db, u: _jsonable(ledger_out(
            svc.change_credits(db, u, user_id, hours, reason, note))), admin=True)

    @mcp.tool(description="[admin] Per-user usage totals (jobs, run seconds, CPU-hours, GB-hours, "
                          "cost) for jobs started in [start, end) (UTC, optional).", annotations=ro)
    def usage_report(ctx: Context, start: datetime | None = None,
                     end: datetime | None = None) -> dict[str, Any]:
        return call(ctx, lambda db, u: _jsonable(svc.usage_report(db, u, start, end)), admin=True)

    # DNS-rebinding protection: only the configured Host headers (and matching Origins) are
    # accepted (DOCKERSERVER_ALLOWED_HOSTS), e.g. the public tunnel hostname plus localhost.
    st = svc.settings
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=st.allowed_hosts, allowed_origins=st.allowed_origins))
    return mcp, _BearerAuth(app, svc)


def _jsonable(obj):
    return json.loads(json.dumps(obj, default=lambda o: o.isoformat() if isinstance(o, datetime) else str(o)))

