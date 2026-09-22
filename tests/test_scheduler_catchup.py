"""Catch-up gating in scheduler.py (audit #17, P0-2).

Daily jobs were gated on `now.hour == H`, so a tick that missed the hour — a
long job ahead of it in the sequential loop, a deploy, a crash — meant the
job silently did not run that day. These pin the replacement: eligible from
the hour onward, still at most once per day, and in an order that respects
the dependencies the hours were quietly encoding.
"""
import pytest
import pathlib
import re
from datetime import datetime

import scheduler


def _at(hour, minute=0, day=18):
    return datetime(2026, 9, day, hour, minute)


# ── _due ───────────────────────────────────────────────────────────────────

def test_a_job_is_not_due_before_its_hour():
    assert not scheduler._due(_at(5, 59), 6)


def test_a_job_is_due_in_its_hour():
    assert scheduler._due(_at(6, 0), 6)


def test_a_job_missed_in_its_hour_is_still_due_later_that_day():
    """The whole point: late, not never."""
    assert scheduler._due(_at(9, 30), 6)
    assert scheduler._due(_at(23, 59), 6)


def test_an_upper_bound_closes_the_window():
    assert scheduler._due(_at(19, 59), 11, until=20)
    assert not scheduler._due(_at(20, 0), 11, until=20)


def test_the_guest_invite_window_closes_before_guest_quiet_hours():
    """A run inside quiet hours defers every invite but still spends the day's
    claim, and tomorrow's run scans a different business date — so those
    guests would never be invited."""
    from guest_marketing import GUEST_SMS_LATEST_HOUR
    assert scheduler.OPTIN_INVITE_LATEST_HOUR < GUEST_SMS_LATEST_HOUR


# ── _latest_slot ───────────────────────────────────────────────────────────

SLOTS = (8, 12, 16, 20)


def test_no_fetch_slot_before_the_first():
    assert scheduler._latest_slot(_at(7, 59), SLOTS) is None


def test_the_slot_in_progress_is_the_one_claimed():
    assert scheduler._latest_slot(_at(12, 5), SLOTS) == 12


def test_a_missed_slot_is_caught_up_as_the_latest_one_only():
    """An outage over 8 and 12 must produce ONE fetch at 13:00, not two back
    to back — claiming the latest slot does that, and the earlier one is
    never separately claimed."""
    assert scheduler._latest_slot(_at(13, 0), SLOTS) == 12


def test_the_last_slot_holds_until_midnight():
    assert scheduler._latest_slot(_at(23, 30), SLOTS) == 20


# ── the loop itself ────────────────────────────────────────────────────────

_SRC = pathlib.Path(scheduler.__file__).read_text()


def _loop_body():
    start = _SRC.index("def scheduler_loop():")
    end = _SRC.index("def start_scheduler():")
    return _SRC[start:end]


def test_no_daily_job_is_gated_on_an_exact_hour_any_more():
    """Asserted against the source: a rule that must hold for every gate
    cannot be covered by exercising one of them."""
    body = _loop_body()
    code = "\n".join(line.split("#")[0] for line in body.splitlines())
    assert "now.hour ==" not in code
    assert "now.hour in (" not in code


def _claim_position(job):
    m = re.search(r'claim_period\("%s"' % re.escape(job), _loop_body())
    assert m, f"no claim for {job}"
    return m.start()


def test_a_catch_up_tick_runs_dependent_jobs_in_dependency_order():
    """Under catch-up several jobs can come due in one tick, and they run in
    the order they appear in the loop. The digest used to sit ABOVE the
    diagnoses it reads, so a late morning would have sent yesterday's."""
    order = [
        "pos_sync",              # 3am  — POS data in
        "inventory_depletion",   # 5am  — reads the synced sales
        "food_cost_snapshots",   # 5am  — written from post-depletion numbers
        "food_cost_diagnoses",   # 6am  — reads the snapshot
        "weekly_digest",         # 9am  — reads both diagnoses
    ]
    positions = [_claim_position(j) for j in order]
    assert positions == sorted(positions), dict(zip(order, positions))


def test_review_diagnoses_precede_the_digest():
    assert _claim_position("review_diagnoses") < _claim_position("weekly_digest")


def test_every_daily_job_keeps_its_once_per_day_claim():
    """Catch-up widens WHEN a job may run, never HOW OFTEN — the per-day
    claim key is what stops a widened gate re-running it every tick."""
    body = _loop_body()
    for job in ("backup_db", "pos_sync", "inventory_depletion", "food_cost_snapshots",
                "review_diagnoses", "food_cost_diagnoses", "ops_digest", "optin_invite",
                "marketing_metrics_sync"):
        assert re.search(r'claim_period\("%s", str\(today\)\)' % job, body), job


def test_the_token_refresh_is_hourly_with_a_per_restaurant_daily_cap():
    """DATA-49: a refresh that failed at 7am used to wait a full day. The job
    is attempted hourly; each restaurant's Graph calls are capped per day by
    their own claim, so the hourly look never becomes an hourly call."""
    import inspect, scheduler
    assert re.search(r'claim_period\("refresh_tokens", f"\{today\}-\{now\.hour\}"\)', _loop_body())
    src = inspect.getsource(scheduler.refresh_expiring_tokens)
    assert 'claim_period(f"refresh_tokens_attempt:{r.id}"' in src
    assert "TOKEN_REFRESH_ATTEMPTS_PER_DAY" in src


def test_owner_facing_jobs_are_attempted_hourly_and_claimed_per_restaurant():
    """Anything an owner reads runs at that hour in the RESTAURANT's
    timezone, so the loop has to look every hour and the once-a-day guard
    moves inside, per restaurant (scheduler.local_due / notify._gated_out)."""
    body = _loop_body()
    for job in ("weekly_digest", "daily_alerts", "monthly_summary", "onboarding"):
        assert re.search(r'claim_period\("%s", f"\{today\}-\{now\.hour\}"\)' % job, body), job
    import scheduler, notify, inspect
    assert "claim_key=\"weekly_digest\"" in inspect.getsource(scheduler.run_weekly_digests)
    assert "claim_key=\"monthly_summary\"" in inspect.getsource(scheduler.run_monthly_summaries)
    assert "claim_key=\"onboarding\"" in inspect.getsource(scheduler.run_onboarding_sequence)
    assert "run_onboarding_sequence, local_hour=10" in body
    assert "local_hour=10" in inspect.getsource(scheduler.run_daily_alert_checks)
    for fn in (notify.check_daily_alerts, notify.check_extra_daily_alerts,
               notify.check_no_response_alerts):
        assert "_gated_out(" in inspect.getsource(fn), fn.__name__


# ── once a MONTH means once a month ───────────────────────────────────────────

def test_local_due_can_be_pinned_to_one_day_of_the_month(monkeypatch):
    """The hourly loop plus a per-day claim only ever made the summaries
    once a DAY. `day=` is the calendar half of the gate, judged on the
    restaurant's own date."""
    claims = []
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda job, period: claims.append((job, period)) or True)
    r = type("R", (), {"id": 4, "timezone": "America/Chicago"})()
    assert scheduler.local_due(r, 9, claim_key="monthly_summary", day=1,
                               now_local=datetime(2026, 9, 2, 9, 5)) is False
    assert claims == []                                    # not even a claim spent
    assert scheduler.local_due(r, 9, claim_key="monthly_summary", day=1,
                               now_local=datetime(2026, 9, 1, 8, 59)) is False
    assert scheduler.local_due(r, 9, claim_key="monthly_summary", day=1,
                               now_local=datetime(2026, 9, 1, 9, 0)) is True
    assert claims == [("monthly_summary:4", "2026-09-01")]
    assert scheduler.local_due(r, 9, claim_key="daily_thing",
                               now_local=datetime(2026, 9, 2, 9, 5)) is True   # no day: unchanged


def test_the_monthly_and_quarterly_summaries_are_gated_to_the_first():
    """Regression for the daily 'month in review' (lost with the loop's
    `now.day == 1` in 964c77a): both summary jobs must pass day=1, so the
    gate lives beside the claim and cannot be dropped with the loop again."""
    import inspect
    assert 'local_due(r, 9, claim_key="monthly_summary", day=1)' in inspect.getsource(scheduler.run_monthly_summaries)
    assert 'local_due(r, 9, claim_key="quarterly_summary", day=1)' in inspect.getsource(scheduler.run_quarterly_summaries)


def test_the_monthly_job_sends_nothing_on_any_day_but_the_first(monkeypatch):
    import emails
    import models
    r = type("R", (), {})()
    r.id = 1; r.name = "R"; r.owner_email = "o@x.com"; r.owner_name = "O"
    r.billing_status = "active"; r.monthly_review_enabled = 1
    r.module_reviews = r.module_labor = r.module_inventory = r.module_marketing = 1
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: [r])
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda *a, **k: True)
    # The real gate, with the clock pinned to 9:05 local on the 2nd — any
    # day that is not the 1st.
    real_local_due = scheduler.local_due
    monkeypatch.setattr(scheduler, "local_due",
                        lambda rr, hour, until=14, claim_key=None, now_local=None, day=None:
                            real_local_due(rr, hour, until, claim_key, datetime(2026, 9, 2, 9, 5), day))
    sent = []
    monkeypatch.setattr(emails, "send_monthly_summary_email", lambda **kw: sent.append(kw["restaurant_id"]))
    monkeypatch.setattr(scheduler, "_push_month_ready", lambda rr: None)
    out = scheduler.run_monthly_summaries()
    assert sent == [] and out["sent"] == 0 and out["skipped"] == 1


# ── the loop itself has to survive a tick ─────────────────────────────────────

class _StopLoop(BaseException):
    """Raised from the stubbed sleep to leave the infinite loop after one tick.
    BaseException so the loop's own `except Exception` can't swallow it."""


def test_one_full_tick_runs_without_a_loop_error(monkeypatch):
    """Drive one real tick of scheduler_loop with every job stubbed out.

    The catch-up gates call the module helper `_due(now, H)`, and later in the
    same function the post publisher's result was assigned to a variable also
    called `_due`. That made `_due` local to the whole function, so the very
    first gate raised UnboundLocalError, the loop's catch-all logged
    "Scheduler loop error", and no scheduled job ran at all. Every existing
    test called `_due` directly and passed. Only running the loop catches it.
    """
    import scheduler
    errors = []
    monkeypatch.setattr(scheduler.log, "error", lambda msg, *a, **k: errors.append(str(msg)))
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    # Nothing claims, so no job body runs — this exercises the gates only.
    monkeypatch.setattr(scheduler._ops, "claim_period", lambda *a, **k: False)
    import marketing_publish
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None, raising=False)

    def _stop(_seconds):
        raise _StopLoop()
    monkeypatch.setattr(scheduler.time, "sleep", _stop)
    with pytest.raises(_StopLoop):
        scheduler.scheduler_loop()
    assert not [e for e in errors if "Scheduler loop error" in e], errors


def test_no_module_helper_is_shadowed_inside_the_loop():
    """The structural form of the same guard: no name the loop assigns may
    also be a module-level function it calls."""
    import scheduler
    local = set(scheduler.scheduler_loop.__code__.co_varnames)
    helpers = {n for n, v in vars(scheduler).items() if callable(v) and n.startswith("_")}
    assert not (local & helpers), local & helpers


def test_a_laptop_never_runs_the_scheduler_unless_told_to(monkeypatch):
    """A local dev server shares production's email/SMS keys but not its
    database or lease, so a local scheduler sent every brief and digest twice."""
    import scheduler
    for var in ("RAILWAY_ENVIRONMENT", "RAILWAY_PROJECT_ID", "ALLOW_LOCAL_SCHEDULER"):
        monkeypatch.delenv(var, raising=False)
    started = []
    monkeypatch.setattr(scheduler.threading, "Thread", lambda **kw: started.append(kw) or _NoThread())
    assert scheduler.start_scheduler() is None and started == []
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert scheduler.start_scheduler() is not None and len(started) == 1
    monkeypatch.delenv("RAILWAY_ENVIRONMENT")
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    assert scheduler.start_scheduler() is not None


class _NoThread:
    def start(self):
        pass


def test_a_released_lease_is_taken_over_at_once_not_after_it_goes_stale(tmp_path, monkeypatch):
    """Every redeploy left the new process idle for up to 30 minutes behind
    the old one's lease. Releasing on exit hands it over on the next tick."""
    import models, ops
    db = str(tmp_path / "lease.db")
    models.init_db(db_path=db)
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: _conn(db))
    assert ops.acquire_scheduler_lease(owner="old")
    assert not ops.acquire_scheduler_lease(owner="new"), "held and fresh: the new one waits"
    assert not ops.release_scheduler_lease(owner="someone-else"), "only the holder can release"
    assert ops.release_scheduler_lease(owner="old")
    assert ops.acquire_scheduler_lease(owner="new")


def _conn(path):
    import sqlite3
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c
