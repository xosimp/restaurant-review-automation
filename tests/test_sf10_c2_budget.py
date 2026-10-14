"""Scheduler fix round 10/10/26, workstream C2 — ARCH-26: the repair loop's
output depends on its inputs, never on the wall clock's headroom.

Each search runs on a deterministic WORK budget with a fixed seed (CP-SAT's
deterministic time, the backtracking solver's WorkClock, the optimizer's
evaluations); the wall clock is only an emergency cap, and when it fires the
week says so. The stage order's deliberate effects are pinned here too."""
import random
import time

import pytest

import schedule_cpsat as cps
import schedule_engine as se
import schedule_optimizer as so
import schedule_rules as sr
import schedule_solver as ss
import shift_quality as sq

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
DAYS = [sq._day_name(d) for d in WEEK]


class _Clock:
    """A job clock with `left` seconds before its deadline."""

    def __init__(self, left):
        self.left = left

    def seconds_left(self):
        return self.left

    def stopped(self):
        return False

    def stop_requested(self):
        return False


def _x(left=None, spent=None):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=["Ann"],
                       active={"ann"})
    x = se.RepairContext(1, {"week_dates": WEEK, "week_days": DAYS, "roster": ["Ann"]}, c,
                         clock=_Clock(left) if left is not None else None)
    if spent:
        x.search_spent.update(spent)
    return x


def _fake_improve(seen):
    def improve(rows, inputs=None, **kw):
        seen.append(kw)
        return {"applied": False, "rows": rows, "stats": {"engine": "cp-sat", "status": "optimal", "kept": "draft"},
                "before_score": None, "after_score": None, "changes": [], "reason": ""}
    return improve


@pytest.mark.parametrize("left", [10_000.0, se.REPAIR_RESERVE_SECONDS + 6.0])
def test_the_solver_gets_the_same_work_budget_whatever_the_clock_has_left(monkeypatch, left):
    seen = []
    monkeypatch.setattr(cps, "improve", _fake_improve(seen))
    monkeypatch.setattr(se, "assign_engine", lambda: "cpsat")
    se._stage_solver([{"date": WEEK[0], "employee": "Ann"}], _x(left))
    (kw,) = seen
    # The budget is the same on an idle box and a busy one (it used to be
    # min(budget, seconds left)); what the clock has left is the cap only —
    # and never less than the searches' reserved time, whatever the loop's
    # other stages took (blind re-audit 10/11/26 SCALE-15).
    assert kw["max_seconds"] == cps.DEFAULT_SECONDS
    assert kw["wall_cap"] == pytest.approx(max(se.REPAIR_SEARCH_RESERVE_SECONDS, left - se.REPAIR_RESERVE_SECONDS),
                                           abs=1.0)


def test_the_backtracking_solver_gets_its_work_budget_too(monkeypatch):
    seen = []
    monkeypatch.setattr(ss, "improve", _fake_improve(seen))
    monkeypatch.setattr(se, "assign_engine", lambda: "backtracking")
    se._stage_solver([{"date": WEEK[0], "employee": "Ann"}], _x(se.REPAIR_RESERVE_SECONDS + 6.0))
    assert seen[0]["max_seconds"] == ss.DEFAULT_SECONDS
    assert seen[0]["wall_cap"] == pytest.approx(se.REPAIR_SEARCH_RESERVE_SECONDS)


def _fake_optimize(seen, evaluations=None, stopped="no improving move"):
    def optimize(rows, inputs=None, **kw):
        seen.append(kw)
        used = kw["max_evaluations"] if evaluations is None else evaluations
        return {"rows": rows, "changes": [], "evaluations": used, "stopped": stopped, "before_score": 50,
                "after_score": 50, "seconds": 0.1, "quality": {"checked": True}}
    return optimize


def test_the_optimizer_budget_is_evaluations_not_the_seconds_it_already_spent(monkeypatch):
    seen = []
    monkeypatch.setattr(so, "optimize", _fake_optimize(seen, evaluations=600))
    monkeypatch.setattr(so, "summary", lambda res, sig: {})
    # 24.9s already spent across earlier cycles: the old wall budget (25s)
    # skipped it — on a busy box a different week. Evaluations decide now.
    x = _x(spent={"optimizer": 24.9})
    se._stage_optimizer([{"date": WEEK[0], "employee": "Ann"}], x)
    se._stage_optimizer([{"date": WEEK[0], "employee": "Ann"}], x)
    assert [k["max_evaluations"] for k in seen] == [se.REPAIR_OPTIMIZER_EVALUATIONS,
                                                    se.REPAIR_OPTIMIZER_EVALUATIONS - 600]
    assert x.wall_capped == []
    x.search_spent["optimizer_evaluations"] = se.REPAIR_OPTIMIZER_EVALUATIONS
    out = se._stage_optimizer([{"date": WEEK[0], "employee": "Ann"}], x)
    assert len(seen) == 2 and "report" not in out                 # its budget is spent: nothing more


def test_a_search_the_emergency_cap_cut_short_is_reported_on_the_week(monkeypatch):
    seen = []
    monkeypatch.setattr(so, "optimize", _fake_optimize(seen, evaluations=40, stopped="budget spent"))
    monkeypatch.setattr(so, "summary", lambda res, sig: {})
    x = _x()
    se._stage_optimizer([{"date": WEEK[0], "employee": "Ann"}], x)
    assert x.wall_capped == ["optimizer"]
    line = se.repair_emergency_line({"emergency": se._emergency_of({"cycles": 2}, x)})
    assert "the Shift Quality optimizer was cut short" in line and "generate again" in line
    loop = se.repair_emergency_line({"emergency": {"loop": True, "cycles": 2}})
    assert f"stopped after 2 of its {se.REPAIR_MAX_CYCLES} cycles" in loop
    assert se.repair_emergency_line({}) == "" and se.repair_emergency_line({"emergency": {}}) == ""


def test_the_loop_stopped_by_the_emergency_cap_says_so(monkeypatch):
    calls = []

    def changes(rows, x):
        calls.append(1)
        return {"rows": [dict(r, notes=f"n{len(calls)}") for r in rows]}
    table = {k: (lambda rows, x: {"rows": rows}) for k in se._STAGE_FNS}
    table["role_times"] = changes
    monkeypatch.setattr(se, "_STAGE_FNS", table)
    x = _x()
    x.max_seconds = -1.0                       # past its emergency cap after the first cycle
    rows = [{"date": WEEK[0], "day": DAYS[0], "employee": "Ann", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    loop = se.repair_week(rows, x)
    assert loop["cycles"] == 1 and loop["emergency"] == {"loop": True, "cycles": 1}
    ordinary = se.repair_week(rows, _x())
    assert ordinary["emergency"] == {}


def _big_week():
    rng = random.Random(5)
    names = [f"P{i}" for i in range(24)]
    roles = {n: ("Server" if i < 12 else "Cook") for i, n in enumerate(names)}
    rows = []
    for d in WEEK:
        for role, k in (("Server", 6), ("Cook", 5)):
            pool = [n for n in names if roles[n] == role]
            for j in range(k):
                start, end = ("10:30am", "3:00pm") if j % 2 else ("4:30pm", "10:30pm")
                rows.append({"date": d, "day": sq._day_name(d), "employee": pool[(j + WEEK.index(d)) % len(pool)],
                             "role": role, "shift_start": start, "shift_end": end,
                             "scheduled_hours": sr.hours_text(sr.span_hours({"date": d, "shift_start": start,
                                                                             "shift_end": end})), "notes": ""})
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    sig = {"roster": names, "roster_roles": roles, "scores": {n: rng.randint(1, 5) for n in names}}
    return rows, c, sig


def test_the_backtracking_solver_finds_the_same_week_on_a_busy_box(monkeypatch):
    """The same draft, the same week — whether the box is idle or the wall
    clock runs ten times faster than the work (two generations sharing the
    GIL). On the wall clock the busy run stopped ten times sooner."""
    rows, c, sig = _big_week()
    idle = ss.solve(rows, c, signals=sig, max_seconds=0.3, wall_cap=600)
    real = time.monotonic
    t0 = real()
    monkeypatch.setattr(ss._time, "monotonic", lambda: t0 + (real() - t0) * 10)
    busy = ss.solve(rows, c, signals=sig, max_seconds=0.3, wall_cap=600)
    assert busy["wall_capped"] is False and busy["deterministic"] is True
    assert busy["nodes"] == idle["nodes"] and busy["work"] == idle["work"]
    assert [r.get("employee") for r in busy["rows"]] == [r.get("employee") for r in idle["rows"]]


def test_on_the_wall_clock_it_would_have_differed(monkeypatch):
    """The control: the same search with the budget on the wall clock
    (SCHEDULE_SOLVER_DETERMINISTIC=0) does less work when the clock runs
    fast — the ARCH-26 failure this guards."""
    rows, c, sig = _big_week()
    idle = ss.solve(rows, c, signals=sig, max_seconds=0.3, wall_cap=600, deterministic=False)
    real = time.monotonic
    t0 = real()
    monkeypatch.setattr(ss._time, "monotonic", lambda: t0 + (real() - t0) * 10)
    busy = ss.solve(rows, c, signals=sig, max_seconds=0.3, wall_cap=600, deterministic=False)
    assert busy["nodes"] < idle["nodes"]


def test_the_optimizer_on_its_evaluations_is_the_same_on_a_busy_box(monkeypatch):
    rows = [{"date": WEEK[5], "day": "Saturday", "employee": "Ann", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""},
            {"date": WEEK[4], "day": "Friday", "employee": "Bob", "role": "Server", "shift_start": "4:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}]
    sig = {"roster": ["Ann", "Bob", "Cat", "Dee"], "typical_headcount": {("Saturday", "night"): {"Server": 3}},
           "availability": {}, "constraints": {}, "scores": {}}
    idle = so.optimize([dict(r) for r in rows], {}, signals=dict(sig), max_seconds=600, max_evaluations=40)
    real = so._time.monotonic
    t0 = real()
    monkeypatch.setattr(so._time, "monotonic", lambda: t0 + (real() - t0) * 10)
    busy = so.optimize([dict(r) for r in rows], {}, signals=dict(sig), max_seconds=600, max_evaluations=40)
    assert busy["evaluations"] == idle["evaluations"]
    assert [(r["employee"], r["date"], r["shift_start"]) for r in busy["rows"]] == \
        [(r["employee"], r["date"], r["shift_start"]) for r in idle["rows"]]


def test_cpsat_is_given_its_whole_deterministic_budget_and_the_wall_cap_apart():
    s = cps._solver_for(20.0, wall_cap=7.0, deterministic=True)
    assert s.parameters.max_deterministic_time == 20.0 and s.parameters.max_time_in_seconds == 7.0
    s = cps._solver_for(20.0, deterministic=True)
    assert s.parameters.max_time_in_seconds == 80.0
    import inspect
    src = inspect.getsource(cps.assign)
    # The budget is never what the model's build left of it on the wall clock.
    assert "max_seconds - (_time.monotonic() - t0)" not in src


# ── the stage order's deliberate effects ──────────────────────────────────

def test_the_stage_order_is_pinned_with_its_reasons():
    order = [k for k, *_r in se.REPAIR_STAGES]
    # Legality, then the manager every minute, then coverage, the policy
    # limits, overtime, minimum hours, the requirements, the budget, quality.
    tiers = [se._STAGE_TIER[k] for k in order]
    assert tiers == sorted(tiers), "a stage runs before one ranked above it"
    # The owner's start/end-time rules run after the searches: a search's
    # choice is re-timed to the owner's rule, never the other way round.
    assert order.index("solver") < order.index("optimizer") < order.index("role_times")
    # The requirements outrank the budget: the top-up fills before the trim,
    # which never cuts under them; the budget is reconciled after the loop.
    assert order.index("top_up") < order.index("budget")
    assert "reconcile" in __import__("inspect").getsource(se.repair_week)
