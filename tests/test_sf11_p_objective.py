"""Schedule fix round 10/11/26, workstream P — ONE objective (blind re-audit
10/11/26 LOGIC-6, ARCH-2, LOGIC-4, LOGIC-8, LOGIC-23): the solver, the trim,
the top-up, the optimizer, the quality gate and Shift Quality's cost read
schedule_objective; priced one change at a time it reads what a whole week
priced at once reads; and Shift Quality and the objective agree on random
legal moves from a solver-drafted week. No model is called."""
import random

import pytest

import schedule_cpsat as cs
import schedule_economics as econ
import schedule_engine as se
import schedule_objective as so
import schedule_optimizer as sopt
import schedule_rules as sr
import shift_quality as sq

pytestmark = pytest.mark.skipif(not cs.available(), reason="OR-Tools not installed")

WEEK = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"]
DAYS = [sq._day_name(d) for d in WEEK]
OPEN, CLOSE = "11:00am", "10:00pm"
RATES = {"server": 10.0, "line cook": 18.0}


def row(date, name, role="Server", start="5:00pm", end="10:00pm", **kw):
    r = {"date": date, "day": sq._day_name(date), "employee": name, "role": role, "shift_start": start,
         "shift_end": end, "scheduled_hours": "", "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    r.update(kw)
    return r


def _fmt(m):
    m = int(m) % 1440
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


# ── a random restaurant, drafted by the solver ───────────────────────────

def world(seed, dates=None):
    """A small restaurant with three weeks of history, demand curves that
    differ by seed, and the week drafted by the solver-first draft; with
    the one objective's context built as the pipeline builds it (the
    demand Stage 1 shaped to as its need) and Shift Quality's signals."""
    rng = random.Random(seed)
    dates = dates or [WEEK[0], WEEK[2], WEEK[3]]
    servers = [f"Srv{i}" for i in range(7)]
    cooks = [f"Cook{i}" for i in range(4)]
    names = servers + cooks
    roles = {n: ("Server" if n in servers else "Line Cook") for n in names}
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names}, week_start_day=2)
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {n.lower(): {roles[n].lower()} for n in names}
    c.open_times = {d: OPEN for d in DAYS}
    c.close_times = {d: CLOSE for d in DAYS}
    reqs = []
    for d in dates:
        busy = rng.choice([1, 2, 3])
        for part, lo, hi in (("morning", 660, 900), ("night", 900, 1320)):
            curve_s = {t: max(1, busy + (1 if 1050 <= t < 1230 else 0) - (1 if part == "morning" else 0))
                       for t in range(lo, hi, 30)}
            curve_c = {t: 1 + (1 if busy >= 2 and 1080 <= t < 1230 else 0) for t in range(lo, hi, 30)}
            reqs.append({"date": d, "daypart": part, "factor": 1.0,
                         "roles": [{"role": "Server", "required": max(curve_s.values()), "floor": 0},
                                   {"role": "Line Cook", "required": max(curve_c.values()), "floor": 0}],
                         "half_need": {t: {"Server": curve_s[t], "Line Cook": curve_c[t]} for t in curve_s}})
    hist = []
    for wk in (1, 2, 3):
        for i, d in enumerate(WEEK):
            from datetime import date as _d, timedelta as _td
            past = (_d.fromisoformat(d) - _td(days=7 * wk)).isoformat()
            for j, n in enumerate(names):
                if (i + j + seed) % 3 == 0:
                    continue
                s, e = ("10:30am", "4:00pm") if (i + j) % 2 else ("4:00pm", "10:00pm")
                hist.append(row(past, n, roles[n], s, e))
    sig_ot = {"rates": dict(RATES), "default_rate": 12.0, "line": 40.0}
    signals = {"roster": names, "roster_roles": roles, "overtime": sig_ot}
    budget = rng.choice([None, 1500.0, 2500.0])
    got = cs.solver_draft(dates, c, reqs, history_rows=hist, signals=signals, roster_roles=roles,
                          budget_dollars=budget, max_seconds=6)
    need = cs.need_from_text(got.get("need"))
    ctx = so.ObjectiveContext.build(None, dates, budget=budget, requirements=reqs, history_rows=hist,
                                    rates=RATES, blended_rate=12.0, ot_line=40.0, constraints=c)
    if need:
        ctx.need = need
    # the day targets are the budget's hours, as labor.week_hours_plan makes
    # them (none without a budget)
    targets = {d: round(budget / 12.0 / len(dates), 1) for d in dates} if budget else {}
    sq_signals = {"daily_target_hours": targets, "objective_need": cs.need_text(ctx.need),
                  "half_hours_by_date": se._half_hours_signal(reqs), "open_times": c.open_times,
                  "close_times": c.close_times,
                  "typical_headcount": {(sq._day_name(r["date"]), r["daypart"]): {e["role"]: e["required"]
                                                                                  for e in r["roles"]}
                                        for r in reqs}}
    return {"rows": got["rows"], "ctx": ctx, "signals": sq_signals, "c": c, "names": names, "roles": roles,
            "dates": dates, "reqs": reqs, "hist": hist}


def sq_raw(rows, signals) -> float:
    return sopt.objective(sq.score_rows([dict(r) for r in rows], **signals))


def legal(before, after, c) -> bool:
    """A move the repair loop could make: nothing new or worse up to the
    budget's rank (schedule_rules.regressions, by breach identity)."""
    return not sr.regressions(sr.breach_profile(before, c), sr.breach_profile(after, c), upto=sr.TIER_BUDGET,
                              hard_only=False)


def random_move(rng, rows, w):
    """(kind, rows after) — one random single move of the kinds the repair
    loop makes: remove, end an hour earlier, start an hour later, run an
    hour longer, add an idle person on a shape their role works that day,
    hand a shift to an idle teammate."""
    if not rows:
        return None
    kind = rng.choice(["remove", "shorten", "late", "extend", "add", "replace"])
    i = rng.randrange(len(rows))
    r = rows[i]
    sp = sr._span(r)
    out = [dict(x) for x in rows]
    if kind == "remove":
        out.pop(i)
    elif kind in ("shorten", "late", "extend"):
        s, e = sp
        if kind == "shorten":
            e -= 60
        elif kind == "late":
            s += 60
        else:
            e += 60
        if e - s < 180 or e > 1320 or s < 600:
            return None
        out[i] = row(r["date"], r["employee"], r["role"], _fmt(s), _fmt(e))
    else:
        on_day = {x["employee"] for x in rows if x["date"] == r["date"]}
        idle = [n for n in w["names"] if w["roles"][n] == r["role"] and n not in on_day]
        if not idle:
            return None
        who = rng.choice(idle)
        new = row(r["date"], who, r["role"], r["shift_start"], r["shift_end"])
        if kind == "add":
            out.append(new)
        else:
            out[i] = new
    return kind, out


# ── one implementation, priced one change at a time ──────────────────────

@pytest.mark.parametrize("seed", [1, 2, 3])
def test_the_week_pricer_reads_exactly_what_price_week_reads(seed):
    w = world(seed)
    rows, ctx = [dict(r) for r in w["rows"]], w["ctx"]
    p = so.WeekPricer(rows, ctx)
    assert p.total() == so.price_week(rows, ctx)["total"]
    rng = random.Random(seed)
    for _ in range(25):
        mv = random_move(rng, rows, w)
        if mv is None:
            continue
        _kind, after = mv
        # the change as one row out and one in (or either alone)
        gone = [r for r in rows if r not in after]
        came = [r for r in after if r not in rows]
        old = next((x for x in rows if gone and x is not None and x == gone[0]), None)
        new = came[0] if came else None
        expect = so.price_week(after, ctx)["total"]
        assert p.try_change(old, new) == pytest.approx(expect, abs=0.05)
        assert p.total() == pytest.approx(so.price_week(rows, ctx)["total"], abs=0.05)   # untouched


def test_a_person_under_half_their_usual_week_costs_more_than_any_budget_saving():
    w = world(1)
    ctx = w["ctx"]
    k = "srv0"
    uh = ctx.usual_hours(k)
    assert uh >= so.STARVED_MIN_USUAL_HOURS
    assert so.starved_hour_usd(ctx, "Srv0") > (1 + so.BUDGET_OVER_FACTOR) * max(RATES.values())


# ── Shift Quality and the objective agree ────────────────────────────────

@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_shift_quality_and_the_objective_agree_on_random_legal_moves(seed):
    """LOGIC-6: one cost model. On random legal single moves from the
    solver's week (seeded), a move the one objective prefers by more than a
    dollar never makes Shift Quality worse by more than half a point — the
    score never calls clearly worse what every pass is told is better — and
    a move Shift Quality prefers by more than half a point never costs the
    objective more than the value it adds, at the one exchange rate the
    passes judge by (week_value: no pass is asked to trade the two against
    each other at another rate)."""
    w = world(seed)
    week, ctx, sig, c = w["rows"], w["ctx"], w["signals"], w["c"]
    base_obj = so.price_week(week, ctx)["total"]
    base_sq = sq_raw(week, sig)
    per_dollar = so.points_per_dollar(so.price_week(week, ctx)["parts"]["labor"])
    rng = random.Random(seed * 7919)
    checked, worse = 0, []
    for _ in range(80):
        mv = random_move(rng, week, w)
        if mv is None:
            continue
        kind, after = mv
        if not legal(week, after, c):
            continue
        checked += 1
        d_obj = so.price_week(after, ctx)["total"] - base_obj
        d_sq = sq_raw(after, sig) - base_sq
        if d_obj < -1.0 and d_sq < -0.5:
            worse.append((kind, round(d_obj, 2), round(d_sq, 3), "objective better, score worse"))
        if d_sq > 0.5 and d_sq - per_dollar * d_obj > 0:
            worse.append((kind, round(d_obj, 2), round(d_sq, 3), "a move the passes would take but the solver did not"))
    assert checked >= 10
    assert not worse, worse
