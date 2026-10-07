"""Integration wave INT-1 — the jobs other workstreams built, registered in
D's registry and run by the loop: C's business metrics and value figures,
H's billing jobs, F's provider probes, E's onboarding nudges and outbox
reapers. Each drives one real scheduler_loop tick with every claim closed
but the one under test, and every job body stubbed to record its call."""
from datetime import datetime

import pytest

# Imported here, at collection, so the modules that bind get_conn at import
# (CLAUDE.md, bound imports) bind the real one.
import admin_ops
import billing_jobs
import jobs_registry
import marketing_publish  # noqa: F401
import models
import morning_brief
import ops
import provider_health
import scheduler
import status_manager
from intelligence import dashboard as intel_dashboard


class _Stop(BaseException):
    pass


@pytest.fixture
def loop(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH

    def redirected(path=None, *a, **k):
        return real(db_path if path in (None, default) else path)
    monkeypatch.setattr(models, "get_conn", redirected)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    calls = []

    def rec(name):
        def fn(*a, **k):
            calls.append((name, k))
            return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
        return fn
    for mod, attr in ((admin_ops, "snapshot_business_metrics"), (intel_dashboard, "snapshot_value_figures"),
                      (provider_health, "run_probes"), (billing_jobs, "run_owed_sends"),
                      (billing_jobs, "run_dunning"), (billing_jobs, "run_contract_chase"),
                      (billing_jobs, "reconcile_stripe"), (scheduler, "run_onboarding_nudges"),
                      (morning_brief, "run_due"), (scheduler, "_minute_duties")):
        monkeypatch.setattr(mod, attr, rec(attr))
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)
    monkeypatch.setattr(scheduler.time, "sleep", lambda s: (_ for _ in ()).throw(_Stop()))

    def tick(now, allow):
        asked = []

        def claim(job, period):
            asked.append((job, period))
            return allow(job, period)
        monkeypatch.setattr(scheduler, "_chi_now", lambda: now)
        monkeypatch.setattr(scheduler._ops, "claim_period", claim)
        del calls[:]
        with pytest.raises(_Stop):
            scheduler.scheduler_loop()
        # Every lane (the Intel one, and since AI cost audit 10/7/26 #6 the
        # AI one): a job started there finishes before the tick is read.
        for _lane in scheduler._LANES.values():
            _lane.join(5)
        return list(calls), asked
    return tick


def _names(calls):
    return [c[0] for c in calls]


def test_the_owed_billing_mail_goes_every_tick(loop):
    calls, _ = loop(datetime(2026, 9, 29, 14, 7), lambda job, period: False)
    assert "run_owed_sends" in _names(calls)


def test_business_metrics_at_1150pm_for_today_and_a_missed_night_before_6am(loop):
    only = lambda job, period: job == "business_metrics"  # noqa: E731
    calls, asked = loop(datetime(2026, 9, 29, 23, 55), only)
    assert ("snapshot_business_metrics", {"day": "2026-09-29"}) in calls
    calls, _ = loop(datetime(2026, 9, 30, 1, 10), only)
    assert ("snapshot_business_metrics", {"day": "2026-09-29"}) in calls, "a missed night is the day it belongs to"
    calls, asked = loop(datetime(2026, 9, 29, 12, 0), only)
    assert "snapshot_business_metrics" not in _names(calls)
    assert not any(job == "business_metrics" for job, _p in asked), "claimed outside its window"


def test_value_figures_run_after_the_outcome_evaluations_on_the_intel_lane(loop, monkeypatch):
    ran_on = []
    lane = scheduler._LANES["intel"]
    real_submit = lane.submit

    def submit(name, fn, *a, **k):
        ran_on.append(name)
        return real_submit(name, fn, *a, **k)
    monkeypatch.setattr(lane, "submit", submit)
    calls, _ = loop(datetime(2026, 9, 29, 6, 5), lambda job, period: job == "value_figures")
    assert ran_on == ["value_figures"] and "snapshot_value_figures" in _names(calls)


def test_the_hourly_and_daily_billing_and_probe_jobs(loop):
    calls, asked = loop(datetime(2026, 9, 29, 10, 20),
                        lambda job, period: job in ("provider_probes", "dunning", "contract_chase", "stripe_reconcile",
                                                    "onboarding_nudges"))
    names = _names(calls)
    for fn in ("run_probes", "run_dunning", "run_contract_chase", "reconcile_stripe"):
        assert fn in names, fn
    assert ("run_onboarding_nudges", {"local_hour": 11}) in calls
    periods = dict(asked)
    assert periods["provider_probes"] == "2026-09-29-10" and periods["dunning"] == "2026-09-29-10"
    assert periods["contract_chase"] == "2026-09-29" and periods["stripe_reconcile"] == "2026-09-29"
    # Before their hour, the daily ones are not even claimed.
    _calls, asked = loop(datetime(2026, 9, 29, 3, 10), lambda job, period: False)
    assert not {"contract_chase", "stripe_reconcile"} & {job for job, _p in asked}


def test_the_new_jobs_are_registered_runnable_and_watched():
    for name, sends in (("business_metrics", False), ("value_figures", False), ("owed_sends", True),
                        ("dunning", True), ("contract_chase", True), ("stripe_reconcile", False),
                        ("provider_probes", True), ("onboarding_nudges", True)):
        spec = jobs_registry.JOBS[name]
        assert spec["runnable"] and spec["sends"] is sends and spec["sla_minutes"], name
        assert name in ops.EXPECTED_JOBS and name in admin_ops.RUNNABLE_JOBS, name
    assert jobs_registry.JOBS["stripe_reconcile"]["sla_minutes"] == 26 * 60
    assert jobs_registry.run_kwargs("onboarding_nudges") == {"local_hour": 11}
    assert jobs_registry.run_kwargs("business_metrics", now=datetime(2026, 9, 29, 23, 51)) == {"day": "2026-09-29"}


def test_the_minute_duties_reap_both_outboxes(monkeypatch):
    import delayed, guest_email, guest_marketing, issues, notify, push, webhooks
    reaped = []
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(delayed, "run_due", lambda *a, **k: {})
    monkeypatch.setattr(issues, "tick", lambda *a, **k: None)
    monkeypatch.setattr(notify, "release_due_alerts", lambda *a, **k: {})
    monkeypatch.setattr(guest_email, "run_newsletter_sends", lambda *a, **k: {})
    monkeypatch.setattr(guest_marketing, "run_campaign_sends", lambda *a, **k: {})
    monkeypatch.setattr(push, "reap_push_outbox", lambda *a, **k: reaped.append("push") or {"submitted": 0})
    monkeypatch.setattr(webhooks, "reap_webhook_outbox", lambda *a, **k: reaped.append("webhooks") or {})
    out = scheduler._minute_duties()
    assert reaped == ["push", "webhooks"] and out["attempted"] == 8 and out["failed"] == 0
    # One reaper failing is captured and counted; the other still runs.
    monkeypatch.setattr(push, "reap_push_outbox", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("pool")))
    captured = []
    monkeypatch.setattr(scheduler._ops, "capture", lambda e, job=None, **k: captured.append(job))
    del reaped[:]
    out = scheduler._minute_duties()
    assert reaped == ["webhooks"] and out["failed"] == 1 and captured == ["push_outbox_reaper"]


# ── retention: the other workstreams' ledgers, and the AI rollup first ─────

def test_the_other_workstreams_ledgers_are_registered_for_retention():
    for table, col, days in (("value_figures_daily", "date", 120), ("admin_issue_resolution_history", "created_at", 400),
                             ("sms_log", "created_at", 90), ("push_outbox", "created_at", 30),
                             ("webhook_outbox", "created_at", 30), ("morning_brief_deliveries", "created_at", 90),
                             ("alert_storm_caps", "started_at", 365), ("login_history", "created_at", 90)):
        assert ops._RETENTION_DAYS[table] == days and ops._RETENTION_COLUMN[table] == col, table
    assert "business_metrics_daily" not in ops._RETENTION_DAYS, "the MRR history is never pruned"
    import auth
    assert ops._RETENTION_DAYS["login_history"] == auth.LOGIN_HISTORY_RETENTION_DAYS, "the boot prune's window"
    # One pruner per table: the platform telemetry is the supervisor's.
    import platform_monitor
    assert not set(platform_monitor.TELEMETRY_RETENTION_DAYS) & set(ops._RETENTION_DAYS)


def _old_ai_row(db_path):
    import sqlite3
    c = sqlite3.connect(db_path)
    try:
        c.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at) "
                  "VALUES (1, 'draft', 'm', 0.01, datetime('now','-400 days'))")
        c.commit()
    finally:
        c.close()


def _ai_rows(db_path):
    import sqlite3
    c = sqlite3.connect(db_path)
    try:
        return c.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0]
    finally:
        c.close()


def test_the_ai_rollup_runs_before_its_raw_rows_are_pruned(db_path, monkeypatch):
    import ai_utils
    order = []
    monkeypatch.setattr(ai_utils, "rollup_usage", lambda db_path=None, **k: order.append("rollup") or {})
    real_delete = ops._chunked_delete

    def spy(conn, table, *a, **k):
        order.append(table)
        return real_delete(conn, table, *a, **k)
    monkeypatch.setattr(ops, "_chunked_delete", spy)
    _old_ai_row(db_path)
    ops.prune_ledgers(db_path)
    assert order[0] == "rollup" and "ai_usage" in order
    assert _ai_rows(db_path) == 0


def test_a_failed_rollup_keeps_the_raw_ai_rows(db_path, monkeypatch):
    import ai_utils
    monkeypatch.setattr(ai_utils, "rollup_usage",
                        lambda db_path=None, **k: (_ for _ in ()).throw(RuntimeError("rollup broke")))
    _old_ai_row(db_path)
    out = ops.prune_ledgers(db_path)
    assert _ai_rows(db_path) == 1, "a raw row went before its day was rolled up"
    assert out["skipped"] >= 2 and "ai_usage" not in out
