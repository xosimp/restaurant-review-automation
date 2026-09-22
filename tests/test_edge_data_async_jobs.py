"""Async jobs and delayed actions after a restart, a double press, or a crash
mid-handler (DATA audit: DATA-9, DATA-19, DATA-23, DATA-29, DATA-46).

What this protects: schedule generation and the competitor refresh run on
daemon threads inside the web process and report through the `async_jobs`
table; delayed actions (auto-publish, trusted supplier orders) run from the
scheduler through `delayed.run_due`. A deploy kills those threads without
running their finally blocks, owners press Generate twice, and a manager
waits on a 70-person week for longer than ten minutes. After each of those
the owner must see one generation, a real answer, and no Python traceback.

Routes are exercised through the mobile blueprint with a real login, the
same surface the iOS app polls. The model-backed job bodies are stubbed.

Tests without a marker pin behaviour that works today; xfail(strict=True)
tests assert the correct behaviour for a defect the audit confirmed.
"""
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import auth
import client_api
import mobile_api
import models
import ops
from auth import create_user, init_auth
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant

# Imported at collection so each binds the real get_conn, not a test's redirect.
import admin_routes, competitor, delayed, schedule_engine  # noqa: E401,F401


@pytest.fixture(autouse=True)
def _init(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    redirect._edge_redirect = True
    for mod in list(sys.modules.values()):
        g = getattr(mod, "get_conn", None) if mod is not None else None
        if g is real or getattr(g, "_edge_redirect", False):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(mobile_bp)
    return app.test_client()


def _restaurant(db_path, **kw):
    kw.setdefault("module_labor", 1)
    return create_restaurant(Restaurant(name=kw.pop("name", "Async Edge"), owner_email="a@x.test", **kw),
                             db_path=db_path)


def _token(client, db_path, rid, username="owner"):
    create_user(rid, username, f"{username}@x.test", "correct-horse-battery", db_path=db_path)
    body = client.post("/mobile/api/login", json={"username": username, "password": "correct-horse-battery"}).get_json()
    return body["token"]


def _pending_job(db_path, job_id, rid, age_minutes):
    conn = models.get_conn(db_path)
    ops._async_conn().close()
    conn.execute("INSERT INTO async_jobs (job_id, kind, restaurant_id, status, created_at) "
                 "VALUES (?, 'schedule', ?, 'pending', datetime('now', ?))", (job_id, rid, f"-{age_minutes} minutes"))
    conn.commit()
    conn.close()


def _age(db_path, job_id, minutes):
    conn = models.get_conn(db_path)
    conn.execute("UPDATE async_jobs SET created_at=datetime('now', ?) WHERE job_id=?", (f"-{minutes} minutes", job_id))
    conn.commit()
    conn.close()


def _jobs(db_path):
    conn = models.get_conn(db_path)
    rows = [dict(r) for r in conn.execute("SELECT job_id, status FROM async_jobs ORDER BY created_at")]
    conn.close()
    return rows


# ── a job orphaned by a restart (DATA-9) ───────────────────────────────────

def test_an_orphan_older_than_ten_minutes_is_failed_at_boot(db_path):
    _pending_job(db_path, "old-orphan", 3, age_minutes=25)
    assert ops.sweep_stale_jobs() == 1                       # what hosted_dashboard runs at boot
    job = ops.read_async_job("old-orphan", restaurant_id=3)
    assert job["status"] == "error" and "try again" in job["result"]["error"]


def test_a_job_from_a_previous_process_is_failed_at_boot_whatever_its_age(db_path):
    _pending_job(db_path, "zombie", 9, age_minutes=2)
    ops.sweep_stale_jobs()                                   # the new process boots
    assert ops.read_async_job("zombie", restaurant_id=9)["status"] == "error"


def test_generate_after_a_restart_does_not_join_the_dead_job(client, db_path, monkeypatch):
    import schedule_engine
    rid = _restaurant(db_path)
    token = _token(client, db_path, rid)
    started = []
    monkeypatch.setattr(schedule_engine, "_run_schedule_job", lambda job_id, *a, **k: started.append(job_id))
    _pending_job(db_path, "zombie", rid, age_minutes=2)
    ops.sweep_stale_jobs()                                   # boot

    body = client.post("/mobile/api/labor/generate-schedule", json={},
                       headers={"Authorization": f"Bearer {token}"}).get_json()
    assert body["ok"] is True
    assert body.get("job_id") != "zombie" and not body.get("joined"), body
    assert started, "no generation was started — the owner is polling a job nothing will finish"


def test_a_job_whose_result_could_not_be_stored_does_not_poll_pending_forever(db_path, monkeypatch):
    ops.start_async_job("lost-result", "schedule", 4)
    real = models.get_conn

    def refuses(*a, **k):
        conn = real(*a, **k)
        conn.execute("PRAGMA query_only=ON")
        return conn
    monkeypatch.setattr(models, "get_conn", refuses)
    ops.finish_async_job("lost-result", "done", {"ok": True, "schedule_csv": "date,day\n"})
    monkeypatch.setattr(models, "get_conn", real)
    _age(db_path, "lost-result", 60)                         # far past any real generation
    assert ops.read_async_job("lost-result", restaurant_id=4)["status"] == "error"


# ── two presses (DATA-23) ──────────────────────────────────────────────────

def test_two_concurrent_generate_presses_start_one_generation(client, db_path, monkeypatch):
    import schedule_engine
    rid = _restaurant(db_path)
    token = _token(client, db_path, rid)
    started = []
    monkeypatch.setattr(schedule_engine, "_run_schedule_job", lambda job_id, *a, **k: started.append(job_id))
    real_active = ops.active_job
    both_checked = threading.Barrier(2)

    def gated(*a, **k):
        found = real_active(*a, **k)
        try:
            both_checked.wait(timeout=1)     # the web press and the phone press check at the same instant
        except threading.BrokenBarrierError:
            pass
        return found
    monkeypatch.setattr(ops, "active_job", gated)

    app = client.application
    bodies = []

    def press():
        bodies.append(app.test_client().post("/mobile/api/labor/generate-schedule", json={},
                                             headers={"Authorization": f"Bearer {token}"}).get_json())
    threads = [threading.Thread(target=press) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len({b.get("job_id") for b in bodies}) == 1, bodies
    assert len(started) == 1, f"{len(started)} paid generations started for one week"


def test_a_press_during_a_long_live_generation_joins_it(client, db_path, monkeypatch):
    import schedule_engine
    rid = _restaurant(db_path)
    token = _token(client, db_path, rid)
    started, release = [], threading.Event()

    def slow_generation(job_id, *a, **k):
        started.append(job_id)
        release.wait(3)                                      # still running in this process
    monkeypatch.setattr(schedule_engine, "_run_schedule_job", slow_generation)
    try:
        first = client.post("/mobile/api/labor/generate-schedule", json={},
                            headers={"Authorization": f"Bearer {token}"}).get_json()
        _age(db_path, first["job_id"], 12)                   # a 70-person week, twelve minutes in
        second = client.post("/mobile/api/labor/generate-schedule", json={},
                             headers={"Authorization": f"Bearer {token}"}).get_json()
    finally:
        release.set()
    assert second.get("job_id") == first["job_id"] and second.get("joined") is True, second
    assert len(started) == 1


# ── a failed job's error (DATA-46) ─────────────────────────────────────────

def test_a_failed_job_never_returns_a_traceback(client, db_path, monkeypatch):
    import schedule_engine
    rid = _restaurant(db_path)
    token = _token(client, db_path, rid)

    def boom(*a, **k):
        raise RuntimeError("no such column: restaurants.secret_col")
    monkeypatch.setattr(schedule_engine, "_build_schedule_result", boom)
    ops.start_async_job("fails", "schedule", rid)
    schedule_engine._run_schedule_job("fails", rid)

    resp = client.get("/mobile/api/labor/schedule-status/fails", headers={"Authorization": f"Bearer {token}"})
    body = resp.get_json()
    text = resp.get_data(as_text=True)
    assert body["status"] == "error" and body["ok"] is False
    assert "traceback" not in body
    assert "Traceback (most recent call last)" not in text and ".py\", line" not in text


# ── a delayed action killed mid-handler (DATA-19) ──────────────────────────

def test_a_delayed_action_left_running_is_reaped_and_reported(db_path, monkeypatch):
    import delayed
    rid = _restaurant(db_path)
    ran = []
    monkeypatch.setitem(delayed.HANDLERS, "schedule_publish", lambda r, p, d: ran.append(p) or {"ok": True})
    action = delayed.schedule(rid, "schedule_publish", {"schedule_id": 7}, 5, db_path=db_path)
    conn = models.get_conn(db_path)
    # Claimed by run_due, then the deploy SIGTERMed the daemon thread mid-send.
    conn.execute("UPDATE delayed_actions SET status='running', execute_at=? WHERE id=?",
                 ((datetime.now(timezone.utc) - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S"), action["id"]))
    conn.commit()
    conn.close()

    delayed.run_due(db_path=db_path, now=datetime.now(timezone.utc))
    conn = models.get_conn(db_path)
    row = dict(conn.execute("SELECT status, result_json FROM delayed_actions WHERE id=?", (action["id"],)).fetchone())
    conn.close()
    assert row["status"] != "running", "the interrupted action is still 'running' and nobody will ever see it"
    assert row["result_json"], "the interruption was not recorded for the owner or support"
    assert ran == [], "a half-sent publish must not be blindly re-run"


# ── the competitor refresh (DATA-29) ───────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-29: the competitor refresh never checks active_job, so every press "
                                       "starts another paid Places + Claude run")
def test_a_second_competitor_refresh_joins_the_running_one(client, db_path, monkeypatch):
    import competitor
    rid = _restaurant(db_path, module_reviews=1, module_inventory=1, module_marketing=1)
    token = _token(client, db_path, rid)
    runs, release = [], threading.Event()

    def slow_analysis(restaurant_id):
        runs.append(restaurant_id)
        release.wait(3)
        return {"ok": True}
    monkeypatch.setattr(competitor, "run_competitor_analysis", slow_analysis)
    # Each job thread ends by writing its result; wait for those writes so no
    # thread outlives this test's database redirect.
    real_finish, finished = ops.finish_async_job, []
    monkeypatch.setattr(ops, "finish_async_job", lambda *a, **k: (real_finish(*a, **k), finished.append(a[0])))
    try:
        first = client.post("/mobile/api/intel/refresh-competitors",
                            headers={"Authorization": f"Bearer {token}"}).get_json()
        second = client.post("/mobile/api/intel/refresh-competitors",
                             headers={"Authorization": f"Bearer {token}"}).get_json()
    finally:
        release.set()
        waited = threading.Event()
        for _ in range(60):
            if len(finished) >= len(runs):
                break
            waited.wait(0.05)
    assert first["ok"] and second["ok"]
    assert second["job_id"] == first["job_id"], "the second press started a second paid refresh"
    pending = [j for j in _jobs(db_path) if j["status"] == "pending"]
    assert len(pending) <= 1
