"""Owners and salaried managers are not held to the hours rules (Simple EJ's,
10/7/26: "erik (owner/manager) rules should not be affected by their hours,
erik is always at the store and most managers work over 40 hours anyways
since they are salaried"). schedule_rules.hours_rules_apply is the one
reader; an hourly manager is still held, and a cap the owner set still holds.
"""
from datetime import date, timedelta

import schedule_rules as sr
import shift_quality as sq

WEEK = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]   # a Monday
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def C(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.compliance["max_shift_hours"] = 16
    c.roster_names = ["Erik", "Jim", "Andrew", "Ann"]
    c.active = {n.lower() for n in c.roster_names}
    c.managers = {"erik": "Owner", "jim": "Manager FOH", "andrew": "Manager FOH"}
    c.salaried = {"jim"}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def R(i, emp, start, end, role="Manager FOH"):
    r = {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role,
         "shift_start": start, "shift_end": end, "notes": ""}
    r["scheduled_hours"] = str(sr.span_hours(r))
    return r


def kinds_for(rows, c, who):
    return {v["kind"] for v in sr.violations(rows, c) if (v.get("employee") or "") == who}


HOURS_KINDS = {"over_max_hours", "over_hours_ceiling", "under_min_hours", "long_run", "rest_gap", "consecutive_days_off",
               "daily_overtime", "payroll_tail_full"}


def test_who_is_held():
    c = C()
    assert not sr.hours_rules_apply(c, "Erik")          # owner
    assert not sr.hours_rules_apply(c, "Jim")           # salaried manager
    assert sr.hours_rules_apply(c, "Andrew")            # hourly manager: overtime is real money
    assert sr.hours_rules_apply(c, "Ann")               # staff


def test_a_long_week_of_closes_flags_the_hourly_manager_only():
    c = C(hours_limits={"erik": (50, None), "andrew": (50, None)})
    for who in ("Erik", "Jim", "Andrew"):
        rows = [R(i, who, "10:00am", "11:30pm") for i in range(7)] + [R(0, who, "1:00am", "2:00am")]
        got = kinds_for(rows, c, who) & HOURS_KINDS
        if who == "Andrew":
            # past the restaurant's ceiling (no maximum of his own): the
            # policy limit, still flagged (LOGIC-1)
            assert {"over_hours_ceiling", "long_run"} <= got, got
        else:
            assert not got, (who, got)
    # A minimum the owner typed is not scored against them either.
    short = [R(0, "Erik", "4:00pm", "11:00pm")]
    assert "under_min_hours" not in kinds_for(short, c, "Erik")
    assert "under_min_hours" in kinds_for([R(0, "Andrew", "4:00pm", "11:00pm")], c, "Andrew")


def test_a_cap_the_owner_set_still_holds():
    c = C(salaried_cap=50)
    rows = [R(i, "Jim", "10:00am", "10:00pm") for i in range(5)]          # 60h
    assert "over_max_hours" in kinds_for(rows, c, "Jim")


def test_the_score_skips_their_hours():
    rows = [{"date": WEEK[i], "start": 600, "end": 1380, "busy": True} for i in range(7)]
    ctx = sq.ShiftContext(hours_limits={"Erik": (50, None), "Ann": (30, None)},
                          week_assignments={"Erik": rows, "Ann": []},
                          hours_exempt={"erik"})
    mh = sq.week_min_hours([ctx])
    assert mh is not None and [s["name"] for s in mh.facts["short"]] == ["Ann"]
    found = sq._fatigue_findings(["Erik"], ctx)
    assert not any(found.values()), found


def test_leaders_setting_their_own_hours_is_the_restaurants_setting_not_a_rule_for_everyone():
    """Blind re-audit 10/11/26 ARCH-22: the exemption came from one client;
    it is compliance leaders_set_own_hours now — on by default (EJ's keeps
    it), off for a restaurant whose leaders are held to its limits."""
    assert sr.DEFAULTS["leaders_set_own_hours"] is True
    c = C()
    c.compliance["leaders_set_own_hours"] = False
    for who in ("Erik", "Jim", "Andrew", "Ann"):
        assert sr.hours_rules_apply(c, who) and sr.max_shift_applies(c, who), who
    rows = [R(i, "Jim", "10:00am", "11:30pm") for i in range(7)]
    assert "long_run" in kinds_for(rows, c, "Jim")
    assert "shift_too_long" in kinds_for([R(0, "Erik", "6:00am", "11:30pm", role="Owner")], c, "Erik")
    c.compliance["leaders_set_own_hours"] = None                  # a blank reads as the default
    assert not sr.hours_rules_apply(c, "Jim") and not sr.max_shift_applies(c, "Erik")


def test_the_setting_is_read_from_the_restaurant_and_merged_on_save():
    class Rest:
        compliance_json = '{"leaders_set_own_hours": false}'
        jurisdiction = None
        compliance_profile_json = None
    assert sr.compliance(Rest())["leaders_set_own_hours"] is False
    Rest.compliance_json = "{}"
    assert sr.compliance(Rest())["leaders_set_own_hours"] is True
    merged = sr.merged_compliance({"max_shift_hours": 10}, {"leaders_set_own_hours": False})
    assert merged["leaders_set_own_hours"] is False and merged["max_shift_hours"] == 10
