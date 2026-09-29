"""Integration wave INT-1 — ops and platform wiring between the workstreams:
a job run's log and AI context (F #38, G #148), the traceback a capture
logs, the platform check paging on broken messaging (E), the morning
brief's retry (E #82), the AI ledger's view-as attribution (A), and the
worker's boot (F #108). The pre-shift text's restaurant (E #14) is tested
beside the nudge's own tests, in tests/test_workflow_queue.py."""
import logging
import sqlite3

import pytest

import ai_utils
import models
import ops


@pytest.fixture
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn",
                        lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    return db_path


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


# ── a job run carries its name into the log and its attribution into the AI ledger

def test_a_job_run_is_attributed_to_the_scheduler_with_its_own_correlation_id(db):
    seen = {}

    def job():
        import logging_setup
        seen["ai"] = ai_utils.current_ai_context()
        seen["log"] = logging_setup.current()
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    ops.run_job("attr_job", job)
    run_id = _q(db, "SELECT id FROM job_runs WHERE job='attr_job'")[0]["id"]
    assert seen["ai"]["trigger"] == "scheduler" and seen["ai"]["correlation_id"] == f"job:attr_job:{run_id}"
    assert seen["log"]["job"] == "attr_job"
    assert ai_utils.current_ai_context() == {}, "the context leaked out of the run"


def test_an_admins_run_keeps_the_admins_attribution(db):
    seen = {}

    def job():
        seen.update(ai_utils.current_ai_context())
        return True
    with ai_utils.ai_context(trigger="admin", actor_user_id=7, correlation_id="run_now:x:1"):
        ops.run_job("admin_job", job)
    assert (seen["trigger"], seen["actor_user_id"], seen["correlation_id"]) == ("admin", 7, "run_now:x:1")


def test_a_captured_exception_logs_its_traceback_redacted(db, caplog):
    def boom():
        raise RuntimeError("403 for url: https://maps.googleapis.com/x?key=AIzaSySECRET123456789012345")
    with caplog.at_level(logging.WARNING, logger="ops"):
        try:
            boom()
        except RuntimeError as e:
            ops.capture(e, job="places_thing", context="restaurant_id=4")
    rec = next(r for r in caplog.records if r.getMessage().startswith("captured places_thing failure"))
    assert "Traceback" in rec.traceback and "boom" in rec.traceback and rec.restaurant_id == 4
    assert "AIzaSySECRET" not in rec.traceback and "AIzaSySECRET" not in rec.getMessage()


# ── the platform check pages on broken messaging (E) ───────────────────────

def test_the_platform_check_pages_on_broken_messaging_but_health_does_not_read_it(db, monkeypatch):
    import notify
    import scheduler
    import status_manager
    monkeypatch.setattr(status_manager, "DB_PATH", db)
    status_manager.record_scheduler_heartbeat(loop_completed=True)
    monkeypatch.setattr(status_manager, "disk_state", lambda p=None: {"state": "ok"})
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(ops, "backup_status", lambda db_path=None: {"state": "ok"})
    asked = []
    monkeypatch.setattr(notify, "messaging_problems",
                        lambda db_path=None: asked.append(1) or ["Twilio delivery reports webhook: 3 refused"])
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, **k: pages.append(lines) or {"sent": True})
    quiet = ops.check_platform_sla(send=False)
    assert asked == [] and quiet["messaging"] == [] and not quiet["alerted"]
    out = ops.check_platform_sla(send=True)
    assert out["alerted"] and any(line.startswith("Messaging: Twilio") for line in pages[0])


# ── the morning brief gives its day back on a transient failure (E #82) ────

def test_a_brief_that_failed_transiently_is_retried_a_few_times(db, monkeypatch):
    import morning_brief
    rid = models.create_restaurant(models.Restaurant(name="Brief Co", owner_email="b@x.test"), db_path=db)
    monkeypatch.setattr(morning_brief, "_brief_queue", lambda db_path=None: ([(rid, "2026-09-29")], []))
    monkeypatch.setattr(morning_brief, "deliver", lambda *a, **k: {"sent": 0, "failed": 1, "retry": True})
    outs = [morning_brief.run_due(db_path=db) for _ in range(morning_brief.BRIEF_RETRIES_PER_DAY + 1)]
    assert [o["failed"] for o in outs[:-1]] == [1] * morning_brief.BRIEF_RETRIES_PER_DAY
    assert outs[-1]["ok"] == 1 and outs[-1]["failed"] == 0, "retried past its daily allowance"
    assert ops.period_claimed(f"morning_brief:{rid}", "2026-09-29")


# ── the AI ledger's actor for a view-as request (A) ────────────────────────

def test_a_view_as_request_is_the_acting_admins(db, monkeypatch):
    import auth
    from flask import Flask
    auth.init_auth(db_path=db)
    hq = models.create_restaurant(models.Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db)
    admin = auth.create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db)
    rid = models.create_restaurant(models.Restaurant(name="Client Grill", owner_email="o@x.test"), db_path=db)
    owner = auth.create_user(rid, "owner", "o@x.test", "Owner-pass-2026", db_path=db)
    token = auth.create_view_as_session(owner, {"id": admin}, read_only=False, db_path=db)
    app = Flask(__name__)
    with app.test_request_context("/api/reviews", headers={"Cookie": f"session_token={token}"}):
        trigger, actor, got_rid = ai_utils._request_attribution()
    assert (trigger, actor, got_rid) == ("admin", admin, rid)


# ── the worker boots like the web process (F #108, #38) ────────────────────

def test_the_worker_checks_the_volume_and_configures_logging_before_anything():
    import inspect
    import worker
    src = inspect.getsource(worker.main)
    assert src.index("logging_setup.configure()") < src.index("models.require_volume()") < src.index("init_db()")
    assert "basicConfig" not in inspect.getsource(worker)


# ── the system card's size trend reads the nightly record (F #28 meets D) ──

def test_the_system_cards_size_trend_reads_the_nightly_record(db):
    import platform_monitor
    assert platform_monitor._size_trend(db)["source"] == "boots", "no nightly record yet"
    mb = 1024 * 1024
    c = sqlite3.connect(db)
    c.execute("INSERT INTO backup_runs (started_at, db_bytes, wal_bytes, backups_bytes, free_bytes) "
              "VALUES (datetime('now','-2 days'), ?, 0, 0, ?)", (100 * mb, 10 * 1024 * mb))
    c.execute("INSERT INTO backup_runs (started_at, db_bytes, wal_bytes, backups_bytes, free_bytes) "
              "VALUES (datetime('now'), ?, 0, 0, ?)", (110 * mb, 10 * 1024 * mb))
    c.commit()
    c.close()
    out = platform_monitor._size_trend(db)
    assert out["source"] == "daily" and out["points"][-1]["db_mb"] == 110.0
    assert out["growth_mb_per_day"] == 5.0 and out["days_to_full"]


# ── the operator's weekly digest reads the console's real record keys (D #35 meets C) ──

def test_the_weekly_digest_reads_churn_risk_and_the_next_onboarding_step(db, monkeypatch):
    import admin_ops
    import emails
    monkeypatch.setattr(admin_ops, "overview", lambda: {"kpis": {"clients": 2}, "issues": []})
    monkeypatch.setattr(admin_ops, "clients", lambda: {"clients": [
        {"name": "Risky Grill", "segment": "customer",
         "churn_risk": {"level": "high", "points": 7, "reasons": ["no logins in 20 days"], "scored": True},
         "onboarding": {"complete": True}},
        {"name": "New Bistro", "segment": "customer", "churn_risk": {"level": "n/a"},
         "onboarding": {"complete": False, "steps": [{"label": "Contract signed", "done": True},
                                                     {"label": "Connect Google", "done": False}]}},
        {"name": "Demo Co", "segment": "internal", "churn_risk": {"level": "high", "points": 9, "reasons": ["x"]},
         "onboarding": {"complete": False, "steps": []}}]})
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: sent.update(payload) or
                        emails.SendResult(True, attempts=1))
    ops.send_operator_weekly_digest()
    assert "Risky Grill" in sent["html"] and "no logins in 20 days" in sent["html"]
    assert "New Bistro" in sent["html"] and "Connect Google" in sent["html"]
    assert "Demo Co" not in sent["html"]
