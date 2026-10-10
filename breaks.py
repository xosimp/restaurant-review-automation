"""
breaks.py — meal and rest breaks placed inside shifts, by the law that holds.

A long shift only ever got a soft "schedule a meal break" flag, and nothing
placed one, so the coverage-by-the-hour score counted a server as on the
floor through her 30-minute meal at exactly the busy half hours — and
Illinois's 20 minutes "beginning no later than 5 hours" into a 7.5-hour
shift, or California's premium hour for a late meal, were never planned for
(schedule audit 10/10/26 MARKET-6).

Now a day's breaks are PLACED, deterministically, from:
  * the jurisdiction's meal rules (compliance_packs, sourced: start-by,
    start-after, end-before-end, clock windows, paid or not),
  * a minor's own meal rule from their state's table (a 14-15 year old in
    Washington eats by hour 4),
  * else the owner's "meal break after N hours" (30 unpaid minutes, midway),
staggered inside each role so two servers never eat at once when it can be
helped, at the quietest half hour of the sales curve inside the window
(the same idea as schedule_economics.stagger_same_starts). Paid rest breaks
(10 minutes per 4 hours where the state requires them) are placed evenly
and listed; they are too short to move the half-hour coverage. Coverage
subtracts the meals (shift_quality.dim_coverage_curve).

After the fact, `audit_punches` reads the POS's meal minutes on each punch
and flags a meal that was missed or short, with the premium the
jurisdiction attaches (California: an hour at the regular rate per
workday). A LATE meal needs the break's clock time, which no POS here
reports yet — said, not guessed.

Pure: no database. The rules come in from compliance_packs.values_in_force.
"""
from datetime import datetime

STEP = 5                 # minutes between candidate starts
SLOT = 30


def _clock(label):
    try:
        from schedule_rules import parse_minutes
        return parse_minutes(label or "")
    except Exception:
        return None


def _fmt(m):
    from schedule_rules import _fmt_minutes
    return _fmt_minutes(int(m) % (24 * 60))


def _span(row):
    s, e = _clock(row.get("shift_start")), _clock(row.get("shift_end"))
    if s is None or e is None:
        return None
    if e <= s:
        e += 24 * 60
    return s, e


def rules_for(comp: dict, values: dict = None) -> dict:
    """The break rules in force: {"meals": [rule], "rest": rule|None,
    "premium": {...}|None, "source": url|None, "basis": words, "by": code}.
    `values` is compliance_packs.values_in_force(restaurant); without a
    jurisdiction meal rule the owner's meal_break_after_hours gives one
    30-minute unpaid meal, midway."""
    values = values or {}
    meal = values.get("meal_breaks")
    out = {"meals": [], "rest": None, "premium": None, "source": None, "basis": None, "by": None}
    if meal and meal.get("value"):
        out.update(meals=[dict(m) for m in meal["value"]], source=meal.get("source"), by=meal.get("by"),
                   basis=meal.get("note"))
    else:
        try:
            after = float((comp or {}).get("meal_break_after_hours") or 0)
        except (TypeError, ValueError):
            after = 0.0
        if after > 0:
            out["meals"] = [{"min_hours": after, "minutes": 30, "inclusive": False, "paid": False, "midway": True,
                             "owner": True}]
            out["basis"] = f"your rule: a 30-minute meal on a shift over {after:g} hours"
    rest = values.get("rest_breaks")
    if rest and rest.get("value"):
        out["rest"] = dict(rest["value"], source=rest.get("source"))
    prem = values.get("break_premium")
    if prem and prem.get("value"):
        out["premium"] = dict(prem["value"], source=prem.get("source"), note=prem.get("note"))
    return out


def minor_meal_rule(minor_band_rules: dict):
    """A state's minor meal rule as a placeable meal rule, or None."""
    m = (minor_band_rules or {}).get("meal")
    if not m or not m.get("after_hours"):
        return None
    return {"min_hours": float(m["after_hours"]), "minutes": int(m.get("minutes") or 30), "inclusive": False,
            "paid": False, "start_by": float(m["after_hours"]), "minor": True,
            "only_if_day_hours": m.get("only_if_day_hours")}


def owed(rule: dict, start: int, end: int, day_hours=None) -> bool:
    """Whether a shift from `start` to `end` (minutes) owes this meal."""
    hours = (end - start) / 60.0
    mh = float(rule.get("min_hours") or 0)
    if rule.get("only_if_day_hours") and (day_hours or hours) < float(rule["only_if_day_hours"]) - 1e-6:
        return False
    if rule.get("max_shift_hours") and hours > float(rule["max_shift_hours"]) + 1e-6:
        return False
    if mh > 0 and not (hours >= mh - 1e-6 if rule.get("inclusive") else hours > mh + 1e-6):
        return False
    if rule.get("spans_clock"):
        a, b = (_clock(x) for x in rule["spans_clock"])
        if a is None or b is None:
            return False
        if rule.get("span_strict"):
            if not (start < a and end > b):
                return False
        elif not (start <= a and end >= b):
            return False
    if rule.get("starts_between"):
        a, b = (_clock(x) for x in rule["starts_between"])
        s = start % (24 * 60)
        inside = (a <= s <= b) if a <= b else (s >= a or s <= b)
        if not inside:
            return False
    return True


def window(rule: dict, start: int, end: int):
    """(earliest start, latest start) of this meal inside the shift, in
    minutes on the shift's clock, or None when the law's window does not fit
    inside the shift."""
    mins = int(rule.get("minutes") or 30)
    lo, hi = start, end - mins
    if rule.get("start_after") is not None:
        lo = max(lo, start + int(round(float(rule["start_after"]) * 60)))
    if rule.get("start_by") is not None:
        hi = min(hi, start + int(round(float(rule["start_by"]) * 60)))
    if rule.get("end_by_before_end") is not None:
        hi = min(hi, end - int(round(float(rule["end_by_before_end"]) * 60)) - mins)
    if rule.get("clock"):
        a, b = (_clock(x) for x in rule["clock"])
        if a is not None and b is not None:
            # the clock window on the shift's own day (or the next, past midnight)
            for base in (0, 24 * 60):
                ca, cb = a + base, b + base - mins
                if cb >= lo and ca <= hi:
                    lo, hi = max(lo, ca), min(hi, cb)
                    break
            else:
                return None
    if rule.get("midway") and rule.get("start_by") is None and rule.get("start_after") is None \
            and not rule.get("clock"):
        mid = start + (end - start) // 2 - mins // 2
        lo, hi = max(lo, mid - 30), min(hi, mid + 30)
    if hi < lo:
        return None
    return lo, hi


def _share_at(curve, minute):
    """The sales share of the hour holding `minute` (0 without a curve)."""
    if not curve:
        return 0.0
    h = (minute // 60) % 24
    try:
        return float(curve.get(h, curve.get(str(h), 0)) or 0)
    except (TypeError, ValueError):
        return 0.0


def _rest_count(rest: dict, hours: float) -> int:
    if not rest:
        return 0
    if hours < float(rest.get("min_shift_hours") or 0) - 1e-6:
        return 0
    per = float(rest.get("per_hours") or 4)
    if rest.get("table"):
        n = 0
        for upto, k in rest["table"]:
            if hours <= float(upto) + 1e-6:
                return int(k)
            n = int(k)
        return n
    if rest.get("major_fraction"):
        return int((hours + per / 2.0) // per)
    return int(hours // per)


def plan_day(rows: list, rules: dict, curve: dict = None, families=None, minors: dict = None) -> dict:
    """{row index: [break]} for one date's rows. Each break: {"kind":
    "meal"|"rest", "start", "end" (minutes on the shift's clock), "minutes",
    "paid", "label" ("1:30pm–2:00pm"), "window" [labels], "basis",
    "source", "placed": bool}. A meal whose legal window does not fit the
    shift comes back placed False — the one case the sweep still flags.

    `families(role)` names a role's family for staggering (default: the
    role, lower-cased); `minors` {row index: minor meal rule}."""
    fam = families or (lambda r: (r or "").strip().lower())
    rules = rules or {}
    meals = list(rules.get("meals") or [])
    out = {}
    jobs = []
    for i, r in enumerate(rows or []):
        sp = _span(r)
        if sp is None or not (r.get("employee") or "").strip():
            continue
        s, e = sp
        mine = [m for m in meals if owed(m, s, e)]
        mm = (minors or {}).get(i)
        if mm and owed(mm, s, e):
            mine.append(mm)
        for m in mine:
            win = window(m, s, e)
            jobs.append((i, r, s, e, m, win))
    # the tightest windows first: they have the fewest places to go
    jobs.sort(key=lambda j: ((j[5][1] - j[5][0]) if j[5] else -1, j[2], j[0]))
    placed_by_family = {}
    for i, r, s, e, m, win in jobs:
        mins = int(m.get("minutes") or 30)
        b = {"kind": "meal", "minutes": mins, "paid": bool(m.get("paid")),
             "basis": rules.get("basis") if not m.get("minor") else "the minor's own meal rule",
             "source": rules.get("source"), "minor": bool(m.get("minor"))}
        if win is None:
            out.setdefault(i, []).append(dict(b, placed=False, start=None, end=None, label="",
                                              window=None))
            continue
        lo, hi = win
        f = fam(r.get("role"))
        mine_already = [x for x in out.get(i, []) if x.get("placed")]
        best, best_cost = None, None
        ideal = lo + (hi - lo) / 2.0
        t = lo
        while t <= hi:
            if any(not (t + mins <= x["start"] or t >= x["end"]) for x in mine_already):
                t += STEP
                continue
            overlap = sum(1 for (bs, be) in placed_by_family.get(f, []) if t < be and bs < t + mins)
            cost = _share_at(curve, t) + _share_at(curve, t + mins - 1) + 2.0 * overlap + abs(t - ideal) / 6000.0
            if best_cost is None or cost < best_cost - 1e-12:
                best, best_cost = t, cost
            t += STEP
        if best is None:
            out.setdefault(i, []).append(dict(b, placed=False, start=None, end=None, label="",
                                              window=[_fmt(lo), _fmt(hi)]))
            continue
        placed_by_family.setdefault(f, []).append((best, best + mins))
        out.setdefault(i, []).append(dict(b, placed=True, start=best, end=best + mins,
                                          label=f"{_fmt(best)}–{_fmt(best + mins)}",
                                          window=[_fmt(lo), _fmt(hi)]))
    rest = rules.get("rest")
    if rest:
        for i, r in enumerate(rows or []):
            sp = _span(r)
            if sp is None or not (r.get("employee") or "").strip():
                continue
            s, e = sp
            n = _rest_count(rest, (e - s) / 60.0)
            meals_here = [x for x in out.get(i, []) if x.get("placed")]
            mins = int(rest.get("minutes") or 10)
            for k in range(n):
                t = s + int((k + 1) * (e - s) / (n + 1)) - mins // 2
                t -= t % STEP
                for mx in meals_here:
                    if t < mx["end"] and mx["start"] < t + mins:
                        t = mx["end"]
                out.setdefault(i, []).append({"kind": "rest", "minutes": mins, "paid": bool(rest.get("paid", True)),
                                              "placed": True, "start": t, "end": t + mins,
                                              "label": f"{_fmt(t)}–{_fmt(t + mins)}", "window": None,
                                              "basis": "a paid rest break", "source": rest.get("source"),
                                              "minor": False})
    for i in out:
        out[i].sort(key=lambda x: (x.get("start") is None, x.get("start") or 0))
    return out


def plan_week(rows: list, rules: dict, curves: dict = None, families=None, minors: dict = None) -> dict:
    """{row index (in `rows`): [break]} for a whole week, day by day.
    `curves` {weekday name: {hour: share}}; `minors` {row index: rule}."""
    by_date = {}
    for i, r in enumerate(rows or []):
        by_date.setdefault(r.get("date") or "", []).append(i)
    out = {}
    for d, idxs in by_date.items():
        day_rows = [rows[i] for i in idxs]
        curve = None
        if curves:
            try:
                curve = curves.get(datetime.strptime(d, "%Y-%m-%d").strftime("%A"))
            except (TypeError, ValueError):
                curve = None
        mm = {k: (minors or {}).get(i) for k, i in enumerate(idxs) if (minors or {}).get(i)}
        for k, bl in plan_day(day_rows, rules, curve, families, mm).items():
            out[idxs[k]] = bl
    return out


def off_floor(breaks_for_row: list, minute: int) -> bool:
    """Whether a person is on a placed MEAL at `minute` (rest breaks are too
    short to count against half-hour coverage)."""
    for b in breaks_for_row or []:
        if b.get("kind") == "meal" and b.get("placed") and b["start"] <= minute < b["end"]:
            return True
    return False


def unplaced(plan: dict) -> list:
    """[(row index, break)] meals the law requires whose window does not fit."""
    return [(i, b) for i, bl in (plan or {}).items() for b in bl if b.get("kind") == "meal" and not b.get("placed")]


def audit_punches(punches: list, rules: dict, rates: dict = None) -> list:
    """Missed or short meals from the POS's punches (pos_punches rows:
    employee_name, business_date, clock_in, clock_out, meal_minutes,
    break_minutes, reg_rate) — [{"employee", "date", "hours", "meal_minutes",
    "required_minutes", "kind": "missed_meal"|"short_meal", "premium_hours",
    "premium_dollars", "source"}]. A punch with no meal minutes reported and
    no break minutes either is read as missed; a late meal cannot be judged
    without the meal's clock time, which the POS does not report."""
    meals = (rules or {}).get("meals") or []
    prem = (rules or {}).get("premium") or {}
    out = []
    per_day_flagged = set()
    for p in punches or []:
        ci, co = p.get("clock_in"), p.get("clock_out")
        try:
            t0 = datetime.fromisoformat(str(ci).replace("Z", "")[:19])
            t1 = datetime.fromisoformat(str(co).replace("Z", "")[:19])
        except (TypeError, ValueError):
            continue
        mins_worked = (t1 - t0).total_seconds() / 60.0
        if mins_worked <= 0:
            continue
        s = t0.hour * 60 + t0.minute
        e = s + int(mins_worked)
        need = sum(int(m.get("minutes") or 30) for m in meals if owed(m, s, e))
        if not need:
            continue
        got = float(p.get("meal_minutes") or 0) or float(p.get("break_minutes") or 0)
        if got >= need - 0.5:
            continue
        kind = "missed_meal" if got <= 0 else "short_meal"
        who = p.get("employee_name") or ""
        day = p.get("business_date") or ""
        row = {"employee": who, "date": day, "hours": round(mins_worked / 60.0, 2), "meal_minutes": got,
               "required_minutes": need, "kind": kind, "premium_hours": 0.0, "premium_dollars": None,
               "source": (rules or {}).get("source")}
        if prem.get("hours_at_regular_rate") and (who, day) not in per_day_flagged:
            rate = float(p.get("reg_rate") or 0) or float((rates or {}).get(" ".join(who.lower().split())) or 0)
            row["premium_hours"] = float(prem["hours_at_regular_rate"])
            row["premium_dollars"] = round(rate * row["premium_hours"], 2) if rate else None
            per_day_flagged.add((who, day))
        out.append(row)
    return out
