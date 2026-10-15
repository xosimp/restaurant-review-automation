"""Workstream S (blind re-audit 10/11/26): the solver core on one objective.

schedule_objective prices a week in dollars (contract #1); schedule_shapes
(Stage 1) is person-aware; schedule_cpsat (Stage 2) weighs a hole by its
demand and role, solves lexicographically and says why each person (contract
#4). Each test names the finding it holds.
"""
import pytest

import schedule_cpsat as cs
import schedule_objective as so
import schedule_rules as sr
import schedule_shapes as shp
import shift_quality as sq
import staffing_curve as scurve

pytestmark = pytest.mark.skipif(not cs.available(), reason="OR-Tools not installed")

WEEK = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"]
LAST = ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"]
DAYS = [sq._day_name(d) for d in WEEK]


def cons(names, roles, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names}, week_start_day=2)
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {n.lower(): {roles[n].lower()} for n in names}
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def row(date, name, role="Server", start="5:00pm", end="10:00pm", **kw):
    r = {"date": date, "day": sq._day_name(date), "employee": name, "role": role, "shift_start": start,
         "shift_end": end, "scheduled_hours": "", "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    r.update(kw)
    return r


def _sig(names, roles, rates=None):
    return {"roster": list(names), "roster_roles": dict(roles),
            "overtime": {"rates": rates or {"server": 10.0, "cook": 18.0, "dishwasher": 15.0, "host": 13.0},
                         "default_rate": 12.0, "line": 40.0}}


def _hours(times):
    o = {d: times[0] for d in DAYS}
    c = {d: times[1] for d in DAYS}
    return o, c


def _req(dates, role, curve, part="night", required=None):
    return [{"date": d, "daypart": part, "factor": 1.0,
             "roles": [{"role": role, "required": required or max(curve.values()), "floor": 0}],
             "half_need": {t: {role: n} for t, n in curve.items()}} for d in dates]


def _on(rows, d, t):
    return sum(1 for r in rows if r["date"] == d and sr._span(r)[0] <= t < sr._span(r)[1])


# ── contract #1: one objective in dollars ─────────────────────────────────

def test_a_busy_half_hour_and_a_critical_role_cost_more_short():          # LOGIC-1, LOGIC-24
    reqs = _req([WEEK[0]], "Server", {1020 + 30 * i: 1 for i in range(8)}) + \
        _req([WEEK[2]], "Server", {1020 + 30 * i: 5 for i in range(8)})
    ctx = so.ObjectiveContext.build(None, WEEK, requirements=reqs)
    assert so.slot_value(ctx, WEEK[2], 1080, "Server") > 2 * so.slot_value(ctx, WEEK[0], 1080, "Server")
    assert so.slot_value(ctx, WEEK[2], 1080, "Dishwasher") > so.slot_value(ctx, WEEK[2], 1080, "Host")
    owned = so.ObjectiveContext.build(None, WEEK, requirements=reqs, criticality={"Host": 2.5})
    assert so.slot_value(owned, WEEK[2], 1080, "Host") > so.slot_value(owned, WEEK[2], 1080, "Dishwasher")


def test_price_week_says_each_part_in_dollars_and_never_sums_value_into_it():
    reqs = _req([WEEK[0]], "Server", {1020 + 30 * i: 2 for i in range(8)})
    ctx = so.ObjectiveContext.build(None, WEEK, requirements=reqs, rates={"server": 10.0}, budget=20)
    got = so.price_week([row(WEEK[0], "Ana", "Server", "5:00pm", "9:00pm")], ctx)
    assert got["parts"]["labor"] == 40.0
    assert got["parts"]["unfilled"] > 0 and got["parts"]["budget_over"] == 20 * so.BUDGET_OVER_FACTOR
    assert got["rates"]["short_person_hour_usd"] == so.SHORT_PERSON_HOUR_USD
    assert round(sum(got["parts"].values()), 2) == got["total"]


def test_stage_two_leaves_the_quiet_slot_empty_not_the_rush():            # LOGIC-1
    names, roles = ["Ana"], {"Ana": "Server"}
    c = cons(names, roles)
    reqs = _req([WEEK[0]], "Server", {t: 1 for t in range(900, 1140, 30)}) + \
        _req([WEEK[0]], "Server", {t: 6 for t in range(1140, 1320, 30)})
    ctx = so.ObjectiveContext.build(None, WEEK, requirements=reqs)
    slots = [row(WEEK[0], "", "Server", "3:00pm", "7:00pm"), row(WEEK[0], "", "Server", "7:00pm", "10:00pm")]
    res = cs.assign(slots, c, signals=_sig(names, roles), max_seconds=3, objective=ctx)
    assert [r["shift_start"] for r in res["rows"] if r.get("employee")] == ["7:00pm"]
    assert res["unfilled"][0]["shift_start"] == "3:00pm"


def test_fairness_never_decides_which_shift_goes_empty():                  # LOGIC-1, LOGIC-22
    names, roles = ["Ana"], {"Ana": "Server"}
    c = cons(names, roles)
    reqs = _req([WEEK[2]], "Server", {t: 4 for t in range(1080, 1260, 30)}) + \
        _req([WEEK[5]], "Server", {t: 1 for t in range(1080, 1260, 30)})
    ctx = so.ObjectiveContext.build(None, WEEK, requirements=reqs)
    slots = [row(WEEK[2], "", "Server", "6:00pm", "9:00pm"), row(WEEK[5], "", "Server", "6:00pm", "9:00pm")]
    res = cs.assign(slots, c, signals=_sig(names, roles), max_seconds=3, objective=ctx,
                    weights={"fairness": 100})
    staffed = {r["date"] for r in res["rows"] if r.get("employee")}
    assert WEEK[2] in staffed                     # the Friday rush is kept, whatever fairness says
    assert [p["phase"] for p in res["phases"]][0] == "rules"


def test_the_budget_cuts_the_least_valuable_half_hours_first():           # LOGIC-3
    names = [f"S{i}" for i in range(6)]
    roles = {n: "Server" for n in names}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    curve = {t: 1 for t in range(660, 1020, 30)}
    curve.update({t: 4 for t in range(1020, 1320, 30)})
    reqs = _req(WEEK[:3], "Server", curve)
    free = shp.shape_week(WEEK[:3], c, reqs, roster_roles=roles, rates={"server": 12.0}, max_seconds=4)
    tight = shp.shape_week(WEEK[:3], c, reqs, roster_roles=roles, rates={"server": 12.0}, budget_dollars=300,
                           max_seconds=4)
    h = lambda rows: sum(sr._span(r)[1] - sr._span(r)[0] for r in rows) / 60.0  # noqa: E731
    assert h(tight["rows"]) < h(free["rows"])                 # the budget moved the week
    d = WEEK[0]
    assert _on(tight["rows"], d, 1110) >= _on(tight["rows"], d, 780)   # the rush kept over the quiet afternoon


def test_the_firm_part_is_kept_past_the_budget():                          # LOGIC-3
    names = ["A", "B"]
    roles = {n: "Server" for n in names}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    hist = [row(x, "A", "Server", "5:00pm", "10:00pm") for x in LAST] + \
        [row(x, "A", "Server", "5:00pm", "10:00pm") for x in ("2026-09-30", "2026-10-01", "2026-10-02")]
    reqs = _req([WEEK[0]], "Server", {t: 1 for t in range(1020, 1320, 30)})
    got = shp.shape_week([WEEK[0]], c, reqs, roster_roles=roles, rates={"server": 12.0}, budget_dollars=1,
                         history_rows=hist, max_seconds=3)
    assert _on(got["rows"], WEEK[0], 1110) == 1               # never without a server at dinner


def test_a_budget_the_trim_holds_is_held_by_the_solve_itself():
    # EJ's week of 10/21/26 (10/15/26): at 3x a $9 hour past the budget cost
    # $36, under the $45 a half-staffed hour is worth, so the solve drafted
    # $2,539 over and the trim cut 206 hours by its own order. With the trim
    # on, the solve prices the budget as the trim will hold it.
    names = [f"S{i}" for i in range(6)]
    roles = {n: "Server" for n in names}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    curve = {t: 2 for t in range(660, 1020, 30)}
    curve.update({t: 4 for t in range(1020, 1320, 30)})
    reqs = _req(WEEK[:3], "Server", curve)
    budget = 400.0

    def spend(rows):
        return sum((sr._span(r)[1] - sr._span(r)[0]) / 60.0 * 9.0 for r in rows)
    held_ctx = so.ObjectiveContext.build(None, WEEK[:3], budget=budget, requirements=reqs, rates={"server": 9.0},
                                         constraints=c, budget_held=True)
    soft_ctx = so.ObjectiveContext.build(None, WEEK[:3], budget=budget, requirements=reqs, rates={"server": 9.0},
                                         constraints=c)
    assert held_ctx.budget_over_factor == so.BUDGET_HELD_FACTOR and soft_ctx.budget_over_factor == so.BUDGET_OVER_FACTOR
    soft = shp.shape_week(WEEK[:3], c, reqs, roster_roles=roles, rates={"server": 9.0}, budget_dollars=budget,
                          objective=soft_ctx, max_seconds=4)
    held = shp.shape_week(WEEK[:3], c, reqs, roster_roles=roles, rates={"server": 9.0}, budget_dollars=budget,
                          objective=held_ctx, max_seconds=4)
    assert spend(soft["rows"]) > budget                       # coverage worth more than 3x: drafted past it
    assert spend(held["rows"]) <= budget * 1.02               # held: the solve keeps inside it
    # No budget, nothing held.
    assert so.ObjectiveContext.build(None, WEEK, budget_held=True).budget_over_factor == so.BUDGET_OVER_FACTOR


# ── Stage 1: person-aware capacity ────────────────────────────────────────

def _lunch_dinner(names_lunch, names_dinner, role="Server"):
    names = names_lunch + names_dinner
    roles = {n: role for n in names}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    for n in names_lunch:
        c.daypart_avail[n.lower()] = {d: "morning" for d in DAYS}
    curve_l = {t: 2 for t in range(660, 900, 30)}
    curve_n = {t: 2 for t in range(1020, 1320, 30)}
    reqs = _req(WEEK, role, curve_l, part="morning") + _req(WEEK, role, curve_n, part="night")
    return names, roles, c, reqs


def test_lunch_only_people_get_the_lunches_and_nobody_a_lunch_to_close_double():   # STRESS-2, LOGIC-18, STRESS-1
    names, roles, c, reqs = _lunch_dinner(["L1", "L2", "L3"], ["D1", "D2", "D3"])
    got = shp.shape_week(WEEK, c, reqs, roster_roles=roles, rates={"server": 12.0}, max_seconds=5)
    by = {}
    for r in got["rows"]:
        by.setdefault(r["_s1"], []).append(r)
        assert sr._span(r)[1] - sr._span(r)[0] <= 10 * 60       # no 11-12h doubles
    for n in ("L1", "L2", "L3"):
        assert by.get(n), n                                       # the lunch crew keeps its hours
        assert all(sr._span(r)[1] <= 15 * 60 + 30 for r in by[n])
    # capacity per person: nobody twice a date, nobody past their week
    seen = set()
    for r in got["rows"]:
        assert (r["_s1"], r["date"]) not in seen
        seen.add((r["_s1"], r["date"]))


def test_a_role_nobody_can_work_is_reported_with_why():                    # STRESS-9
    names = ["Cook A", "Server A"]
    roles = {"Cook A": "Line Cook", "Server A": "Server"}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    c.blocked_dates["cook a"] = {d: "on approved time off" for d in WEEK}
    reqs = [{"date": d, "daypart": "night", "factor": 1.0,
             "roles": [{"role": "Line Cook", "required": 2, "floor": 0}, {"role": "Server", "required": 1, "floor": 0}],
             "half_need": {t: {"Line Cook": 2, "Server": 1} for t in range(1020, 1260, 30)}} for d in WEEK]
    got = shp.shape_week(WEEK, c, reqs, roster_roles=roles, rates={"server": 10.0}, max_seconds=3)
    nobody = [s for s in got["short"] if s["kind"] == "nobody" and s["role"] == "Line Cook"]
    assert len(nobody) == 7 and "approved time off" in nobody[0]["why"]
    rows = [dict(r, employee=r["_s1"]) for r in got["rows"]]
    un = shp.uncovered_roles(rows, reqs, c)
    assert {u["date"] for u in un} == set(WEEK) and un[0]["role"] == "Line Cook"
    assert shp.uncovered_line(un).startswith("⚠ Roles with nobody on:")


def test_doubles_only_for_people_who_work_them():                          # LOGIC-19
    names, roles = ["Ana"], {"Ana": "Server"}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    reqs = _req([WEEK[0]], "Server", {t: 1 for t in range(660, 840, 30)}, part="morning") + \
        _req([WEEK[0]], "Server", {t: 1 for t in range(1080, 1260, 30)})
    one = shp.shape_week([WEEK[0]], c, reqs, roster_roles=roles, rates={"server": 10.0}, max_seconds=3)
    assert len(one["rows"]) <= 1
    c.doubles_ok = {"ana": True}
    two = shp.shape_week([WEEK[0]], c, reqs, roster_roles=roles, rates={"server": 10.0}, max_seconds=3)
    legs = sorted(sr._span(r) for r in two["rows"])
    assert len(legs) == 2 and legs[1][0] - legs[0][1] >= shp.DOUBLE_BREAK_MINUTES
    res = cs.assign([dict(r, employee="") for r in two["rows"]], c, signals=_sig(names, roles), max_seconds=3)
    assert sum(1 for r in res["rows"] if r.get("employee") == "Ana") == 2


def test_a_minor_never_works_a_double():                                   # LOGIC-19 (a minor's day cap)
    ctx = so.ObjectiveContext.build(None, WEEK)
    c = cons(["Kid"], {"Kid": "Server"}, minors={"kid": True}, doubles_ok={"kid": True})
    assert not ctx.doubles_ok("Kid", c)


def test_shifts_keep_to_the_restaurants_own_lengths():                     # STRESS-1, LOGIC-5
    names = [f"S{i}" for i in range(8)]
    roles = {n: "Server" for n in names}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    hist = []
    for i, d in enumerate(LAST):
        for j, n in enumerate(names):
            if (i + j) % 7 < 5:
                hist.append(row(d, n, "Server", "11:00am", "4:00pm") if j % 2 else row(d, n, "Server", "4:30pm", "10:00pm"))
    reqs = _req(WEEK, "Server", {t: 2 for t in range(660, 960, 30)}, part="morning") + \
        _req(WEEK, "Server", {t: 3 for t in range(990, 1320, 30)})
    got = shp.shape_week(WEEK, c, reqs, roster_roles=roles, history_rows=hist, rates={"server": 10.0}, max_seconds=5)
    lengths = [sr._span(r)[1] - sr._span(r)[0] for r in got["rows"]]
    assert lengths and max(lengths) <= 8 * 60
    days = {}
    for r in got["rows"]:
        days.setdefault(r["_s1"], set()).add(r["date"])
    assert max(len(v) for v in days.values()) <= 6


# ── shoulders, prep and 24 hours ──────────────────────────────────────────

def test_the_dish_pit_stays_after_close_and_cooks_come_in_before_open():  # LOGIC-11, STRESS-22
    names = ["Dish", "Dish Two", "Cook", "Cook Two"]
    roles = {"Dish": "Dishwasher", "Dish Two": "Dishwasher", "Cook": "Line Cook", "Cook Two": "Line Cook"}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl, role_names={"dishwasher": "Dishwasher",
                                                                       "line cook": "Line Cook"})
    stays, leads = so.shoulder_habits(c, [])
    assert stays.get("dishwasher") == 45 and leads.get("line cook") == 60
    c.learned_stays, c.learned_leads = stays, leads
    assert sr.close_cap_minutes(c, {"date": WEEK[0], "role": "Dishwasher", "employee": ""}) == 22 * 60 + 45
    c.role_buffers = {"Dishwasher": 0}                       # the owner's word, even 0, is theirs
    assert sr.close_cap_minutes(c, {"date": WEEK[0], "role": "Dishwasher", "employee": ""}) == 22 * 60
    c.role_buffers = {}
    reqs = [{"date": WEEK[0], "daypart": p, "factor": 1.0,
             "roles": [{"role": "Dishwasher", "required": 1, "floor": 0}, {"role": "Line Cook", "required": 1, "floor": 0}],
             "half_need": {t: {"Dishwasher": 1, "Line Cook": 1} for t in rng}}
            for p, rng in (("morning", range(660, 900, 30)), ("night", range(900, 1320, 30)))]
    got = shp.shape_week([WEEK[0]], c, reqs, roster_roles=roles, rates={"dishwasher": 15, "line cook": 18},
                         max_seconds=4)
    dish = [sr._span(r) for r in got["rows"] if r["role"] == "Dishwasher"]
    cook = [sr._span(r) for r in got["rows"] if r["role"] == "Line Cook"]
    assert max(e for _s, e in dish) > 22 * 60
    assert min(s for s, _e in cook) < 11 * 60


def test_measured_prep_before_the_doors_is_kept():                         # STRESS-22
    names = ["Prep"]
    roles = {"Prep": "Prep Cook"}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    conc = {(d, "morning"): {"Prep Cook": {"slots": {t: 1 for t in range(420, 900, 30)}, "peak": 1}} for d in DAYS}
    reqs = [{"date": WEEK[0], "daypart": "morning", "factor": 1.0, "roles": [{"role": "Prep Cook", "required": 1}],
             "half_need": {t: {"Prep Cook": 1} for t in range(660, 900, 30)}}]
    got = shp.shape_week([WEEK[0]], c, reqs, concurrent=conc, roster_roles=roles, rates={"prep cook": 16},
                         max_seconds=3)
    assert min(sr._span(r)[0] for r in got["rows"]) <= 7 * 60 + 30


def test_a_24_hour_restaurant_staffs_its_graveyard_across_midnight():     # STRESS-4
    names = ["G1", "G2", "D1", "D2"]
    roles = {n: "Server" for n in names}
    o, cl = _hours(("12:00am", "12:00am"))
    c = cons(names, roles, open_times=o, close_times=cl)
    assert sr.open_hours(c, DAYS[0]) == (240, 240 + 1440)
    assert shp.is_24h(c, DAYS[0])
    assert sr.close_cap_minutes(c, {"date": WEEK[0], "role": "Server", "employee": ""}) > 30 * 60
    hist = []
    for d in LAST + ["2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
                     "2026-10-06"]:
        hist += [row(d, "G1", "Server", "10:00pm", "6:00am"), row(d, "D1", "Server", "6:00am", "2:00pm")]
    got = shp.shape_week(WEEK[:2], c, [], roster_roles=roles, history_rows=hist, rates={"server": 10.0},
                         max_seconds=4)
    grave = [r for r in got["rows"] if sr._span(r)[0] >= 22 * 60]
    assert grave and all(sr._span(r)[1] > 24 * 60 for r in grave)


# ── determinism, stability, the usual week ────────────────────────────────

def _bistro():
    names = [f"S{i}" for i in range(6)]
    roles = {n: "Server" for n in names}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    hist = []
    for wk in range(4):
        for i in range(7):
            from datetime import date, timedelta
            d = (date(2026, 9, 16) + timedelta(days=7 * wk + i)).isoformat()
            for j, n in enumerate(names):
                if (i + j) % 6 < 4:
                    hist.append(row(d, n, "Server", "11:00am", "4:00pm") if j < 3 else row(d, n, "Server", "4:00pm",
                                                                                            "10:00pm"))
    reqs = _req(WEEK, "Server", {t: 2 for t in range(660, 960, 30)}, part="morning") + \
        _req(WEEK, "Server", {t: 2 for t in range(960, 1320, 30)})
    return names, roles, c, hist, reqs


def test_the_same_inputs_draft_the_same_week():                            # LOGIC-9, STRESS-10
    names, roles, c, hist, reqs = _bistro()
    kw = dict(history_rows=hist, signals=_sig(names, roles), roster_roles=roles, max_seconds=6)
    a = cs.solver_draft(WEEK, c, reqs, **kw)
    b = cs.solver_draft(WEEK, c, reqs, **kw)
    sig = lambda g: sorted((r["date"], r["employee"], r["shift_start"], r["shift_end"]) for r in g["rows"])  # noqa
    assert sig(a) == sig(b)
    assert a["stats"]["budget"]["unit"] == "deterministic" and a["stats"]["seed"] == cs.SEED


def test_last_weeks_week_is_kept_when_nothing_changed():                   # LOGIC-10
    names, roles, c, hist, reqs = _bistro()
    kw = dict(history_rows=hist, signals=_sig(names, roles), roster_roles=roles, max_seconds=6)
    w1 = cs.solver_draft(WEEK, c, reqs, **kw)
    last = [dict(r, date=LAST[WEEK.index(r["date"])]) for r in w1["rows"]]
    w2 = cs.solver_draft(WEEK, c, reqs, last_week_rows=last, **kw)
    k = lambda r: (r["date"], r["shift_start"], r["shift_end"], r["employee"])  # noqa: E731
    same = len(set(map(k, w1["rows"])) & set(map(k, w2["rows"])))
    assert same >= 0.95 * len(w1["rows"])


def test_the_first_draft_keeps_peoples_usual_days():                       # STRESS-15
    names, roles, c, hist, reqs = _bistro()
    got = cs.solver_draft(WEEK, c, reqs, history_rows=hist, signals=_sig(names, roles), roster_roles=roles,
                          max_seconds=6)
    assert got["stats"]["usual_days"]["pct"] >= 80
    assert got["stats"]["history_weeks"] >= 3


def test_a_new_restaurant_gets_full_shifts_and_its_unfloored_roles():     # STRESS-20
    names = ["S1", "S2", "S3", "Dish"]
    roles = {"S1": "Server", "S2": "Server", "S3": "Server", "Dish": "Dishwasher"}
    o, cl = _hours(("11:00am", "9:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl, role_floors={"Server": {"morning": 1, "night": 1}})
    reqs = [{"date": d, "daypart": p, "factor": 1.0, "roles": [{"role": "Server", "required": 1, "floor": 1}]}
            for d in WEEK for p in ("morning", "night")]
    got = shp.shape_week(WEEK, c, reqs, roster_roles=roles, rates={"server": 10, "dishwasher": 14}, max_seconds=5)
    assert any(r["role"] == "Dishwasher" for r in got["rows"])
    assert got["assumed"] and got["assumed"][0]["role"] == "Dishwasher"
    assert max(sr._span(r)[1] - sr._span(r)[0] for r in got["rows"]) >= 5 * 60   # open-to-mid, mid-to-close


def test_the_code_draft_line_claims_only_what_was_measured():              # STRESS-15, STRESS-20
    import schedule_fallback as fb
    none = fb.note_line("solver", "other", [WEEK[0]], basis={"history_weeks": 0})
    assert "no shift history yet" in none and "usual crew" not in none
    some = fb.note_line("solver", "other", [WEEK[0]], basis={"history_weeks": 8, "usual_days": {"pct": 92.5}})
    assert "92.5% of their usual days kept" in some


# ── LOGIC-16: the shoulders round, the rush rounds up ─────────────────────

def test_a_fraction_of_a_person_off_the_rush_rounds_at_the_fractile():
    assert scurve.round_need(1.2) == 1 and scurve.round_need(1.6) == 2
    assert scurve.round_need(2.1, at_peak=True) == 3


# ── contract #4: reasons, and conflicts that name the real cause ──────────

def test_reasons_are_typed_and_never_argue_against_the_pick():             # contract #4, TRUST-1
    names = ["Kim", "Lee"]
    roles = {n: "Server" for n in names}
    c = cons(names, roles)
    rows = [row(d, "Kim", "Server", "9:00am", "5:00pm", _pinned=True) for d in WEEK[:5]]
    rows.append(row("2026-10-19", "", "Server", "4:00pm", "10:00pm", shift_id="a" * 32))
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=3)
    why = next(x for x in res["reasons"] if x["date"] == "2026-10-19")
    assert why["employee"] == "Lee" and why["shift_id"] == "a" * 32
    assert "overtime_avoided" in {f["key"] for f in why["facts"]}
    assert why["runner_up"]["employee"] == "Kim" and why["runner_up"]["delta_cost"] > 0
    assert why["text"].startswith("Lee, not Kim: Kim would hit overtime")
    for x in res["reasons"]:
        # a "P, not Q:" line always cites a fact FOR the pick; else it says
        # the week as a whole decided
        if x.get("runner_up") and ", not " in x["text"].split(":")[0]:
            assert x["facts"], x
        elif x.get("runner_up"):
            assert "the rest of the week decided" in x["text"]


def test_a_lunch_only_cook_is_named_as_such_and_splitting_is_offered():   # STRESS-21
    names = ["Cook A", "Cook B"]
    roles = {n: "Line Cook" for n in names}
    c = cons(names, roles)
    c.daypart_avail["cook b"] = {d: "morning" for d in DAYS}
    c.unavailable_days["cook a"] = {"Monday"}
    rows = [row("2026-10-19", "", "Line Cook", "11:00am", "9:00pm", _floor=True)]
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=3)
    text = res["conflicts"][0]["text"]
    assert "Cook B works only lunch on Mondays" in text
    assert any(r["kind"] == "split" for r in res["conflicts"][0]["relax_one_of"])


# ── the owner's levers reach the solver ───────────────────────────────────

def test_doubles_and_role_weights_are_stored_and_read(tmp_path):    # LOGIC-19, LOGIC-24
    import models
    import staff_settings as ss
    db = str(tmp_path / "s.db")
    models.init_db(db)
    rid = models.create_restaurant(models.Restaurant(name="Weights", owner_email="w@example.com"), db)
    assert ss.upsert(rid, "Ana", doubles=True, db_path=db)["doubles"] is True
    assert ss.upsert(rid, "Ana", doubles=False, db_path=db)["doubles"] is False
    assert ss.upsert(rid, "Ana", doubles="auto", db_path=db)["doubles"] is None
    models.update_restaurant(rid, {"role_criticality_json": '{"Host": 2.0}'}, db_path=db)
    assert models.get_restaurant(rid, db).role_criticality_json == '{"Host": 2.0}'


def test_the_engine_gives_the_solver_a_fixed_budget_and_the_clock_only_as_its_cap(monkeypatch):   # LOGIC-9
    import schedule_engine as se

    class Clock:
        def seconds_left(self):
            return se.SCHEDULE_POST_MODEL_SECONDS + 7.0
    seen = {}
    monkeypatch.setattr(se, "current_clock", lambda: Clock())
    monkeypatch.setattr(cs, "solver_draft", lambda *a, **k: seen.update(k) or {"rows": []})
    names, roles = ["Ana"], {"Ana": "Server"}
    se._solver_first_rows(None, WEEK, {"concurrent": {}}, cons(names, roles), {}, [], [], {"overtime": {"x": 1}},
                          None, roles, 5.0, [], {})
    assert seen["max_seconds"] == se.SOLVER_DRAFT_SECONDS          # never what the clock has left
    assert seen["wall_cap"] == 7.0


def test_the_half_hour_curve_is_the_requirement_not_the_habits_head_count():   # LOGIC-17
    names = [f"S{i}" for i in range(8)]
    roles = {n: "Server" for n in names}
    o, cl = _hours(("11:00am", "10:00pm"))
    c = cons(names, roles, open_times=o, close_times=cl)
    reqs = _req([WEEK[0]], "Server", {t: 2 for t in range(1020, 1320, 30)}, required=6)
    got = shp.shape_week([WEEK[0]], c, reqs, roster_roles=roles, rates={"server": 10.0}, max_seconds=3)
    # two on at once all night need two or three people, never the usual six
    assert 2 <= len(got["rows"]) <= 3
    assert max(_on(got["rows"], WEEK[0], t) for t in range(1020, 1320, 30)) == 2
