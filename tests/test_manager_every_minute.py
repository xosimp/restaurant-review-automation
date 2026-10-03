"""A manager on the floor every minute anybody is (owner, 10/2/26: "there
can be ZERO shifts without ONE manager. always" — Erik's first generated
week had no manager and neither owner on any day)."""
from datetime import date, timedelta

import schedule_rules as sr

WEEK = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ROSTER = {"Ann": "Server", "Ben": "Line Cook", "Andrew": "Manager FOH", "Erik": "Owner"}


def _c(managers=None, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=DAYS)
    c.compliance = dict(sr.DEFAULTS)
    c.compliance["max_consecutive_days"] = 7
    c.compliance["min_consecutive_days_off"] = 0
    c.roster_names = list(ROSTER)
    c.managers = {"andrew": "Manager FOH", "erik": "Owner"} if managers is None else managers
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(i, emp, start, end, hours, role=None):
    return {"date": WEEK[i], "day": DAYS[i], "employee": emp, "role": role or ROSTER.get(emp, "Server"),
            "shift_start": start, "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _kinds(rows, c):
    return [v["kind"] for v in sr.violations(rows, c)]


def test_manager_and_owner_roles_count_and_others_do_not():
    for role in ("Manager FOH", "General Manager", "GM", "Owner", "AGM", "MOD", "Shift Lead"):
        assert sr.is_manager_role(role), role
    # A department's manager does not run the floor by its title alone: the
    # owner says so per person (schedule audit 10/3/26 P-7).
    for role in ("Server", "Bartender AM", "Line Cook", "Host PM", "Busser", "", "Kitchen Manager", "Bar Supervisor"):
        assert not sr.is_manager_role(role), role


def test_a_day_with_nobody_managing_is_a_hard_breach():
    rows = [_row(0, "Ann", "11:00am", "4:00pm", 5), _row(0, "Ben", "10:00am", "3:00pm", 5)]
    v = [x for x in sr.violations(rows, _c()) if x["kind"] == "no_manager"]
    assert v and v[0]["hard"] and "10:00am" in v[0]["detail"] and "4:00pm" in v[0]["detail"]


def test_even_a_short_stretch_without_a_manager_is_caught():
    rows = [_row(0, "Ann", "11:00am", "10:00pm", 11), _row(0, "Andrew", "11:00am", "9:30pm", 10.5)]
    v = [x for x in sr.violations(rows, _c()) if x["kind"] == "no_manager"]
    assert len(v) == 1 and "9:30pm" in v[0]["detail"] and "10:00pm" in v[0]["detail"]


def test_a_manager_covering_every_minute_clears_it_whatever_role_their_row_is():
    rows = [_row(0, "Ann", "11:00am", "4:00pm", 5), _row(0, "Erik", "10:00am", "4:00pm", 6, role="Bartender")]
    assert "no_manager" not in _kinds(rows, _c())


def test_two_managers_overlapping_cover_the_day():
    rows = [_row(0, "Ann", "10:00am", "11:00pm", 13), _row(0, "Andrew", "10:00am", "4:30pm", 6.5),
            _row(0, "Erik", "4:00pm", "11:00pm", 7)]
    assert "no_manager" not in _kinds(rows, _c())


def test_a_roster_with_no_manager_says_so_without_holding_every_week():
    rows = [_row(0, "Ann", "11:00am", "4:00pm", 5)]
    v = [x for x in sr.violations(rows, _c(managers={})) if x["kind"].startswith("no_manager")]
    assert [x["kind"] for x in v] == ["no_manager_roster"] and not v[0]["hard"]


def test_the_backstop_adds_a_manager_to_a_day_with_none_salaried_first():
    c = _c(salaried={"erik"})
    rows = [_row(1, "Ann", "11:00am", "4:00pm", 5), _row(1, "Ben", "10:00am", "3:00pm", 5)]
    out = sr.cover_manager_gaps(rows, c)
    assert out["added"] and out["added"][0]["employee"] == "Erik"
    new = out["rows"][-1]
    assert new["employee"] == "Erik" and new["role"] == "Owner" and new["date"] == WEEK[1]
    assert new["shift_start"] == "10:00am" and new["shift_end"] == "4:00pm"
    assert "no_manager" not in _kinds(out["rows"], c) and not out["left"]


def test_the_backstop_extends_a_manager_already_on_rather_than_adding_one():
    rows = [_row(2, "Ann", "11:00am", "10:00pm", 11), _row(2, "Andrew", "11:00am", "9:00pm", 10)]
    out = sr.cover_manager_gaps(rows, _c())
    assert out["extended"] and not out["added"]
    assert out["rows"][1]["shift_end"] == "10:00pm"
    assert "no_manager" not in _kinds(out["rows"], _c())


def test_the_backstop_never_uses_a_manager_who_cannot_work_that_day():
    c = _c(unavailable_days={"erik": {"Wednesday"}, "andrew": {"Wednesday"}})
    rows = [_row(2, "Ann", "11:00am", "4:00pm", 5)]
    out = sr.cover_manager_gaps(rows, c)
    assert not out["added"] and not out["extended"]
    assert out["left"] and out["left"][0]["day"] == "Wednesday"
    assert "no_manager" in _kinds(out["rows"], c), "a gap nobody can legally fill stays a hard flag"


def test_a_long_day_is_covered_end_to_end():
    c = _c(salaried={"erik"})
    rows = [_row(5, "Ann", "9:30am", "4:00pm", 6.5), _row(5, "Ben", "4:00pm", "11:00pm", 7)]
    out = sr.cover_manager_gaps(rows, c)
    assert "no_manager" not in _kinds(out["rows"], c), out
    assert not [v for v in sr.violations(out["rows"], c) if v["hard"]]


def test_the_rule_leads_the_models_instructions():
    c = _c()
    block = sr.prompt_block(c)
    assert block.index("NON-NEGOTIABLE") < block.index("hours in the payroll week")
    assert "Andrew (Manager FOH)" in block and "Erik (Owner)" in block


def test_generation_runs_the_backstop_and_checks_the_finished_rows_again():
    src = open(sr.__file__.replace("schedule_rules.py", "schedule_engine.py")).read()
    assert src.count("_rules.cover_manager_gaps(preview_rows, _constraints, editable=_editable)") == 2
    assert src.index("_rules.close_out_gaps(") < src.index("_rules.cover_manager_gaps(")
    assert src.index("_opt.optimize(") < src.rindex("_rules.cover_manager_gaps(")


# ── the person breaches the replace pass left (Erik's first week, 10/2/26) ──

def test_a_minor_past_the_limit_is_cut_to_it():
    c = _c(managers={}, minors={"alesx"}, minor_bands={"alesx": "16-17"})
    rows = [_row(3, "Alesx", "4:30pm", "10:30pm", 6, role="Busser PM")]
    assert "minor_late" in _kinds(rows, c)
    out = sr.fix_person_breaches(rows, c)
    assert out["fixes"] and out["rows"][0]["shift_end"] == "10:00pm" and out["rows"][0]["scheduled_hours"] == "5.5"
    assert "minor_late" not in _kinds(out["rows"], c)


def test_seven_days_in_a_row_hands_one_to_a_teammate_with_room():
    c = _c(managers={})
    c.compliance["max_consecutive_days"] = 6
    rows = [_row(i, "Jose", "8:30am", "2:30pm", 6, role="Dishwasher") for i in range(7)]
    rows += [_row(0, "Cesar", "4:00pm", "10:00pm", 6, role="Dishwasher")]
    assert "long_run" in _kinds(rows, c)
    out = sr.fix_person_breaches(rows, c, roster_roles={"Jose": "Dishwasher", "Cesar": "Dishwasher"})
    assert out["fixes"] and out["fixes"][0]["to"] == "Cesar"
    assert "long_run" not in _kinds(out["rows"], c)
    assert sum(1 for r in out["rows"] if r["employee"] == "Jose") == 6


def test_a_run_nobody_can_take_stays_flagged():
    c = _c(managers={})
    c.compliance["max_consecutive_days"] = 6
    rows = [_row(i, "Jose", "8:30am", "2:30pm", 6, role="Dishwasher") for i in range(7)]
    out = sr.fix_person_breaches(rows, c, roster_roles={"Jose": "Dishwasher"})
    assert not out["fixes"] and "long_run" in _kinds(out["rows"], c)


def test_days_off_in_a_row_are_no_rule_whatever_was_stored():
    """Owner, 10/2/26: "owners dont care about people not getting multiple
    days off in a row" — off by default and a stored value is ignored."""
    from models import Restaurant
    assert sr.DEFAULTS["min_consecutive_days_off"] is None and sr.DEFAULTS["part_time_days_off"] is None
    r = Restaurant(name="X", owner_email="o@x.test", compliance_json='{"min_consecutive_days_off": 2, "part_time_days_off": 3}')
    comp = sr.compliance(r)
    assert comp["min_consecutive_days_off"] is None and comp["part_time_days_off"] is None
    rows = [_row(i, "Ann", "11:00am", "3:00pm", 4, role="Server") for i in range(6)]
    assert "days_off" not in _kinds(rows, _c(managers={}, compliance=comp))


def test_a_leader_rule_on_an_am_or_pm_job_keeps_to_its_half_of_the_day():
    import models
    assert models.leader_rule_daypart("Host AM") == "morning"
    assert models.leader_rule_daypart("Server PM") == "night"
    assert models.leader_rule_daypart("Bartender") is None
