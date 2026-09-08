"""Async request-scoped jobs (schedule generation, competitor intel).

From the pre-launch audit: both lived in module-level dicts. Railway runs one
gunicorn worker today so it worked, but the moment a second is added — the
obvious response to load — the poll lands on a worker that never saw the job
and returns "Job not found" forever, throwing away a 30-second Claude call
the client is watching a spinner for. A redeploy mid-job did the same. The
dicts also grew without bound (a job nobody polls) and, because the result
was keyed only by an unguessable UUID, the poll routes couldn't scope by
tenant even where the caller was authenticated.
"""
import json

import pytest
from flask import Flask

import auth
import client_api
import guest_marketing
import mobile_api
import models
import ops
from auth import create_user, init_auth
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant


def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)


@pytest.fixture(autouse=True)
def _init(db_path, monkeypatch):
    _redirect_db(monkeypatch, db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)


def _restaurant(db_path, **kw):
    return create_restaurant(
        Restaurant(name=kw.pop("name", "Async Co"), owner_email=kw.pop("owner_email", "a@x.test"), **kw),
        db_path=db_path,
    )


# ── the store itself ────────────────────────────────────────────────────────

def test_a_pending_job_reads_back_as_pending(db_path):
    ops.start_async_job("job-1", "schedule", 7)
    assert ops.read_async_job("job-1") == {"status": "pending", "result": None}


def test_a_finished_job_hands_back_its_payload(db_path):
    ops.start_async_job("job-2", "schedule", 7)
    ops.finish_async_job("job-2", "done", {"ok": True, "schedule_csv": "date,day\n", "summary": ["a", "b"]})
    job = ops.read_async_job("job-2")
    assert job["status"] == "done"
    assert job["result"]["schedule_csv"] == "date,day\n"
    assert job["result"]["summary"] == ["a", "b"]


def test_a_result_survives_the_worker_that_produced_it(db_path):
    """The whole point: the poll can land on a different process."""
    ops.start_async_job("job-3", "schedule", 7)
    ops.finish_async_job("job-3", "done", {"ok": True})

    import importlib
    fresh = importlib.reload(ops)
    try:
        assert fresh.read_async_job("job-3")["result"] == {"ok": True}
    finally:
        importlib.reload(ops)


def test_reading_a_finished_job_consumes_it(db_path):
    ops.start_async_job("job-4", "schedule", 7)
    ops.finish_async_job("job-4", "done", {"ok": True})
    assert ops.read_async_job("job-4") is not None
    assert ops.read_async_job("job-4") is None, "a consumed result must not linger"


def test_an_unknown_job_is_not_found(db_path):
    assert ops.read_async_job("never-existed") is None


def test_a_job_belongs_to_the_restaurant_that_started_it(db_path):
    ops.start_async_job("job-5", "schedule", 7)
    ops.finish_async_job("job-5", "done", {"ok": True, "schedule_csv": "secret"})
    assert ops.read_async_job("job-5", restaurant_id=8) is None
    assert ops.read_async_job("job-5", restaurant_id=7)["result"]["schedule_csv"] == "secret"


def test_an_unserializable_result_becomes_an_error_not_a_crash(db_path):
    ops.start_async_job("job-6", "schedule", 7)
    ops.finish_async_job("job-6", "done", {"when": object()})
    job = ops.read_async_job("job-6")
    assert job["status"] == "error" and job["result"]["ok"] is False


def test_abandoned_jobs_are_pruned(db_path):
    ops.start_async_job("old-job", "schedule", 7)
    ops.finish_async_job("old-job", "done", {"ok": True})
    conn = models.get_conn()
    conn.execute("UPDATE async_jobs SET created_at = datetime('now','-2 days')")
    conn.commit()
    conn.close()

    ops.start_async_job("new-job", "schedule", 7)  # any start runs the prune
    assert ops.read_async_job("old-job") is None
    assert ops.read_async_job("new-job") is not None


def test_the_admin_console_sees_every_worker_jobs(db_path):
    ops.start_async_job("a", "schedule", 7)
    ops.start_async_job("b", "competitor_intel", 8)
    listed = {j["job_id"]: j["kind"] for j in ops.inflight_async_jobs()}
    assert listed == {"a": "schedule", "b": "competitor_intel"}


# ── the poll routes ─────────────────────────────────────────────────────────

@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


def _login(client, db_path, rid, username="alice"):
    create_user(rid, username, f"{username}@x.test", "correct-horse", db_path=db_path)
    return client.post("/mobile/api/login", json={"username": username, "password": "correct-horse"}).get_json()["token"]


def test_the_phone_poll_returns_a_result_written_by_the_web_worker(app, db_path):
    rid = _restaurant(db_path, module_labor=1)
    client = app.test_client()
    token = _login(client, db_path, rid)
    # Exactly what client_api._run_schedule_job writes, from "another worker".
    ops.start_async_job("shared-job", "schedule", rid)
    ops.finish_async_job("shared-job", "done", {"ok": True, "schedule_csv": "date,day\n", "summary": []})

    resp = client.get("/mobile/api/labor/schedule-status/shared-job",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.get_json()["schedule_csv"] == "date,day\n"


def test_the_phone_poll_will_not_read_another_restaurant_job(app, db_path):
    mine = _restaurant(db_path, name="Mine", module_labor=1)
    theirs = _restaurant(db_path, name="Theirs", owner_email="t@x.test", module_labor=1)
    ops.start_async_job("their-job", "schedule", theirs)
    ops.finish_async_job("their-job", "done", {"ok": True, "schedule_csv": "their staffing"})

    client = app.test_client()
    token = _login(client, db_path, mine, username="me")
    resp = client.get("/mobile/api/labor/schedule-status/their-job",
                      headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


def test_a_pending_job_polls_as_pending_over_http(app, db_path):
    rid = _restaurant(db_path, module_labor=1)
    client = app.test_client()
    token = _login(client, db_path, rid)
    ops.start_async_job("still-going", "schedule", rid)
    body = client.get("/mobile/api/labor/schedule-status/still-going",
                      headers={"Authorization": f"Bearer {token}"}).get_json()
    assert body["status"] == "pending"
