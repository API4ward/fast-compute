import time

import docker
import pytest
from fastapi.testclient import TestClient

from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.config import Settings
from dockerserver.models import TERMINAL_STATUSES, User


def docker_available():
    try:
        docker.from_env().ping()
        return True
    except Exception:
        return False


needs_docker = pytest.mark.skipif(not docker_available(), reason="Docker not reachable")


@pytest.fixture
def app(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.logs_dir.mkdir(parents=True, exist_ok=True)
    return create_app(s, recover=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_token(app):
    with app.state.Session() as db:
        u = User(name="root", role="admin", max_cpus=1.0, max_mem_mb=256, max_run_seconds=60)
        db.add(u)
        db.commit()
        return issue_token(db, u)[1]


def auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def make_user(client, admin_token, name, **limits):
    r = client.post("/admin/users", json={"name": name, **limits}, headers=auth(admin_token))
    assert r.status_code == 201, r.text
    return r.json()


def wait_done(client, tok, job_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/jobs/{job_id}", headers=auth(tok)).json()
        if j["status"] in TERMINAL_STATUSES and j["billed"]:
            return j
        time.sleep(0.5)
    raise AssertionError(f"job {job_id} did not finish: {j}")
