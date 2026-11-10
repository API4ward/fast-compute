"""Credit math, ledger, rejection when broke, stop on exhaustion, idempotent billing."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import auth, make_user, needs_docker, wait_done
from dockerserver.api import create_app
from dockerserver.auth import issue_token
from dockerserver.billing import (UH_PER_HOUR, available_uh, balance_total_check, bill_job,
                                  cost_uh, to_uh)
from dockerserver.config import Settings
from dockerserver.models import Job, LedgerEntry, User, utcnow


def test_cost_math():
    assert cost_uh(1, 256, 3600) == UH_PER_HOUR            # the unit
    assert cost_uh(2, 256, 3600) == 2 * UH_PER_HOUR        # CPU-bound size
    assert cost_uh(1, 1024, 3600) == 4 * UH_PER_HOUR       # memory-bound size
    assert cost_uh(0.5, 128, 3600) == UH_PER_HOUR // 2     # smaller than a unit
    assert cost_uh(0.5, 512, 1800) == UH_PER_HOUR          # max(0.5, 2) * 0.5h
    assert cost_uh(1, 256, 1) == round(UH_PER_HOUR / 3600)
    assert cost_uh(1, 256, -5) == 0


class StubRunner:
    def submit(self, *a):
        pass

    def recover(self):
        pass


@pytest.fixture
def stub_app(tmp_path):
    s = Settings(data_dir=tmp_path, default_credit_hours=10)
    s.logs_dir.mkdir(parents=True)
    app = create_app(s, runner=StubRunner(), recover=False)
    with app.state.Session() as db:
        adm = User(name="root", role="admin")
        db.add(adm)
        db.commit()
        app.state.admin_token = issue_token(db, adm)[1]
    return app


def test_initial_grant_topup_and_ledger(stub_app):
    atok = stub_app.state.admin_token
    with TestClient(stub_app) as c:
        u = make_user(c, atok, "eve")
        assert u["balance_hours"] == 10
        r = c.post(f"/admin/users/{u['id']}/credits", json={"hours": 2.5, "note": "paid"}, headers=auth(atok))
        assert r.status_code == 201 and r.json()["balance_after_hours"] == 12.5
        r = c.post(f"/admin/users/{u['id']}/credits", json={"hours": -0.5, "reason": "adjust"}, headers=auth(atok))
        assert r.json()["balance_after_hours"] == 12.0
        assert c.get("/me", headers=auth(u["token"])).json()["balance_hours"] == 12.0
        ledger = c.get(f"/admin/users/{u['id']}/ledger", headers=auth(atok)).json()
        assert [e["reason"] for e in ledger] == ["adjust", "topup", "topup"]
        assert ledger[0]["admin_id"] is not None
        # non-admin can't top up
        r = c.post(f"/admin/users/{u['id']}/credits", json={"hours": 99}, headers=auth(u["token"]))
        assert r.status_code == 403
        with stub_app.state.Session() as db:
            assert balance_total_check(db, u["id"])


def test_reject_when_broke(stub_app):
    atok = stub_app.state.admin_token
    with TestClient(stub_app) as c:
        u = make_user(c, atok, "frank")
        c.post(f"/admin/users/{u['id']}/credits", json={"hours": -10, "reason": "adjust"}, headers=auth(atok))
        r = c.post("/jobs", data={"command": "echo hi"}, headers=auth(u["token"]))
        assert r.status_code == 402
        # 1 minute of 1 CPU/256 MB costs 1/60 h; give a bit more than that
        c.post(f"/admin/users/{u['id']}/credits", json={"hours": 0.02}, headers=auth(atok))
        assert c.post("/jobs", data={"command": "echo hi"}, headers=auth(u["token"])).status_code == 201


def test_idempotent_billing_and_usage(stub_app):
    atok = stub_app.state.admin_token
    with TestClient(stub_app) as c:
        u = make_user(c, atok, "gina")
        now = utcnow()
        with stub_app.state.Session() as db:
            j = Job(user_id=u["id"], image="x", command="x", cpus=2, mem_mb=256, timeout_seconds=60,
                    status="succeeded", started_at=now - timedelta(minutes=30), finished_at=now)
            running = Job(user_id=u["id"], image="x", command="x", cpus=1, mem_mb=256,
                          timeout_seconds=9999, status="running", started_at=now - timedelta(hours=1))
            db.add_all([j, running])
            db.commit()
            assert bill_job(db, j.id) == UH_PER_HOUR          # 2 cores x 0.5 h
            assert bill_job(db, j.id) is None                 # second call is a no-op
            assert db.query(LedgerEntry).filter_by(job_id=j.id).count() == 1
            # available = balance (10 - 1) minus the running job's ~1 h so far
            assert abs(available_uh(db, u["id"]) - to_uh(8)) < to_uh(0.01)
            assert balance_total_check(db, u["id"])
        me = c.get("/me/usage", headers=auth(u["token"])).json()
        assert me["balance_hours"] == 9
        assert me["total"]["jobs"] == 2
        assert me["per_day"] and me["jobs"][0]["cost_hours"] > 0
        adm = c.get("/admin/usage", headers=auth(atok)).json()
        row = [r for r in adm["users"] if r["name"] == "gina"][0]
        assert row["cpu_hours"] == pytest.approx(2.0, abs=0.01)   # 2x0.5 + 1x1
        assert c.get("/admin/usage", headers=auth(u["token"])).status_code == 403


@needs_docker
def test_runs_bill_and_stop_on_exhaustion(tmp_path):
    s = Settings(data_dir=tmp_path, default_credit_hours=1, monitor_interval=1, min_start_seconds=1)
    s.logs_dir.mkdir(parents=True)
    app = create_app(s, recover=False)
    with app.state.Session() as db:
        adm = User(name="root", role="admin")
        db.add(adm)
        db.commit()
        atok = issue_token(db, adm)[1]
    with TestClient(app) as c:
        u = make_user(c, atok, "hank", max_cpus=0.5, max_mem_mb=64, max_run_seconds=60)
        h = auth(u["token"])
        # Leave ~3 s worth of credit at 0.5 CPU (factor 0.5): 3 s * 0.5 / 3600 h
        c.post(f"/admin/users/{u['id']}/credits", json={"hours": -1 + 3 * 0.5 / 3600, "reason": "adjust"},
               headers=auth(atok))
        r = c.post("/jobs", data={"command": "python -c 'while True: pass'", "cpus": "0.5", "mem_mb": "64"},
                   headers=h)
        assert r.status_code == 201, r.text
        job = wait_done(c, u["token"], r.json()["id"], timeout=60)
        assert job["status"] == "out_of_credits", job
        assert job["billed"] and job["cost_hours"] > 0
        assert job["duration_seconds"] < 20
        assert job["stat_samples"] >= 1 and job["cpu_peak_pct"] > 10   # burning CPU
        me = c.get("/me", headers=h).json()
        assert me["balance_hours"] <= 0.0005                           # ~used up (may dip slightly below 0)
        assert c.post("/jobs", data={"command": "echo hi"}, headers=h).status_code == 402
        with app.state.Session() as db:
            assert balance_total_check(db, u["id"])
