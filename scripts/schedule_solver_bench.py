"""
scripts/schedule_solver_bench.py — the solver-first draft against the code
draft it replaces, on synthetic restaurants (scheduler audit 10/10/26 RM-21,
LOGIC-24): solve time, hard breaches, Shift Quality, labor dollars,
overtime, unmanaged minutes and unfilled demand.

    python scripts/schedule_solver_bench.py --sizes 60 150 250 --seeds 3

Arms (each followed by the same ranked repair loop's hard-rule stages —
schedule_engine.repair_rows — and judged by the same oracle,
schedule_rules.violations, and the same score, shift_quality.score_rows):
  solver     schedule_cpsat.solver_draft: Stage 1 shapes (schedule_shapes)
             then Stage 2 CP-SAT assignment
  code_v0    schedule_fallback.solver_rows: SHIFT REQUIREMENTS laid onto the
             usual shift templates, people by the backtracking solver — the
             code draft the pipeline had (the model's draft cannot be run
             offline; its stored answers are E's eval harness, scripts/
             schedule_model_eval.py --arms, which needs a frozen set)
  carry      last published week carried forward (schedule_fallback)

Nothing here reads the database, calls a model or writes anything. The
synthetic restaurant (synthetic()) is also what tests/test_sf10_f_*.py use.
"""
import argparse
import json
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _throwaway_database():
    """The repair stages read a few tables (staff availability, notes): a
    throwaway database with the boot schema, never anybody's reviews.db —
    pointed at before models is imported, as tests/conftest.py does."""
    if os.environ.get("SCHEDULE_BENCH_KEEP_DB") or "models" in sys.modules:
        return
    import tempfile
    vol = tempfile.mkdtemp(prefix="cavnar-bench-volume-")
    open(os.path.join(vol, "reviews.db"), "a").close()
    os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = vol
    import models
    models.init_db()
    models.init_staff_notes()
    models.init_staff_availability()
    models.ensure_columns()


if __name__ == "__main__":
    _throwaway_database()

import schedule_rules as sr  # noqa: E402
import schedule_time as _stime  # noqa: E402
import shift_quality as sq  # noqa: E402

WEEK = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"]
LAST = ["2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12", "2026-10-13"]
ROLE_MIX = (("Server", 0.36), ("Line Cook", 0.22), ("Bartender", 0.1), ("Host", 0.1), ("Dishwasher", 0.1),
            ("Busser", 0.07))
RATES = {"server": 9.0, "line cook": 18.0, "bartender": 10.0, "host": 13.0, "dishwasher": 15.0, "busser": 9.0,
         "manager": 24.0}
# Per role: people on at once at each half hour, per 100 staff, on a normal
# day, as (from, to, people) runs — lunch and dinner, cooks leading and dish
# lagging.
_SHAPE = {
    "server": ((11 * 60, 14 * 60, 5), (14 * 60, 16 * 60 + 30, 2), (17 * 60, 20 * 60 + 30, 9),
               (20 * 60 + 30, 22 * 60, 4)),
    "line cook": ((10 * 60, 14 * 60, 4), (14 * 60, 16 * 60, 2), (16 * 60, 21 * 60, 6), (21 * 60, 22 * 60, 2)),
    "bartender": ((11 * 60 + 30, 15 * 60, 1), (16 * 60, 23 * 60, 3)),
    "host": ((11 * 60, 14 * 60, 1), (17 * 60, 21 * 60, 2)),
    "dishwasher": ((11 * 60, 15 * 60, 1), (17 * 60, 23 * 60, 3)),
    "busser": ((17 * 60, 21 * 60, 2),),
}
_DAY_LIFT = {"Monday": 0.7, "Tuesday": 0.75, "Wednesday": 0.85, "Thursday": 0.95, "Friday": 1.3, "Saturday": 1.4,
             "Sunday": 1.0}


def _fmt(m):
    m = int(m) % 1440
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def _row(d, name, role, s, e, **kw):
    r = {"date": d, "day": sq._day_name(d), "employee": name, "role": role, "shift_start": _fmt(s),
         "shift_end": _fmt(e), "scheduled_hours": "", "notes": ""}
    r["scheduled_hours"] = _stime.row_hours_text(r)
    r.update(kw)
    return r


def synthetic(size=60, seed=1):
    """A restaurant of `size` people: roster, Constraints, the concurrent
    baseline, the week's requirements, the managers' plan (pinned rows), a
    published last week and the quality signals. Deterministic in `seed`."""
    rng = random.Random(seed * 7919 + size)
    n_mgr = max(3, size // 25)
    names, roles = [], {}
    for i in range(n_mgr):
        n = f"Manager {i + 1}"
        names.append(n)
        roles[n] = "Manager"
    k = 0
    for role, share in ROLE_MIX:
        for _i in range(max(2, int(round((size - n_mgr) * share)))):
            k += 1
            n = f"{role.split()[0]} {k}"
            names.append(n)
            roles[n] = role
    c = sr.Constraints(restaurant_id=900 + size, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=list(names), active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    c.managers = {n.lower(): "Manager" for n in names if roles[n] == "Manager"}
    c.known_roles = {n.lower(): {roles[n].lower()} for n in names}
    c.role_names = {roles[n].lower(): roles[n] for n in names}
    c.open_times = {sq._day_name(d): "11:00am" for d in WEEK}
    c.close_times = {sq._day_name(d): ("11:00pm" if sq._day_name(d) in ("Friday", "Saturday") else "10:00pm")
                     for d in WEEK}
    c.section_cap = max(4, int(round(size * 0.09 * 1.4)))
    c.foh_roles = {"server"}
    c.closers_by_role = {}
    for role in ("server", "line cook", "bartender", "dishwasher"):
        people = [n.lower() for n in names if roles[n].lower() == role]
        c.closers_by_role[sq.role_family(role)] = set(people[: max(1, len(people) // 3)])
    c.role_floors = {"Server": {"night": max(2, size // 30)}, "Line Cook": {"night": max(1, size // 50)}}
    # availability, time off, own maxima, minimums, minors
    for n in names:
        key = n.lower()
        if roles[n] == "Manager":
            continue                       # the plan below is theirs
        x = rng.random()
        if x < 0.25:
            c.unavailable_days[key] = {rng.choice(list(_DAY_LIFT))}
        elif x < 0.35:
            c.daypart_avail[key] = {rng.choice(list(_DAY_LIFT)): rng.choice(("morning", "night"))}
        if rng.random() < 0.06:
            c.blocked_dates[key] = {rng.choice(WEEK): "on approved time off"}
        if rng.random() < 0.1:
            c.hours_limits[key] = (None, float(rng.choice((25, 30, 32))))
        elif rng.random() < 0.1:
            c.hours_limits[key] = (float(rng.choice((20, 24, 30))), None)
    for n in [x for x in names if roles[x] in ("Host", "Busser")][:2]:
        c.minors.add(n.lower())
        c.minor_bands[n.lower()] = "16-17"
    # the concurrent baseline (people on at once), scaled to the roster
    scale = size / 100.0
    concurrent = {}
    for day, lift in _DAY_LIFT.items():
        for part, lo, hi in (("morning", 0, 15 * 60), ("night", 15 * 60, 48 * 60)):
            cell = {}
            for role, runs in _SHAPE.items():
                slots = {}
                for a, b, k_ in runs:
                    for t in range(a, b, 30):
                        if lo <= t < hi:
                            slots[t] = max(1, int(round(k_ * scale * lift)))
                if slots:
                    disp = next(r for r, _s in ROLE_MIX if r.lower() == role)
                    cell[disp] = {"held": min(slots.values()), "peak": max(slots.values()), "slots": slots}
            concurrent[(day, part)] = cell
    requirements = []
    for d in WEEK:
        day = sq._day_name(d)
        for part in ("morning", "night"):
            cell = concurrent.get((day, part)) or {}
            entries = []
            for role, figs in cell.items():
                peak = figs["peak"]
                fl = sr.floor_for(c.role_floors, role, day, part)
                entries.append({"role": role, "required": max(fl, int(math.ceil(peak * 1.3))), "floor": fl,
                                "typical": peak})
            requirements.append({"date": d, "day": day, "daypart": part, "roles": entries, "factor": 1.0,
                                 "demand": "high" if _DAY_LIFT[day] > 1.2 else "normal"})
    # the managers' plan: an opener and a closer each day
    mgrs = [n for n in names if roles[n] == "Manager"]
    plan = []
    for i, d in enumerate(WEEK):
        day = sq._day_name(d)
        close = sr.parse_minutes(c.close_times[day])
        a, b = mgrs[(2 * i) % len(mgrs)], mgrs[(2 * i + 1) % len(mgrs)]
        plan.append(_row(d, a, "Manager", 10 * 60, 16 * 60 + 30, _pinned=True))
        plan.append(_row(d, b, "Manager", 16 * 60, close + 30, _pinned=True))
    # last week, published: a plausible week from the same shape
    last = []
    for d0, d in zip(LAST, WEEK):
        day = sq._day_name(d)
        for role, runs in _SHAPE.items():
            disp = next(r for r, _s in ROLE_MIX if r.lower() == role)
            people = [n for n in names if roles[n] == disp]
            rng.shuffle(people)
            need = max(1, int(round(max(k_ for _a, _b, k_ in runs) * scale * _DAY_LIFT[day])))
            for j, n in enumerate(people[:need]):
                start = runs[0][0] if j % 2 == 0 else 16 * 60 + 30
                end = start + (5 if j % 2 == 0 else 6) * 60
                last.append(_row(d0, n, disp, start, end))
    sig = {"roster": list(names), "roster_roles": dict(roles),
           "overtime": {"rates": dict(RATES), "default_rate": 14.0, "line": 40.0}}
    budget = 0.0
    for r in requirements:
        for e in r["roles"]:
            budget += e["typical"] * 5.5 * RATES.get(e["role"].lower(), 14.0)
    return {"constraints": c, "roster_roles": roles, "concurrent": concurrent, "requirements": requirements,
            "plan": plan, "last_week": last, "signals": sig, "budget_dollars": round(budget * 1.05, 0),
            "names": names}


def _req_map(requirements):
    out = {}
    for r in requirements:
        cell = {}
        for e in r["roles"]:
            cell[e["role"]] = e["required"]
        out[f"{r['date']}|{r['daypart']}"] = cell
    return out


def _judge(rows, s):
    c = s["constraints"]
    import schedule_engine as _se
    t0 = time.monotonic()
    fixed = _se.repair_rows([dict(r) for r in rows], c, roster_roles=s["roster_roles"])
    rep_s = time.monotonic() - t0
    viols = sr.violations(fixed, c)
    hard = [v for v in viols if v.get("hard")]
    gaps = sr.manager_gaps(fixed, c)
    mins = sum(e - a for d in gaps.values() for a, e, _i in d)
    req = {}
    for r in s["requirements"]:
        req[(r["date"], r["daypart"])] = {e["role"]: e["required"] for e in r["roles"]}
    q = sq.score_rows(fixed, requirements_by_date=req, roster=s["names"], roster_roles=s["roster_roles"],
                      hard_breaches=hard, role_floors=c.role_floors, open_times=c.open_times,
                      close_times=c.close_times, section_cap=c.section_cap)
    hours = {}
    dollars = 0.0
    for r in fixed:
        n = (r.get("employee") or "").strip()
        h = _stime.real_hours(r)
        hours[n] = hours.get(n, 0.0) + h
        dollars += h * RATES.get(str(s["roster_roles"].get(n, "")).lower(), 14.0)
    ot = sum(max(0.0, h - 40.0) for h in hours.values())
    dollars += sum(max(0.0, h - 40.0) * 0.5 * RATES.get(str(s["roster_roles"].get(n, "")).lower(), 14.0)
                   for n, h in hours.items())
    return {"rows": len(fixed), "hard": len(hard), "hard_kinds": sorted({v["kind"] for v in hard}),
            "no_manager_minutes": mins, "quality": q.get("score"), "dollars": round(dollars),
            "overtime_hours": round(ot, 1), "repair_seconds": round(rep_s, 2)}


def run_arm(arm, s, seconds=20.0):
    import schedule_cpsat as cs
    import schedule_fallback as fb
    c = s["constraints"]
    t0 = time.monotonic()
    if arm == "solver":
        got = cs.solver_draft(WEEK, c, s["requirements"], concurrent=s["concurrent"], history_rows=s["last_week"],
                              context_rows=s["plan"], last_week_rows=s["last_week"], signals=s["signals"],
                              roster_roles=s["roster_roles"], budget_dollars=s["budget_dollars"],
                              max_seconds=seconds)
        rows = list(s["plan"]) + got["rows"]
        extra = {"unfilled": len(got["unassigned"]), "status": got["stats"].get("status")}
    elif arm == "code_v0":
        got = fb.solver_rows(WEEK, _req_map(s["requirements"]), c, base_rows=s["last_week"], context_rows=s["plan"],
                             open_times=c.open_times, close_times=c.close_times, signals=s["signals"],
                             roster_roles=s["roster_roles"], max_seconds=seconds)
        rows = list(s["plan"]) + got["rows"]
        extra = {"unfilled": len(got["unassigned"]), "status": (got.get("stats") or {}).get("status")}
    elif arm == "carry":
        base = {"rows": s["last_week"], "week_start": LAST[0], "week_end": LAST[-1], "source": "published"}
        got = fb.carry_forward_rows(base, WEEK, c, s["names"], [])
        rows = list(s["plan"]) + [r for r in got["rows"] if (r.get("role") or "") != "Manager"]
        extra = {"unfilled": len(got.get("dropped") or []), "status": "carried"}
    else:
        raise ValueError(arm)
    draft_s = time.monotonic() - t0
    out = {"arm": arm, "draft_seconds": round(draft_s, 2)}
    out.update(extra)
    out.update(_judge(rows, s))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sizes", nargs="*", type=int, default=[60, 150, 250])
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--arms", nargs="*", default=["solver", "code_v0", "carry"])
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    results = []
    for size in a.sizes:
        for seed in range(1, a.seeds + 1):
            s = synthetic(size, seed)
            for arm in a.arms:
                r = run_arm(arm, s, a.seconds)
                r.update(size=size, seed=seed)
                results.append(r)
                if not a.json:
                    print(f"{size:4d} s{seed} {arm:8s} draft {r['draft_seconds']:6.2f}s  rows {r['rows']:4d}  "
                          f"hard {r['hard']:3d}  no-mgr {r['no_manager_minutes']:4d}m  quality {r['quality']}  "
                          f"${r['dollars']:,}  OT {r['overtime_hours']}h  unfilled {r['unfilled']}  "
                          f"{r['status']}  {','.join(r['hard_kinds'])[:60]}", flush=True)
    if a.json:
        print(json.dumps(results, indent=1))
    return results


if __name__ == "__main__":
    main()
