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

from conftest import auth, needs_docker
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


def mcp_call(base, token, fn, path="/mcp"):
    """Open an MCP session with the given bearer token and run `await fn(session)`."""
    async def main():
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with create_mcp_http_client(headers=headers) as http:
            async with streamable_http_client(f"{base}{path}", http_client=http) as (r, w):
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


USER_TOOLS = {"submit_job", "list_jobs", "get_job", "get_job_logs", "stop_job", "get_balance", "get_usage",
              "list_env", "set_env", "delete_env", "put_site_file", "publish_site", "unpublish_site",
              "list_sites", "list_site_files", "delete_site_file", "delete_site"}
ADMIN_TOOLS = {"list_users", "create_user", "set_user_status", "set_user_limits", "suspend_user",
               "list_user_jobs", "add_credits", "get_ledger", "usage_report", "issue_token", "revoke_token",
               "delete_user", "list_sites", "list_nodes", "set_node_draining",
               "lambda_settings", "diagnose_lambda_node", "lambda_function_logs"}


async def _names(s):
    return {t.name for t in (await s.list_tools()).tools}


def test_user_and_admin_services_are_separate(server):
    # /mcp: same 7 own-job tools for everyone, admin included; no admin tools
    assert mcp_call(server["base"], server["user"], _names) == USER_TOOLS
    assert mcp_call(server["base"], server["admin"], _names) == USER_TOOLS
    # /mcp-admin: admin tools only, admin tokens only
    assert mcp_call(server["base"], server["admin"], _names, "/mcp-admin") == ADMIN_TOOLS
    r = httpx.post(f"{server['base']}/mcp-admin", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                   headers={"Authorization": f"Bearer {server['user']}",
                            "Accept": "application/json, text/event-stream"})
    assert r.status_code == 403
    with pytest.raises(BaseException):
        mcp_call(server["base"], server["user"], _names, "/mcp-admin")

    async def balance(s):
        return (await s.call_tool("get_balance", {})).structured_content
    bal = mcp_call(server["base"], server["user"], balance)
    assert bal["balance_hours"] == 1000 and bal["limits"]["max_cpus"] == 0.5

    async def create(s):
        return (await s.call_tool("create_user", {"name": "viv"})).structured_content
    viv = mcp_call(server["base"], server["admin"], create, "/mcp-admin")
    assert viv["token"].startswith("ds_")

    async def limits(s):
        return (await s.call_tool("set_user_limits", {"user_id": viv["id"], "max_cpus": 0.25})).structured_content
    assert mcp_call(server["base"], server["admin"], limits, "/mcp-admin")["max_cpus"] == 0.25

    async def disable(s):
        return (await s.call_tool("set_user_status", {"user_id": viv["id"], "active": False})).structured_content
    assert mcp_call(server["base"], server["admin"], disable, "/mcp-admin")["active"] is False
    with pytest.raises(BaseException):           # disabled token can't even connect
        mcp_call(server["base"], viv["token"], _names)

    async def over_limit(s):
        return await s.call_tool("submit_job", {"command": "true", "cpus": 4})
    res = mcp_call(server["base"], server["user"], over_limit)
    assert res.is_error and "limit" in res.content[0].text


def test_site_tools(server):
    files = {"index.html": "<h1>枪斗术</h1><script src=game.js></script>", "game.js": "let x = '中';"}

    async def run(s):
        out = []
        for path, text in files.items():
            out.append((await s.call_tool("put_site_file", {"site": "gun-fu", "path": path,
                                                             "content": text})).structured_content)
        bad = await s.call_tool("put_site_file", {"site": "gun-fu", "path": "../x", "content": "x"})
        pub = (await s.call_tool("publish_site", {"site": "gun-fu"})).structured_content
        lst = (await s.call_tool("list_sites", {})).structured_content
        fls = (await s.call_tool("list_site_files", {"site": "gun-fu"})).structured_content
        return out, bad, pub, lst, fls
    out, bad, pub, lst, fls = mcp_call(server["base"], server["user"], run)
    assert out[1]["site_files"] == 2 and out[1]["bytes"] == len(files["game.js"].encode())
    assert bad.is_error and "400" in bad.content[0].text
    assert pub["published"] and pub["url"].endswith("/sites/uma/gun-fu/")
    assert lst["result"][0]["site"] == "gun-fu"
    assert sorted(f["path"] for f in fls["result"]) == ["game.js", "index.html"]

    async def admin_list(s):
        return (await s.call_tool("list_sites", {})).structured_content
    rows = mcp_call(server["base"], server["admin"], admin_list, "/mcp-admin")["result"]
    assert rows[0]["user"] == "uma" and rows[0]["file_count"] == 2

    async def dele(s):
        return (await s.call_tool("delete_site", {"site": "gun-fu"})).structured_content
    assert mcp_call(server["base"], server["user"], dele)["deleted"] is True


@needs_docker
def test_submit_job_with_wait(server):
    async def run(s):
        return (await s.call_tool("submit_job", {
            "command": "python main.py", "files": {"main.py": "print('hello mcp')"},
            "cpus": 0.5, "mem_mb": 64, "wait": 60})).structured_content
    out = mcp_call(server["base"], server["user"], run)
    assert out["job"]["status"] == "succeeded", out
    assert out["job"]["image"] == "python:3.12-slim"

    async def listed(s):
        return (await s.call_tool("list_jobs", {})).structured_content["result"]
    assert mcp_call(server["base"], server["user"], listed)[0]["image"] == "python:3.12-slim"
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


@needs_docker
def test_admin_cannot_touch_single_job_but_can_suspend(server):
    base, adm, usr = server["base"], server["admin"], server["user"]
    async def submit(s):
        return (await s.call_tool("submit_job", {"command": "sleep 60", "cpus": 0.5, "mem_mb": 64})).structured_content
    jid = mcp_call(base, usr, submit)["job"]["id"]
    uid = httpx.get(f"{base}/me", headers=auth(usr)).json()["id"]
    for _ in range(60):
        if httpx.get(f"{base}/jobs/{jid}", headers=auth(usr)).json()["status"] == "running":
            break
        time.sleep(0.5)

    # the admin can't stop / read this single job, via MCP or REST
    async def stop(s):
        return await s.call_tool("stop_job", {"job_id": jid})
    assert mcp_call(base, adm, stop).is_error
    async def logs(s):
        return await s.call_tool("get_job_logs", {"job_id": jid})
    assert mcp_call(base, adm, logs).is_error
    assert httpx.post(f"{base}/jobs/{jid}/stop", headers=auth(adm)).status_code == 404
    assert httpx.get(f"{base}/jobs/{jid}/logs", headers=auth(adm)).status_code == 404
    # read-only metadata is visible, without the command
    async def meta(s):
        return (await s.call_tool("list_user_jobs", {"user_id": uid})).structured_content["result"]
    m = mcp_call(base, adm, meta, "/mcp-admin")
    assert m[0]["id"] == jid and m[0]["status"] == "running" and "command" not in m[0]

    # suspend: account disabled + all running containers killed, billed
    async def suspend(s):
        return (await s.call_tool("suspend_user", {"user_id": uid})).structured_content
    out = mcp_call(base, adm, suspend, "/mcp-admin")
    assert out["killed_job_ids"] == [jid] and out["user"]["active"] is False
    m = mcp_call(base, adm, meta, "/mcp-admin")
    assert m[0]["status"] == "killed_by_admin" and m[0]["billed"] and m[0]["cost_hours"] > 0
    import docker
    assert not docker.from_env().containers.list(all=True, filters={"label": f"dockerserver.job={jid}"})
    # blocked: token rejected, so no new submits
    assert httpx.post(f"{base}/jobs", headers=auth(usr), data={"command": "true"}).status_code == 401

    # re-enable -> works again
    async def enable(s):
        return (await s.call_tool("set_user_status", {"user_id": uid, "active": True})).structured_content
    assert mcp_call(base, adm, enable, "/mcp-admin")["active"] is True
    assert httpx.get(f"{base}/me", headers=auth(usr)).status_code == 200
