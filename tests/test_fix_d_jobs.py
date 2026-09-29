"""Fix round D — the job registry, Run now and the loop's guards.

#31 #40 #56  one registry drives the SLAs, RUNNABLE_JOBS and the Jobs page,
             and the loop and the registry cannot drift.
#39          every scheduled function returns the standard counts (or raises).
#9 #153 #64  Run now refuses sending jobs off the production scheduler, hands
             the job to the scheduler process, and the loop never starts a
             job that is already running; the opt-in invite run uses each
             restaurant's own day.
#56          a failed non-sending job gives its period back, with backoff.
#40          the Jobs payload: per-job history, three outcome states, stuck by
             each job's own bound.
#98          the weekly Intel sweeps run on a lane beside the loop.
"""
import importlib
import inspect
import re
import sqlite3
import threading
import time
from datetime import datetime

import pytest

# Imported here, at collection, so the modules that bind get_conn at import
# (CLAUDE.md, bound imports) bind the real one — never a test's patch, which
# monkeypatch would then "restore" into every later test.
import admin_ops
import admin_routes
import jobs_registry
import marketing_publish  # noqa: F401
import models
import morning_brief  # noqa: F401
import ops
import scheduler
import status_manager

_LOOP_SRC = inspect.getsource(scheduler.scheduler_loop)


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(admin_ops, "get_conn", redirected)
    monkeypatch.setattr(admin_routes, "get_conn", redirected)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    return db_path


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


# ── the registry and the loop (#31, #40) ────────────────────────────────────

def _loop_job_names():
    names = set(re.findall(r'run_job\("([a-z_]+)"', _LOOP_SRC))
    names |= set(re.findall(r'run_in_lane\("[a-z]+", "([a-z_]+)"', _LOOP_SRC))
    return names


def test_every_job_the_loop_runs_is_registered_and_every_registered_job_runs():
    loop = _loop_job_names()
    # minute_duties runs from the pulse and the loop's run_duties helper.
    assert loop - set(jobs_registry.JOBS) == set(), "a job the loop runs has no registry entry"
    assert set(jobs_registry.JOBS) - loop - {"minute_duties"} == set(), "a registered job is never run"


def test_every_registry_entry_is_complete_and_its_target_exists():
    for name, spec in jobs_registry.JOBS.items():
        for field in ("cadence", "sla_minutes", "sends", "runnable", "label", "description"):
            assert field in spec, (name, field)
        mod, fn = spec["target"]
        assert callable(getattr(importlib.import_module(mod), fn)), (name, spec["target"])
        if spec.get("sla_minutes"):
            assert spec["sla_minutes"] >= 60, name


def test_every_job_the_loop_runs_has_an_sla_unless_it_is_quarterly():
    no_sla = {n for n, s in jobs_registry.JOBS.items() if not s.get("sla_minutes")}
    assert no_sla == {"restore_drill", "quarterly_summaries"}
    assert set(ops.EXPECTED_JOBS) == set(jobs_registry.JOBS) - no_sla
    assert ops.EXPECTED_JOBS["pos_sync"] == 26 and ops.EXPECTED_JOBS["review_fetch"] == 16


def test_runnable_jobs_come_from_the_registry():
    import admin_ops
    runnable = {n for n, s in jobs_registry.JOBS.items() if s.get("runnable")}
    assert set(admin_ops.RUNNABLE_JOBS) == runnable
    assert len(runnable) > 50
    for name in ("daily_alerts", "competitor_analysis", "prune_ledgers", "food_cost_diagnoses", "morning_brief"):
        assert name in admin_ops.RUNNABLE_JOBS, name
    assert admin_ops.RUNNABLE_JOBS["review_fetch"]["sends"] is True


def test_the_opt_in_invite_run_uses_each_restaurants_own_day():
    assert jobs_registry.run_kwargs("toast_optin_invites") == {}
    assert "business_date" not in inspect.getsource(__import__("admin_ops").run_job_now)
    assert jobs_registry.run_kwargs("onboarding_emails") == {"local_hour": 10}
    assert set(jobs_registry.run_kwargs("auto_draft_schedule", now=datetime(2026, 9, 24, 6))) == {"now"}


# ── #39: the standard counts ────────────────────────────────────────────────

# Scheduled functions other workstreams own; the integration wave brings
# them onto the counts (their shapes are read by ops.standard_counts until).
_PENDING = {"run_weekly_digests", "run_onboarding_sequence", "run_monthly_summaries",
            "run_quarterly_summaries", "run_features", "run_learning", "run_reservation_sync",
            "run_toast_optin_invites", "run_review_request_followups", "run_campaign_attribution"}
_KEYS = {"attempted", "ok", "failed", "skipped", "hit_bound"}


def test_every_scheduled_function_returns_the_standard_counts_or_raises(db, monkeypatch):
    for v in ("META_APP_ID", "META_APP_SECRET", "BACKUP_ENCRYPTION_KEY", "RESEND_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    import emails
    monkeypatch.setattr(emails, "deliver", lambda **k: emails.SendResult(False, error="no key", attempts=0))
    monkeypatch.setenv("BACKUP_DIR", str(__import__("pathlib").Path(db).parent / "backups"))
    checked = []
    for name, spec in jobs_registry.JOBS.items():
        mod, fn_name = spec["target"]
        if fn_name in _PENDING:
            continue
        fn = getattr(importlib.import_module(mod), fn_name)
        try:
            out = fn(**jobs_registry.run_kwargs(name, now=datetime(2026, 9, 29, 9, 0)))
        except Exception:
            continue                                   # "or raises"
        assert isinstance(out, dict) and _KEYS <= set(out), (name, out)
        checked.append(name)
    assert len(checked) >= 40, checked


# ── #9 / #153 / #64: Run now ────────────────────────────────────────────────

def test_run_now_refuses_a_sending_job_off_the_production_scheduler(db, monkeypatch):
    import admin_ops
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    out = admin_ops.run_job_now("weekly_digests", "will")
    assert out["ok"] is False and out["status"] == 409 and "not the production scheduler" in out["error"]
    assert not _q(db, "SELECT 1 FROM job_run_requests")


def test_run_now_route_answers_409_with_the_sentence(db, monkeypatch):
    import auth
    from flask import Flask
    from admin_routes import admin_bp
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": None, "is_admin": 1,
                                                            "username": "will", "email": "w@x.com", "role": "admin"})
    r = app.test_client().post("/admin/api/jobs/onboarding_emails/run")
    assert r.status_code == 409 and "Resend and Twilio" in r.get_json()["error"] and "status" not in r.get_json()


def test_on_railway_run_now_is_queued_for_the_scheduler_and_the_loop_runs_it(db, monkeypatch):
    import admin_ops, marketing_publish, morning_brief
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    ran = []
    monkeypatch.setattr(scheduler, "run_toast_sync", lambda: ran.append(threading.current_thread().name) or
                        {"attempted": 1, "ok": 1, "failed": 0, "skipped": 0, "hit_bound": False})
    out = admin_ops.run_job_now("pos_sync", "will")
    assert out["ok"] and out["queued"] and out["request_id"]
    assert ran == [], "Run now ran on the web thread instead of in the scheduler"
    assert admin_ops.run_job_now("pos_sync", "will")["request_id"] == out["request_id"], "a double press joins"
    # One real tick: every gate closed; the loop takes the request.
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda *a, **k: False)
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(morning_brief, "run_due", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)

    class _Stop(BaseException):
        pass
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        scheduler.scheduler_loop()
    assert len(ran) == 1
    req = _q(db, "SELECT status, ok FROM job_run_requests WHERE id=?", (out["request_id"],))[0]
    assert req == {"status": "done", "ok": 1}
    run = _q(db, "SELECT context, request_id, ok FROM job_runs WHERE job='pos_sync'")[0]
    assert run == {"context": "manual by will", "request_id": out["request_id"], "ok": 1}


def test_a_stale_request_is_expired_not_run_late(db):
    _x(db, "INSERT INTO job_run_requests (job, requested_by, requested_at) VALUES ('pos_sync', 'will', datetime('now','-2 hours'))")
    assert ops.take_job_requests() == []
    assert _q(db, "SELECT status FROM job_run_requests")[0]["status"] == "expired"


def test_the_loop_never_starts_a_job_that_is_already_running(db, monkeypatch):
    monkeypatch.setattr(scheduler, "record_running_job", lambda *a, **k: None)
    pulsed = scheduler._PulsedOps()
    assert pulsed.claim_period("backup_db", "2026-09-29") is True
    _x(db, "INSERT INTO job_runs (job, started_at, owner, pulse_at) VALUES "
           "('backup_db', datetime('now','-1 minutes'), 'another-process', datetime('now'))")
    ran = []
    assert pulsed.run_job("backup_db", lambda: ran.append(1)) is None
    assert ran == []
    assert not ops.period_claimed("backup_db", "2026-09-29"), "the period goes back for a later tick"


def test_a_failed_non_sending_job_gives_its_period_back_after_a_backoff(db, monkeypatch):
    monkeypatch.setattr(scheduler, "record_running_job", lambda *a, **k: None)
    clock = {"t": 1000.0}
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: clock["t"])
    pulsed = scheduler._PulsedOps()
    assert pulsed.claim_period("pos_sync", "2026-09-29") is True
    pulsed.run_job("pos_sync", lambda: (_ for _ in ()).throw(RuntimeError("POS API down")))
    assert pulsed.claim_period("pos_sync", "2026-09-29") is False, "retried before its backoff"
    clock["t"] += jobs_registry.RETRY_BACKOFF_MINUTES[0] * 60 + 1
    assert pulsed.claim_period("pos_sync", "2026-09-29") is True, "a failed nightly job waited a whole day"
    pulsed.run_job("pos_sync", lambda: {"attempted": 2, "ok": 2, "failed": 0})
    assert ("pos_sync", "2026-09-29") not in pulsed._retries


def test_a_sending_job_is_never_retried(db, monkeypatch):
    monkeypatch.setattr(scheduler, "record_running_job", lambda *a, **k: None)
    pulsed = scheduler._PulsedOps()
    assert pulsed.claim_period("stale_inventory", "2026-09-28") is True
    pulsed.run_job("stale_inventory", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert pulsed._retries == {} and ops.period_claimed("stale_inventory", "2026-09-28")


# ── #98: the Intel lane ─────────────────────────────────────────────────────

def test_the_weekly_sweeps_run_on_a_lane_beside_the_loop(db, monkeypatch):
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    gate, seen = threading.Event(), {}

    def sweep():
        seen["thread"] = threading.current_thread().name
        gate.wait(2)
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    pulsed = scheduler._PulsedOps()
    assert pulsed.run_in_lane("intel", "competitor_analysis", sweep) is True
    time.sleep(0.05)
    assert pulsed.run_in_lane("intel", "ai_visibility", sweep) is False, "one job at a time on a lane"
    gate.set()
    scheduler._LANES["intel"].join(3)
    assert seen["thread"] == "scheduler-lane-intel"
    assert _q(db, "SELECT ok FROM job_runs WHERE job='competitor_analysis'")[0]["ok"] == 1


def test_the_loop_puts_the_weekly_sweeps_on_the_lane_and_releases_a_busy_claim():
    assert 'run_in_lane("intel", "competitor_analysis", run_weekly_competitor_analysis)' in _LOOP_SRC
    assert 'run_in_lane("intel", "ai_visibility", run_weekly_ai_visibility)' in _LOOP_SRC
    assert '_ops.release_period("competitor_analysis", _iso_week)' in _LOOP_SRC
    assert '_ops.run_job("competitor_analysis"' not in _LOOP_SRC


# ── #40: the Jobs payload ───────────────────────────────────────────────────

def test_the_jobs_payload_has_per_job_history_and_three_outcome_states(db, monkeypatch):
    import admin_ops
    for i, ok in enumerate((1, 2, 0)):
        _x(db, "INSERT INTO job_runs (job, started_at, finished_at, ok) VALUES "
               "('pos_sync', datetime('now', ?), datetime('now', ?), ?)", (f"-{3 - i} hours", f"-{3 - i} hours", ok))
    for i in range(20):
        _x(db, "INSERT INTO job_runs (job, started_at, finished_at, ok) VALUES "
               "('dsr_sweep', datetime('now', ?), datetime('now', ?), 2)", (f"-{i} minutes", f"-{i} minutes"))
    _x(db, "INSERT INTO job_runs (job, started_at) VALUES ('dsr_delivery', datetime('now','-100 minutes'))")
    _x(db, "INSERT INTO job_runs (job, started_at) VALUES ('review_fetch', datetime('now','-100 minutes'))")
    out = admin_ops.jobs()
    rows = {j["job"]: j for j in out["jobs"]}
    assert set(rows) == set(jobs_registry.JOBS)
    pos = rows["pos_sync"]
    assert [r["state"] for r in pos["history"]] == ["failed", "partial", "ok"] and pos["state"] == "failed"
    assert len(rows["dsr_sweep"]["history"]) == admin_ops.JOB_HISTORY_RUNS
    assert rows["dsr_sweep"]["state"] == "partial" and rows["dsr_sweep"]["last_ok_at"], \
        "a job whose runs are all partial read 'never ran'"
    assert rows["dsr_delivery"]["state"] == "stuck" and rows["review_fetch"]["state"] == "running"
    assert {s["job"] for s in out["stuck"]} == {"dsr_delivery"}
    for key in ("heartbeat", "lease", "backup", "storage", "requests", "operator_alert", "missed_windows",
                "dsr_missing", "local_sends_refused"):
        assert key in out, key
    assert out["heartbeat"]["stale_after_minutes"] == jobs_registry.HEARTBEAT_STALE_MINUTES


def test_one_jobs_history_route(db, monkeypatch):
    import auth
    from flask import Flask
    from admin_routes import admin_bp
    for _ in range(3):
        _x(db, "INSERT INTO job_runs (job, started_at, finished_at, ok, context) VALUES "
               "('backup_db', datetime('now'), datetime('now'), 2, 'manual by will')")
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": None, "is_admin": 1,
                                                            "username": "will", "email": "w@x.com", "role": "admin"})
    c = app.test_client()
    d = c.get("/admin/api/jobs/backup_db/runs?limit=2").get_json()
    assert len(d["runs"]) == 2 and d["runs"][0]["state"] == "partial" and d["runs"][0]["manual"]
    assert c.get("/admin/api/jobs/nope/runs").status_code == 404
    assert c.get("/admin/api/backup").get_json()["status"]["state"] == "never"
