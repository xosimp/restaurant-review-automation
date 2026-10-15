"""Schedule fix round 10/11/26, workstream P — the managers inside the solve
(blind re-audit 10/11/26 ARCH-23), the passes that follow the one objective
(LOGIC-8 top-up, LOGIC-23 savings, the trim), and the demand Stage 1 reads
before the open (SP's finding 4). No model is called."""
from datetime import date, timedelta

import pytest

import schedule_cpsat as cs
import schedule_economics as econ
import schedule_engine as se
import schedule_objective as so
import schedule_optimizer as sopt
import schedule_rules as sr
import schedule_shapes as shp
import schedule_skeleton as sk
import shift_quality as sq

pytestmark = pytest.mark.skipif(not cs.available(), reason="OR-Tools not installed")

# A Wednesday-to-Tuesday week, as Simple EJ's (week_start_day 2).
WEEK = [(date(2026, 10, 14) + timedelta(days=i)).isoformat() for i in range(7)]      # Wed 10/14 - Tue 10/20
DAYS = [sq._day_name(d) for d in WEEK]
OPEN = {d: "11:00am" for d in DAYS}
CLOSE = {d: "10:00pm" for d in DAYS}


def _row(d, emp, start, end, role="Server", **kw):
    r = {"date": d, "day": sq._day_name(d), "employee": emp, "role": role, "shift_start": start,
         "shift_end": end, "scheduled_hours": "", "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    r.update(kw)
    return r


def _c(names, roles, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names}, week_start_day=2)
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {n.lower(): {roles[n].lower()} for n in names}
    c.open_times, c.close_times = dict(OPEN), dict(CLOSE)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _req(dates, role, curve, part="night"):
    return [{"date": d, "daypart": part, "factor": 1.0,
             "roles": [{"role": role, "required": max(curve.values()), "floor": 0}],
             "half_need": {t: {role: n} for t, n in curve.items()}} for d in dates]


def _sig(names, roles):
    return {"roster": list(names), "roster_roles": dict(roles),
            "overtime": {"rates": {"server": 10.0, "manager": 20.0}, "default_rate": 12.0, "line": 40.0}}


# ── ARCH-23: the manager plan is solved, not pinned ──────────────────────

def test_the_plans_own_choice_is_a_slot_the_solve_staffs_and_the_owners_word_stays():
    names = ["Mia", "Ned", "Ann"]
    roles = {"Mia": "Manager", "Ned": "Manager", "Ann": "Server"}
    c = _c(names, roles, managers={"mia": "Manager", "ned": "Manager"})
    plan = {"rows": [], "dates": list(WEEK), "uncovered": [], "windows": {}}
    for d in WEEK[:6]:
        plan["rows"].append(dict(_row(d, "Mia", "10:30am", "10:00pm", "Manager"), _pinned=sk.PLAN_SOURCE,
                                 _plan_source="usual", _pin_reason="Mia usually works",
                                 notes=f"{sk.PLAN_NOTE} — Mia usually works"))
    plan["rows"].append(dict(_row(WEEK[6], "Mia", "10:30am", "10:00pm", "Manager"), _pinned=sk.PLAN_SOURCE,
                             _plan_source="standing", _pin_reason="Mia's standing Tuesday shift",
                             notes=f"{sk.PLAN_NOTE} — Mia's standing Tuesday shift"))
    locked, flex, extra = se.manager_slots(plan, WEEK, c)
    assert [r["date"] for r in locked] == [WEEK[6]] and len(flex) == 6 and not extra
    reqs = _req(WEEK, "Server", {t: 1 for t in range(1020, 1320, 30)})
    got = cs.solver_draft(WEEK, c, reqs, context_rows=locked, signals=_sig(names, roles),
                          roster_roles=roles, max_seconds=6, manager_slots=flex + extra)
    solved = [r for r in got["rows"] if r.get("_mgr_plan")]
    assert len(solved) == 6, "every planned shift is staffed by somebody who manages"
    # Mia planned 7 x 11.5h = 80.5h: the solve hands days to Ned, who had none
    # — the trade the pinned plan could never make (overtime against a
    # teammate with room)
    assert any(r["employee"] == "Ned" for r in solved)
    acct = se.solved_manager_plan(plan, WEEK, locked, solved, flex)
    assert acct["changed"] >= 1 and acct["slots"] == 6
    tue = [r for r in plan["rows"] if r["date"] == WEEK[6]]
    assert [(r["employee"], r["shift_start"], r["_plan_source"]) for r in tue] == [("Mia", "10:30am", "standing")]
    assert all(r["_pinned"] == sk.PLAN_SOURCE and sk.is_plan_note(r["notes"]) for r in plan["rows"])
    week = [r for r in plan["rows"]] + [r for r in got["rows"] if not r.get("_mgr_plan")]
    assert not sr.manager_gaps(week, c)


def test_erik_stays_the_only_tuesday_manager_and_comes_in_an_hour_before_open():
    """Simple EJ's (owner non-negotiable): on Tuesdays Erik is the only
    manager and arrives about an hour before the open. With the manager plan
    solved, not pinned, Tuesday is still his, from 10:00am."""
    names = ["Erik", "Gabriel", "Jim", "Ann", "Bo"]
    roles = {"Erik": "Owner", "Gabriel": "Manager", "Jim": "Manager", "Ann": "Server", "Bo": "Server"}
    hist = []
    for w in range(1, 5):
        for i, d in enumerate(WEEK):
            dd = (date.fromisoformat(d) - timedelta(weeks=w)).isoformat()
            if DAYS[i] == "Tuesday":
                hist.append(_row(dd, "Erik", "10:00am", "10:00pm", "Owner"))
            else:
                hist.append(_row(dd, "Gabriel" if i % 2 else "Jim", "10:00am", "4:30pm", "Manager"))
                hist.append(_row(dd, "Jim" if i % 2 else "Gabriel", "4:00pm", "10:00pm", "Manager"))
            hist.append(_row(dd, "Ann", "11:00am", "4:00pm"))
            hist.append(_row(dd, "Bo", "4:00pm", "10:00pm"))
    c = _c(names, roles, managers={"erik": "Owner", "gabriel": "Manager", "jim": "Manager"},
           unavailable_days={"gabriel": {"Tuesday"}, "jim": {"Tuesday"}}, salaried={"erik"})
    plan = sk.plan_manager_coverage(c, WEEK, history=hist)
    locked, flex, extra = se.manager_slots(plan, WEEK, c, history=hist)
    reqs = _req(WEEK, "Server", {t: 1 for t in range(660, 1320, 30)})
    got = cs.solver_draft(WEEK, c, reqs, history_rows=hist, context_rows=locked, signals=_sig(names, roles),
                          roster_roles=roles, max_seconds=8, manager_slots=flex + extra)
    solved = [r for r in got["rows"] if r.get("_mgr_plan")]
    se.solved_manager_plan(plan, WEEK, locked, solved, flex)
    tue = [r for r in plan["rows"] if r["date"] == WEEK[6]]
    assert {r["employee"] for r in tue} == {"Erik"}
    assert min(sr._span(r)[0] for r in tue) == 10 * 60, "an hour before the 11am open"
    week = list(plan["rows"]) + [r for r in got["rows"] if not r.get("_mgr_plan")]
    assert not sr.manager_gaps(week, c)


def test_a_solved_managers_stay_after_close_is_the_owners_allowance_as_a_kept_ones_is():
    """The solve's live manager unit earns the closing manager's stay (the
    owner's allowance, LOGIC-12) exactly as a pinned one: a 10am-9pm solved
    close no longer leaves every 9:30pm crew shift empty."""
    names = ["Jo", "Ann"]
    roles = {"Jo": "Manager", "Ann": "Server"}
    c = _c(names, roles, managers={"jo": "Manager"})
    mgr = dict(_row(WEEK[0], "Jo", "10:00am", "9:30pm", "Manager"), _pinned=sk.PLAN_SOURCE, _plan_source="usual")
    slot = _row(WEEK[0], "", "4:00pm", "10:00pm", "Server")
    res = cs.assign([dict(mgr, _pinned=None, employee="", _open=True, _mgr_plan=True, _s1="Jo"), slot], c,
                    signals=_sig(names, roles), max_seconds=3, only_dates=[WEEK[0]])
    assert {r["employee"] for r in res["rows"] if r.get("employee")} == {"Jo", "Ann"}


# ── LOGIC-8: the top-up runs to the requirements ─────────────────────────

def test_the_top_up_has_no_quarter_of_the_target_cap():
    import inspect
    src = inspect.getsource(se._top_up_hours_gap)
    assert "0.25 * float(daily_target_hours" not in src and "cap_day" not in src


def test_the_top_up_adds_only_where_the_objective_says_the_half_hours_are_worth_it():
    names = ["Ann", "Bo", "Cy"]
    roles = {n: "Server" for n in names}
    c = _c(names, roles, roster_roles=dict(roles))
    reqs = _req([WEEK[2]], "Server", {t: (3 if 1080 <= t < 1260 else 1) for t in range(1020, 1320, 30)})
    rows = [_row(WEEK[2], "Ann", "5:00pm", "10:00pm")]
    ctx = so.ObjectiveContext.build(None, WEEK, requirements=reqs, rates={"server": 10.0}, constraints=c)
    out, hours, _d = se._top_up_hours_gap([dict(r) for r in rows], {WEEK[2]: 20.0}, 100.0, 5.0, None,
                                          dict(CLOSE), {}, constraints=c, requirements=reqs, objective_ctx=ctx)
    assert hours > 0 and len(out) == 3, "the Friday rush is worth more than two more servers' wages"
    poor = so.ObjectiveContext.build(None, WEEK, requirements=reqs, rates={"server": 200.0}, constraints=c)
    out2, hours2, _d2 = se._top_up_hours_gap([dict(r) for r in rows], {WEEK[2]: 20.0}, 100.0, 5.0, None,
                                             dict(CLOSE), {}, constraints=c, requirements=reqs, objective_ctx=poor)
    assert hours2 == 0 and len(out2) == 1, "at $200 an hour the half hours are worth less than the shift"


# ── LOGIC-23: a saving is credited; the optimizer tightens a week under budget ──

def test_the_optimizer_takes_out_hours_above_the_need_under_the_budget():
    names = ["Ann", "Bo", "Cy", "Dee"]
    roles = {n: "Server" for n in names}
    c = _c(names, roles)
    reqs = _req([WEEK[2]], "Server", {t: 2 for t in range(1020, 1320, 30)})
    rows = [_row(WEEK[2], n, "5:00pm", "10:00pm") for n in names] + [_row(WEEK[3], n, "5:00pm", "10:00pm")
                                                                      for n in names]
    ctx = so.ObjectiveContext.build(None, WEEK, requirements=reqs, rates={"server": 10.0}, budget=10000,
                                    constraints=c)
    sig = dict(_sig(names, roles), objective_need=cs.need_text(ctx.need), half_hours_by_date=se._half_hours_signal(reqs),
               typical_headcount={("Friday", "night"): {"Server": 2}, ("Saturday", "night"): {"Server": 2}})
    res = sopt.optimize(rows, {"constraints": c}, signals=sig, constraints=c, objective_ctx=ctx, max_seconds=10)
    fri = [r for r in res["rows"] if r["date"] == WEEK[2]]
    assert len(fri) < 4, "two servers the half hours do not need, under the budget, are tightened"
    assert all(sum(1 for r in fri if sr._span(r)[0] <= t < sr._span(r)[1]) >= 2 for t in range(1020, 1320, 30))


# ── the trim reads the objective ──────────────────────────────────────────

def test_the_trim_cuts_the_quiet_half_hour_not_the_busy_one():
    names = [f"S{i}" for i in range(6)]
    roles = {n: "Server" for n in names}
    c = _c(names, roles)
    curve_quiet = {t: 3 for t in range(1020, 1320, 30)}
    reqs = _req([WEEK[0]], "Server", curve_quiet) + _req([WEEK[2]], "Server", {t: 3 for t in range(1020, 1320, 30)})
    reqs[1]["factor"] = 4.0
    rows = [_row(d, n, "5:00pm", "10:00pm") for d in (WEEK[0], WEEK[2]) for n in names[:3]]
    ctx = so.ObjectiveContext.build(None, WEEK, requirements=reqs, rates={"server": 15.0}, budget=350,
                                    demand_curve={"Wednesday": {17: 1, 18: 1, 19: 1, 20: 1, 21: 1},
                                                  "Friday": {17: 6, 18: 6, 19: 6, 20: 6, 21: 6}},
                                    constraints=c)
    out, trimmed, _h = econ.trim_to_budget([dict(r) for r in rows], 20, {}, constraints=c,
                                           requirements=reqs, objective_ctx=ctx)
    assert trimmed and all(t["date"] == WEEK[0] for t in trimmed), trimmed


# ── SP's finding 4: the prep crew before the open ────────────────────────

def test_stage_one_reads_the_prep_crew_on_before_the_open_from_history():
    hist = []
    for w in range(1, 4):
        for i, d in enumerate(WEEK):
            dd = (date.fromisoformat(d) - timedelta(weeks=w)).isoformat()
            for n in ("P1", "P2", "P3", "P4"):
                hist.append(_row(dd, n, "7:00am", "3:00pm", "Prep Cook"))
    c = _c(["P1"], {"P1": "Prep Cook"})
    got = shp.history_shoulders(hist, c)
    assert got[("prep cook", DAYS[0])][7 * 60] == 4 and min(got[("prep cook", DAYS[0])]) == 7 * 60
