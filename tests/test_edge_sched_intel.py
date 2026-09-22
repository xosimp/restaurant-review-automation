"""Edge cases of what the schedule learns from its own record (SCHED audit).

A week published twice counted twice in outcomes and the rotation ledger,
denied drops read as preferences, a holiday on the far side of New Year,
the Monday outcomes job and the Thursday auto-draft running unbounded or
resuming only a week later, and ledgers that grow without a retention rule.

xfail(strict=True) marks a confirmed defect, asserting the correct
behaviour; the marker comes off with the fix.
"""
import datetime as dt
import sys
import time

import pytest

# Imported here, before any fixture patches models.get_conn, so no module is
# first imported mid-test and left holding a redirect to a deleted database.
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401

import models
import ops
import schedule_engine as se
import schedule_intel as si
import scheduler
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        # The real one, or a redirect some earlier test left bound in a
        # module it imported for the first time mid-test.
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _restaurant(db_path, name="Intel Co", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email="i@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _publish(db_path, rid, dates, rows):
    text = HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},Server,{s},{e},{h}," for d, n, s, e, h in rows)
    hid = models.save_schedule_history(rid, dates[0], dates[-1], 0, 0, 30, text, [], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    return hid


def _q(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


# ── SCHED-10: a week published twice, counted twice ──────────────────────

def test_a_week_published_twice_is_recorded_once_in_outcomes(db):
    rid = _restaurant(db)
    _publish(db, rid, W1, [(W1[5], "Ana", "5:00pm", "10:00pm", 5)])
    _publish(db, rid, W1, [(W1[5], "Ben", "5:00pm", "10:00pm", 5)])
    si.record_outcomes(rid, today=dt.date(2026, 10, 20))
    rows = _q(db, "SELECT history_id FROM schedule_outcomes WHERE restaurant_id=? AND date=?", rid, W1[5])
    assert len(rows) == 1, [tuple(r) for r in rows]


def test_a_week_published_twice_counts_once_in_the_rotation_ledger(db):
    rid = _restaurant(db)
    _publish(db, rid, W1, [(W1[5], "Ana", "5:00pm", "10:00pm", 5)])
    _publish(db, rid, W1, [(W1[5], "Ana", "5:00pm", "10:00pm", 5)])
    ledger = si.fairness_ledger(rid, today=dt.date(2026, 10, 20))
    assert ledger["Ana"]["weekend"] == 1 and ledger["Ana"]["weeks"] == 1


def test_one_published_week_is_recorded_per_date_and_daypart(db):
    rid = _restaurant(db)
    _publish(db, rid, W1, [(W1[5], "Ana", "11:00am", "3:00pm", 4), (W1[5], "Ben", "5:00pm", "10:00pm", 5)])
    assert si.record_outcomes(rid, today=dt.date(2026, 10, 20))["written"] == 2
    assert si.record_outcomes(rid, today=dt.date(2026, 10, 20))["written"] == 2       # idempotent upsert
    assert len(_q(db, "SELECT 1 FROM schedule_outcomes WHERE restaurant_id=?", rid)) == 2


# ── SCHED-33: a holiday on the far side of New Year ──────────────────────

@pytest.mark.xfail(strict=True, reason="SCHED-33: the ledger resolves holidays by week_start's year, so Jan 1 in a week starting Dec 28 is missed")
def test_new_years_day_in_a_week_that_starts_in_december_counts_as_a_holiday(db):
    rid = _restaurant(db)
    week = ["2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02", "2027-01-03"]
    _publish(db, rid, week, [("2027-01-01", "Ana", "5:00pm", "10:00pm", 5)])
    assert si.fairness_ledger(rid, today=dt.date(2027, 1, 10))["Ana"]["holiday"] == 1


def test_christmas_in_a_december_week_counts_as_a_holiday(db):
    rid = _restaurant(db)
    week = ["2026-12-21", "2026-12-22", "2026-12-23", "2026-12-24", "2026-12-25", "2026-12-26", "2026-12-27"]
    _publish(db, rid, week, [("2026-12-25", "Ana", "5:00pm", "10:00pm", 5)])
    assert si.fairness_ledger(rid, today=dt.date(2027, 1, 3))["Ana"]["holiday"] == 1


# ── SCHED-40: denied drops are not preferences ───────────────────────────

def _request(db_path, rid, name, date, start, status, kind="drop", replacement=None):
    hid = _publish(db_path, rid, [date], [(date, name, start, "10:00pm", 5)])
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                 "shift_end, role, status, kind, replacement_name) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (rid, hid, name, date, start, "10:00pm", "Server", status, kind, replacement))
    conn.commit()
    conn.close()


@pytest.mark.xfail(strict=True, reason="SCHED-40: behaviour_preferences counts denied drops as 'avoids', overriding the manager's decision")
def test_two_denied_sunday_night_drops_do_not_become_an_avoid(db):
    rid = _restaurant(db)
    for d in ("2026-09-06", "2026-09-13"):
        _request(db, rid, "Ana", d, "5:00pm", "denied")
    assert "Ana" not in si.behaviour_preferences(rid)


def test_two_approved_sunday_night_drops_are_an_avoid_and_two_claims_a_preference(db):
    rid = _restaurant(db)
    _request(db, rid, "Ana", "2026-09-06", "5:00pm", "open")
    _request(db, rid, "Ana", "2026-09-13", "5:00pm", "covered", replacement="Ben")
    _request(db, rid, "Cy", "2026-09-20", "5:00pm", "covered", replacement="Ben")
    prefs = si.behaviour_preferences(rid)
    assert prefs["Ana"]["avoids"] == ["Sunday night"]
    assert prefs["Ben"]["prefers"] == ["Sunday night"]


# ── SCHED-27: the Monday outcomes job ─────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SCHED-27: run_schedule_outcomes loops every labor restaurant with no bound or cursor")
def test_the_outcomes_job_keeps_a_cursor_and_resumes_after_it(db, monkeypatch):
    rids = [_restaurant(db, name=f"Labor {i}", module_labor=1) for i in range(3)]
    seen = []
    monkeypatch.setattr(si, "record_outcomes", lambda rid, **k: seen.append(rid) or {"written": 0})
    strategy_jobs.run_schedule_outcomes()
    cursors = _q(db, "SELECT key FROM job_cursors WHERE key LIKE '%outcome%'")
    assert cursors, "a pass over every restaurant needs a cursor to resume from"
    conn = models.get_conn(db)
    conn.execute("UPDATE job_cursors SET value=? WHERE key=?", (str(rids[0]), cursors[0]["key"]))
    conn.commit()
    conn.close()
    seen.clear()
    strategy_jobs.run_schedule_outcomes()
    assert seen and seen[0] == rids[1]


def test_the_outcomes_job_only_visits_labor_restaurants(db, monkeypatch):
    on = _restaurant(db, name="Has Labor", module_labor=1)
    _restaurant(db, name="No Labor", module_labor=0)
    seen = []
    monkeypatch.setattr(si, "record_outcomes", lambda rid, **k: seen.append(rid) or {"written": 1})
    assert strategy_jobs.run_schedule_outcomes() == {"rows": 1} and seen == [on]


@pytest.mark.xfail(strict=True, reason="SCHED-27: schedule_recommendation_events has no retention rule and grows on every rescore")
def test_recommendation_events_have_a_retention_window(db):
    rid = _restaurant(db)
    si.record_recommendation(rid, "coverage", "old", "shown")
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_recommendation_events SET created_at=datetime('now','-900 days')")
    conn.commit()
    conn.close()
    ops.prune_ledgers()
    assert _q(db, "SELECT COUNT(*) AS n FROM schedule_recommendation_events")[0]["n"] == 0


# ── SCHED-11: the Thursday auto-draft resumes the same day ───────────────

class _StopLoop(BaseException):
    pass


@pytest.mark.xfail(strict=True, reason="SCHED-11: auto-draft is claimed once per Thursday, so a time-bounded pass resumes only next week")
def test_a_time_bounded_auto_draft_pass_is_finished_later_the_same_thursday(db, monkeypatch):
    rids = [_restaurant(db, name=f"Draft {i}", module_labor=1, auto_draft_schedule=1) for i in range(3)]
    drafted = []

    def stub_generation(job_id, rid, **kw):
        drafted.append(rid)
        end = time.perf_counter() + 0.06                      # a "slow" generation, without sleeping
        while time.perf_counter() < end:
            pass
        ops.finish_async_job(job_id, "done", {"ok": True})
    monkeypatch.setattr(se, "_run_schedule_job", stub_generation)
    monkeypatch.setattr(strategy_jobs, "AUTO_DRAFT_MAX_SECONDS", 0.05)

    clock = {"now": dt.datetime(2026, 10, 8, 6, 30)}           # Thursday, 6:30am Chicago
    monkeypatch.setattr(scheduler, "_chi_now", lambda: clock["now"])
    monkeypatch.setattr(scheduler._ops, "acquire_scheduler_lease", lambda *a, **k: True)
    real_claim = ops.claim_period
    # Only the draft job's own claims are real; every other gate stays shut.
    monkeypatch.setattr(scheduler._ops, "claim_period",
                        lambda job, period: real_claim(job, period) if job.startswith("auto_draft") else False)
    import marketing_publish
    monkeypatch.setattr(marketing_publish, "run_due_posts", lambda **k: {})
    monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None, raising=False)
    ticks = {"n": 0}

    def next_tick(_seconds):
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise _StopLoop()
        clock["now"] = dt.datetime(2026, 10, 8, 7, 30)        # the next tick an hour later, same Thursday
    monkeypatch.setattr(scheduler.time, "sleep", next_tick)
    with pytest.raises(_StopLoop):
        scheduler.scheduler_loop()
    assert drafted, "the first tick drafted somebody"
    assert sorted(set(drafted)) == sorted(rids), drafted
