"""MCP over real HTTP: uvicorn in a thread + the official SDK client."""
import socket
import threading
import time

import anyio
import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from conftest import auth, make_user, needs_docker
from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.config import Settings
from dockerserver.models import User


@pytest.fixture
def server(tmp_path):
    s = Settings(data_dir=tmp_path, monitor_interval=2, allowed_hosts=["127.0.0.1:*"])
    s.logs_dir.mkdir(parents=True)
    app = create_app(s, recover=False)
    with app.state.Session() as db:
        adm = User(name="root", role="admin", max_cpus=1, max_mem_mb=256, max_run_seconds=60)
        db.add(adm)
        db.commit()
        atok = issue_token(db, adm)[1]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    t = threading.Thread(target=srv.run, kwargs={"sockets": [sock]}, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    user = httpx.post(f"{base}/admin/users", headers=auth(atok),
                      json={"name": "uma", "max_cpus": 0.5, "max_mem_mb": 128, "max_run_seconds": 30}).json()
    yield {"base": base, "admin": atok, "user": user["token"]}
    srv.should_exit = True
    t.join(10)
    sock.close()


def mcp_call(base, token, fn):
    """Open an MCP session with the given bearer token and run `await fn(session)`."""
    async def main():
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with create_mcp_http_client(headers=headers) as http:
            async with streamable_http_client(f"{base}/mcp", http_client=http) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    return await fn(session)
    return anyio.run(main)


def test_bad_token_rejected(server):
    r = httpx.post(f"{server['base']}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    r = httpx.post(f"{server['base']}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Authorization": "Bearer ds_nope", "Accept": "application/json, text/event-stream"})
    assert r.status_code == 401
    with pytest.raises(BaseException):
        mcp_call(server["base"], "ds_nope", lambda s: s.list_tools())


def test_tools_listing_and_admin_gating(server):
    async def names(s):
        return {t.name for t in (await s.list_tools()).tools}
    user_tools = mcp_call(server["base"], server["user"], names)
    admin_tools = mcp_call(server["base"], server["admin"], names)
    base = {"submit_job", "list_jobs", "get_job", "get_job_logs", "stop_job", "get_balance", "get_usage"}
    admin = {"list_users", "create_user", "add_credits", "usage_report"}
    assert base <= user_tools and not (admin & user_tools)
    assert base | admin <= admin_tools

    async def try_admin(s):
        return await s.call_tool("list_users", {})
    res = mcp_call(server["base"], server["user"], try_admin)
    assert res.is_error if hasattr(res, "is_error") else res.isError

    async def balance(s):
        return (await s.call_tool("get_balance", {})).structured_content
    bal = mcp_call(server["base"], server["user"], balance)
    assert bal["balance_hours"] == 1000 and bal["limits"]["max_cpus"] == 0.5

    async def create(s):
        return (await s.call_tool("create_user", {"name": "viv"})).structured_content
    assert mcp_call(server["base"], server["admin"], create)["token"].startswith("ds_")

    async def over_limit(s):
        return await s.call_tool("submit_job", {"command": "true", "cpus": 4})
    res = mcp_call(server["base"], server["user"], over_limit)
    assert res.is_error and "limit" in res.content[0].text


@needs_docker
def test_submit_job_with_wait(server):
    async def run(s):
        return (await s.call_tool("submit_job", {
            "command": "python main.py", "files": {"main.py": "print('hello mcp')"},
            "cpus": 0.5, "mem_mb": 64, "wait": 60})).structured_content
    out = mcp_call(server["base"], server["user"], run)
    assert out["job"]["status"] == "succeeded", out
    assert "hello mcp" in out["logs_tail"]
    assert out["job"]["cost_hours"] is not None

    async def usage(s):
        return (await s.call_tool("get_usage", {})).structured_content
    assert mcp_call(server["base"], server["user"], usage)["total"]["jobs"] == 1


def test_mcp_rejects_unknown_host(server):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    h = {"Authorization": f"Bearer {server['admin']}", "Accept": "application/json, text/event-stream"}
    ok = httpx.post(f"{server['base']}/mcp", json=body, headers=h)
    assert ok.status_code == 200
    bad = httpx.post(f"{server['base']}/mcp", json=body, headers={**h, "Host": "evil.example.com"})
    assert bad.status_code in (400, 403, 421)
    bad_origin = httpx.post(f"{server['base']}/mcp", json=body, headers={**h, "Origin": "https://evil.example.com"})
    assert bad_origin.status_code in (400, 403)
