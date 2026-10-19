"""The hourly pages of 10/19/26 (owner: "noti's from all this every hour on text and email and push")."""
import inspect

import ops


def test_quality_calibration_runs_on_the_queue_and_no_longer_asks_a_removed_switch():
    import job_queue
    import strategy_jobs
    assert not hasattr(job_queue, "enabled")
    fn = strategy_jobs.quality_calibration_pass()           # raised AttributeError every Sunday since 10/11/26
    assert callable(fn) and fn is not strategy_jobs.run_quality_calibration
    assert "if not job_queue.enabled()" not in inspect.getsource(strategy_jobs.quality_calibration_pass)


def _sla(monkeypatch, overdue, hb=1):
    import scheduler
    pages = []
    monkeypatch.setattr(ops, "jobs_overdue", lambda **k: overdue)
    monkeypatch.setattr(ops, "page_operator", lambda key, subject, lines, cooldown_minutes=60: pages.append((key, cooldown_minutes)) or {"sent": True})
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    src = inspect.getsource(ops.check_platform_sla)
    assert "platform_sla_overdue:" in src
    return pages


def test_a_weekly_job_overdue_pages_once_a_day_not_every_hour(monkeypatch, db_path):
    pages = _sla(monkeypatch, [{"job": "quality_calibration", "hours_since": 199, "max_hours": 192}])
    out = ops.check_platform_sla(send=True, db_path=db_path, write_ok=True)
    keyed = [p for p in pages if p[0].startswith("platform_sla_overdue:")]
    if out.get("heartbeat_minutes") is None or out["heartbeat_minutes"] <= ops.HEARTBEAT_ALERT_MINUTES:
        assert keyed == [("platform_sla_overdue:quality_calibration", 24 * 60)], pages


def test_an_hourly_job_overdue_still_pages_hourly(monkeypatch, db_path):
    pages = _sla(monkeypatch, [{"job": "review_fetch", "hours_since": 3, "max_hours": 1}])
    out = ops.check_platform_sla(send=True, db_path=db_path, write_ok=True)
    if out.get("heartbeat_minutes") is None or out["heartbeat_minutes"] <= ops.HEARTBEAT_ALERT_MINUTES:
        assert ("platform_sla_overdue:review_fetch", 60) in pages, pages


def test_the_daily_review_fetch_never_asks_google_about_the_showcase():
    import scheduler
    src = inspect.getsource(scheduler.run_daily_fetch)
    assert "_demo_store.is_simulated(r[\"id\"])" in src
