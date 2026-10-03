"""The trim and fill passes ask the rules (schedule audit 10/3/26, A1).

E-2 / P-13  the budget trim took away the night's only closer and the last
            manager on a night; the second trim ran without its scorer or
            the rain, after close-out.
P-27        the trim removed whole shifts only: a 2h overage cost a 7h shift.
P-29        the section-cap trim undid role floors and closers.
SQ-7        the trim protected the owner's floors only, not the requirement
            the coverage score judges; the conflict was never said.
L-24        the trim cut the weekday where cuts measured worse and the
            daypart that has gone wrong before.
P-20        the top-up's "thin" was measured against the draft's own average.
E-19        both section-cap sweeps skipped servers working past midnight.
P-10        the early passes changed days a partial redo kept.
P-40        the replacement picker swept the week twice per candidate.
E-1 / P-2   the fill passes could land a minor past their end, a seventh day
            in a row or a shift past the manager; replacement checks compared
            breaches by (row, kind), so a second manager gap was invisible.
"""
import inspect
import json
import sys

import pytest

import models
import schedule_economics as econ
import schedule_engine as se
import schedule_rules as sr

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MON, TUE, WED, THU, FRI, SAT, SUN = WEEK


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS))
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(date, emp, role, start, end, notes="", **extra):
    s, e = sr.parse_minutes(start), sr.parse_minutes(end)
    hours = round(((e - s) % (24 * 60)) / 60, 2)
    out = {"date": date, "day": DAYS[WEEK.index(date)], "employee": emp, "role": role, "shift_start": start,
           "shift_end": end, "scheduled_hours": f"{hours:g}", "notes": notes}
    out.update(extra)
    return out


def _need(date, role, n, part="night", half=None):
    """One SHIFT REQUIREMENTS row (schedule_requirements.shift_requirements)."""
    row = {"date": date, "day": DAYS[WEEK.index(date)], "daypart": part,
           "roles": [{"role": role, "required": n, "floor": 0, "typical": n}]}
    if half:
        row["half_hours"] = {role: half}
    return row


def _kinds(rows, c, kind):
    return [v for v in sr.violations(rows, c) if v["kind"] == kind]


@pytest.fixture
def no_avail(monkeypatch):
    monkeypatch.setattr(models, "get_staff_availability", lambda r, *a, **k: [])


# ── E-2 / P-13: the budget trim never takes the closer or the last manager ──

def test_the_budget_trim_never_takes_the_nights_only_closer():
    """The audit's t4: the later starter on Friday is the only closer — the
    trim's "latest starter first" took her and the night had no closer."""
    c = _c(keyholders={"kay"}, close_times={"Friday": "2:00am"}, roster_names=["Jo", "Kay"], active={"jo", "kay"})
    rows = [_row(FRI, "Jo", "Bartender", "4:00pm", "11:00pm"), _row(FRI, "Kay", "Bartender", "6:00pm", "2:00am"),
            _row(THU, "Jo", "Bartender", "4:00pm", "11:00pm"), _row(THU, "Kay", "Bartender", "6:00pm", "2:00am")]
    assert not _kinds(rows, c, "keyholder_until_close")
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 22, {}, constraints=c)
    assert removed > 0, "the week is over budget and Jo's hours can go"
    kay = [r for r in out if r["employee"] == "Kay"]
    assert len(kay) == 2 and all(r["shift_end"] == "2:00am" for r in kay)
    assert all(t["employee"] != "Kay" for t in trimmed)
    assert not _kinds(out, c, "keyholder_until_close")


def test_the_budget_trim_never_opens_a_manager_gap_and_says_why_it_stopped():
    """The audit's t5: two managers on Friday night; the trim took the one
    who stays to close and left 11pm-2am with no manager. Here Ida (a
    manager) works a bartender row to 2am beside another bartender, so no
    rule of the trim's own protects her — only the manager rule does."""
    c = _c(managers={"max": "Manager", "ida": "Manager"}, roster_names=["Max", "Ida", "Bo"],
           active={"max", "ida", "bo"}, close_times={"Friday": "2:00am"})
    rows = [_row(FRI, "Max", "Manager", "3:00pm", "11:00pm"), _row(FRI, "Ida", "Bartender", "5:00pm", "2:00am"),
            _row(FRI, "Bo", "Bartender", "10:00pm", "2:00am"), _row(THU, "Ida", "Manager", "3:00pm", "11:00pm")]
    assert not _kinds(rows, c, "no_manager")
    report = {}
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 20, {}, constraints=c, report=report)
    ida = next(r for r in out if r["employee"] == "Ida" and r["date"] == FRI)
    assert (ida["shift_start"], ida["shift_end"]) == ("5:00pm", "2:00am"), trimmed
    assert trimmed == [] and not _kinds(out, c, "no_manager")
    # Still over: what held the rest is said once, the manager rule among it.
    assert report["conflict"]["held"].get("rule"), report
    assert any(x["reason"] == "rule" and "manager" in x["label"] for x in report["conflict"]["examples"])
    # and Ida is also never the one taken when she stays latest in her role
    rows[2] = _row(FRI, "Bo", "Bartender", "10:00pm", "1:00am")
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 20, {}, constraints=c)
    assert any(r["employee"] == "Ida" and r["date"] == FRI and r["shift_end"] == "2:00am" for r in out)


def test_the_trim_never_touches_a_pinned_row():
    rows = [_row(MON, n, "Server", "4:00pm", "10:00pm") for n in ("Ana", "Bob")]
    rows.append(_row(MON, "Cy", "Server", "5:00pm", "11:00pm", _pinned="manager_plan"))
    rows += [_row(TUE, n, "Server", "4:00pm", "8:00pm") for n in ("Ana", "Bob", "Cy")]
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 20, {}, constraints=_c())
    cy = [r for r in out if r["employee"] == "Cy" and r["date"] == MON]
    assert cy and cy[0]["shift_end"] == "11:00pm" and cy[0]["shift_start"] == "5:00pm"
    assert not any(t["employee"] == "Cy" and t["date"] == MON for t in trimmed)


def test_the_post_fix_trim_has_its_scorer_rain_requirement_and_lessons():
    """The second trim (after the fix pass) ran with no score_fn and no
    rain, after close-out; it now runs exactly as the first."""
    src = inspect.getsource(se._run_schedule_job)
    calls = src.split("_econ.trim_to_budget(")[1:]
    assert len(calls) == 2
    for call in calls:
        body = call.split("report=", 1)[0]
        for kw in ("score_fn=", "rainy_dates=_rainy", "requirements=_reqs", "only_dates=_editable",
                   "learned_worse=result.get(\"learned_worse\")", "outcomes=result.get(\"outcomes_by_daypart\")",
                   "constraints=_constraints"):
            assert kw in body, kw


# ── P-27: tails first, the close stagger ─────────────────────────────────

def test_a_small_overage_ends_a_tail_instead_of_removing_a_shift():
    rows = [_row(d, n, "Server", "4:00pm", "11:00pm") for d in (MON, TUE) for n in ("Ana", "Bob", "Cy")]   # 42h
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 40, {}, constraints=_c())
    assert removed == 2.0 and len(out) == len(rows), "a 2h overage costs 2h, not a 7h shift"
    assert [t["kind"] for t in trimmed] == ["cut"]
    t = trimmed[0]
    assert t["shift_end"] == "11:00pm" and t["to"] == "9:00pm" and t["hours"] == 2.0
    cut = next(r for r in out if r["employee"] == t["employee"] and r["date"] == t["date"])
    assert cut["shift_end"] == "9:00pm" and cut["scheduled_hours"] == "5" and "hours budget" in cut["notes"]
    # the rest of the role stays on to close
    assert sum(1 for r in out if r["date"] == t["date"] and r["shift_end"] == "11:00pm") == 2


def test_the_first_in_is_the_first_cut_and_the_last_out_never_is():
    rows = [_row(MON, "Ana", "Server", "4:00pm", "11:00pm"), _row(MON, "Bob", "Server", "5:00pm", "11:00pm"),
            _row(MON, "Cy", "Server", "6:00pm", "11:00pm")]
    rows += [_row(TUE, n, "Server", "4:00pm", "8:00pm") for n in ("Ana", "Bob", "Cy")]
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 29, {}, constraints=_c())
    assert trimmed[0]["employee"] == "Ana" and trimmed[0]["kind"] == "cut"
    # a lone closer is never sent home early, and no shift goes under 4h
    for r in out:
        assert float(r["scheduled_hours"]) >= 4.0
    mon = sorted((sr.end_minutes(r), r["employee"]) for r in out if r["date"] == MON)
    assert mon[-1][0] == 23 * 60


def test_a_tail_is_never_cut_under_a_floor_through_the_hours_cut():
    floors = {"Cook": {"morning": 0, "night": 2, "days": {}}}
    rows = [_row(d, n, "Cook", "4:00pm", "10:00pm") for d in (MON, TUE) for n in ("Ana", "Bob")]
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 10, {}, constraints=_c(), floors=floors)
    assert trimmed == [] and removed == 0


def test_a_shift_cut_and_then_removed_is_one_entry():
    rows = [_row(MON, "Ana", "Cook", "4:00pm", "10:00pm"), _row(MON, "Bob", "Cook", "4:00pm", "10:00pm"),
            _row(MON, "Cy", "Host", "4:00pm", "10:00pm"), _row(TUE, "Ana", "Prep", "4:00pm", "10:00pm"),
            _row(TUE, "Bob", "Dish", "4:00pm", "10:00pm")]
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 6.0, {MON: 6}, constraints=_c(), floors={})
    assert removed == 6 and len(out) == len(rows) - 1
    assert len(trimmed) == 1 and trimmed[0]["kind"] == "removed" and trimmed[0]["hours"] == 6
    assert trimmed[0]["shift_end"] == "10:00pm" and "to" not in trimmed[0]


def test_trim_lines_say_what_ended_early_and_what_went():
    lines = econ.trim_lines([{"kind": "cut", "day": "Monday", "role": "Server", "shift_start": "4:00pm",
                              "shift_end": "11:00pm", "employee": "Ana", "reason": "ends 9:00pm, 2h early"},
                             {"kind": "removed", "day": "Tuesday", "role": "Server", "shift_start": "4:00pm",
                              "shift_end": "8:00pm", "employee": "Bob", "reason": "second leg of a double"}], 6, 120)
    assert lines[0] == "Trimmed 6h to fit the 120h budget: 1 shift removed, 1 ended early."


# ── SQ-7: the trim respects the requirement and names the conflict once ──

def test_the_trim_never_cuts_under_the_requirement_and_names_the_conflict():
    rows = [_row(d, n, "Server", "4:00pm", "8:00pm") for d in (THU, FRI) for n in ("Ana", "Bob", "Cy")]
    reqs = [_need(FRI, "Server", 3), _need(THU, "Server", 3)]
    report = {}
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 12, {}, constraints=_c(),
                                                requirements=reqs, report=report)
    assert trimmed == [] and len(out) == 6, "every server is what a shift needs"
    conflict = report["conflict"]
    assert conflict["over_by"] == 12 and conflict["held"]["requirement"] == 6
    line = econ.budget_conflict_line(conflict)
    assert "12h over the 12h budget" in line and "Thursday dinner needs 3 servers" in line
    assert "Raise the hours budget" in line
    # without the requirement the old floors-only trim removes them
    out2, trimmed2, _ = econ.trim_to_budget([dict(r) for r in rows], 12, {}, constraints=_c())
    assert trimmed2


def test_the_half_hour_ramp_is_held_through_a_tail_cut():
    # three servers until 11pm; dinner needs 3 from 7pm and 2 from 9pm
    rows = [_row(d, n, "Server", "4:00pm", "11:00pm") for d in (FRI, THU) for n in ("Ana", "Bob", "Cy")]
    half = [(17 * 60, 2), (19 * 60, 3), (21 * 60, 2)]
    reqs = [_need(FRI, "Server", 2, half=half), _need(THU, "Server", 2, half=half)]
    c = _c(close_times={"Friday": "11:00pm", "Thursday": "11:00pm"})
    report = {}
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 30, {}, constraints=c,
                                                requirements=reqs, report=report)
    # one tail per night to 9pm: a second would leave one server on after 9pm
    assert sorted((t["date"], t["kind"], t["to"]) for t in trimmed) == [(THU, "cut", "9:00pm"), (FRI, "cut", "9:00pm")]
    assert report["conflict"]["held"]["requirement"]
    assert any(x.get("at") for x in report["conflict"]["examples"] if x["reason"] == "requirement")


def test_the_engine_says_the_budget_conflict_once_beside_the_over_budget_line():
    src = inspect.getsource(se._run_schedule_job)
    block = src.split("over the ceiling\")", 1)[1].split("result[\"review\"][\"trimmed\"]", 1)[0]
    assert "budget_conflict_line(" in block and "lines\"].insert(1, _bcl)" in block
    assert "result[\"review\"][\"budget_conflict\"] = _bc" in block


# ── L-24: what was learned is protected ──────────────────────────────────

def test_the_trim_keeps_off_a_day_cuts_measured_worse_and_a_troubled_daypart():
    rows = [_row(d, n, "Server", "4:00pm", "8:00pm") for d in (FRI, THU, WED) for n in ("Ana", "Bob", "Cy")]
    worse = {"friday": {"worsened": 2, "measured": 3, "label": "staffing cuts on Fridays"}}
    troubled = {"Thursday": {"night": {"weeks": 6, "troubled": True}}}
    out, trimmed, removed = econ.trim_to_budget([dict(r) for r in rows], 28, {}, constraints=_c(),
                                                learned_worse=worse, outcomes=troubled)
    assert trimmed and {t["date"] for t in trimmed} == {WED}
    report = {}
    econ.trim_to_budget([dict(r) for r in rows if r["date"] != WED], 16, {}, constraints=_c(),
                        learned_worse=worse, outcomes=troubled, report=report)
    held = report["conflict"]["held"]
    assert held.get("learned") == 3 and held.get("troubled") == 3
    line = econ.budget_conflict_line(report["conflict"])
    assert "Fridays, where staffing cuts measured worse here" in line and "Thursday dinner" in line


def test_the_engine_hands_both_trims_what_was_learned():
    src = inspect.getsource(se._run_schedule_job)
    assert src.count("learned_worse=result.get(\"learned_worse\")") == 2
    assert src.count("outcomes=result.get(\"outcomes_by_daypart\")") == 2


# ── P-20: thin is measured against the requirement ─────────────────────

def test_a_quiet_monday_is_not_thin_against_a_busy_weekend(no_avail):
    rows = [_row(MON, n, "Server", "4:00pm", "9:00pm") for n in ("Ana", "Bob")]
    for d in (FRI, SAT):
        rows += [_row(d, n, "Server", "4:00pm", "9:00pm") for n in ("Cy", "Dee", "Eve", "Fay", "Gus")]
    c = _c(roster_roles={"Hal": "Server"})
    reqs = [_need(MON, "Server", 2), _need(FRI, "Server", 5), _need(SAT, "Server", 5)]
    out, added, dates = se._top_up_hours_gap([dict(r) for r in rows], {d: 40.0 for d in WEEK}, 400.0, 60.0, 1,
                                             {}, {}, constraints=c, requirements=reqs)
    assert dates == {} and added == 0, "Monday meets its own requirement; the week's average is not a requirement"


def test_a_busy_day_the_model_staffed_like_any_other_is_thin(no_avail):
    rows = [_row(d, n, "Server", "4:00pm", "9:00pm") for d in (MON, TUE, FRI) for n in ("Ana", "Bob", "Cy")]
    c = _c(roster_roles={"Hal": "Server", "Ivy": "Server"})
    reqs = [_need(MON, "Server", 3), _need(TUE, "Server", 3), _need(FRI, "Server", 5)]
    out, added, dates = se._top_up_hours_gap([dict(r) for r in rows], {d: 40.0 for d in WEEK}, 400.0, 45.0, 1,
                                             {}, {}, constraints=c, requirements=reqs)
    assert dates == {FRI: 2}
    assert sorted(r["employee"] for r in out if r["date"] == FRI and "top-up" in r["notes"]) == ["Hal", "Ivy"]


def test_the_top_up_covers_the_half_hours_the_ramp_is_short(no_avail):
    # three servers 4-9pm meet the crew; the ramp needs two on until 11pm
    rows = [_row(FRI, n, "Server", "4:00pm", "9:00pm") for n in ("Ana", "Bob", "Cy")]
    c = _c(roster_roles={"Hal": "Server", "Ivy": "Server"}, close_times={"Friday": "11:00pm"})
    reqs = [_need(FRI, "Server", 3, half=[(17 * 60, 3), (21 * 60, 2)])]
    out, added, dates = se._top_up_hours_gap([dict(r) for r in rows], {FRI: 40.0}, 400.0, 15.0, 1,
                                             {}, {}, constraints=c, requirements=reqs)
    new = [r for r in out if "top-up" in r["notes"]]
    assert len(new) == 2 and all(sr.end_minutes(r) == 23 * 60 for r in new), new
    assert not se.requirement_shortfall(out, se.requirement_index(reqs, c), FRI, c)


def test_the_engine_builds_the_requirements_once_and_hands_them_on():
    src = inspect.getsource(se._run_schedule_job)
    assert "_reqs = week_requirements(restaurant_id, result, _constraints, curve=_curve)" in src
    top = src.split("_top_up_hours_gap(", 1)[1].split("if hours_added", 1)[0]
    assert "requirements=_reqs" in top
    stagger = src.split("stagger_same_starts(", 1)[1].split("result[\"staggered\"]", 1)[0]
    assert "requirements=_reqs" in stagger


def test_week_requirements_are_the_prompts_numbers():
    c = _c(role_floors={"Cook": {"morning": 0, "night": 2, "days": {}}}, close_times={"Friday": "11:00pm"},
           closed_dates={MON})
    result = {"week_dates": list(WEEK), "typical_headcount": {("Friday", "night"): {"Server": 4, "Cook": 1}}}
    rows = se.week_requirements(1, result, c, curve={"Friday": {17: 0.2, 18: 0.3, 19: 0.4, 20: 0.1}})
    fri = next(r for r in rows if r["date"] == FRI and r["daypart"] == "night")
    assert {x["role"]: x["required"] for x in fri["roles"]} == {"Server": 4, "Cook": 2}
    assert not any(r["date"] == MON for r in rows), "a closed day needs nobody"
    idx = se.requirement_index(rows, c)
    assert idx[FRI]["night"]["server"]["required"] == 4
    assert idx[FRI]["night"]["server"]["half"], "the half-hour ramp rides along"


# ── P-2 / E-1: the fill passes ask can_add, fillable and the day's rules ──

def _cook_week(names_by_day):
    return [_row(d, n, "Cook", "5:00pm", "11:00pm") for d, names in names_by_day.items() for n in names]


def test_the_floor_pass_never_lands_a_minor_past_their_end_or_a_seventh_day(no_avail):
    floors = {"Cook": {"morning": 0, "night": 1, "days": {}}}
    # Sev cooks Tuesday to Sunday: a Monday shift is his seventh day in a row
    rows = _cook_week({d: ["Sev"] for d in WEEK[1:]})
    c = _c(roster_roles={"Kid": "Cook", "Sev": "Cook", "Ok": "Cook"}, minors={"kid"}, minor_bands={"kid": "16-17"},
           roster_names=["Kid", "Sev", "Ok"], active={"kid", "sev", "ok"})
    c.compliance["minor_latest_end"] = "10:00pm"
    out, added, dates = se._ensure_role_floors([dict(r) for r in rows], WEEK, DAYS, 1, {}, {}, floors=floors,
                                               constraints=c)
    mon = [r for r in out if r["date"] == MON]
    assert [r["employee"] for r in mon] == ["Ok"], mon
    # with nobody legal the floor stays open (and flagged), never filled illegally
    c.roster_roles = {"Kid": "Cook", "Sev": "Cook"}
    out, added, dates = se._ensure_role_floors([dict(r) for r in rows], WEEK, DAYS, 1, {}, {}, floors=floors,
                                               constraints=c)
    assert MON not in dates
    assert not _kinds(out, c, "minor_late") and not _kinds(out, c, "long_run")


def test_the_station_pass_never_lands_a_minor_past_their_end(no_avail):
    import kitchen_stations as ks
    c = _c(roster_names=["Kid", "Bo"], active={"kid", "bo"}, minors={"kid"}, minor_bands={"kid": "16-17"})
    c.compliance["minor_latest_end"] = "10:00pm"
    c.stations = ks.normalise({"roles": ["Kitchen"], "stations": ["Grill", "Saute"],
                               "needs": [{"station": "Saute", "daypart": "night", "days": ["Monday"]}],
                               "skills": {"Kid": ["Saute"], "Bo": ["Grill"]}})
    rows = [_row(MON, "Bo", "Kitchen", "4:00pm", "11:00pm")]
    out, added, unfilled = se._ensure_station_coverage([dict(r) for r in rows], WEEK, DAYS, {}, {}, constraints=c)
    assert added == 0 and unfilled and unfilled[0]["station"] == "Saute"
    assert not _kinds(out, c, "minor_late")


def test_the_top_up_never_adds_a_seventh_day(no_avail):
    rows = [_row(d, n, "Server", "4:00pm", "9:00pm") for d in WEEK[1:] for n in ("Ana", "Bob")]
    rows.append(_row(MON, "Ana", "Server", "4:00pm", "9:00pm"))
    reqs = [_need(MON, "Server", 2)]
    out, added, dates = se._top_up_hours_gap([dict(r) for r in rows], {d: 40.0 for d in WEEK}, 400.0, 65.0, 1,
                                             {}, {}, constraints=_c(), requirements=reqs)
    assert dates == {}, "Bob's Monday would be his seventh day in a row"


def test_a_fill_never_opens_a_manager_gap(no_avail):
    floors = {"Dish": {"morning": 0, "night": 1, "days": {}}}
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ana", "Dee"], active={"max", "ana", "dee"},
           roster_roles={"Dee": "Dish"})
    # the manager is on until 10pm, the floor's only template runs to 11pm
    rows = [_row(MON, "Max", "Manager", "3:00pm", "10:00pm"), _row(MON, "Ana", "Server", "4:00pm", "10:00pm"),
            _row(TUE, "Dee", "Dish", "5:00pm", "11:00pm")]
    out, added, dates = se._ensure_role_floors([dict(r) for r in rows], [MON], ["Monday"], 1, {}, {},
                                               floors=floors, constraints=c)
    assert MON not in dates, "a dish shift to 11pm would leave 10-11pm with no manager"
    assert not [v for v in _kinds(out, c, "no_manager") if v["date"] == MON]
    # a template inside the manager's hours is added
    rows[2] = _row(TUE, "Dee", "Dish", "5:00pm", "10:00pm")
    out, added, dates = se._ensure_role_floors([dict(r) for r in rows], [MON], ["Monday"], 1, {}, {},
                                               floors=floors, constraints=c)
    assert dates == {MON: 1} and not [v for v in _kinds(out, c, "no_manager") if v["date"] == MON]


def test_a_floor_takes_somebody_under_their_overtime_line_first_and_overtime_only_as_a_last_resort(no_avail):
    floors = {"Cook": {"morning": 0, "night": 1, "days": {}}}
    c = _c(roster_roles={"Ann": "Cook", "Ben": "Cook"}, roster_names=["Ann", "Ben"], active={"ann", "ben"})
    c.compliance["weekly_hours_ceiling"] = 50.0        # the owner allows hours past 40
    # Ann has the fewest hours this week but 30h already published in the
    # payroll week: a 6h shift is overtime for her, not for Ben
    c.base_hours = {"ann": {c.bucket(MON): 30.0}}
    rows = [_row(TUE, "Ann", "Cook", "5:00pm", "11:00pm"), _row(TUE, "Ben", "Cook", "9:00am", "3:00pm"),
            _row(WED, "Ben", "Cook", "9:00am", "3:00pm")]
    out, added, dates = se._ensure_role_floors([dict(r) for r in rows], [MON], ["Monday"], 1, {}, {},
                                               floors=floors, constraints=c)
    assert [r["employee"] for r in out if r["date"] == MON] == ["Ben"]
    # nobody under their line: the floor (above overtime in the ranking) is filled past it, never past 50h
    c.roster_roles = {"Ann": "Cook"}
    out, added, dates = se._ensure_role_floors([dict(r) for r in rows if r["employee"] == "Ann"], [MON], ["Monday"],
                                               1, {}, {}, floors=floors, constraints=c)
    assert [r["employee"] for r in out if r["date"] == MON] == ["Ann"]
    # the top-up (below overtime in the ranking) never does
    reqs = [_need(MON, "Cook", 2)]
    base = [_row(MON, "Zed", "Cook", "5:00pm", "11:00pm")] + [r for r in rows if r["employee"] == "Ann"]
    out, h, dates = se._top_up_hours_gap([dict(r) for r in base], {MON: 40.0}, 400.0, 12.0, 1, {}, {},
                                         constraints=c, requirements=reqs)
    assert dates == {}


# ── E-1: the replacement check compares breaches by identity ──────────────

def test_a_replacement_that_opens_a_second_manager_gap_is_refused(monkeypatch):
    """The audit's t3: a day with a 9-10am gap already; handing the
    manager's evening bartender row to a non-manager opened 3-10pm too, and
    the (row, kind) comparison never saw it."""
    monkeypatch.setattr(models, "get_unavailability_map", lambda *a, **k: {})
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ann", "Bob"], active={"max", "ann", "bob"})
    rows = [_row(WED, "Ann", "Server", "9:00am", "9:00pm"), _row(WED, "Max", "Manager", "10:00am", "3:00pm"),
            _row(WED, "Max", "Bartender", "3:00pm", "10:00pm"), _row(THU, "Bob", "Bartender", "3:00pm", "10:00pm")]
    ok, why = se.replacement_is_legal(1, rows, 2, "Bob", constraints=c)
    assert not ok and "no manager" in why
    # the control: an ordinary row with no gap behind it is fine
    ok, why = se.replacement_is_legal(1, rows, 0, "Bob", constraints=c)
    assert ok, why


def test_a_staffing_gap_the_week_already_had_is_not_the_newcomers(monkeypatch):
    monkeypatch.setattr(models, "get_unavailability_map", lambda *a, **k: {})
    c = _c(keyholders={"kay"}, close_times={"Wednesday": "11:00pm"}, roster_names=["Ann", "Bob", "Kay"],
           active={"ann", "bob", "kay"})
    rows = [_row(WED, "Ann", "Server", "4:00pm", "10:00pm"), _row(THU, "Kay", "Server", "4:00pm", "11:00pm")]
    assert _kinds(rows, c, "keyholder_until_close"), "Wednesday already has nobody who can close"
    ok, why = se.replacement_is_legal(1, rows, 0, "Bob", constraints=c)
    assert ok, why


# ── P-40: the picker prepares the week once ──────────────────────────────

def test_the_replacement_check_reads_availability_and_sweeps_the_week_once(monkeypatch):
    reads, sweeps = [], []
    monkeypatch.setattr(models, "get_unavailability_map", lambda *a, **k: reads.append(1) or {})
    real = sr.breach_profile
    monkeypatch.setattr(sr, "breach_profile", lambda *a, **k: sweeps.append(1) or real(*a, **k))
    c = _c(roster_names=[f"P{i}" for i in range(10)], active={f"p{i}" for i in range(10)})
    rows = [_row(WED, "P0", "Server", "4:00pm", "10:00pm")]
    prepared = se.prepare_replacements(1, rows, constraints=c)
    for i in range(1, 10):
        assert se.replacement_is_legal(1, rows, 0, f"P{i}", constraints=c, prepared=prepared)[0]
    assert len(reads) == 1
    assert len(sweeps) == 1 + 9, "the week as it stands once, then one trial per candidate"


def test_the_picker_route_prepares_once():
    import mobile_api
    src = inspect.getsource(mobile_api.mobile_schedule_replacements)
    assert "prepared = _se.prepare_replacements(rid, rows, constraints=c)" in src
    assert "prepared=prepared" in src.split("for low, (name, their_role)", 1)[1]


# ── E-19: the section cap past midnight ──────────────────────────────────

def test_the_section_cap_rule_counts_servers_working_past_midnight():
    """The audit's t2: five servers 6pm-12:30am against four sections read
    clean; the same servers ending 11:30pm were flagged."""
    c = _c(section_cap=4, foh_roles={"server"})
    late = [_row(FRI, f"S{i}", "Server", "6:00pm", "12:30am") for i in range(5)]
    early = [_row(FRI, f"S{i}", "Server", "6:00pm", "11:30pm") for i in range(5)]
    assert len(_kinds(late, c, "over_section_cap")) == 1
    assert len(_kinds(early, c, "over_section_cap")) == 1
    # the cap counts the role family: "Server PM" is a server
    pm = [_row(FRI, f"S{i}", "Server PM", "6:00pm", "1:00am") for i in range(5)]
    assert len(_kinds(pm, c, "over_section_cap")) == 1


def test_the_cap_backstop_sees_and_cuts_past_midnight():
    rows = [_row(SAT, "S0", "Server", "6:00pm", "12:30am", notes="added — coverage top-up")]
    rows += [_row(SAT, f"S{i}", "Server", f"{6 + i // 2}:{'30' if i % 2 else '00'}pm", "12:30am") for i in range(1, 5)]
    assert se._peak_server_overlap(rows) == (5, 20 * 60)
    out, n, dates = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=4)
    assert n == 1 and se._peak_server_overlap(out)[0] == 4
    s0 = next(r for r in out if r["employee"] == "S0")
    assert s0["shift_end"] == "8:00pm" and s0["scheduled_hours"] == "2.0", "the top-up row absorbs the cut"


def test_the_optimizer_cap_check_reads_past_midnight_too():
    import schedule_optimizer as so
    src = inspect.getsource(so.optimize)
    assert "_peak_server_overlap" in src
    assert se._peak_server_overlap([_row(FRI, "A", "Server", "10:00pm", "2:00am"),
                                    _row(FRI, "B", "Server", "11:00pm", "1:00am")]) == (2, 23 * 60)


# ── P-29: the section-cap trim never undoes a floor or a closer ──────────

def test_the_cap_trim_stops_at_a_floor_and_names_the_conflict():
    floors = {"Server": {"morning": 0, "night": 4, "days": {}}}
    rows = [_row(SAT, n, "Server", "5:00pm", "10:00pm") for n in ("Ana", "Bob", "Cy", "Dee")]
    report = {}
    out, n, dates = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=3,
                                                constraints=_c(role_floors=floors), report=report)
    assert n == 0 and len(out) == 4
    conf = report["conflicts"]
    assert conf and conf[0]["date"] == SAT and conf[0]["on"] == 4 and conf[0]["cap"] == 3
    assert conf[0]["held_by"][0] == {"kind": "floor", "role": "Server", "daypart": "night", "floor": 4}
    line = se.cap_floor_conflict_line(conf)
    assert line.startswith("Saturday 10/10/26 at 5:00pm: 4 on the floor against 3 sections")
    assert "under your floor of 4 for dinner" in line and "change one of them" in line
    # the settings conflict, said before any draft is built
    static = se.floor_cap_conflicts(floors, 3, ["server"])
    assert len(static) == 7 and static[0] == {"day": "Monday", "daypart": "night", "floor": 4, "cap": 3,
                                              "roles": ["Server"]}
    assert se.floor_cap_conflicts(floors, 4, ["server"]) == [] and se.floor_cap_conflicts(floors, 0, None) == []


def test_the_cap_trim_never_takes_the_closer():
    c = _c(keyholders={"kay"}, close_times={"Saturday": "11:00pm"}, roster_names=["Ann", "Bob", "Kay"],
           active={"ann", "bob", "kay"})
    rows = [_row(SAT, "Ann", "Server", "4:00pm", "10:00pm"), _row(SAT, "Bob", "Server", "5:00pm", "11:00pm"),
            _row(SAT, "Kay", "Server", "6:00pm", "11:00pm")]
    out, n, dates = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=2, constraints=c)
    assert n >= 1
    kay = next(r for r in out if r["employee"] == "Kay")
    assert (kay["shift_start"], kay["shift_end"]) == ("6:00pm", "11:00pm")
    assert not _kinds(out, c, "keyholder_until_close")


def test_the_rules_screen_carries_the_floor_cap_conflict(db_path, monkeypatch):
    import strategy_routes
    from models import Restaurant, create_restaurant, update_restaurant
    real = models.get_conn
    for mod in (models, se, sr):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = create_restaurant(Restaurant(name="Caps", owner_email="caps@x.com"), db_path=db_path)
    update_restaurant(rid, {"module_labor": 1, "section_count": 3,
                            "role_floors_json": json.dumps({"Server": {"morning": 0, "night": 4}})}, db_path=db_path)
    owner = {"id": 1, "restaurant_id": rid, "role": "client", "is_admin": False, "username": "o"}
    payload, status = strategy_routes._do_compliance_get(owner)
    assert status == 200 and len(payload["floor_cap_conflicts"]) == 7
    monkeypatch.setattr(strategy_routes, "_body", lambda: {"section_count": 5})
    payload, status = strategy_routes._do_compliance_set(owner)
    assert status == 200 and payload["floor_cap_conflicts"] == []


def test_the_engine_names_the_cap_conflict_once():
    src = inspect.getsource(se._run_schedule_job)
    assert "report=_cap_report" in src and "cap_floor_conflict_line(" in src
    cap_call = src.split("_trim_server_overlap_cap(", 1)[1].split("result[\"cap_floor_conflicts\"]", 1)[0]
    assert "floors=_constraints.role_floors" in cap_call and "constraints=_constraints" in cap_call


# ── P-10: a partial redo never changes the days the owner kept ──────────

def test_every_early_pass_keeps_to_the_days_being_redone(no_avail):
    keep = {TUE}
    # role floors: Monday is short, but only Tuesday is being redone
    floors = {"Cook": {"morning": 0, "night": 1, "days": {}}}
    rows = [_row(WED, "Ana", "Cook", "5:00pm", "11:00pm")]
    c = _c(roster_roles={"Ana": "Cook", "Bob": "Cook"})
    out, added, dates = se._ensure_role_floors([dict(r) for r in rows], [MON, TUE], ["Monday", "Tuesday"], 1, {}, {},
                                               floors=floors, constraints=c, only_dates=keep)
    assert set(dates) == {TUE}
    # the top-up
    rows = [_row(MON, "Ana", "Server", "4:00pm", "9:00pm"), _row(TUE, "Bob", "Server", "4:00pm", "9:00pm")]
    c = _c(roster_roles={"Hal": "Server", "Ivy": "Server"})
    reqs = [_need(MON, "Server", 2), _need(TUE, "Server", 2)]
    out, h, dates = se._top_up_hours_gap([dict(r) for r in rows], {MON: 40.0, TUE: 40.0}, 400.0, 10.0, 1, {}, {},
                                         constraints=c, requirements=reqs, only_dates=keep)
    assert set(dates) == {TUE}
    # the section cap
    rows = [_row(d, n, "Server", "5:00pm", "10:00pm") for d in (MON, TUE) for n in ("A", "B", "C")]
    out, n, dates = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=2, only_dates=keep)
    assert set(dates) == {TUE} and sum(1 for r in out if r["date"] == MON) == 3
    # the stagger
    rows = [_row(d, n, "Server", "5:00pm", "10:00pm") for d in (MON, TUE) for n in ("A", "B", "C")]
    curve = {"Monday": {17: 0.1, 18: 0.2, 19: 0.4, 20: 0.3}, "Tuesday": {17: 0.1, 18: 0.2, 19: 0.4, 20: 0.3}}
    out, changes = econ.stagger_same_starts([dict(r) for r in rows], curve, only_dates=keep)
    assert changes and {ch["date"] for ch in changes} == {TUE}
    # the stations
    import kitchen_stations as ks
    c = _c(active={"ana", "bo"}, roster_names=["Ana", "Bo"])
    c.stations = ks.normalise({"roles": ["Kitchen"], "stations": ["Grill"],
                               "needs": [{"station": "Grill", "daypart": "night"}], "skills": {"Ana": ["Grill"]}})
    rows = [_row(MON, "Bo", "Kitchen", "4:00pm", "10:00pm"), _row(TUE, "Bo", "Kitchen", "4:00pm", "10:00pm")]
    out, added, unfilled = se._ensure_station_coverage([dict(r) for r in rows], [MON, TUE], ["Monday", "Tuesday"],
                                                       {}, {}, constraints=c, only_dates=keep)
    assert {r["date"] for r in out if r["employee"] == "Ana"} == {TUE}


def test_the_engine_threads_the_redo_days_through_every_early_pass():
    src = inspect.getsource(se._run_schedule_job)
    for call in ("_ensure_role_floors(", "_ensure_station_coverage(", "_top_up_hours_gap(",
                 "_trim_server_overlap_cap(", "stagger_same_starts("):
        for seg in src.split(call)[1:]:
            assert "only_dates=_editable" in seg.split("\n            if ", 1)[0], call


def test_a_pinned_row_is_never_staggered_or_cut_at_the_cap():
    rows = [_row(SAT, n, "Server", "5:00pm", "10:00pm") for n in ("A", "B")]
    rows.append(_row(SAT, "C", "Server", "5:00pm", "10:00pm", _pinned="kept"))
    curve = {"Saturday": {17: 0.1, 18: 0.2, 19: 0.4, 20: 0.3}}
    out, changes = econ.stagger_same_starts([dict(r) for r in rows], curve)
    assert all(ch["employee"] != "C" for ch in changes)
    rows = [_row(SAT, "A", "Server", "4:00pm", "10:00pm"), _row(SAT, "B", "Server", "4:30pm", "10:00pm"),
            _row(SAT, "C", "Server", "5:00pm", "10:00pm", _pinned="kept")]
    out, n, dates = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=2)
    c_row = next(r for r in out if r["employee"] == "C")
    assert n == 1 and c_row["shift_end"] == "10:00pm"


def test_a_stagger_never_opens_a_manager_gap():
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "A", "B"], active={"max", "a", "b"})
    rows = [_row(SAT, "A", "Bartender", "5:00pm", "11:00pm"), _row(SAT, "B", "Bartender", "5:00pm", "11:00pm"),
            _row(SAT, "Max", "Bartender", "5:00pm", "11:00pm")]
    curve = {"Saturday": {17: 0.1, 18: 0.2, 19: 0.4, 20: 0.3}}
    out, changes = econ.stagger_same_starts([dict(r) for r in rows], curve, constraints=c)
    assert all(ch["employee"] != "Max" for ch in changes) or not _kinds(out, c, "no_manager")
    assert not _kinds(out, c, "no_manager")


# ── the job end to end: requirements built, the trim held, said once ─────

@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def test_the_job_trims_to_the_requirement_and_says_the_conflict_once(db, monkeypatch):
    from models import Restaurant, create_restaurant
    rid = create_restaurant(Restaurant(name="Trim Co", owner_email="trim@x.com"), db_path=db)
    header = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
    lines = [f"{d},{DAYS[WEEK.index(d)]},{n},Server,4:00pm,11:00pm,7," for d in (THU, FRI) for n in ("Ana", "Bob", "Cy")]
    base = {"ok": True, "schedule_csv": header + "\n" + "\n".join(lines), "week_dates": list(WEEK),
            "week_days": list(DAYS), "summary": [], "hours_budget": 20, "daily_target_hours": {THU: 10, FRI: 10},
            "labor_target": 30, "blended_rate": 20.0,
            # what the model was told each night needs: three servers
            "typical_headcount": {("Thursday", "night"): {"Server": 3}, ("Friday", "night"): {"Server": 3}}}
    monkeypatch.setattr(se, "_build_schedule_result", lambda r, week_start=None: dict(base))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda job_id, status, result: finished.update(status=status, result=result))
    se._run_schedule_job("trim-job", rid)
    assert finished["status"] == "done", finished
    res = finished["result"]
    for d in (THU, FRI):
        assert sum(1 for r in res["preview_rows"] if r["date"] == d) == 3, "every server is what the night needs"
    assert all(t["kind"] == "cut" for t in res["trimmed"]), res["trimmed"]
    lines = res["review"]["lines"]
    assert lines[0].startswith("⚠") and "over the ceiling" in lines[0]
    assert sum(1 for x in lines if x.startswith("The trim stopped")) == 1
    assert "needs 3 servers" in lines[1]
    assert res["review"]["budget_conflict"]["held"]["requirement"]


def test_the_cap_trim_prefers_a_cut_that_keeps_the_requirement():
    # four servers and the night's one bartender against four sections: the
    # bartender starts last, but the night needs one; a server is spare
    rows = [_row(SAT, n, "Server", "5:00pm", "10:00pm") for n in ("Ana", "Bob", "Cy", "Dee")]
    rows.append(_row(SAT, "Bea", "Bartender", "6:00pm", "11:00pm"))
    roles = {"server", "bartender"}
    out, n, _d = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=4, roles=roles)
    assert "Bea" not in {r["employee"] for r in out}          # by its own order: last in, first cut
    reqs = [{"date": SAT, "day": "Saturday", "daypart": "night",
             "roles": [{"role": "Server", "required": 3}, {"role": "Bartender", "required": 1}]}]
    out, n, _d = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=4, roles=roles,
                                             requirements=reqs)
    assert n == 1 and any(r["employee"] == "Bea" and r["shift_end"] == "11:00pm" for r in out)
    assert se._peak_server_overlap(out)[0] == 4
