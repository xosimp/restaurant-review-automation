"""Edge cases of the schedule rules and compliance sweep (SCHED audit).

schedule_rules is the one legality answer every pass shares, so its edges
are everybody's edges: a window or close past midnight, a person with no day
off when the run limit is set high, a rest gap across a clock change, and a
week that was published twice feeding the payroll-week tail.

xfail(strict=True) marks a confirmed defect, asserting the correct
behaviour; the marker comes off with the fix.
"""
import sys

import pytest

# Imported here, before any fixture patches models.get_conn, so no module is
# first imported mid-test and left holding a redirect to a deleted database.
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401

import models
import schedule_engine as se
import schedule_rules as sr
import staff_settings
from models import create_restaurant, Restaurant

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
PREV = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]
NEXT = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
DAYS = list(sr.DAYS)
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


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
    return db_path


def _c(week=WEEK, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(week), week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(date, emp, start, end, hours, role="Server"):
    return {"date": date, "day": "", "employee": emp, "role": role, "shift_start": start, "shift_end": end,
            "scheduled_hours": str(hours), "notes": ""}


def _restaurant(db_path, **cols):
    rid = create_restaurant(Restaurant(name="Rules Co", owner_email="r@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        conn.commit()
        conn.close()
    for n in ("Ana", "Ben"):
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    return rid


def _publish(db_path, rid, dates, rows):
    text = HEADER + "\n" + "\n".join(",".join(str(x) for x in r) for r in rows)
    hid = models.save_schedule_history(rid, dates[0], dates[-1], 0, 0, 30, text, [], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    return hid


# ── SCHED-13: windows that run past midnight ──────────────────────────────

def test_a_window_until_one_am_accepts_a_five_to_eleven_shift():
    c = _c(time_windows={"ana": {"Friday": (None, sr.parse_minutes("1:00am"))}})
    assert c.window_ok("Ana", "2026-10-09", "5:00pm", "11:00pm") == (True, "")


def test_a_window_until_ten_pm_refuses_a_five_pm_to_two_am_shift():
    c = _c(time_windows={"ana": {"Friday": (None, sr.parse_minutes("10:00pm"))}})
    ok, why = c.window_ok("Ana", "2026-10-09", "5:00pm", "2:00am")
    assert not ok and "not after 10:00pm" in why


def test_a_window_until_ten_pm_still_refuses_a_five_to_eleven_shift():
    c = _c(time_windows={"ana": {"Friday": (None, sr.parse_minutes("10:00pm"))}})
    assert c.window_ok("Ana", "2026-10-09", "5:00pm", "11:00pm")[0] is False
    assert c.window_ok("Ana", "2026-10-09", "5:00pm", "9:30pm")[0] is True


def test_an_overnight_availability_window_can_be_saved():
    out = staff_settings._clean_windows({"Friday": {"earliest": "5:00pm", "latest": "1:00am"}})
    assert out == {"Friday": {"earliest": "5:00pm", "latest": "1:00am"}}


def test_a_window_that_ends_before_it_starts_on_the_same_day_is_still_refused():
    with pytest.raises(staff_settings.StaffSettingsError):
        staff_settings._clean_windows({"Friday": {"earliest": "3:00pm", "latest": "3:00pm"}})


# ── SCHED-31: no day off at all ────────────────────────────────────────────

def test_seven_shifts_with_a_fourteen_day_run_limit_still_breach_the_days_off_rule():
    c = _c()
    c.compliance["max_consecutive_days"] = 14
    rows = [_row(d, "Ana", "11:00am", "3:00pm", 4) for d in WEEK]
    kinds = {v["kind"] for v in sr.violations(rows, c)}
    assert "days_off" in kinds


def test_seven_shifts_under_the_default_run_limit_are_a_long_run():
    rows = [_row(d, "Ana", "11:00am", "3:00pm", 4) for d in WEEK]
    assert "long_run" in {v["kind"] for v in sr.violations(rows, _c())}


def test_five_shifts_with_two_days_off_together_break_nothing():
    rows = [_row(d, "Ana", "11:00am", "3:00pm", 4) for d in WEEK[:5]]
    assert [v for v in sr.violations(rows, _c()) if v["kind"] in ("days_off", "long_run")] == []


# ── SCHED-32: rest gaps across a DST change ───────────────────────────────

def test_a_close_and_open_across_the_fall_back_night_is_not_a_rest_gap(db):
    rid = _restaurant(db, timezone="America/Chicago")
    dates = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31", "2026-11-01"]
    c = sr.build_constraints(rid, dates, DAYS)
    rows = [_row("2026-10-31", "Ana", "5:00pm", "11:00pm", 6), _row("2026-11-01", "Ana", "9:00am", "2:00pm", 5)]
    # 11pm Saturday to 9am Sunday is 11 real hours (clocks fall back at 2am); rule is 10h.
    c.compliance["min_rest_hours"] = 10.5
    assert "rest_gap" not in {v["kind"] for v in sr.violations(rows, c)}


def test_a_close_and_open_across_the_spring_forward_night_is_a_rest_gap(db):
    rid = _restaurant(db, timezone="America/Chicago")
    dates = ["2027-03-08", "2027-03-09", "2027-03-10", "2027-03-11", "2027-03-12", "2027-03-13", "2027-03-14"]
    c = sr.build_constraints(rid, dates, DAYS)
    rows = [_row("2027-03-13", "Ana", "5:00pm", "11:00pm", 6), _row("2027-03-14", "Ana", "9:00am", "2:00pm", 5)]
    # 11pm Saturday to 9am Sunday is 9 real hours (clocks spring forward at 2am); rule is 9.5h.
    c.compliance["min_rest_hours"] = 9.5
    assert "rest_gap" in {v["kind"] for v in sr.violations(rows, c)}


def test_an_ordinary_night_rest_gap_is_measured_on_the_wall_clock():
    rows = [_row("2026-10-09", "Ana", "5:00pm", "11:00pm", 6), _row("2026-10-10", "Ana", "8:00am", "2:00pm", 6)]
    v = [x for x in sr.violations(rows, _c()) if x["kind"] == "rest_gap"]
    assert len(v) == 1 and "9.0h" in v[0]["detail"]


# ── SCHED-10: a week published twice feeds the payroll-week tail ─────────

def test_only_the_newest_published_version_of_a_week_feeds_the_tail(db):
    rid = _restaurant(db, week_start_day=3)                        # Thursday payroll weeks
    v1 = [(d, "X", "Ana", "Server", "3:00pm", "11:00pm", 8, "") for d in PREV[3:]]
    v2 = [(d, "X", "Ben", "Server", "3:00pm", "11:00pm", 8, "") for d in PREV[3:]]
    _publish(db, rid, PREV, v1)
    _publish(db, rid, PREV, v2)                                     # the week as it really ran
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert not c.base_hours.get("ana") and not c.base_rows.get("ana")
    assert c.base_hours.get("ben")
    rows = [_row(WEEK[0], "Ana", "7:00am", "3:00pm", 8), _row(WEEK[1], "Ana", "7:00am", "3:00pm", 8)]
    assert [v for v in sr.violations(rows, c) if v["hard"]] == []


def test_two_versions_of_a_later_week_never_hide_the_previous_weeks_close(db):
    rid = _restaurant(db)
    _publish(db, rid, PREV, [(PREV[6], "Sunday", "Ana", "Server", "5:00pm", "11:00pm", 6, "")])
    _publish(db, rid, NEXT, [(NEXT[0], "Monday", "Ben", "Server", "5:00pm", "10:00pm", 5, "")])
    _publish(db, rid, NEXT, [(NEXT[0], "Monday", "Ben", "Server", "4:00pm", "10:00pm", 6, "")])
    c = sr.build_constraints(rid, WEEK, DAYS)
    rows = [_row(WEEK[0], "Ana", "7:00am", "3:00pm", 8)]              # 8h after Sunday's 11pm close
    assert "rest_gap" in {v["kind"] for v in sr.violations(rows, c)}


def test_a_single_published_previous_week_feeds_rest_and_hours(db):
    rid = _restaurant(db, week_start_day=3)
    _publish(db, rid, PREV, [(d, "X", "Ana", "Server", "3:00pm", "11:00pm", 8, "") for d in PREV[3:]])
    c = sr.build_constraints(rid, WEEK, DAYS)
    assert c.base_hours["ana"] == {PREV[3]: 32.0}
    rows = [_row(WEEK[0], "Ana", "7:00am", "3:00pm", 8)]
    assert "rest_gap" in {v["kind"] for v in sr.violations(rows, c)}
