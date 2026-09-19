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
                "review_diagnoses", "food_cost_diagnoses", "ops_digest", "weekly_digest",
                "daily_alerts", "onboarding", "optin_invite", "refresh_tokens",
                "marketing_metrics_sync"):
        assert re.search(r'claim_period\("%s", str\(today\)\)' % job, body), job


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
