"""Workstream F (scheduler audit 10/10/26): the solver-first scheduler.

schedule_cpsat — who works each shift, one CP-SAT model (RM-11, LOGIC-24,
LOGIC-19, LOGIC-28), with marginal-cost reasons and minimal conflicting
rule sets (RM-12, LOGIC-32), stability against last published week per
slot (RM-3, LOGIC-18); schedule_shapes — Stage 1 shapes (RM-8). The rule
sweep (schedule_rules.violations) is the oracle every answer is held to.
"""
import random

import pytest

import schedule_cpsat as cs
import schedule_rules as sr
import schedule_shapes as shp
import shift_quality as sq

pytestmark = pytest.mark.skipif(not cs.available(), reason="OR-Tools not installed")

WEEK = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"]
LAST = ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"]


def cons(names, roles, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=list(names), active={n.lower() for n in names}, week_start_day=2)
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


def _hard(rows, c):
    return [v for v in sr.violations(rows, c) if v["hard"]]


def _sig(names, roles, rates=None):
    return {"roster": list(names), "roster_roles": dict(roles),
            "overtime": {"rates": rates or {"server": 10.0, "cook": 18.0, "manager": 25.0}, "default_rate": 12.0,
                         "line": 40.0}}


# ── legality: the sweep finds nothing the model let through ───────────────

@pytest.mark.parametrize("seed", range(4))
def test_no_legal_rule_is_ever_broken_on_random_weeks(seed):
    rng = random.Random(seed)
    names = [f"P{i}" for i in range(12)]
    roles = {n: rng.choice(("Server", "Server", "Cook")) for n in names}
    c = cons(names, roles)
    for n in names:
        if rng.random() < 0.3:
            c.unavailable_days[n.lower()] = {rng.choice([sq._day_name(d) for d in WEEK])}
        if rng.random() < 0.2:
            c.hours_limits[n.lower()] = (None, 18.0)
        if rng.random() < 0.15:
            c.blocked_dates[n.lower()] = {rng.choice(WEEK): "on approved time off"}
    slots = []
    for d in WEEK:
        for _k in range(3):
            slots.append(row(d, "", "Server", rng.choice(("11:00am", "4:00pm")), "10:00pm"))
        slots.append(row(d, "", "Cook", "3:00pm", "10:00pm"))
    res = cs.assign(slots, c, signals=_sig(names, roles), max_seconds=5)
    assert res["status"] in ("optimal", "time_limit")
    legal = [v for v in _hard(res["rows"], c) if sr.tier_of(v) == sr.TIER_LEGAL]
    assert not legal, legal
    # nobody has two shifts on one date
    seen = set()
    for r in res["rows"]:
        k = (r["employee"], r["date"])
        assert k not in seen
        seen.add(k)


# ── overtime: the owner's teammate rule falls out of the cost ─────────────

def _ot_week(lee_free=True):
    names = ["Kim", "Lee"]
    roles = {"Kim": "Server", "Lee": "Server"}
    c = cons(names, roles)
    if not lee_free:
        c.unavailable_days["lee"] = {"Monday"}
    rows = [row(d, "Kim", "Server", "9:00am", "5:00pm", _pinned=True) for d in WEEK[:5]]      # Kim at 40h
    rows.append(row("2026-10-19", "", "Server", "4:00pm", "10:00pm"))                          # Monday dinner
    return names, roles, c, rows


def test_overtime_goes_to_nobody_while_a_teammate_has_room():
    names, roles, c, rows = _ot_week(lee_free=True)
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=5)
    got = [r for r in res["rows"] if r["date"] == "2026-10-19"]
    assert got and got[0]["employee"] == "Lee"
    why = next(x for x in res["reasons"] if x["date"] == "2026-10-19")
    assert why["text"].startswith("Lee, not Kim: Kim would hit overtime (+$")


def test_overtime_when_nobody_else_can_take_the_shift_a_floor_needs():
    names, roles, c, rows = _ot_week(lee_free=False)
    rows[-1]["_floor"] = True                          # coverage outranks overtime and the ceiling
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=5)
    got = [r for r in res["rows"] if r["date"] == "2026-10-19"]
    assert got and got[0]["employee"] == "Kim"
    assert "overtime" in res["terms"]


def test_a_requirement_shift_is_left_rather_than_paid_as_overtime():
    names, roles, c, rows = _ot_week(lee_free=False)   # not a floor: requirements rank below overtime
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=5)
    assert not [r for r in res["rows"] if r["date"] == "2026-10-19"]
    assert res["unfilled"] and not res["unfilled"][0]["floor"]


def test_a_persons_own_maximum_is_never_passed():
    names, roles, c, rows = _ot_week(lee_free=False)
    rows[-1]["_floor"] = True
    c.hours_limits["kim"] = (None, 40.0)               # Kim's own maximum: hard
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=5)
    assert not [r for r in res["rows"] if r["date"] == "2026-10-19"]
    assert res["unfilled"] and res["unfilled"][0]["date"] == "2026-10-19"


# ── the manager on the floor every minute ─────────────────────────────────

def test_a_stretch_nobody_manages_takes_a_manager_from_the_open_slots():
    names = ["Max", "Sue", "Ann"]
    roles = {"Max": "Manager", "Sue": "Manager", "Ann": "Server"}
    c = cons(names, roles, managers={"max": "Manager", "sue": "Manager"})
    c.known_roles = {"max": {"manager", "server"}, "sue": {"manager", "server"}, "ann": {"server"}}
    d = WEEK[0]
    rows = [row(d, "Max", "Manager", "10:00am", "4:00pm", _pinned=True),
            row(d, "", "Server", "4:00pm", "10:00pm"), row(d, "", "Server", "4:00pm", "10:00pm")]
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=5)
    on = sorted(r["employee"] for r in res["rows"] if r["shift_start"] == "4:00pm")
    assert "Sue" in on, on
    assert not sr.manager_gaps(res["rows"], c)


# ── locked rows (contract #4) and determinism ─────────────────────────────

def test_locked_and_pinned_rows_are_kept_exactly():
    names = ["Kim", "Lee", "Ana"]
    roles = {n: "Server" for n in names}
    c = cons(names, roles)
    rows = [row(WEEK[0], "Kim", locked=True), row(WEEK[0], "Lee", _pinned=True), row(WEEK[1], "")]
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=3)
    assert res["rows"][0]["employee"] == "Kim" and res["rows"][1]["employee"] == "Lee"
    assert res["rows"][0].get("locked") is True


def test_same_inputs_same_week():
    rng = random.Random(3)
    names = [f"P{i}" for i in range(10)]
    roles = {n: "Server" for n in names}
    c = cons(names, roles)
    slots = [row(d, "", "Server", rng.choice(("11:00am", "4:00pm")), "10:00pm") for d in WEEK for _k in range(3)]
    a = cs.assign([dict(r) for r in slots], c, signals=_sig(names, roles), max_seconds=3)
    b = cs.assign([dict(r) for r in slots], c, signals=_sig(names, roles), max_seconds=3)
    assert [r["employee"] for r in a["rows"]] == [r["employee"] for r in b["rows"]]


# ── stability against last published week (RM-3) ─────────────────────────

def test_last_weeks_person_keeps_their_slot_when_nothing_else_differs():
    names = ["Ana", "Ben", "Cal"]
    roles = {n: "Server" for n in names}
    c = cons(names, roles)
    last = [row(LAST[1], "Cal", "Server", "4:00pm", "10:00pm")]           # Thursday dinner last week
    rows = [row(WEEK[1], "", "Server", "4:30pm", "10:00pm")]               # Thursday dinner, 30 min later
    res = cs.assign(rows, c, signals=_sig(names, roles), last_week=last, max_seconds=3)
    assert res["rows"][0]["employee"] == "Cal"
    assert "had this shift last week" in res["reasons"][0]["text"]


def test_a_slot_starting_more_than_thirty_minutes_off_is_a_different_slot():
    names = ["Ana", "Cal"]
    roles = {n: "Server" for n in names}
    c = cons(names, roles)
    prob = cs.CpProblem([row(WEEK[1], "", "Server", "5:00pm", "10:00pm")], c, signals=_sig(names, roles))
    assert cs.last_week_slots(prob, [row(LAST[1], "Cal", "Server", "4:00pm", "10:00pm")]) == {}
    assert cs.last_week_slots(prob, [row(LAST[1], "Cal", "Server", "4:30pm", "10:00pm")]) == {0: {1}}


# ── the smallest set of rules that conflict (LOGIC-32) ────────────────────

def test_an_unfillable_floor_names_the_smallest_set_of_rules_to_relax():
    names = ["Kim", "Lee", "Ana"]
    roles = {n: "Server" for n in names}
    c = cons(names, roles)
    c.hours_limits["kim"] = (None, 30.0)
    c.unavailable_days["lee"] = {"Tuesday"}
    d = "2026-10-20"                                                       # a Tuesday
    rows = [row(x, "Kim", "Server", "9:00am", "3:00pm", _pinned=True) for x in WEEK[:5]]      # Kim at 30h
    rows += [row(d, "Ana", "Server", "4:00pm", "10:00pm", _pinned=True),
             row(d, "", "Server", "4:00pm", "10:00pm", _floor=True)]
    res = cs.assign(rows, c, signals=_sig(names, roles), max_seconds=3)
    assert res["unfilled"] and res["unfilled"][0]["floor"]
    conflict = res["conflicts"][0]
    kinds = sorted(r["kind"] for r in conflict["relax_one_of"])
    assert kinds == ["availability", "floor", "max_hours"], conflict
    assert "Relax one of them" in conflict["text"] and "Kim's 30h maximum" in conflict["text"]


# ── Stage 1: shapes ───────────────────────────────────────────────────────

def _shape_inputs():
    names = [f"S{i}" for i in range(8)]
    roles = {n: "Server" for n in names}
    c = cons(names, roles, open_times={sq._day_name(d): "11:00am" for d in WEEK},
             close_times={sq._day_name(d): "10:00pm" for d in WEEK}, section_cap=4, foh_roles={"server"},
             role_floors={"Server": {"night": 2}}, closers_by_role={"server": {"s0", "s1", "s2"}})
    concurrent = {}
    for d in WEEK:
        day = sq._day_name(d)
        concurrent[(day, "morning")] = {"Server": {"slots": {t: 2 for t in range(660, 900, 30)}}}
        concurrent[(day, "night")] = {"Server": {"slots": {**{t: 2 for t in range(900, 1080, 30)},
                                                           **{t: 5 for t in range(1080, 1230, 30)},
                                                           **{t: 2 for t in range(1230, 1320, 30)}}}}
    reqs = [{"date": d, "daypart": p, "factor": 1.0, "roles": [{"role": "Server", "required": 3, "floor": 2}]}
            for d in WEEK for p in ("morning", "night")]
    return names, roles, c, concurrent, reqs


def test_shapes_cover_people_on_at_once_hold_the_section_cap_and_end_one_at_close():
    names, roles, c, concurrent, reqs = _shape_inputs()
    got = shp.shape_week(WEEK, c, reqs, concurrent=concurrent, roster_roles=roles, max_seconds=5)
    assert got["stats"]["status"] == "optimal"
    for d in WEEK:
        mine = [r for r in got["rows"] if r["date"] == d]
        spans = [sr._span(r) for r in mine]
        for t in range(1080, 1230, 30):
            on = sum(1 for s, e in spans if s <= t and t + 30 <= e)
            assert on == 4, (d, t, on)                    # demand 5, held to the section count of 4
        assert any(e >= 22 * 60 - 15 for _s, e in spans)  # a closer's shift ends at close
        assert sum(1 for r in mine if r.get("_floor")) == 2
        assert all(not r["employee"] and r["_open"] for r in mine)


def test_shapes_follow_last_weeks_shapes_when_they_cost_the_same():
    names, roles, c, concurrent, reqs = _shape_inputs()
    first = shp.shape_week(WEEK, c, reqs, concurrent=concurrent, roster_roles=roles, max_seconds=5)
    last = [dict(r, date=LAST[WEEK.index(r["date"])], employee="S1") for r in first["rows"]]
    again = shp.shape_week(WEEK, c, reqs, concurrent=concurrent, roster_roles=roles, last_week_rows=last,
                           max_seconds=5)
    shapes = lambda rows: sorted((r["date"], r["shift_start"], r["shift_end"]) for r in rows)  # noqa: E731
    assert shapes(again["rows"]) == shapes(first["rows"])


def test_the_solver_first_draft_of_a_synthetic_restaurant_breaks_no_hard_rule():
    import scripts.schedule_solver_bench as bench
    s = bench.synthetic(60, 1)
    got = cs.solver_draft(bench.WEEK, s["constraints"], s["requirements"], concurrent=s["concurrent"],
                          history_rows=s["last_week"], context_rows=s["plan"], last_week_rows=s["last_week"],
                          signals=s["signals"], roster_roles=s["roster_roles"], budget_dollars=s["budget_dollars"],
                          max_seconds=15)
    rows = list(s["plan"]) + got["rows"]
    assert got["rows"] and not got["unassigned"]
    assert not _hard(rows, s["constraints"])
    assert not sr.manager_gaps(rows, s["constraints"])
    assert got["reasons"] and got["stats"]["engine"] == "shapes+cp-sat"


# ── the repair loop's stage ───────────────────────────────────────────────

def test_improve_reassigns_only_when_its_own_cost_falls_and_nothing_above_the_budget_regresses():
    names, roles, c, rows = _ot_week(lee_free=True)
    rows[-1] = dict(rows[-1], employee="Kim")                    # the draft gave Kim overtime
    res = cs.improve(rows, {"roster_roles": roles}, signals=_sig(names, roles), constraints=c, max_seconds=3)
    assert res["applied"] and res["stats"]["engine"] == "cp-sat"
    assert res["rows"][-1]["employee"] == "Lee"
    assert res["stats"]["cost"] < res["stats"]["draft_cost"]
    again = cs.improve(res["rows"], {"roster_roles": roles}, signals=_sig(names, roles), constraints=c, max_seconds=3)
    assert not again["applied"]


def test_improve_falls_back_to_the_backtracking_solver_without_cp_sat(monkeypatch):
    names, roles, c, rows = _ot_week(lee_free=True)
    rows[-1] = dict(rows[-1], employee="Kim")
    monkeypatch.setattr(cs, "available", lambda: False)
    res = cs.improve(rows, {"roster_roles": roles}, signals=_sig(names, roles), constraints=c, max_seconds=2)
    assert res["stats"]["engine"] == "backtracking"


# ── the quality gate solved exactly before any model call (LOGIC-26) ──────

def _gate_week(lee_free):
    names = ["Kim", "Lee", "Ana"]
    roles = {n: "Server" for n in names}
    c = cons(names, roles, role_floors={"Server": {"night": 2}},
             open_times={sq._day_name(d): "11:00am" for d in WEEK},
             close_times={sq._day_name(d): "10:00pm" for d in WEEK})
    c.hours_limits["kim"] = (None, 30.0)
    if not lee_free:
        c.unavailable_days["lee"] = {"Tuesday"}
    d = "2026-10-20"
    rows = [row(x, "Kim", "Server", "9:00am", "3:00pm") for x in WEEK[:5]] + \
        [row(d, "Ana", "Server", "4:00pm", "10:00pm")]
    return names, roles, c, rows, d


def test_the_gate_names_the_rules_in_the_way_when_no_legal_fix_exists():
    import schedule_engine as se
    names, roles, c, rows, d = _gate_week(lee_free=False)
    assert any(v["kind"] == "coverage_floor" and v["date"] == d for v in sr.violations(rows, c))
    got = cs.gate_exact(rows, c, [d], signals=_sig(names, roles))
    assert not got["fixes"] and got["probed"] == 1
    kinds = sorted(r["kind"] for r in got["conflicts"][0]["relax_one_of"])
    assert kinds == ["availability", "floor", "max_hours"]
    gate = {"dates": [d], "kinds": ["rules"]}
    assert se._gate_settled_by_solver(gate, got)                    # no model rewrite is paid for
    assert not se._gate_settled_by_solver(dict(gate, kinds=["rules", "coverage"]), got)


def test_the_gate_takes_the_exact_fix_when_one_exists():
    import schedule_engine as se
    names, roles, c, rows, d = _gate_week(lee_free=True)
    got = cs.gate_exact(rows, c, [d], signals=_sig(names, roles))
    assert [r["employee"] for r in got["fixes"]] == ["Lee"] and not got["conflicts"]
    assert not se._gate_settled_by_solver({"dates": [d], "kinds": ["rules"]}, got)
    fixed = rows + got["fixes"]
    assert not any(v["kind"] == "coverage_floor" and v["date"] == d for v in sr.violations(fixed, c))
