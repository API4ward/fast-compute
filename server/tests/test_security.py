"""Auth-failure rate limiting and config defaults."""
from fastapi.testclient import TestClient

from conftest import auth
from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.config import Settings
from dockerserver.models import User


def test_auth_failures_rate_limited(tmp_path):
    s = Settings(data_dir=tmp_path, auth_fail_limit=3, auth_fail_window=60)
    s.logs_dir.mkdir(parents=True)
    app = create_app(s, recover=False)
    with app.state.Session() as db:
        u = User(name="root", role="admin")
        db.add(u)
        db.commit()
        good = issue_token(db, u)[1]
    with TestClient(app) as c:
        assert c.get("/me").status_code == 401          # missing token doesn't count
        for _ in range(3):
            assert c.get("/me", headers=auth("ds_wrong")).status_code == 401
        r = c.get("/me", headers=auth("ds_wrong"))
        assert r.status_code == 429 and r.headers.get("retry-after")
        # while blocked, even a good token from the same IP waits
        assert c.get("/me", headers=auth(good)).status_code == 429
        # MCP endpoint shares the limiter
        assert c.post("/mcp", headers=auth("ds_wrong"), json={}).status_code == 429


def test_default_allowed_hosts():
    s = Settings()
    assert "compute.llmfreedomforge.com" in s.allowed_hosts
    assert "https://compute.llmfreedomforge.com" in s.allowed_origins


def test_project_env_file_is_loaded_and_wins(tmp_path, monkeypatch):
    from dockerserver.config import DEFAULT_ENV_FILE, PROJECT_DIR, load_settings
    assert DEFAULT_ENV_FILE == PROJECT_DIR / ".env"
    env = tmp_path / ".env"
    env.write_text(f"DOCKERSERVER_DATA_DIR={tmp_path}/d\nDOCKERSERVER_DEFAULT_MAX_CPUS=3\n"
                   "DOCKERSERVER_NODES='[{\"name\":\"n1\",\"url\":\"http://x:1\",\"token_env\":\"N1_TOK\"}]'\n"
                   "N1_TOK=\n")
    monkeypatch.setenv("DOCKERSERVER_ENV_FILE", str(env))
    monkeypatch.setenv("DOCKERSERVER_DEFAULT_MAX_CPUS", "9")   # inherited value; .env wins
    # register every key the file sets so monkeypatch restores them after the test
    for k in ("DOCKERSERVER_DATA_DIR", "DOCKERSERVER_NODES", "N1_TOK"):
        monkeypatch.setenv(k, "placeholder")
    s = load_settings()
    assert s.default_max_cpus == 3 and str(s.data_dir) == f"{tmp_path}/d"
    assert s.nodes == [] and s.use_local          # empty token -> node skipped -> local mode


def test_warns_when_token_set_without_nodes(tmp_path, monkeypatch, caplog):
    from dockerserver.config import load_settings
    env = tmp_path / ".env"
    env.write_text(f"DOCKERSERVER_DATA_DIR={tmp_path}/d\nDOCKERSERVER_NODE_TOKEN_MINI=abc\n")
    monkeypatch.setenv("DOCKERSERVER_ENV_FILE", str(env))
    for k in ("DOCKERSERVER_DATA_DIR", "DOCKERSERVER_NODE_TOKEN_MINI", "DOCKERSERVER_NODES"):
        monkeypatch.setenv(k, "")
    with caplog.at_level("WARNING"):
        s = load_settings()
    assert s.nodes == [] and "DOCKERSERVER_NODES is empty" in caplog.text
