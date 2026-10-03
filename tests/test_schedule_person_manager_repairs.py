"""Person and manager repairs (schedule audit 10/3/26, workstream A2).

One rule for a person's weekly maximum everywhere (P-12, D-18); a minor's
daily cap and daily overtime summed over the whole day (E-6); minors' weekly
cap and earliest start repaired (P-5, E-27); a run of days fixed until legal
(P-30); overtime moved to cross-trained teammates, second legs and splits
(P-31); minimum hours filled (P-4); the manager backstop's cap, ranking,
second legs, gap-sized shifts and acting managers (E-12, E-13, E-16, E-17);
real hours on the night the clocks change (E-23); a start in the small hours
read as its night (E-32); and the payroll week's reserve for next week (E-10).
"""
import inspect
from datetime import date, timedelta

import schedule_rules as sr
import shift_quality as sq

WEEK = [(date(2026, 10, 5) + timedelta(days=i)).isoformat() for i in range(7)]      # Mon 10/5 - Sun 10/11
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS))
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(i, emp, start, end, role="Server", d=None):
    r = {"date": d or WEEK[i], "day": DAYS[i] if d is None else sr._weekday_of(d), "employee": emp, "role": role,
         "shift_start": start, "shift_end": end, "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    return r


def _kinds(rows, c):
    return [v["kind"] for v in sr.violations(rows, c)]


def _hours(rows, name):
    return sum(float(r["scheduled_hours"]) for r in rows if r["employee"] == name)


def _hard(rows, c):
    return [v for v in sr.violations(rows, c) if v["hard"]]


# ── P-12 / D-18: an owner-set maximum above the ceiling is one answer ─────────

def test_a_forty_to_forty_five_cook_is_held_to_45_by_the_code_and_the_prompt():
    c = _c(roster_names=["Cook"], active={"cook"}, hours_limits={"cook": (40, 45)})
    assert c.max_hours("Cook") == 45 and sr.overtime_line(c, "Cook") == 45
    week44 = [_row(i, "Cook", "8:00am", "4:00pm", "Line Cook") for i in range(5)] + \
             [_row(5, "Cook", "8:00am", "12:00pm", "Line Cook")]
    assert _hours(week44, "Cook") == 44
    assert "over_max_hours" not in _kinds(week44, c)
    # the rebalance leaves overtime the owner allowed alone
    out = sr.rebalance_overtime(week44, c, roster_roles={"Cook": "Line Cook"})
    assert not out["moves"] and not out["trims"] and not out["left"]
    week46 = week44[:-1] + [_row(5, "Cook", "8:00am", "2:00pm", "Line Cook")]
    assert "over_max_hours" in _kinds(week46, c)
    block = sr.prompt_block(c)
    assert "at most 45h (overtime past 40h allowed for them)" in block
    assert "except where a person's own maximum below is higher" in block


def test_the_swap_index_reads_the_same_maximum():
    import schedule_engine
    c = _c(roster_names=["Cook", "Ann"], active={"cook", "ann"}, hours_limits={"cook": (40, 45)},
           salaried={"erik"})
    rules = schedule_engine._rules_for_swaps(c)
    assert rules["caps"]["cook"] == 45 and rules["caps"]["ann"] == 40
    assert rules["caps"]["erik"] == sr.SALARIED_HOURS_CAP
    idx = sq._SwapIndex([], {}, {}, rules)
    assert idx.cap("cook") == 45
    # without the computed caps the index still lets an own maximum stand
    assert sq._SwapIndex([], {}, {}, {"hours_limits": {"Cook": (None, 45)}}).cap("cook") == 45


# ── E-6: the day's hours, every leg of it ────────────────────────────────

def test_a_16_17_double_is_flagged_like_the_same_hours_in_one_shift():
    c = _c(roster_names=["Host"], active={"host"}, minors={"host"}, minor_bands={"host": "16-17"})
    c.compliance["minor_max_daily_hours"] = 8
    split = [_row(5, "Host", "10:00am", "3:00pm", "Host"), _row(5, "Host", "4:00pm", "9:00pm", "Host")]
    flags = [v for v in sr.violations(split, c) if v["kind"] == "minor_hours"]
    assert len(flags) == 1 and flags[0]["index"] == 1, "the leg that crosses the cap carries it"
    assert flags[0]["hard"] and flags[0]["severity"] == 2
    one = [_row(5, "Host", "10:00am", "8:00pm", "Host")]
    assert [v["kind"] for v in sr.violations(one, c) if v["kind"] == "minor_hours"] == ["minor_hours"]


def test_daily_overtime_is_summed_over_a_split_day_and_agrees_with_the_price():
    import schedule_economics as econ
    c = _c(roster_names=["Sam"], active={"sam"})
    c.compliance["daily_ot_hours"] = 8
    split = [_row(4, "Sam", "10:00am", "4:00pm"), _row(4, "Sam", "5:00pm", "11:00pm")]
    flags = [v for v in sr.violations(split, c) if v["kind"] == "daily_ot"]
    assert len(flags) == 1 and flags[0]["index"] == 1 and flags[0]["severity"] == 4
    assert "2 shifts" in flags[0]["detail"]
    prof = sr.breach_profile(split, c)
    assert prof["by_id"][("daily_ot", WEEK[4], "sam")] == 4
    cost = econ.priced_cost(split, {}, 15.0, ceiling=40, daily_ot_hours=8)
    assert cost["overtime_hours"] == 4


def test_a_14_15_school_day_still_sums_to_the_band_cap():
    c = _c(roster_names=["Kid"], active={"kid"}, minors={"kid"}, minor_bands={"kid": "14-15"})
    rows = [_row(1, "Kid", "3:30pm", "5:00pm", "Host"), _row(1, "Kid", "5:30pm", "7:00pm", "Host")]
    assert "minor_hours" not in _kinds(rows, c)          # 3h on a school day: at the cap
    rows[1] = _row(1, "Kid", "5:30pm", "7:00pm", "Host")
    rows.append(_row(1, "Kid", "12:00pm", "1:00pm", "Host"))
    assert "minor_hours" in _kinds(rows, c)


# ── P-5 / E-27: a minor's weekly cap and earliest start are repaired ─────

def test_person_fixable_holds_the_minor_week_and_early_start():
    assert {"minor_week_hours", "minor_early"} <= sq.PERSON_FIXABLE


def _school_week_minor():
    c = _c(roster_names=["Kid", "Ana", "Bo"], active={"kid", "ana", "bo"}, minors={"kid"},
           minor_bands={"kid": "14-15"})
    # 3h a school day x 5 + 5h Saturday = 20h, the school-week cap is 18h
    rows = [_row(i, "Kid", "4:00pm", "7:00pm", "Host") for i in range(5)]
    rows.append(_row(5, "Kid", "10:00am", "3:00pm", "Host"))
    rows.append(_row(5, "Ana", "3:00pm", "9:00pm", "Host"))
    return c, rows


def test_the_rebalance_uses_a_minors_weekly_cap_as_their_line():
    c, rows = _school_week_minor()
    assert "minor_week_hours" in _kinds(rows, c)
    out = sr.rebalance_overtime(rows, c, roster_roles={"Kid": "Host", "Ana": "Host", "Bo": "Host"})
    assert out["moves"] and out["moves"][0]["kind"] == "minor" and out["moves"][0]["from"] == "Kid"
    assert "minor_week_hours" not in _kinds(out["rows"], c)
    assert not _hard(out["rows"], c)


def test_fix_person_breaches_moves_the_shift_that_crosses_the_week_cap():
    c, rows = _school_week_minor()
    out = sr.fix_person_breaches(rows, c, roster_roles={"Kid": "Host", "Ana": "Host", "Bo": "Host"})
    assert out["fixes"] and out["fixes"][0]["kind"] == "minor"
    assert "minor_week_hours" not in _kinds(out["rows"], c)
    assert _hours(out["rows"], "Kid") <= 18


def test_an_early_start_is_cut_to_the_band_and_its_stretch_covered():
    c = _c(roster_names=["Kid", "Ana"], active={"kid", "ana"}, minors={"kid"}, minor_bands={"kid": "14-15"})
    rows = [_row(5, "Kid", "6:00am", "12:00pm", "Busser"), _row(5, "Ana", "8:00am", "2:00pm", "Busser")]
    assert "minor_early" in _kinds(rows, c)
    out = sr.fix_person_breaches(rows, c, roster_roles={"Kid": "Busser", "Ana": "Busser"})
    kid = next(r for r in out["rows"] if r["employee"] == "Kid")
    assert kid["shift_start"] == "7:00am" and kid["scheduled_hours"] == "5"
    assert "minor_early" not in _kinds(out["rows"], c)
    # 6-7am had nobody else in the role: Ana comes in earlier to cover it
    ana = next(r for r in out["rows"] if r["employee"] == "Ana")
    assert ana["shift_start"] == "6:00am"
    assert not _hard(out["rows"], c)


def test_a_minor_cut_at_close_leaves_no_hole_when_a_teammate_can_run_on():
    c = _c(roster_names=["Kid", "Ana", "Max"], active={"kid", "ana", "max"}, minors={"kid"},
           minor_bands={"kid": "16-17"}, close_times={"Thursday": "11:00pm"})
    rows = [_row(3, "Kid", "5:00pm", "11:00pm", "Busser"), _row(3, "Ana", "4:00pm", "9:00pm", "Busser"),
            _row(3, "Max", "4:00pm", "11:00pm", "Server")]
    assert "minor_late" in _kinds(rows, c)
    out = sr.fix_person_breaches(rows, c, roster_roles={"Kid": "Busser", "Ana": "Busser", "Max": "Server"})
    kid = next(r for r in out["rows"] if r["employee"] == "Kid")
    ana = next(r for r in out["rows"] if r["employee"] == "Ana")
    assert kid["shift_end"] == "10:00pm" and ana["shift_end"] == "11:00pm"
    assert "minor_late" not in _kinds(out["rows"], c)
    assert "Ana covers" in out["fixes"][0]["reason"]


def test_a_cut_nobody_can_cover_still_stands_and_says_the_stretch_is_open():
    c = _c(roster_names=["Kid", "Max"], active={"kid", "max"}, minors={"kid"}, minor_bands={"kid": "16-17"},
           close_times={"Thursday": "11:00pm"})
    rows = [_row(3, "Kid", "5:00pm", "11:00pm", "Busser"), _row(3, "Max", "4:00pm", "11:00pm", "Server")]
    out = sr.fix_person_breaches(rows, c, roster_roles={"Kid": "Busser", "Max": "Server"})
    assert "minor_late" not in _kinds(out["rows"], c), "the minor's own legality comes first"
    assert "open" in out["fixes"][0]["reason"]


def test_a_minors_split_day_past_the_cap_is_repaired():
    c = _c(roster_names=["Host", "Ana"], active={"host", "ana"}, minors={"host"}, minor_bands={"host": "16-17"})
    c.compliance["minor_max_daily_hours"] = 8
    rows = [_row(5, "Host", "10:00am", "3:00pm", "Host"), _row(5, "Host", "4:00pm", "9:00pm", "Host"),
            _row(5, "Ana", "3:00pm", "7:00pm", "Host")]
    out = sr.fix_person_breaches(rows, c, roster_roles={"Host": "Host", "Ana": "Host"})
    assert "minor_hours" not in _kinds(out["rows"], c)
    assert _hours([r for r in out["rows"] if r["date"] == WEEK[5]], "Host") <= 8


# ── P-30: the run of days is fixed until it is legal ──────────────────────

def test_a_nine_day_run_counting_the_published_tail_is_split_until_legal():
    tail = [dict(_row(0, "Jose", "8:30am", "2:30pm", "Dishwasher", d=d)) for d in ("2026-10-03", "2026-10-04")]
    c = _c(roster_names=["Jose", "Cesar"], active={"jose", "cesar"}, base_rows={"jose": tail},
           held_roles={"cesar": {"dishwasher"}})
    c.compliance["max_consecutive_days"] = 3      # nine in a row needs two days moved
    rows = [_row(i, "Jose", "8:30am", "2:30pm", "Dishwasher") for i in range(7)]
    rows += [_row(0, "Cesar", "4:00pm", "10:00pm", "Prep Cook")]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "long_run"]
    assert v and "9 days in a row" in v[0]["detail"]
    # Cesar's roster role is Prep Cook: he is a teammate through the role he holds
    out = sr.fix_person_breaches(rows, c, roster_roles={"Jose": "Dishwasher", "Cesar": "Prep Cook"})
    assert "long_run" not in _kinds(out["rows"], c)
    assert len([f for f in out["fixes"] if f["kind"] == "long_run"]) >= 2
    assert all(f["to"] == "Cesar" for f in out["fixes"])
    assert out["sweeps"] <= sr.PERSON_FIX_MAX_SWEEPS


def test_the_run_fix_stops_at_its_sweep_cap():
    c = _c(roster_names=["Jose", "Cesar"], active={"jose", "cesar"})
    c.compliance["max_consecutive_days"] = 2
    rows = [_row(i, "Jose", "8:30am", "2:30pm", "Dishwasher") for i in range(7)]
    out = sr.fix_person_breaches(rows, c, roster_roles={"Jose": "Dishwasher", "Cesar": "Dishwasher"}, max_sweeps=1)
    assert out["sweeps"] <= 1


# ── P-31: the overtime rebalance reaches cross-trained people and splits ──

def test_overtime_goes_to_a_teammate_who_holds_the_role():
    c = _c(roster_names=["Vince", "Ray"], active={"vince", "ray"}, held_roles={"ray": {"line cook"}})
    rows = [_row(i, "Vince", "2:00pm", "10:00pm", "Line Cook") for i in range(6)]
    out = sr.rebalance_overtime(rows, c, roster_roles={"Vince": "Line Cook", "Ray": "Dishwasher"})
    assert out["moves"] and out["moves"][0]["to"] == "Ray"
    assert _hours(out["rows"], "Vince") <= 40


def test_overtime_goes_to_a_teammate_already_on_that_day_as_a_second_leg():
    c = _c(roster_names=["Vince", "Bea"], active={"vince", "bea"})
    c.compliance["max_consecutive_days"] = 7
    rows = [_row(i, "Vince", "5:00pm", "11:00pm", "Line Cook") for i in range(7)]       # 42h
    rows += [_row(i, "Bea", "10:00am", "2:00pm", "Line Cook") for i in range(7)]         # 28h, every day
    out = sr.rebalance_overtime(rows, c, roster_roles={"Vince": "Line Cook", "Bea": "Line Cook"})
    assert out["moves"] and out["moves"][0]["to"] == "Bea"
    moved = out["rows"][out["moves"][0]["index"]]
    assert sum(1 for r in out["rows"] if r["employee"] == "Bea" and r["date"] == moved["date"]) == 2
    assert not _hard(out["rows"], c)


def test_a_teammate_running_on_takes_the_tail_of_a_shift():
    c = _c(roster_names=["Vince", "Bea"], active={"vince", "bea"})
    rows = [_row(i, "Vince", "2:00pm", "10:00pm", "Line Cook") for i in range(5)]
    rows.append(_row(5, "Vince", "12:00pm", "10:00pm", "Line Cook"))                    # 50h
    rows.append(_row(5, "Bea", "11:00am", "7:00pm", "Line Cook"))                       # 8h
    out = sr.rebalance_overtime(rows, c, roster_roles={"Vince": "Line Cook", "Bea": "Line Cook"})
    assert _hours(out["rows"], "Vince") <= 40
    assert not out["left"]
    assert not _hard(out["rows"], c)


def test_a_trim_bigger_than_one_shift_allows_is_spread_over_shifts():
    c = _c(roster_names=["Vince", "Bea"], active={"vince", "bea"})
    rows = [_row(i, "Vince", "2:00pm", "10:30pm", "Line Cook") for i in range(5)]       # 42.5h
    rows += [_row(i, "Bea", "2:00pm", "10:30pm", "Line Cook") for i in range(5)]        # 42.5h, nobody has room
    out = sr.rebalance_overtime(rows, c, roster_roles={"Vince": "Line Cook", "Bea": "Line Cook"})
    assert _hours(out["rows"], "Vince") <= 40 and _hours(out["rows"], "Bea") <= 40
    assert len(out["trims"]) >= 2 and not out["left"]


def test_an_overtime_move_never_opens_a_manager_gap():
    # the audit's reproduction (E-1): a 9-10am gap already there; the
    # manager over 40h works the evening as a Bartender
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ann", "Bob"], active={"max", "ann", "bob"})
    rows = [_row(2, "Ann", "9:00am", "9:00pm"), _row(2, "Max", "10:00am", "3:00pm", "Manager"),
            _row(2, "Max", "3:00pm", "10:00pm", "Bartender")]
    rows += [_row(i, "Max", "10:00am", "6:00pm", "Manager") for i in (0, 1, 3, 4)]
    rows += [_row(2, "Bob", "4:00pm", "10:00pm", "Bartender")]
    before = sr.breach_profile(rows, c)["manager"]
    out = sr.rebalance_overtime(rows, c, roster_roles={"Max": "Manager", "Ann": "Server", "Bob": "Bartender"})
    assert sr.breach_profile(out["rows"], c)["manager"].get(WEEK[2], 0) <= before.get(WEEK[2], 0)


def test_a_pinned_row_is_never_moved_or_retimed():
    c = _c(roster_names=["Vince", "Bea"], active={"vince", "bea"})
    rows = [dict(_row(i, "Vince", "2:00pm", "10:00pm", "Line Cook"), _pinned="manager_plan") for i in range(6)]
    rows += [_row(0, "Bea", "10:00am", "2:00pm", "Line Cook")]
    out = sr.rebalance_overtime(rows, c, roster_roles={"Vince": "Line Cook", "Bea": "Line Cook"})
    assert not out["moves"] and not out["trims"] and out["left"]
    assert out["rows"][:6] == rows[:6]


# ── P-4: minimum hours are filled ────────────────────────────────────────

def _min_week():
    c = _c(roster_names=["Cook", "Lee", "Mo"], active={"cook", "lee", "mo"},
           hours_limits={"cook": (40, 45), "mo": (30, None)})
    rows = [_row(0, "Cook", "11:00am", "6:00pm", "Line Cook")]                            # 7h of 40-45
    rows += [_row(i, "Lee", "2:00pm", "10:00pm", "Line Cook") for i in range(1, 6)]      # 40h, no target
    rows += [_row(i, "Mo", "9:00am", "3:00pm", "Line Cook") for i in range(1, 6)]        # 30h, at his minimum
    return c, rows


def test_a_full_timer_on_7h_is_given_shifts_from_teammates_above_their_target():
    c, rows = _min_week()
    assert any(v["kind"] == "under_min_hours" and v["employee"] == "Cook" for v in sr.violations(rows, c))
    out = sr.fill_min_hours(rows, c, roster_roles={"Cook": "Line Cook", "Lee": "Line Cook", "Mo": "Line Cook"})
    assert _hours(out["rows"], "Cook") >= 40 - 0.05 or out["left"]
    assert _hours(out["rows"], "Cook") >= 31, out
    assert _hours(out["rows"], "Mo") >= 30, "never below a donor's own minimum"
    assert all(m["from"] == "Lee" for m in out["moves"])
    assert not _hard(out["rows"], c)


def test_minimum_hours_are_added_inside_the_budget_only():
    c = _c(roster_names=["Cook", "Lee"], active={"cook", "lee"}, hours_limits={"cook": (24, None)})
    rows = [_row(0, "Cook", "11:00am", "7:00pm", "Line Cook")]
    rows += [_row(i, "Lee", "11:00am", "7:00pm", "Line Cook") for i in range(1, 4)]     # Lee's target is 0, but
    c.hours_limits["lee"] = (24, None)                                                  # now Lee needs his 24h
    out = sr.fill_min_hours(rows, c, roster_roles={"Cook": "Line Cook", "Lee": "Line Cook"}, hours_budget=40)
    assert _hours(out["rows"], "Cook") == 16 and len(out["added"]) == 1          # 32h + 8h = the 40h budget
    assert out["left"] and out["left"][0]["employee"] == "Cook"
    assert sr.hourly_hours(out["rows"], c) <= 40
    none = sr.fill_min_hours(rows, c, roster_roles={"Cook": "Line Cook", "Lee": "Line Cook"})
    assert not none["added"], "no budget, no added hours"


def test_under_min_hours_is_fixable_and_the_solver_may_not_bring_it_back():
    assert "under_min_hours" in sr.FIXABLE_SOFT
    import schedule_solver
    c, rows = _min_week()
    assert ("cook", "under_min_hours") in schedule_solver._soft_repaired(rows, c)


def test_the_score_counts_the_minimum_hours_given():
    signals = {"hours_limits": {"Cook": (40, 45)}, "roster": ["Cook", "Lee"], "profiles": [sq.ShiftProfile()],
               "typical_headcount": {(d, p): {"Line Cook": 1} for d in DAYS for p in ("morning", "night")}}
    low = [_row(0, "Cook", "11:00am", "6:00pm", "Line Cook")] + \
          [_row(i, "Lee", "2:00pm", "10:00pm", "Line Cook") for i in range(1, 6)]
    more = [dict(r, employee="Cook") if r["date"] == WEEK[1] else r for r in low]
    a = sq.score_rows(low, **signals)
    b = sq.score_rows(more, **signals)
    da = next(d for d in a["week_dimensions"] if d["key"] == "min_hours")
    db = next(d for d in b["week_dimensions"] if d["key"] == "min_hours")
    assert da["score"] == 18 and db["score"] == 38           # 7 of 40h, then 15 of 40h
    assert "Cook has 7h of the 40h minimum you set." in da["weaknesses"]
    assert sq.week_min_hours([sq.ShiftContext(hours_limits={})]) is None
    # somebody with a minimum and no shift at all counts too
    none = sq.week_min_hours([sq.ShiftContext(hours_limits={"Bo": (20, None)}, week_assignments={})])
    assert none.score == 0 and none.facts["short"] == [{"name": "Bo", "hours": 0, "min": 20.0}]
    assert sq.DIMENSION_LABELS["min_hours"] == "Minimum hours"


def test_generation_runs_the_minimum_hours_pass_after_the_repairs():
    import schedule_engine
    src = inspect.getsource(schedule_engine._run_schedule_job)
    assert src.index("_rules.fix_person_breaches(") < src.index("_rules.fill_min_hours(") < src.index("_sqf.apply_fixes(")
    assert "hours_budget=float(result.get(\"hours_budget\") or 0) or None" in src


# ── E-12 / E-17: a salaried cap, and ranked by fatigue and hours ─────────

def test_a_salaried_person_has_a_weekly_cap_code_never_passes():
    c = _c(roster_names=["Erik", "Ann"], active={"erik", "ann"}, salaried={"erik"}, managers={"erik": "Owner"})
    assert sr.SALARIED_HOURS_CAP == 55 and c.salaried_limit("Erik") == 55
    c.salaried_cap = 60
    assert c.salaried_limit("Erik") == 60 and c.max_hours("Erik") == 60
    c.hours_limits["erik"] = (None, 70)
    assert c.salaried_limit("Erik") == 70
    c.hours_limits.pop("erik")
    c.salaried_cap = None
    week = [_row(i, "Erik", "10:00am", "9:00pm", "Owner") for i in range(5)]             # 55h
    ok, why = c.can_add(_row(5, "Erik", "10:00am", "2:00pm", "Owner"), week)
    assert not ok and "55h weekly cap" in why


def test_a_salaried_week_is_rebalanced_only_past_the_owners_own_limit():
    c = _c(roster_names=["Erik", "Bo"], active={"erik", "bo"}, salaried={"erik"})
    rows = [_row(i, "Erik", "10:00am", "10:00pm", "Bartender") for i in range(5)]       # 60h, nobody set a limit
    rows += [_row(0, "Bo", "10:00am", "2:00pm", "Bartender")]
    roles = {"Erik": "Bartender", "Bo": "Bartender"}
    assert not sr.rebalance_overtime(rows, c, roster_roles=roles)["moves"], "a model's long owner week stays his"
    c.hours_limits["erik"] = (None, 50)
    out = sr.rebalance_overtime(rows, c, roster_roles=roles)
    assert out["moves"] and out["moves"][0]["to"] == "Bo" and "inside the 50h you set for them" in out["moves"][0]["reason"]
    assert _hours(out["rows"], "Erik") <= 50


def test_the_gap_filler_ranks_by_fatigue_and_hours_not_salaried_first():
    c = _c(roster_names=["Erik", "Andrew", "Ann"], active={"erik", "andrew", "ann"}, salaried={"erik"},
           managers={"erik": "Owner", "andrew": "Manager FOH"})
    rows = [_row(i, "Erik", "10:00am", "9:00pm", "Owner") for i in range(4)]             # 44h, 4 days running
    rows += [_row(i, "Andrew", "10:00am", "3:00pm", "Manager FOH") for i in (0, 1)]     # 10h
    rows += [_row(4, "Ann", "11:00am", "4:00pm")]
    out = sr.cover_manager_gaps(rows, c)
    assert out["added"] and out["added"][0]["employee"] == "Andrew"
    assert "no_manager" not in _kinds(out["rows"], c)


def test_an_owner_operator_week_is_covered_to_the_cap_and_the_rest_explained():
    # an owner (salaried-style) and two servers, open 11am-10pm seven days
    c = _c(roster_names=["Olive", "Sam", "Tia"], active={"olive", "sam", "tia"}, salaried={"olive"},
           managers={"olive": "Owner"})
    c.compliance["max_consecutive_days"] = 7
    rows = []
    for i in range(7):
        rows += [_row(i, "Sam", "11:00am", "4:30pm"), _row(i, "Tia", "4:30pm", "10:00pm")]
    out = sr.cover_manager_gaps(rows, c)
    assert _hours(out["rows"], "Olive") <= 55 + 0.01
    assert out["left"] and out["shortfall"] and out["shortfall"]["unmanaged_hours"] > 0
    assert any("55h weekly cap" in r["why"] for x in out["left"] for r in x["reasons"])
    assert "acting manager" in out["shortfall"]["text"]
    # every no_manager left is about its day, never an innocent server's row
    s = sr.summarize(sr.violations(out["rows"], c))
    assert not any(out["rows"][i]["employee"] in ("Sam", "Tia") for i in s["hard_rows"])
    assert s["hard_days"] and all(d["kind"] == "no_manager" for d in s["hard_days"])
    # the owner raises the cap: more of the week is covered
    c.salaried_cap = 80
    more = sr.cover_manager_gaps(rows, c)
    assert _hours(more["rows"], "Olive") > _hours(out["rows"], "Olive")


# ── E-16: a second leg for a manager on the day, shifts sized to the gap ──

def test_a_one_hour_gap_buys_a_one_hour_shift_not_four():
    c = _c(roster_names=["Erik", "Andrew", "Ann"], active={"erik", "andrew", "ann"},
           managers={"erik": "Owner", "andrew": "Manager FOH"})
    rows = [_row(2, "Ann", "4:00pm", "11:00pm"), _row(2, "Andrew", "11:00am", "6:00pm", "Manager FOH"),
            _row(2, "Erik", "4:00pm", "10:00pm", "Owner")]
    rows += [_row(i, "Erik", "10:00am", "8:00pm", "Owner") for i in (0, 1, 3)]          # 36h + 6h already
    out = sr.cover_manager_gaps(rows, c)
    assert "no_manager" not in _kinds(out["rows"], c)
    changed = out["extended"] + out["added"]
    assert changed
    added = [x for x in out["added"]]
    for x in added:
        assert float(out["rows"][x["index"]]["scheduled_hours"]) == 1.0


def test_the_owners_shortest_shift_sizes_an_added_shift():
    c = _c(roster_names=["Erik", "Ann"], active={"erik", "ann"}, managers={"erik": "Owner"})
    c.compliance["min_shift_hours"] = 3
    rows = [_row(2, "Ann", "5:00pm", "11:00pm")]
    out = sr.cover_manager_gaps(rows, c)
    new = out["rows"][out["added"][0]["index"]]
    assert new["scheduled_hours"] == "6"
    rows = [_row(2, "Ann", "9:00pm", "11:00pm")]
    out = sr.cover_manager_gaps(rows, c)
    new = out["rows"][out["added"][0]["index"]]
    assert float(new["scheduled_hours"]) == 3 and new["shift_end"] == "11:00pm" and new["shift_start"] == "8:00pm"


def test_a_manager_on_that_day_takes_a_second_leg():
    c = _c(roster_names=["Andrew", "Ann", "Bob"], active={"andrew", "ann", "bob"}, managers={"andrew": "Manager FOH"})
    rows = [_row(2, "Andrew", "9:00am", "1:00pm", "Manager FOH"), _row(2, "Ann", "9:00am", "1:00pm"),
            _row(2, "Bob", "6:00pm", "10:00pm")]
    out = sr.cover_manager_gaps(rows, c)
    assert out["added"] and out["added"][0]["employee"] == "Andrew" and out["added"][0]["leg"]
    assert "no_manager" not in _kinds(out["rows"], c)
    # a second leg that would make the day longer than the longest shift is refused
    c.compliance["max_shift_hours"] = 7
    out = sr.cover_manager_gaps(rows, c)
    assert not out["added"] and out["left"]


# ── E-13: acting managers, and the breach shown on the day ───────────────

def test_an_acting_manager_covers_the_week_the_only_manager_is_away():
    off = {d: sr.LABELS["approved_time_off"] for d in WEEK}
    c = _c(roster_names=["Max", "Kay", "Ann"], active={"max", "kay", "ann"}, managers={"max": "Manager"},
           blocked_dates={"max": off}, acting_managers={"kay": {WEEK[2], WEEK[3]}})
    rows = [_row(2, "Ann", "11:00am", "9:00pm"), _row(3, "Ann", "11:00am", "9:00pm"), _row(4, "Ann", "11:00am", "9:00pm")]
    out = sr.cover_manager_gaps(rows, c)
    assert {x["employee"] for x in out["added"]} == {"Kay"}
    gaps = sr.manager_gaps(out["rows"], c)
    assert WEEK[2] not in gaps and WEEK[3] not in gaps and WEEK[4] in gaps
    left = [x for x in out["left"] if x["date"] == WEEK[4]]
    assert left and any(r["employee"] == "Max" and "time off" in r["why"] for r in left[0]["reasons"])


def test_with_no_manager_an_acting_managers_dates_are_still_held_to_the_rule():
    c = _c(roster_names=["Kay", "Ann"], active={"kay", "ann"}, managers={}, acting_managers={"kay": {WEEK[2]}})
    rows = [_row(2, "Ann", "11:00am", "9:00pm"), _row(3, "Ann", "11:00am", "9:00pm")]
    v = sr.violations(rows, c)
    assert [x["date"] for x in v if x["kind"] == "no_manager"] == [WEEK[2]]
    assert any(x["kind"] == "no_manager_roster" and not x["hard"] for x in v)
    out = sr.cover_manager_gaps(rows, c)
    assert [x["employee"] for x in out["added"]] == ["Kay"]


def test_a_day_level_breach_is_never_needs_review_on_an_innocent_row():
    import schedule_engine
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ann"], active={"max", "ann"})
    rows = [_row(2, "Ann", "9:00am", "3:00pm"), _row(2, "Max", "10:00am", "3:00pm", "Manager")]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "no_manager"]
    assert v and v[0]["day_level"]
    s = sr.summarize(sr.violations(rows, c))
    assert s["hard_rows"] == [] and s["hard_days"][0]["date"] == WEEK[2]
    assert s["lines"][0] == "⚠ 10/7/26 — no manager on Wednesday from 9:00am to 10:00am"
    src = inspect.getsource(schedule_engine._run_schedule_job)
    assert 'if not _v["hard"] or _v.get("day_level"):' in src
    import mobile_api
    marked = [dict(r, notes="NEEDS REVIEW") for r in rows]
    mobile_api._mark_review_rows(marked, sr.violations(rows, c))
    assert all("NEEDS REVIEW" not in r["notes"] for r in marked)
    import client_api
    gate = inspect.getsource(client_api.publish_review)
    assert "_sr.breach_text(v)" in gate


# ── E-23: real hours on the night the clocks go back ─────────────────────

def test_halloween_close_counts_its_extra_hour():
    from schedule_engine import _reconcile_scheduled_hours
    row = {"date": "2026-10-31", "shift_start": "5:00pm", "shift_end": "2:00am", "scheduled_hours": "9", "notes": ""}
    drift = _reconcile_scheduled_hours(row, tz="America/Chicago")
    assert row["scheduled_hours"] == "10.0" and row["dst_hours"] == 1.0
    assert drift == 0.0, "the model's wall-clock arithmetic was right; the clock change is ours"
    assert "extra hour when the clocks go back" in row["notes"]
    plain = {"date": "2026-10-24", "shift_start": "5:00pm", "shift_end": "2:00am", "scheduled_hours": "9"}
    assert _reconcile_scheduled_hours(plain, tz="America/Chicago") == 0.0 and plain["scheduled_hours"] == "9"
    assert sr.span_hours({"date": "2026-10-31", "shift_start": "5:00pm", "shift_end": "2:00am"},
                         "America/Chicago") == 10.0
    import schedule_engine
    src = inspect.getsource(schedule_engine._run_schedule_job)
    assert "_reconcile_scheduled_hours(\n                    _row, tz=" in src


def test_a_pass_writes_real_hours():
    c = _c(tz="America/Chicago", roster_names=["Andrew", "Ann"], active={"andrew", "ann"},
           managers={"andrew": "Manager FOH"},
           week_dates=[(date(2026, 10, 26) + timedelta(days=i)).isoformat() for i in range(7)])
    d = "2026-10-31"
    rows = [{"date": d, "day": "Saturday", "employee": "Ann", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "2:00am", "scheduled_hours": "10", "notes": ""},
            {"date": d, "day": "Saturday", "employee": "Andrew", "role": "Manager FOH", "shift_start": "5:00pm",
             "shift_end": "12:00am", "scheduled_hours": "7", "notes": ""}]
    out = sr.cover_manager_gaps(rows, c)
    andrew = next(r for r in out["rows"] if r["employee"] == "Andrew")
    assert andrew["shift_end"] == "2:00am" and andrew["scheduled_hours"] == "10"


# ── E-32: a start in the small hours is its night's ─────────────────────

def test_a_late_porter_is_read_as_the_night_it_works():
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Pat"], active={"max", "pat"},
           close_times={"Friday": "2:00am"})
    porter = _row(4, "Pat", "12:30am", "3:00am", "Porter")
    assert sr.shift_span(porter)[0].isoformat() == "2026-10-10T00:30:00"
    assert sr.daypart_of("12:30am") == "night" and sq.daypart_of("12:30am") == "night"
    assert sr.daypart_of("5:00am") == "morning"
    # the porter's Thursday close is no overlap or rest breach
    rows = [_row(3, "Pat", "5:00pm", "11:00pm", "Porter"), porter,
            _row(4, "Max", "5:00pm", "2:00am", "Manager")]
    kinds = _kinds(rows, c)
    assert "overlap" not in kinds and "rest_gap" not in kinds
    # and the only unmanaged stretch is after the manager leaves at 2am
    gaps = sr.manager_gaps(rows, c)
    assert gaps[WEEK[4]] and [(gs, ge) for gs, ge, _i in gaps[WEEK[4]]] == [(26 * 60, 27 * 60)]
    # an overnight close still reads as before
    close = _row(4, "Max", "5:00pm", "2:00am", "Manager")
    s, e = sr.shift_span(close)
    assert (s.isoformat(), e.isoformat()) == ("2026-10-09T17:00:00", "2026-10-10T02:00:00")


def test_a_night_window_holds_a_small_hours_start():
    assert sr.window_allows(18 * 60, None, 30, 3 * 60) == (True, "")
    assert sr.window_allows(18 * 60, 2 * 60, 30, 4 * 60) == (False, "late")
    assert sr.window_allows(None, 23 * 60, 30, 3 * 60) == (False, "late")
    assert sr.window_allows(60, 6 * 60, 30, 3 * 60) == (False, "early")


def test_the_swap_index_reads_a_small_hours_start_as_its_night():
    s, e = sq._span({"date": "2026-10-09", "shift_start": "12:30am", "shift_end": "3:00am"})
    assert s.isoformat() == "2026-10-10T00:30:00" and e.isoformat() == "2026-10-10T03:00:00"


# ── E-10: the payroll week that runs into next week keeps a reserve ──────

def test_a_wednesday_payroll_week_full_by_sunday_is_flagged_softly():
    c = _c(week_start_day=2, roster_names=["Ann"], active={"ann"})
    rows = [_row(i, "Ann", "10:00am", "6:00pm") for i in range(2, 7)]                  # Wed-Sun 40h
    v = [x for x in sr.violations(rows, c) if x["kind"] == "payroll_tail_full"]
    assert len(v) == 1 and not v[0]["hard"] and v[0]["date"] == WEEK[6]
    assert "Mon 10/12/26 and Tue 10/13/26" in v[0]["detail"] and "10/7/26–10/13/26" in v[0]["detail"]
    assert c.bucket_tail(c.bucket(WEEK[2])) == ["2026-10-12", "2026-10-13"]
    assert c.tail_reserve(40, c.bucket(WEEK[2])) == round(40 * 2 / 7, 2)
    # a Monday payroll week ends with the schedule week: nothing to keep
    assert "payroll_tail_full" not in _kinds(rows, _c(roster_names=["Ann"], active={"ann"}))
    # nor for a salaried person, who owes no overtime
    assert "payroll_tail_full" not in _kinds(rows, _c(week_start_day=2, salaried={"ann"}))
    # the model is told to keep the reserve too
    assert "runs on into next week (Mon 10/12/26 and Tue 10/13/26)" in sr.prompt_block(c)
    assert "runs on into next week" not in sr.prompt_block(_c())


def test_the_rebalance_keeps_the_reserve_where_a_teammate_has_room():
    c = _c(week_start_day=2, roster_names=["Vince", "Ray", "Bo"], active={"vince", "ray", "bo"})
    rows = [_row(i, "Vince", "2:00pm", "10:00pm", "Line Cook") for i in range(2, 7)]
    rows.append(_row(6, "Vince", "8:00am", "1:00pm", "Line Cook"))                    # 45h Wed-Sun
    rows += [_row(i, "Ray", "8:00am", "1:00pm", "Line Cook") for i in (2, 3, 4, 5)]    # 20h, Wed-Sat
    rows += [_row(i, "Bo", "8:00am", "1:00pm", "Line Cook") for i in (2, 3, 4)]        # 15h, Wed-Fri: but
    rows += [_row(i, "Bo", "2:00pm", "10:00pm", "Line Cook") for i in (0, 1)]          # Mon-Tue are last week's
    out = sr.rebalance_overtime(rows, c, roster_roles={"Vince": "Line Cook", "Ray": "Line Cook", "Bo": "Line Cook"})
    assert out["moves"]
    # Ray would end the payroll week at 25h, inside 40h less the 2-of-7 reserve; so would Bo
    for m in out["moves"]:
        b = c.bucket(out["rows"][m["index"]]["date"])
        assert sr._bucket_hours(c, out["rows"], m["to"].lower(), b) <= 40 - c.tail_reserve(40, b) + 0.05


# ── the shared machinery every pass uses (E-1, P-2) ──────────────────────

def test_generation_keeps_day_level_breaches_off_rows_and_says_why_once():
    import schedule_engine
    src = inspect.getsource(schedule_engine._run_schedule_job)
    assert '_hard = [v for v in _viols if v["hard"] and not v.get("day_level")' in src
    assert 'result["review"]["lines"].append(_short)' in src
    assert "manager_coverage=result.get(\"manager_coverage\")" in src and "min_hours=result.get(\"min_hours\")" in src


def test_every_pass_judges_by_breach_identity_and_asks_can_add():
    for fn in (sr.rebalance_overtime, sr.fix_person_breaches, sr.cover_manager_gaps, sr.fill_min_hours):
        src = inspect.getsource(fn)
        assert "_Repair(" in src and "_hard_keys" not in src, fn.__name__
    src = inspect.getsource(sr._receivers)
    assert "c.can_add(" in src and "c.fillable(" in src
    assert "c.can_add(" in inspect.getsource(sr.cover_manager_gaps)
    assert "c.fillable(" in inspect.getsource(sr.cover_manager_gaps)
