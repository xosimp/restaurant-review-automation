"""Schedule fix round 10/11/26, workstream R — rules, legal and the manager
rule (the blind re-audit of 10/11/26).

STRESS-5/6 minors' school hours and the shortest shift, STRESS-3/18 the
opening prep and the single-manager restaurant, STRESS-19 the short
turnaround, LOGIC-12 the manager allowances per restaurant, LOGIC-13 the
second manager at peaks, LOGIC-20 policy limits labelled by tier,
BUGSGEN-5/6/7/8/12/17/18/19/25, ARCH-12 and the STRESS-24 regressions.
Pure where it can be: a rule that only holds for one rendered payload is
not a rule.
"""
import inspect
import os
import re
from datetime import date, timedelta

import pytest

import schedule_rules as sr
import schedule_time as st

WEEK = [(date(2026, 10, 12) + timedelta(days=i)).isoformat() for i in range(7)]      # Mon 10/12 - Sun 10/18
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS))
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(i, emp, start, end, role="Server", d=None, notes=""):
    d = d or WEEK[i]
    r = {"date": d, "day": sr._weekday_of(d), "employee": emp, "role": role,
         "shift_start": start, "shift_end": end, "notes": notes}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    return r


def _kinds(rows, c):
    return [v["kind"] for v in sr.violations(rows, c)]


# ── BUGSGEN-19: a start equal to its end is unreadable, not 24 hours ──────

def test_a_start_equal_to_its_end_is_unreadable_not_a_day_long_shift():
    assert st.place(17 * 60, 17 * 60) is None
    row = {"date": "2026-10-31", "shift_start": "5:00pm", "shift_end": "5:00pm"}
    assert st.span(row) is None
    assert st.real_hours(row, "America/Chicago") == 0.0          # was 25.0 on the fall-back night
    assert st.wall_hours(row) == 0.0
    # 12:00am-12:00am too; a real overnight is still read
    assert st.place(0, 0) is None and st.place(22 * 60, 2 * 60) == (22 * 60, 26 * 60)


def test_the_parser_drops_and_counts_a_start_equal_to_its_end():
    import schedule_job
    src = inspect.getsource(schedule_job)
    assert "_stime.span(_row) is None" in src


# ── ARCH-12 / BUGSGEN-25: labor reads time through schedule_time ──────────

def test_labor_has_no_private_clock_parser():
    import labor
    for name in ("_clock_minutes", "_close_m", "_late_end"):
        assert not hasattr(labor, name), name


def test_a_midnight_close_has_its_late_crew():
    import labor
    rows = []
    for f in ("2026-09-18", "2026-09-25", "2026-10-02"):
        rows += [{"date": f, "employee": "Bea", "role": "Bartender", "shift_start": "6:00pm", "shift_end": "12:30am"},
                 {"date": f, "employee": "Cal", "role": "Server", "shift_start": "noon", "shift_end": "6:00pm"}]
    for close in ("midnight", "12:00 a.m.", "12:30am"):
        pats = labor.historical_patterns(rows, close_times={"Friday": close}, open_times={"Friday": "11:00am"})
        assert pats["late_headcount"].get("Friday") == {"Bartender": 1}, close
    # a "noon" start reads (it was None to labor's strptime)
    pats = labor.historical_patterns(rows)
    assert pats["typical_headcount"][("Friday", "morning")] == {"Server": 1}


def test_no_module_parses_a_clock_time_with_strptime():
    """The lint ARCH-12 asked for: a clock time is read by schedule_time's
    parse_minutes, never a private strptime (which reads no 'noon',
    'midnight', '12:00 a.m.' or '5p')."""
    pat = re.compile(r"strptime\([^)]*[\"'](%I:%M%p|%I%p|%H:%M|%H:%M:%S|%I:%M %p)[\"']")
    allowed = {os.path.join("event_intel", "gameday.py")}     # a sports feed's fixed kickoff format, not a shift
    hits = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv", "node_modules", "tests", ".claude", "ios")]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(base, f)
            rel = os.path.relpath(path, ROOT)
            if rel in allowed or rel == "schedule_time.py":
                continue
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
            if pat.search(text) or re.search(r"for fmt in \(\"%I:%M%p\"", text):
                hits.append(rel)
    assert not hits, hits


# ── BUGSGEN-18: overnight hours count in the week they are worked ─────────

def test_overnight_hours_split_at_the_payroll_seam():
    import labor
    # Wednesday-start payroll (week_start_day=2); Tuesday 10/13 10pm-2am
    row = {"date": "2026-10-13", "shift_start": "10:00pm", "shift_end": "2:00am", "scheduled_hours": "4"}
    assert labor.week_hours_split(row, 2) == {"2026-10-07": 2.0, "2026-10-14": 2.0}
    # inside a week nothing moves; a small-hours row is all the next week's
    assert labor.week_hours_split(dict(row, date="2026-10-15"), 2) == {"2026-10-14": 4.0}
    tail = {"date": "2026-10-13", "shift_start": "12:30am", "shift_end": "4:30am", "scheduled_hours": "4"}
    assert labor.week_hours_split(tail, 2) == {"2026-10-14": 4.0}


def test_the_sweep_counts_overtime_by_the_week_worked():
    c = _c(week_start_day=2, compliance={**sr.DEFAULTS, "weekly_hours_ceiling": 40.0})
    # Wed-Mon: 6 x 6h = 36h in the 10/14 payroll week; Tuesday 10/20 6pm-2am
    # is 6h Tuesday (still 10/14's week) and 2h Wednesday (10/21's)
    dates = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19"]
    rows = [_row(0, "Ana", "4:00pm", "10:00pm", d=d) for d in dates]
    rows.append(_row(0, "Ana", "6:00pm", "2:00am", d="2026-10-20"))
    c.week_dates = dates + ["2026-10-20"]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "over_hours_ceiling"]
    assert v and v[0]["over_by"] == 2.0          # 42h worked in the week, not 44
    assert c.bucket_hours(rows[-1]) == {"2026-10-14": 6.0, "2026-10-21": 2.0}


# ── STRESS-19: a short turnaround is a soft flag where no rest rule is set ─

def test_a_one_hour_turnaround_is_flagged_soft_without_a_rest_rule():
    c = _c()
    rows = [_row(6, "Bo", "9:00pm", "5:00am", "Bartender"), _row(0, "Bo", "6:00am", "12:00pm", "Bartender",
                                                                    d="2026-10-19")]
    c.week_dates = WEEK + ["2026-10-19"]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "short_turnaround"]
    assert v and not v[0]["hard"] and "1.0h" in v[0]["detail"]
    assert sr.BREACH_TIER["short_turnaround"] == sr.TIER_QUALITY
    # 9h is fine; a rest rule replaces the flag; a manager keeps their own
    ok = [_row(6, "Bo", "9:00pm", "2:00am", "Bartender"), _row(0, "Bo", "11:00am", "5:00pm", "Bartender",
                                                                  d="2026-10-19")]
    assert "short_turnaround" not in _kinds(ok, c)
    c2 = _c(compliance={**sr.DEFAULTS, "min_rest_hours": 10.0})
    c2.week_dates = c.week_dates
    assert "short_turnaround" not in _kinds(rows, c2) and "rest_gap" in _kinds(rows, c2)
    c3 = _c(managers={"bo": "Manager"})
    c3.week_dates = c.week_dates
    assert "short_turnaround" not in _kinds(rows, c3)


# ── STRESS-5: 14-15 year olds never work during school hours ──────────────

def _minor_c(**kw):
    return _c(roster_names=["Kid", "Ana"], active={"kid", "ana"}, minors={"kid"}, minor_bands={"kid": "14-15"},
              **kw)


def test_school_hours_are_a_hard_minor_rule_on_a_school_day():
    c = _minor_c()
    for s, e in (("11:00am", "2:00pm"), ("8:00am", "11:00am"), ("3:00pm", "6:00pm")):
        v = [x for x in sr.violations([_row(1, "Kid", s, e, "Host")], c) if x["kind"] == "minor_school_hours"]
        assert v and v[0]["hard"] and sr.BREACH_TIER["minor_school_hours"] == sr.TIER_LEGAL, (s, e)
    # after the bell, and Saturday, are legal
    assert "minor_school_hours" not in _kinds([_row(1, "Kid", "4:00pm", "7:00pm", "Host")], c)
    assert "minor_school_hours" not in _kinds([_row(5, "Kid", "11:00am", "2:00pm", "Host")], c)
    # 16-17s have no federal school-hours rule
    c.minor_bands = {"kid": "16-17"}
    assert "minor_school_hours" not in _kinds([_row(1, "Kid", "11:00am", "2:00pm", "Host")], c)


def test_the_districts_bell_times_and_breaks_decide_the_window():
    import school_calendar as sc
    cal = sc.clean_calendar({"years": [{"start": "2026-08-19", "end": "2027-06-04",
                                        "breaks": [{"start": "2026-10-12", "end": "2026-10-13", "label": "Fall"}]}],
                             "hours": {"start": "8:00am", "end": "3:10pm"}})
    assert cal["hours"] == {"start": "8:00am", "end": "3:10pm"} and sc.school_hours(cal) == (480, 910)
    c = _minor_c(school_calendar=cal)
    assert "minor_school_hours" not in _kinds([_row(1, "Kid", "11:00am", "2:00pm", "Host")], c)   # fall break
    assert "minor_school_hours" in _kinds([_row(2, "Kid", "3:00pm", "6:00pm", "Host")], c)        # bell 3:10
    assert "minor_school_hours" not in _kinds([_row(2, "Kid", "3:10pm", "6:10pm", "Host")], c)
    with pytest.raises(ValueError):
        sc.clean_calendar({"years": [], "hours": {"start": "3:00pm", "end": "8:00am"}})
    assert sc.school_hours({}) == (450, 930)


def test_a_state_table_can_add_the_prohibition_never_remove_it():
    sr.JURISDICTION_MINOR_RULES["ZZ"] = {"14-15": {"outside_school_hours": False},
                                         "16-17": {"outside_school_hours": True, "source": "test"}}
    try:
        assert sr.minor_rules("14-15", "ZZ")["outside_school_hours"] is True
        assert sr.minor_rules("16-17", "ZZ")["outside_school_hours"] is True
    finally:
        sr.JURISDICTION_MINOR_RULES.pop("ZZ", None)


def test_every_solver_domain_reads_the_one_minor_rule():
    import schedule_cpsat
    import schedule_solver
    for mod in (schedule_cpsat, schedule_solver):
        src = inspect.getsource(mod)
        assert "minor_row_breach(" in src and "minor_earliest(c, low)" not in src, mod.__name__
    c = _minor_c()
    assert sr.minor_row_breach(c, "kid", _row(1, "Kid", "11:00am", "2:00pm", "Host")) == \
        sr.LABELS["minor_school_hours"]
    assert sr.minor_row_breach(c, "kid", _row(1, "Kid", "4:00pm", "7:00pm", "Host")) is None


def test_the_repair_keeps_the_after_school_part_and_covers_the_rest():
    c = _minor_c()
    rows = [_row(1, "Kid", "1:00pm", "6:00pm", "Host"), _row(1, "Ana", "9:00am", "1:00pm", "Host")]
    out = sr.fix_person_breaches(rows, c, roster_roles={"Kid": "Host", "Ana": "Host"})
    kid = next(r for r in out["rows"] if r["employee"] == "Kid")
    assert sr.parse_minutes(kid["shift_start"]) >= 15 * 60 + 30
    assert "minor_school_hours" not in _kinds(out["rows"], c)
    ana = next(r for r in out["rows"] if r["employee"] == "Ana")
    assert ana["shift_end"] in ("3:30pm",)                   # Ana runs on to the bell


def test_the_prompt_states_the_school_hours_rule():
    c = _minor_c()
    lines = "\n".join(sr._minor_band_lines(c))
    assert "never during school hours (7:30am–3:30pm) on a school day" in lines
