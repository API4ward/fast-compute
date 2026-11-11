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
