"""Fix round D — the scheduler's liveness and the operator's pages.

#4    the heartbeat is its own column, written only by the loop: the liveness
      check and the console's status "Change" no longer reset it.
#121  loop_completed, the running job, and a per-job runtime watchdog.
#27   an SMS channel to WILL_PHONE; the cooldown claimed only after a delivery.
#3 #33 the external dead-man ping at the end of every tick, from the pulse
      while a job is inside its bound and the loop is keeping up, and after
      the backup and the digest; the digest covers overdue jobs and
      everything since the last digest that was sent, and gives its day
      back on failure.
#28 #105 disk and a write probe in the platform SLA, paged without a
      database write.
#35   the Monday operator digest.
"""
import sqlite3
import threading
import time
from datetime import datetime

import pytest

# Imported at collection, so the modules that bind get_conn at import
# (CLAUDE.md, bound imports) bind the real one, never a test's patch.
import admin_ops  # noqa: F401
import marketing_publish  # noqa: F401
import models
import morning_brief  # noqa: F401
import ops
import status_manager


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    # An explicit path other than the module default is honoured: a module
    # that binds get_conn at import (CLAUDE.md, bound imports) and is first
    # imported inside this patch must not carry a path-ignoring copy into
    # the next test.
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn",
                        lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    status_manager.seed_default_services()
    ops._claim_fallback.clear()
    ops._page_memory.clear()
    for v in ("WILL_PHONE", "HEALTHCHECK_PING_URL"):
        monkeypatch.delenv(v, raising=False)
    yield
    ops._page_memory.clear()


def _x(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _q(db_path, sql, args=()):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _beat(db_path, minutes_ago, loop_minutes_ago=None, running=None, running_minutes=None):
    _x(db_path, "UPDATE scheduler_heartbeat SET beat_at=datetime('now', ?), loop_completed_at=datetime('now', ?), "
                "running_job=?, running_since=CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', ?) END WHERE id=1",
       (f"-{minutes_ago} minutes", f"-{loop_minutes_ago if loop_minutes_ago is not None else minutes_ago} minutes",
        running, running, f"-{running_minutes or 0} minutes"))


# ── #4: the heartbeat is the loop's own ─────────────────────────────────────

def test_one_threshold_everywhere():
    import jobs_registry
    assert ops.HEARTBEAT_ALERT_MINUTES == status_manager.SCHEDULER_STALE_MINUTES == jobs_registry.HEARTBEAT_STALE_MINUTES


def test_two_health_checks_on_a_dead_scheduler_both_report_it_stale(db_path, monkeypatch):
    monkeypatch.setattr(ops, "check_platform_sla", lambda **k: {"jobs_overdue": []})
    _beat(db_path, 60)
    first, _ = status_manager.health_snapshot(db_path)
    second, _ = status_manager.health_snapshot(db_path)
    assert first["scheduler"] == "stale" and second["scheduler"] == "stale", \
        "the liveness check reset the heartbeat it measured"
    assert status_manager.scheduler_heartbeat_age_minutes() > 55
    assert _q(db_path, "SELECT status FROM service_status WHERE service_key='scheduler'")[0]["status"] == "outage"


def test_the_consoles_status_change_does_not_reset_the_heartbeat(db_path):
    _beat(db_path, 60)
    status_manager.update_service_status("scheduler", "operational", "set by an admin")
    assert status_manager.scheduler_heartbeat_age_minutes() > 55


def test_only_the_loop_stamps_it(db_path):
    _beat(db_path, 60)
    status_manager.record_scheduler_heartbeat()
    assert status_manager.scheduler_heartbeat_age_minutes() < 1


def test_a_scheduler_that_never_stamped_goes_stale_rather_than_unknown(db_path):
    _x(db_path, "UPDATE scheduler_heartbeat SET beat_at=NULL, created_at=datetime('now','-40 minutes') WHERE id=1")
    assert status_manager.scheduler_heartbeat_age_minutes() > 35


# ── #121: loop completed, the running job, the watchdog ─────────────────────

def test_a_job_past_its_bound_is_wedged(db_path):
    _beat(db_path, 2, loop_minutes_ago=400, running="review_fetch", running_minutes=300)
    st = status_manager.scheduler_state()
    assert st["wedged"] and st["running_job"] == "review_fetch" and not st["loop_stalled"]


def test_a_long_job_inside_its_bound_is_not_a_stall(db_path):
    _beat(db_path, 2, loop_minutes_ago=90, running="review_fetch", running_minutes=90)
    st = status_manager.scheduler_state()
    assert not st["wedged"] and not st["loop_stalled"]


def test_a_tick_failing_part_way_is_a_stalled_loop(db_path):
    _beat(db_path, 2, loop_minutes_ago=60)
    st = status_manager.scheduler_state()
    assert st["loop_stalled"] and not st["stale"]


def test_the_sla_pages_a_wedged_job_and_a_stalled_loop(db_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    sent = []
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: sent.append(lines) or True)
    _beat(db_path, 2, loop_minutes_ago=400, running="review_fetch", running_minutes=300)
    out = ops.check_platform_sla()
    assert out["alerted"] and any("past its" in x and "review_fetch" in x for x in sent[0])
    ops._page_memory.clear()
    _x(db_path, "DELETE FROM job_period_claims WHERE job_key LIKE 'cooldown:%'")
    _beat(db_path, 2, loop_minutes_ago=60)
    ops.check_platform_sla()
    assert any("has not completed a tick" in x for x in sent[-1])


def test_the_pulse_stops_vouching_past_the_bound_and_says_so_once(monkeypatch):
    import scheduler, jobs_registry
    stamps, captured = [], []
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: stamps.append(1))
    monkeypatch.setattr(scheduler, "_minute_duties", lambda: {})
    monkeypatch.setattr(scheduler, "_pulse_interval", lambda: 0.03)
    monkeypatch.setattr(jobs_registry, "max_minutes", lambda name: 0)
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append((str(e), k.get("context"))))
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    pulsed = scheduler._PulsedOps()
    pulsed.run_job("wedge_test", lambda: time.sleep(0.2))
    assert stamps == [], "a job past its bound kept the heartbeat fresh"
    assert [c for c in captured if c[1] == "watchdog"] == [
        ("wedge_test has run past its 0-minute bound", "watchdog")]


def _pinging_pulse(monkeypatch, bound_minutes=60):
    import scheduler, jobs_registry
    pings = []
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "_minute_duties", lambda: {})
    monkeypatch.setattr(scheduler, "_pulse_interval", lambda: 0.03)
    monkeypatch.setattr(jobs_registry, "max_minutes", lambda name: bound_minutes)
    monkeypatch.setattr(ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(ops, "ping_healthcheck", lambda status="ok": pings.append(status) or True)
    return scheduler._PulsedOps(), pings


def test_a_long_job_inside_its_bound_keeps_the_monitor_fed(monkeypatch):
    """A review fetch or diagnoses pass inside its bound is a live
    scheduler; pinged only at the end of a tick, it paged a healthy
    platform for as long as the job ran (#3)."""
    pulsed, pings = _pinging_pulse(monkeypatch)
    pulsed.begin_tick()                       # the process's first tick
    pulsed.run_job("long_test", lambda: time.sleep(0.2))
    assert pings, "the pulse vouched for the loop but the monitor heard nothing"
    pings.clear()
    pulsed.tick_completed()
    assert pings == ["ok"]
    pulsed.begin_tick()                       # the next tick, right after
    pings.clear()
    pulsed.run_job("long_test", lambda: time.sleep(0.2))
    assert pings


def test_a_tick_failing_part_way_silences_the_pulse(monkeypatch):
    pulsed, pings = _pinging_pulse(monkeypatch)
    pulsed.begin_tick()                       # tick 1 never completes ...
    pulsed.begin_tick()                       # ... and tick 2 starts
    pulsed.run_job("long_test", lambda: time.sleep(0.2))
    assert pings == [], "a loop failing part-way kept the dead-man monitor quiet"
    pulsed.became_runner()                    # a fresh runner starts clean
    pulsed.begin_tick()
    pulsed.run_job("long_test", lambda: time.sleep(0.2))
    assert pings


def test_a_job_past_its_bound_silences_the_pulse(monkeypatch):
    pulsed, pings = _pinging_pulse(monkeypatch, bound_minutes=0)
    pulsed.begin_tick()
    pulsed.run_job("wedge_test", lambda: time.sleep(0.2))
    assert pings == []


def test_a_real_tick_stamps_loop_completed_and_pings(db_path, monkeypatch):
    """Drive the loop: every gate closed, the heartbeat NOT stubbed."""
    import scheduler, marketing_publish, morning_brief
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda *a, **k: False)
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(morning_brief, "run_due", lambda *a, **k: {"attempted": 0})
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)
    pings = []
    monkeypatch.setattr(ops, "ping_healthcheck", lambda status="ok": pings.append(status) or True)
    _beat(db_path, 60)

    class _Stop(BaseException):
        pass
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        scheduler.scheduler_loop()
    st = status_manager.scheduler_state()
    assert st["beat_age_minutes"] < 1 and st["loop_completed_age_minutes"] < 1 and st["running_job"] is None
    assert pings == ["ok"]
    jobs = {r["job"] for r in _q(db_path, "SELECT job FROM job_runs")}
    assert {"morning_brief", "minute_duties"} <= jobs, "the brief and the minute duties write job runs (#31)"


# ── #27 / #105: paging ─────────────────────────────────────────────────────

def test_alert_will_texts_will_phone(db_path, monkeypatch):
    import notify
    monkeypatch.setenv("WILL_PHONE", "+15555550100")
    texts = []
    monkeypatch.setattr(notify, "send_sms", lambda to, body, use_case="alert", **k: texts.append((to, body)) or True)
    res = ops.alert_will("Cavnar AI: test", ["the scheduler stopped"])
    assert bool(res) and res.channels["sms"] is True
    assert texts and texts[0][0] == "+15555550100" and "the scheduler stopped" in texts[0][1]


def test_a_queued_push_alone_is_not_a_delivery(db_path, monkeypatch):
    import auth, push
    auth.init_auth(db_path)
    rid = models.create_restaurant(models.Restaurant(name="Admin Home", owner_email="a@x.test"), db_path=db_path)
    uid = auth.create_user(rid, "will", "will@x.test", "pw", db_path=db_path)
    _x(db_path, "UPDATE users SET is_admin=1, is_active=1 WHERE id=?", (uid,))
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: 3)
    res = ops.alert_will("Cavnar AI: test", ["x"])
    assert not res and res.channels["push"] is True


def test_the_cooldown_is_claimed_only_after_a_delivery(db_path, monkeypatch):
    outcomes = [False, False, True, True]
    calls = []
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: calls.append(1) or outcomes[len(calls) - 1])
    assert ops.page_operator("k", "s", ["l"])["sent"] is False
    assert ops.page_operator("k", "s", ["l"])["sent"] is False, "a failed page used up the cooldown"
    assert ops.page_operator("k", "s", ["l"])["sent"] is True
    assert ops.page_operator("k", "s", ["l"]) == {"sent": False, "reason": "cooldown", "channels": {}}
    assert len(calls) == 3
    last = ops.last_operator_alert()
    assert last["key"] == "k" and last["sent"] == 1


def test_the_cooldown_holds_in_memory_when_the_database_cannot_be_written(db_path, monkeypatch):
    monkeypatch.setattr(ops, "alert_will", lambda s, l: True)
    boom = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("database or disk is full"))
    monkeypatch.setattr(models, "get_conn", boom)
    assert ops.page_operator("full", "s", ["l"])["sent"] is True
    assert ops.page_operator("full", "s", ["l"])["reason"] == "cooldown", "a full disk paged on every request"


def test_disk_and_a_refused_write_are_platform_problems(db_path, monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(status_manager, "disk_state", lambda p=None: {"state": "critical", "free_mb": 20, "pct_free": 1})
    monkeypatch.setattr(ops, "write_probe", lambda *a, **k: (False, "database is locked"))
    _beat(db_path, 1)
    sent = []
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: sent.append(lines) or True)
    out = ops.check_platform_sla()
    assert out["alerted"] and out["write_ok"] is False
    assert any("almost full" in x for x in sent[0]) and any("refuses writes" in x for x in sent[0])


def test_the_write_probe_sees_a_held_lock(db_path):
    assert ops.write_probe(db_path) == (True, None)
    holder = sqlite3.connect(db_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        ok, err = ops.write_probe(db_path, timeout=0.1)
        assert ok is False and "locked" in err
    finally:
        holder.rollback()
        holder.close()


# ── #3 / #33: the dead-man ping and the digest ──────────────────────────────

def test_the_ping_is_optional_short_and_never_raises(monkeypatch):
    import requests
    assert ops.ping_healthcheck() is False                     # unset: nothing
    monkeypatch.setenv("HEALTHCHECK_PING_URL", "https://hc-ping.example/abc")
    seen = []
    monkeypatch.setattr(requests, "get", lambda url, timeout=None: seen.append((url, timeout)) or
                        type("R", (), {"status_code": 200})())
    assert ops.ping_healthcheck() is True and ops.ping_healthcheck("fail") is True
    assert seen == [("https://hc-ping.example/abc", (3, 5)), ("https://hc-ping.example/abc/fail", (3, 5))]
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert ops.ping_healthcheck() is False


def test_a_job_new_to_this_database_is_not_overdue_until_its_sla_has_passed(db_path):
    """A never-run job was measured from the oldest run of ANY job — on a
    live database 45 days back — so the first /health after the deploy that
    added the Monday operator digest read it as weeks overdue and paged
    every hour until Monday (#31)."""
    assert len(_q(db_path, "SELECT job FROM job_expected_since")) == len(ops.EXPECTED_JOBS)
    _x(db_path, "INSERT INTO job_runs (job, started_at, finished_at, ok) VALUES "
                "('review_fetch', datetime('now','-40 days'), datetime('now','-40 days'), 1)")
    _x(db_path, "UPDATE job_expected_since SET since=datetime('now','-10 minutes')")
    late = {j["job"] for j in ops.jobs_overdue()}
    assert "operator_weekly_digest" not in late and "morning_brief" not in late
    assert "review_fetch" in late, "a job that ran, 40 days ago, is still overdue"
    _x(db_path, "UPDATE job_expected_since SET since=datetime('now','-2 hours') WHERE job='morning_brief'")
    assert "morning_brief" in {j["job"] for j in ops.jobs_overdue()}, "expected 2h ago, an hour's SLA, never ran"
    # A job that has run and only ever failed keeps the old base.
    _x(db_path, "INSERT INTO job_runs (job, started_at, finished_at, ok) VALUES "
                "('pos_sync', datetime('now','-39 days'), datetime('now','-39 days'), 0)")
    assert "pos_sync" in {j["job"] for j in ops.jobs_overdue()}


def test_the_digest_includes_overdue_jobs(db_path, monkeypatch):
    import emails
    _x(db_path, "INSERT INTO job_runs (job, started_at, finished_at, ok) VALUES "
                "('pos_sync', datetime('now','-30 hours'), datetime('now','-30 hours'), 1)")
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: sent.update(payload) or
                        emails.SendResult(True, attempts=1))
    out = ops.send_failure_digest()
    assert out["sent"] and out["overdue"] >= 1
    assert "pos_sync" in sent["html"] and "overdue" in sent["subject"]


def test_the_digest_covers_everything_since_the_last_one_sent(db_path, monkeypatch):
    import emails
    _x(db_path, "INSERT INTO job_cursors (key, value) VALUES ('ops_failure_digest_sent_at', datetime('now','-3 days'))")
    _x(db_path, "INSERT INTO job_failures (job, error, created_at) VALUES ('missed_job', 'boom', datetime('now','-40 hours'))")
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: sent.update(payload) or
                        emails.SendResult(True, attempts=1))
    ops.send_failure_digest()
    assert "missed_job" in sent["html"], "a failure from a day whose digest never went out was lost"
    cursor = _q(db_path, "SELECT value FROM job_cursors WHERE key='ops_failure_digest_sent_at'")[0]["value"]
    assert cursor > (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")[:10])


def test_a_digest_that_did_not_send_raises_and_pings_fail(db_path, monkeypatch):
    import emails
    ops.capture(RuntimeError("x"), job="something")
    monkeypatch.setattr(emails, "deliver", lambda **k: emails.SendResult(False, error="Resend down", attempts=2))
    pings = []
    monkeypatch.setattr(ops, "ping_healthcheck", lambda status="ok": pings.append(status))
    with pytest.raises(ops.DigestNotSent):
        ops.send_failure_digest()
    assert pings == ["fail"]
    assert not _q(db_path, "SELECT 1 FROM job_cursors WHERE key='ops_failure_digest_sent_at'")


def test_the_loop_gives_the_digest_day_back_when_it_failed(db_path, monkeypatch):
    import scheduler, marketing_publish, morning_brief
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 9, 29, 8, 5))
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    real_claim = ops.claim_period
    monkeypatch.setattr(scheduler._ops, "claim_period",
                        lambda job, period: real_claim(job, period) if job.startswith("ops_digest") else False)
    monkeypatch.setattr(ops, "send_failure_digest", lambda: (_ for _ in ()).throw(ops.DigestNotSent("down")))
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(morning_brief, "run_due", lambda *a, **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)

    class _Stop(BaseException):
        pass
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))
    with pytest.raises(_Stop):
        scheduler.scheduler_loop()
    assert not ops.period_claimed("ops_digest", "2026-09-29"), "the day's digest was lost to one failed send"
    assert ops.period_claimed("ops_digest_retry", "2026-09-29#0")


def test_the_weekly_operator_digest(db_path, monkeypatch):
    import admin_ops, emails
    monkeypatch.setattr(admin_ops, "overview", lambda: {"kpis": {"clients": 4, "attention": 2, "mrr": 1396},
                                                         "issues": [{"restaurant": "A", "title": "Past due"}]})
    monkeypatch.setattr(admin_ops, "clients", lambda: {"clients": [
        {"name": "Risky Grill", "churn": {"level": "high", "score": 80, "reasons": ["no logins in 20 days"]},
         "onboarding": {"complete": False, "next_step": "Connect Google"}}]})
    sent = {}
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: sent.update(payload) or
                        emails.SendResult(True, attempts=1))
    out = ops.send_operator_weekly_digest()
    assert out["ok"] == 1
    assert "Risky Grill" in sent["html"] and "Connect Google" in sent["html"] and "Past due" in sent["html"]
    monkeypatch.setattr(emails, "deliver", lambda **k: emails.SendResult(False, error="x", attempts=1))
    with pytest.raises(RuntimeError):
        ops.send_operator_weekly_digest()


# ── #17: a DSR night missing is a platform problem ──────────────────────────

def test_a_missing_dsr_night_pages(db_path, monkeypatch):
    import scheduler
    from dsr import pipeline
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setattr(pipeline, "nights_missing", lambda db_path=None: [
        {"restaurant_id": 5, "restaurant": "Simple EJ's", "business_date": "2026-09-28", "status": "missing"}])
    _beat(db_path, 1)
    sent = []
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: sent.append(lines) or True)
    assert ops.check_platform_sla()["alerted"]
    assert any("Daily Sales Report missing" in x and "Simple EJ's" in x for x in sent[0])
