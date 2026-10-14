"""Schedule fix round 10/11/26, workstream R — the shortest shift (contract
#2), the cuts and retimes that read it, the manager rule's per-restaurant
allowances and second manager, policy limits labelled by tier, the
roster-made run, carry-forward and the plan's own rows, the restaurant's
lunch/dinner boundary, and the STRESS-24 regressions.

BUGSGEN-5/6/7/8/12/17, STRESS-3/6/18/24, LOGIC-12/13/20. Pure where it
can be; the end-to-end archetypes are tests/test_schedule_benchmark.py.
"""
import inspect
from datetime import date, timedelta

import schedule_rules as sr
import schedule_skeleton as sk
import schedule_time as st

WEEK = [(date(2026, 10, 12) + timedelta(days=i)).isoformat() for i in range(7)]      # Mon 10/12 - Sun 10/18
DAYS = list(sr.DAYS)
CLOSE = {d: ("10:00pm" if d not in ("Friday", "Saturday") else "11:00pm") for d in DAYS}


def _c(**kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS))
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _row(d, emp, start, end, role="Server", notes=""):
    r = {"date": d, "day": sr._weekday_of(d), "employee": emp, "role": role, "shift_start": start,
         "shift_end": end, "notes": notes}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    return r


def _kinds(rows, c):
    return [v["kind"] for v in sr.violations(rows, c)]


# ── contract #2 / BUGSGEN-6: one shortest shift ───────────────────────────

def test_one_shortest_shift_default_and_owner_set():
    assert sr.DEFAULT_MIN_SHIFT_MINUTES == 180
    assert sr.min_shift_minutes(None) == 180 and sr.min_shift_minutes({}) == 180
    assert sr.min_shift_minutes({"min_shift_hours": 4}) == 240
    assert sr.min_shift_minutes(_c()) == 180
    c = _c()
    c.compliance["min_shift_hours"] = 2.5
    assert sr.min_shift_minutes(c) == 150 and sr.shortest_shift_hours(c, 4.0) == 2.5


def test_a_fragment_under_the_shortest_shift_is_flagged_even_with_no_rule_set():
    c = _c()
    v = [x for x in sr.violations([_row(WEEK[4], "Zoe", "4:00pm", "4:30pm")], c) if x["kind"] == "shift_too_short"]
    assert v and not v[0]["hard"] and "3h shortest shift (the default" in v[0]["detail"]
    assert "shift_too_short" not in _kinds([_row(WEEK[4], "Zoe", "4:00pm", "7:00pm")], c)


def test_a_minors_legal_day_lowers_their_shortest_shift():
    c = _c(minors={"kid"}, minor_bands={"kid": "14-15"}, roster_names=["Kid"], active={"kid"})
    c.compliance["min_shift_hours"] = 4
    assert sr.shortest_for(c, "Kid", WEEK[1]) == 180          # a school day: the law's 3h
    assert sr.shortest_for(c, "Kid", WEEK[5]) == 240          # Saturday: the owner's 4h
    assert "shift_too_short" not in _kinds([_row(WEEK[1], "Kid", "3:30pm", "6:30pm", "Host")], c)


def test_every_pass_reads_the_one_rule_and_no_private_minimum_is_left():
    import schedule_economics
    import schedule_engine
    import schedule_shapes
    for name in ("ROLE_TIME_MIN_MINUTES", "OT_MIN_SHIFT_HOURS", "MINOR_MIN_LEFT_HOURS", "MANAGER_MIN_SHIFT_MIN"):
        assert not hasattr(sr, name), name
    assert not hasattr(schedule_economics, "TRIM_MIN_SHIFT_HOURS")
    assert not hasattr(schedule_shapes, "DEFAULT_MIN_SHIFT")
    assert not hasattr(sk, "MIN_SHIFT_MIN")
    assert "min_shift_minutes(c)" in inspect.getsource(schedule_shapes.shape_week)
    assert "min_shift_minutes(c)" in inspect.getsource(sk.plan_manager_coverage)
    assert "shortest_for(" in inspect.getsource(schedule_economics.trim_to_budget)
    assert "shortest_for(" in inspect.getsource(schedule_engine._overlap_cut)


def test_the_prompt_states_the_shortest_shift():
    c = _c(roster_names=["Ana"], active={"ana"})
    text = sr.prompt_block(c)
    assert "No shift shorter than 3 hours (the default; the owner has set none)" in text
    c.compliance["min_shift_hours"] = 4
    assert "No shift shorter than 4 hours." in sr.prompt_block(c)


# ── STRESS-6: shapes the people can work ──────────────────────────────────

def test_shape_lengths_come_from_the_people_who_may_work_them():
    import schedule_shapes as shp
    c = _c(roster_names=["T1", "T2"], active={"t1", "t2"}, minors={"t1", "t2"},
           minor_bands={"t1": "16-17", "t2": "16-17"}, known_roles={"t1": {"Server"}, "t2": {"Server"}})
    lo, hi = shp._length_bounds(c, "Server", WEEK[1], {"T1": "Server", "T2": "Server"}, 3.0, 12.0)
    assert hi == 8.0, "no shape longer than the 8h a 16-17 year old may work in a day"
    c.minor_bands = {"t1": "14-15", "t2": "14-15"}
    lo, hi = shp._length_bounds(c, "Server", WEEK[1], {"T1": "Server", "T2": "Server"}, 4.0, 12.0)
    assert lo == 3.0 and hi == 3.0, "a 14-15 year old's school day is a 3h shape, not none"


# ── BUGSGEN-7: the section-cap trim at the handover ───────────────────────

def test_the_cap_trim_cuts_the_lunch_tail_not_the_dinner_server():
    import schedule_engine as se
    sat = WEEK[5]
    rows = [_row(sat, "Lu1", "11:00am", "5:00pm"), _row(sat, "Lu2", "11:00am", "5:00pm"),
            _row(sat, "Di1", "4:00pm", "11:00pm"), _row(sat, "Di2", "4:00pm", "11:00pm")]
    out, n, _d = se._trim_server_overlap_cap([dict(r) for r in rows], {}, {}, max_overlap=3)
    assert n >= 1 and se._peak_server_overlap(out)[0] <= 3
    dinner = {r["employee"]: r for r in out if r["employee"].startswith("Di")}
    assert all(r["shift_start"] == "4:00pm" and r["shift_end"] == "11:00pm" for r in dinner.values())
    for r in out:
        assert st.real_hours(r) >= 3.0, r          # never a 30-minute stub


def test_a_late_starter_is_started_after_the_handover_when_that_leaves_a_shift():
    import schedule_engine as se
    row = _row(WEEK[5], "Di", "4:00pm", "11:00pm")
    lunch = _row(WEEK[5], "Lu", "11:00am", "5:00pm")
    cut = se._overlap_cut(row, 16 * 60, "Saturday", {}, {}, 2, day_rows=[row, lunch])
    assert cut["shift_start"] == "5:00pm" and cut["shift_end"] == "11:00pm"
    short = _row(WEEK[5], "Late", "4:00pm", "6:00pm")
    assert se._overlap_cut(short, 16 * 60, "Saturday", {}, {}, 2, day_rows=[short, lunch]) is None


# ── BUGSGEN-5: role windows before the trims, length kept ─────────────────

def test_a_role_window_moves_the_shift_inside_it_keeping_its_length():
    assert sr._into_window(10 * 60, 18 * 60, {"earliest": 14 * 60, "latest": 23 * 60}) == (14 * 60, 22 * 60)
    assert sr._into_window(10 * 60, 18 * 60, {"earliest": 14 * 60, "latest": 20 * 60}) == (14 * 60, 20 * 60)
    import schedule_engine as se
    order = [k for k, *_r in se.REPAIR_STAGES]
    assert order.index("role_windows") < order.index("budget") < order.index("role_times")
    assert dict((k, t) for k, t, *_r in se.REPAIR_STAGES)["role_windows"] == sr.TIER_COVERAGE


# ── BUGSGEN-8: carry-forward and the manager plan ─────────────────────────

def test_carry_forward_drops_every_carried_manager_row_on_a_planned_date():
    import schedule_fallback as fb
    last = [(date.fromisoformat(d) - timedelta(days=7)).isoformat() for d in WEEK]
    base = {"source": "published", "rows": [
        {"date": last[4], "employee": "Parker", "role": "Manager", "shift_start": "4:00pm", "shift_end": "9:30pm",
         "notes": ""},
        {"date": last[4], "employee": "Ana", "role": "Server", "shift_start": "4:00pm", "shift_end": "10:00pm",
         "notes": ""}]}
    c = _c(roster_names=["Parker", "Ana"], active={"parker", "ana"}, managers={"parker": "Manager"})
    got = fb.carry_forward_rows(base, [WEEK[4]], c, roster_names=["Parker", "Ana"], plan_dates=[WEEK[4]])
    assert [r["employee"] for r in got["rows"]] == ["Ana"]
    kept = fb.carry_forward_rows(base, [WEEK[4]], c, roster_names=["Parker", "Ana"], plan_dates=[])
    assert {r["employee"] for r in kept["rows"]} == {"Parker", "Ana"}


# ── BUGSGEN-17: a plan row stretches, never a stub beside it ──────────────

def test_the_filler_extends_the_planned_managers_own_row():
    c = _c(roster_names=["Luis", "Ann"], active={"luis", "ann"}, managers={"luis": "Manager"})
    plan = dict(_row(WEEK[2], "Luis", "7:30am", "3:00pm", "Manager", notes="Cavnar AI: manager plan — opens"),
                _pinned=sr.PLAN_PIN)
    rows = [plan, _row(WEEK[2], "Ann", "7:00am", "1:00pm")]
    out = sr.cover_manager_gaps(rows, c)
    luis = [r for r in out["rows"] if r["employee"] == "Luis"]
    assert len(luis) == 1 and luis[0]["shift_start"] == "7:00am" and luis[0]["_pinned"] == sr.PLAN_PIN
    assert out["extended"] and not out["added"]
    assert sr.PLAN_PIN == sk.PLAN_SOURCE


def test_the_loop_keeps_a_plan_row_the_manager_stage_extended_and_nothing_else_moves_it():
    import schedule_engine as se
    plan = dict(_row(WEEK[5], "Dana", "7:30am", "6:00pm", "Manager"), _pinned=sr.PLAN_PIN)
    longer = dict(plan, shift_start="7:00am")
    shorter = dict(plan, shift_start="8:00am")
    other = dict(plan, employee="Luis")
    assert se._kept_rows_moved([plan], [longer], None, tier=sr.TIER_MANAGER) == ""
    assert se._kept_rows_moved([plan], [longer], None, tier=sr.TIER_COVERAGE)        # only the manager rank
    assert se._kept_rows_moved([plan], [shorter], None, tier=sr.TIER_MANAGER)        # never shortened
    assert se._kept_rows_moved([plan], [other], None, tier=sr.TIER_MANAGER)          # never another person
    locked = dict(plan, _pinned="locked")
    assert se._kept_rows_moved([locked], [dict(locked, shift_start="7:00am")], None, tier=sr.TIER_MANAGER)


def test_a_plan_reason_keeps_one_extension_clause():
    r = sk._one_extension_reason("covers open — stays on 8:00am–10:00am, less than a shift for anybody else",
                                 " — stays on 8:00am–9:00am, less than a shift for anybody else")
    assert r.count("stays on") == 1 and r.startswith("covers open")


# ── LOGIC-12: the manager allowances are the restaurant's ─────────────────

def test_the_allowances_are_settings_with_simple_ejs_values_as_defaults():
    assert sr.DEFAULTS["closing_manager_stays_minutes"] == 60 == sr.CLOSING_MANAGER_STAYS_MINUTES
    assert sr.DEFAULTS["opening_manager_lead_minutes"] == 60 == sr.OPENING_MANAGER_LEAD_MINUTES
    merged = sr.merged_compliance({}, {"closing_manager_stays_minutes": 0, "opening_manager_lead_minutes": 30})
    assert merged["closing_manager_stays_minutes"] == 0 and merged["opening_manager_lead_minutes"] == 30
    assert sr.closing_stay_minutes(merged) == 0 and sr.opening_lead_minutes(merged) == 30
    assert sr.closing_stay_minutes(_c()) == 60
    try:
        sr.merged_compliance({}, {"opening_manager_lead_minutes": 500})
        raise AssertionError("refused")
    except ValueError:
        pass
    assert "closing_manager_stays_minutes" not in sr.merged_compliance(merged, {}, reset=["closing_manager_stays_minutes"])


def test_a_restaurant_whose_manager_leaves_on_time_gets_the_closing_hour_as_a_gap():
    rows = [_row(WEEK[0], "Max", "11:00am", "9:00pm", "Manager"), _row(WEEK[0], "Ann", "11:00am", "10:00pm")]
    c = _c(managers={"max": "Manager"}, roster_names=["Max", "Ann"], active={"max", "ann"})
    assert "no_manager" not in _kinds(rows, c)                    # the 60-minute stay (Simple EJ's)
    c.compliance["closing_manager_stays_minutes"] = 0
    assert "no_manager" in _kinds(rows, c)
    # the opening lead: prep before a manager 30 minutes before open
    c2 = _c(managers={"max": "Manager"}, roster_names=["Max", "Ann"], active={"max", "ann"},
            open_times={d: "11:00am" for d in DAYS})
    rows2 = [_row(WEEK[0], "Max", "10:30am", "10:00pm", "Manager"), _row(WEEK[0], "Ann", "10:00am", "4:00pm")]
    assert "no_manager" in _kinds(rows2, c2)
    c2.compliance["opening_manager_lead_minutes"] = 30
    assert "no_manager" not in _kinds(rows2, c2)
    assert sr.opening_prep((600, 630), [630], 660, 30) and not sr.opening_prep((600, 630), [630], 660, 0)


def test_the_allowances_are_learned_from_punches_as_a_suggestion():
    rows = []
    for d in WEEK[:5]:
        rows += [_row(d, "Max", "10:30am", "10:30pm", "Manager"), _row(d, "Ann", "10:00am", "10:00pm")]
    got = sr.learn_manager_allowances(rows, lambda n, d: n == "Max", {d: "11:00am" for d in DAYS})
    assert got["closing_stay"] == {"minutes": 30, "dates": 5}
    assert got["opening_lead"] == {"minutes": 30, "dates": 5}
    assert sr.learn_manager_allowances(rows[:4], lambda n, d: n == "Max")["closing_stay"] is None


# ── STRESS-3: the plan never buys a seventh day for half an hour of prep ──

def test_the_opener_comes_in_for_the_prep_rather_than_another_manager_on_a_seventh_day():
    hist = []
    for w in range(1, 5):
        for i, d in enumerate(WEEK):
            dd = (date.fromisoformat(d) - timedelta(weeks=w)).isoformat()
            day, cl = DAYS[i], CLOSE[DAYS[i]]
            if day in ("Monday", "Tuesday"):
                hist.append(_row(dd, "Alex", "10:30am", cl, "Manager"))
            elif day in ("Saturday", "Sunday"):
                hist.append(_row(dd, "Bea", "10:30am", cl, "Manager"))
            else:
                hist += [_row(dd, "Alex", "10:30am", "5:00pm", "Manager"), _row(dd, "Bea", "4:00pm", cl, "Manager")]
            hist.append(_row(dd, "Cook", "10:00am", "4:00pm", "Line Cook"))
    c = _c(roster_names=["Alex", "Bea", "Cook"], active={"alex", "bea", "cook"},
           managers={"alex": "Manager", "bea": "Manager"}, open_times={d: "11:00am" for d in DAYS},
           close_times=dict(CLOSE))
    plan = sk.plan_manager_coverage(c, WEEK, history=hist)
    assert not plan["uncovered"]
    days = {}
    for r in plan["rows"]:
        days.setdefault(r["employee"], set()).add(r["date"])
    assert max(len(v) for v in days.values()) <= 6
    sat = [r for r in plan["rows"] if r["date"] == WEEK[5]]
    assert min(sr._span(r)[0] for r in sat) == 10 * 60


# ── STRESS-18 / LOGIC-20: policy limits and the roster's own shape ────────

def test_a_policy_limit_is_over_your_limit_and_law_stays_hard():
    c = _c(roster_names=["Bo"], active={"bo"})
    rows = [_row(d, "Bo", "4:00pm", "10:00pm") for d in WEEK]
    v = next(x for x in sr.violations(rows, c) if x["kind"] == "long_run")
    assert v["hard"] and v["policy"] and not sr.owner_hard(v)
    s = sr.summarize(sr.violations(rows, c))
    assert s["hard"] == 0 and s["over_limit"] >= 1 and any(ln.startswith("Over your limit") for ln in s["lines"])
    law = dict(sr.DEFAULTS, _pack={"code": "IL", "law": ["max_consecutive_days"]})
    c2 = _c(roster_names=["Bo"], active={"bo"}, compliance=law)
    v2 = next(x for x in sr.violations(rows, c2) if x["kind"] == "long_run")
    assert sr.owner_hard(v2) and not v2["policy"]
    assert "Over your limit" in sr.policy_text(v)


def test_a_one_manager_week_is_the_rosters_shape_named_once():
    c = _c(roster_names=["Mia", "Ann"], active={"mia", "ann"}, managers={"mia": "Manager"})
    st_ = sr.manager_structure(c)
    assert "mia" in st_ and "acting manager" in st_["mia"]["text"]
    rows = [_row(d, "Mia", "6:30am", "3:00pm", "Manager") for d in WEEK]
    v = next(x for x in sr.violations(rows, c) if x["kind"] == "long_run")
    assert v["structural"] and v["policy"]
    # a second manager on any day: no longer the roster's shape
    c2 = _c(roster_names=["Mia", "Rae"], active={"mia", "rae"}, managers={"mia": "Manager", "rae": "Manager"})
    assert not sr.manager_structure(c2)
    import schedule_publish
    src = inspect.getsource(schedule_publish.publish_review)
    assert 'tier=DECIDE' in src and 'v.get("structural")' in src


def test_the_default_salaried_cap_gives_way_to_the_manager_rule_but_an_owner_set_one_never():
    assert "salaried_cap_set(name)" in inspect.getsource(sk._week_cap)
    c = _c(roster_names=["Erik"], active={"erik"}, managers={"erik": "Owner"}, salaried={"erik"})
    assert sk._week_cap(c, "Erik", mode=sr.FILL_ROUNDS[2]) == sr.SALARIED_HOURS_MAX
    assert sk._week_cap(c, "Erik", mode=sr.FILL_ROUNDS[0]) == sr.SALARIED_CAP_DEFAULT
    c.salaried_cap = 50
    assert sk._week_cap(c, "Erik", mode=sr.FILL_ROUNDS[2]) == 50


# ── LOGIC-13: a second manager at the rush ────────────────────────────────

def test_the_second_manager_line_is_learned_from_the_restaurants_own_weeks():
    rows = []
    for d in WEEK[:5]:
        rows += [_row(d, "M1", "11:00am", "11:00pm", "Manager"), _row(d, "M2", "5:00pm", "11:00pm", "Manager")]
        rows += [_row(d, f"S{i}", "11:00am", "3:00pm") for i in range(3)]
        rows += [_row(d, f"D{i}", "5:00pm", "11:00pm") for i in range(8)]
    got = sr.learn_second_manager(rows, lambda n, d: n.startswith("M"))
    assert got and got["staff"] == 8
    assert sr.learn_second_manager(rows[:10], lambda n, d: n.startswith("M")) is None


def test_a_rush_on_one_manager_is_flagged_and_a_salaried_manager_comes_in_first():
    c = _c(roster_names=["Sal", "Hou"] + [f"D{i}" for i in range(8)],
           active={"sal", "hou"} | {f"d{i}" for i in range(8)},
           managers={"sal": "Manager", "hou": "Manager"}, salaried={"sal"})
    c.compliance["second_manager_from_staff"] = 6
    fri = WEEK[4]
    rows = [_row(fri, "Hou", "11:00am", "11:00pm", "Manager")] + [_row(fri, f"D{i}", "5:00pm", "11:00pm")
                                                                  for i in range(8)]
    v = [x for x in sr.violations(rows, c) if x["kind"] == "second_manager"]
    assert v and not v[0]["hard"] and sr.BREACH_TIER["second_manager"] == sr.TIER_REQUIREMENT
    out = sr.cover_second_manager(rows, c)
    assert out["added"] and out["added"][0]["employee"] == "Sal"
    assert "second_manager" not in _kinds(out["rows"], c)
    c.compliance["second_manager_from_staff"] = 0
    assert sr.second_manager_from(c) == (None, None) and "second_manager" not in _kinds(rows, c)
    import schedule_engine as se
    order = [k for k, *_r in se.REPAIR_STAGES]
    assert order.index("top_up") < order.index("second_manager") < order.index("budget")


# ── BUGSGEN-12: the restaurant's boundary whatever the caller's scope ─────

def test_availability_reads_the_restaurants_boundary_outside_any_scope():
    c = _c(daypart_avail={"ana": {"Monday": "morning"}}, daypart_split=17 * 60)
    row = _row(WEEK[0], "Ana", "3:30pm", "5:15pm")
    assert st.daypart_split() == 15 * 60                          # no scope entered
    assert c.can_work_row("Ana", row)[0]
    assert not [v for v in sr.violations([row], c) if v["kind"] == "unavailable_daypart"]
    import schedule_cpsat
    import schedule_fallback
    import schedule_solver
    for mod in (schedule_cpsat, schedule_solver, schedule_fallback):
        assert "can_work_row(" in inspect.getsource(mod), mod.__name__


def test_the_studio_and_the_replay_repair_enter_the_restaurants_boundary():
    import schedule_engine as se
    assert "daypart_scope" in inspect.getsource(se.repair_rows)
    assert getattr(se.studio_prepared, "__wrapped__", None) is not None


# ── STRESS-24: what held up stays held ────────────────────────────────────

def test_dst_nights_are_real_hours():
    fall = {"date": "2026-10-31", "shift_start": "8:00pm", "shift_end": "3:00am"}
    spring = {"date": "2027-03-13", "shift_start": "8:00pm", "shift_end": "3:00am"}
    assert st.real_hours(fall, "America/Chicago") == 8.0
    assert st.real_hours(spring, "America/Chicago") == 6.0


def test_a_wednesday_payroll_week_and_closed_dates_hold():
    c = _c(week_start_day=2, closed_dates={WEEK[0]})
    assert c.bucket(WEEK[1]) == "2026-10-07" and c.bucket(WEEK[2]) == "2026-10-14"
    v = [x for x in sr.violations([_row(WEEK[0], "Ana", "4:00pm", "10:00pm")], c) if x["kind"] == "closed_day"]
    assert v and v[0]["hard"]


def test_cross_week_overlap_and_runs_are_caught():
    tail = [_row((date.fromisoformat(WEEK[0]) - timedelta(days=i)).isoformat(), "Bo", "4:00pm", "10:00pm")
            for i in range(1, 8)]
    c = _c(base_rows={"bo": tail})
    kinds = _kinds([_row(WEEK[0], "Bo", "4:00pm", "10:00pm")], c)
    assert "long_run" in kinds
    c2 = _c(base_rows={"bo": [_row((date.fromisoformat(WEEK[0]) - timedelta(days=1)).isoformat(), "Bo",
                                   "9:00pm", "5:00am")]})
    assert "overlap" in _kinds([_row(WEEK[0], "Bo", "4:00am", "10:00am")], c2)
