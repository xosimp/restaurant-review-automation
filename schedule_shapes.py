"""
schedule_shapes.py — which shifts the week has: Stage 1 of the solver-first
draft, a covering model over the restaurant's own shift patterns (scheduler
audit 10/10/26 LOGIC-24, RM-8).

The model used to write every shift's start and end and its person; code
then re-derived the numbers it was given. Here the shapes are chosen
exactly, per role and date, and schedule_cpsat (Stage 2) chooses who works
each one.

Patterns P(role, date) — about 30-150 each, enumerated:
  - the restaurant's own start/end pairs for the role family on that
    weekday (its published weeks and punches, on the quarter hour),
  - the owner's role times (Constraints.role_times) and each role's window
    (the RESTAURANT HOURS & SHIFT RULES — role_window_ok),
  - starts on each step of the role's own demand curve, lengths from the
    shortest to the longest shift in half-hour steps,
  kept to the day's opening, the close plus the role's allowed stay, and
  the planned managers' span (others may start no earlier than the first
  manager unless that manager is in an hour before open — the owner's
  opening-prep allowance — and end no later than the last manager's end
  plus the closing stay).

Variables n[r, d, p] >= 0 — how many work pattern p — and per half hour t
the shortfall under the demand. The demand is PEOPLE ON AT ONCE (contract
#2): the restaurant's own measured people-on-at-once at each half hour
(labor.staffing_baseline's `concurrent`, the lower recency-weighted median
over its last same weekdays — each role its own curve, so cooks lead and
dish lags as they really do) scaled by that daypart's demand ratio (the
requirement row's factor), else the requirement's half-hour ramp.

Constraints and costs (the owner's rank order, schedule_rules.TIER_TABLE):
  floors       people on at once over the core window >= the floor (coverage
               tier — a slot short of it costs that tier)
  section cap  the capped roles' people on at once <= the section count, and
               the owner's "most of a role on at once" (coverage tier)
  closers      somebody of each closing family ends at the close plus stay
  capacity     no more of a role on a date than people who may legally work it
  demand       each half hour short of the demand (requirement tier)
  distinct     the requirement's count of different people per daypart (the
               scorer's coverage — requirement tier, lighter)
  budget       labor dollars past the hourly budget (budget tier), each role
               at its people's rate — priced, never a hard cut, so the week
               never depends on a starting draft
  quality      the dollars themselves, a shift's fixed overhead, and change
               against last published week's shapes (stability)

CP-SAT solves it to optimality in well under a second at restaurant size.
Nothing here calls a model, reads the database or writes anything.
"""
import math
from collections import Counter, defaultdict

import schedule_rules as _rules
import schedule_time as _stime
import shift_quality as sq

try:
    from ortools.sat.python import cp_model as _cp
except Exception:                      # pragma: no cover
    _cp = None

SLOT = 30
GRID = 15
MAX_PATTERNS = 150
HISTORY_MIN_SHARE = 0.05
HISTORY_TOP = 12
DEFAULT_MAX_SHIFT = 10.0
DEFAULT_SECONDS = 8.0

# weights (integer units shared with schedule_cpsat's tiers)
W_FLOOR_SLOT = 10 ** 10 // 2         # half an hour of a floor short (coverage)
W_CAP_SLOT = 10 ** 10 // 2           # half an hour over the section cap / role ceiling (coverage)
W_CLOSER = 4 * 10 ** 10
W_DEMAND_SLOT = 10 ** 7 // 2         # half an hour of a person short of the demand (requirement)
W_MIN_HOUR_HUNDREDTH = 3 * 10 ** 5   # a hundredth of an hour short of a family's minimums (min hours)
W_DISTINCT = 10 ** 7                 # one different person short of the requirement's count
W_BUDGET_DOLLAR = 300                # a dollar past the hourly budget (budget)
W_DOLLAR = 100                       # a labor dollar (quality, ~one point a dollar)
W_SHIFT = 400                        # a shift's fixed overhead (one more person to manage)
W_STABILITY = 1500                   # one shift's shape changed from last published week


def available() -> bool:
    return _cp is not None


def _q(m) -> int:
    return int(round(float(m) / GRID) * GRID)


def _fmt(m) -> str:
    m = int(m) % (24 * 60)
    h, mm = divmod(m, 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def _span(r):
    sp = _stime.span(r)
    if not sp or sp[1] <= sp[0]:
        return None
    return sp


def _covers(start, end, t) -> bool:
    return start <= t and t + SLOT <= end


def history_patterns(rows, family) -> dict:
    """{(family, weekday): Counter((start, end))} — the restaurant's own
    shifts on the quarter hour, from published weeks and punches."""
    out = defaultdict(Counter)
    for r in rows or []:
        sp = _span(r)
        day = sq._day_name(r.get("date") or "", r.get("day") or "")
        role = str(r.get("role") or "").strip()
        if not sp or not day or not role:
            continue
        out[(family(role), day)][(_q(sp[0]), _q(sp[1]))] += 1
    return out


def _window(c, role, d, day, plan_bounds):
    """(earliest start, latest end) a pattern of `role` on date `d` may use."""
    o, _cl = _rules.open_hours(c, day) if c is not None else (None, None)
    close_cap = _rules.close_cap_minutes(c, {"date": d, "role": role, "employee": ""}) if c is not None else None
    lo = 0 if o is None else max(0, o - 4 * 60)
    hi = 30 * 60 if close_cap is None else close_cap
    w = c.role_window_for(role, d) if c is not None else {}
    if w.get("earliest") is not None:
        lo = max(lo, int(w["earliest"]))
    if w.get("latest") is not None:
        hi = min(hi, int(w["latest"]))
    b = plan_bounds.get(d)
    if b:
        mlo, mhi, prep_ok = b
        if not prep_ok:
            lo = max(lo, mlo)
        hi = min(hi, mhi + _rules.CLOSING_MANAGER_STAYS_MINUTES)
    return lo, hi


def plan_bounds(context_rows, c) -> dict:
    """{date: (first manager start, last manager end, prep allowed)} from
    the planned managers among `context_rows`."""
    out = {}
    if c is None:
        return out
    for r in context_rows or []:
        name = (r.get("employee") or "").strip()
        d = r.get("date")
        if not name or not d or not _rules.counts_as_manager(c, r):
            continue
        sp = _rules._span(r)
        if not sp:
            continue
        lo, hi, _p = out.get(d, (sp[0], sp[1], False))
        out[d] = (min(lo, sp[0]), max(hi, sp[1]), False)
    for d, (lo, hi, _p) in list(out.items()):
        o, _cl = _rules.open_hours(c, sq._day_name(d))
        out[d] = (lo, hi, o is not None and lo <= o - _rules.OPENING_MANAGER_LEAD_MINUTES)
    return out


def patterns_for(role, d, demand, hist, c, bounds, min_h, max_h, role_times=None) -> list:
    """[(start, end)] business minutes: history pairs, the owner's role
    times, and curve steps x lengths — all inside the role's window."""
    day = sq._day_name(d)
    lo, hi = _window(c, role, d, day, bounds)
    if hi - lo < min_h * 60:
        return []
    cand = Counter()
    fam = c.family(role) if c is not None else sq.role_family(role)
    hist_here = hist.get((fam, day)) or Counter()
    total = float(sum(hist_here.values()) or 1)
    for (s, e), n in hist_here.most_common(HISTORY_TOP):
        if n / total >= HISTORY_MIN_SHARE:
            cand[(s, e)] += 1000 + n
    for (rl, wd, _part), t in (role_times or {}).items():
        if wd == day and (c.family(rl) if c is not None else rl) == fam:
            s = t.get("start")
            e = t.get("end")
            if s is not None and e is not None:
                cand[(int(s), int(e) if e > s else int(e) + 1440)] += 2000
    slots = sorted(t for t in (demand or {}) if demand[t] > 0)
    steps = set()
    prev, prev_t = 0, None
    for t in slots:
        n = demand[t]
        # a rise, or the first half hour after a break in the curve (each
        # daypart's own start)
        if n > prev or prev_t is None or t - prev_t > SLOT:
            steps.add(t)
        prev, prev_t = n, t
    ends = set()
    prev, prev_t = None, None
    for t in slots:
        n = demand[t]
        if prev is not None and (n < prev or t - prev_t > SLOT):
            ends.add(t if n < prev else prev_t + SLOT)
        prev, prev_t = n, t
    if slots:
        ends.add(min(hi, slots[-1] + SLOT))
    # the close, and the close plus the role's stay: where a closer ends
    ends.add(hi)
    if c is not None:
        cl = _rules.close_minutes(c, day)
        if cl is not None and lo < cl <= hi:
            ends.add(cl)
    for s in sorted(steps):
        for length in range(int(min_h * 60), int(max_h * 60) + 1, 60):
            cand[(s, s + length)] += 1
        for e in ends:
            if min_h * 60 <= e - s <= max_h * 60:
                cand[(s, e)] += 5
    out = []
    for (s, e), w in cand.most_common():
        if s < lo or e > hi or e - s < min_h * 60 - 1 or e - s > max_h * 60 + 1:
            continue
        if c is not None:
            ok, _w = c.role_window_ok({"date": d, "role": role, "shift_start": _fmt(s), "shift_end": _fmt(e)})
            if not ok:
                continue
        out.append((s, e))
        if len(out) >= MAX_PATTERNS:
            break
    return out


def demand_curves(dates, requirements, concurrent=None, demand_curve=None, open_times=None, close_times=None,
                  roles=None) -> dict:
    """{(date, role): {minute: people on at once}} — the restaurant's own
    measured people-on-at-once (`concurrent`, {(weekday, daypart): {role:
    {"slots": {minute: n}}}}) scaled by the requirement row's demand factor;
    a role the requirement asks for with no measured curve takes the
    requirement's half-hour ramp, else its count spread over the core
    window. Only roles the requirement row asks for that daypart (role
    windows, ceilings and the roles this request staffs already applied)."""
    import staffing_curve as _curve
    out = {}
    req_by = {}
    for row in requirements or []:
        if row.get("daypart") not in ("morning", "night"):
            continue
        req_by[(row["date"], row["daypart"])] = row
    split = _stime.daypart_split()
    for d in dates or []:
        day = sq._day_name(d)
        for part in ("morning", "night"):
            row = req_by.get((d, part))
            if not row:
                continue
            factor = float(row.get("factor") or 1.0)
            lo_p, hi_p = (0, split) if part == "morning" else (split, 48 * 60)
            conc = (concurrent or {}).get((day, part)) or {}
            low_conc = {str(k).strip().lower(): v for k, v in conc.items()}
            # The requirement's own people-on-at-once per half hour (G2:
            # work ÷ the labor standard at the rush plus the fixed crew, each
            # role's lead or lag measured) — the one the score judges.
            half_need = {}
            for m, need_at in (row.get("half_need") or {}).items():
                for r_h, n_h in (need_at or {}).items():
                    half_need.setdefault(str(r_h).strip().lower(), {})[int(m)] = int(n_h or 0)
            for entry in row.get("roles") or []:
                role = str(entry.get("role") or "").strip()
                if not role or (roles is not None and role.lower() not in roles):
                    continue
                need = int(entry.get("firm", entry.get("required")) or 0)
                if need <= 0 and not entry.get("floor"):
                    continue
                cur = out.setdefault((d, role), {})
                own = half_need.get(role.lower())
                if own:
                    for m, n in own.items():
                        if lo_p <= m < hi_p and n > 0:
                            cur[m] = max(cur.get(m, 0), n)
                    continue
                figs = low_conc.get(role.lower())
                slots = (figs or {}).get("slots") or {}
                if slots:
                    for m, n in slots.items():
                        m = int(m)
                        if lo_p <= m < hi_p:
                            v = int(math.floor(float(n) * factor + 0.5))
                            cur[m] = max(cur.get(m, 0), v)
                    continue
                ramp = (row.get("half_hours") or {}).get(role)
                if ramp:
                    pts = sorted((int(m), int(n)) for m, n in ramp)
                    o_end = hi_p
                    for i, (m, n) in enumerate(pts):
                        nxt = pts[i + 1][0] if i + 1 < len(pts) else min(o_end, m + 4 * 60)
                        for t in range(m, nxt, SLOT):
                            cur[t] = max(cur.get(t, 0), n)
                    continue
                win = sq.floor_window(part, _rules.parse_minutes((open_times or {}).get(day) or ""),
                                      _rules.parse_minutes((close_times or {}).get(day) or ""))
                if not win:
                    continue
                each = max(1, int(math.ceil(_curve.PEAK_SHARE * max(need, int(entry.get("floor") or 0)) - 1e-9)))
                for t in range(win[0] - win[0] % SLOT, win[1], SLOT):
                    cur[t] = max(cur.get(t, 0), each)
    return {k: v for k, v in out.items() if v}


def _available(c, role, d, roster_roles) -> int:
    """How many people may legally work `role` on `d` (can_work, fillable,
    holds) — the capacity bound."""
    if c is None:
        return 99
    n = 0
    for name in c.roster_names or []:
        if not c.holds(name, role, d):
            continue
        mine = (c.known_roles or {}).get(c.key(name))
        if not mine and (roster_roles or {}).get(name):
            if c.family((roster_roles or {}).get(name)) != c.family(role):
                continue
        elif mine and c.family(role) not in {c.family(x) for x in mine}:
            continue
        if c.can_work(name, d)[0] and c.fillable(name, d)[0]:
            n += 1
    return n


def _length_bounds(c, role, d, roster_roles, min_h, max_h) -> tuple:
    """(shortest, longest) hours a shape of `role` on `d` may be, from the
    people who may legally work it (STRESS-6): the one shortest shift
    lowered to a minor's legal day (a 14-15 year old's 3h school-day cap is
    a shift, not nothing — schedule_rules.shortest_for), and no shape longer
    than the longest day anybody available may work (three 16-17 servers
    capped at 8h got 0h when every shape was 11am-10pm)."""
    if c is None:
        return min_h, max_h
    lo, hi = min_h, None
    for name in c.roster_names or []:
        if not c.holds(name, role, d):
            continue
        mine = (c.known_roles or {}).get(c.key(name))
        if not mine and (roster_roles or {}).get(name):
            if c.family((roster_roles or {}).get(name)) != c.family(role):
                continue
        elif mine and c.family(role) not in {c.family(x) for x in mine}:
            continue
        if not (c.can_work(name, d)[0] and c.fillable(name, d)[0]):
            continue
        lo = min(lo, _rules.shortest_for(c, name, d) / 60.0)
        cap = None
        if c.key(name) in (c.minors or ()):
            cap, _t = _rules.minor_daily_cap(c, c.key(name), d)
        longest = min(max_h, float(cap)) if cap else max_h
        hi = longest if hi is None else max(hi, longest)
    return lo, (max_h if hi is None else max(lo, hi))


def shape_week(dates, constraints, requirements, concurrent=None, history_rows=None, context_rows=None,
               last_week_rows=None, rates=None, blended_rate=None, budget_dollars=None, roster_roles=None,
               demand_curve=None, max_seconds=DEFAULT_SECONDS, seed=7, staffing_value=None) -> dict:
    """The week's shift shapes: {"rows": [slot rows, no employee, `_open`,
    `_floor` where a floor needs them], "short": [{date, role, minutes,
    people}], "stats": {...}}. `context_rows` (the manager plan, a redo's
    kept days, locked rows) count toward coverage and bound the span.
    `staffing_value` — {(family, daypart): {"per_hour", "lo", "hi"}}
    (staffing_elasticity.marginal_values, MARKET-23): where one more person
    measurably returns more than they cost (the interval clear of zero),
    each half hour one person over the demand earns that net value back,
    so the model staffs the extra person exactly where it pays."""
    c = constraints
    out = {"rows": [], "short": [], "stats": {"status": "nothing_to_solve"}}
    if not available() or not dates:
        out["stats"]["status"] = "unavailable" if not available() else "nothing_to_solve"
        return out
    family = (c.family if c is not None else sq.role_family)
    open_times = getattr(c, "open_times", None) or {}
    close_times = getattr(c, "close_times", None) or {}
    curves = demand_curves(dates, requirements, concurrent, demand_curve, open_times, close_times)
    comp = getattr(c, "compliance", None) or {}
    # The one shortest shift (contract #2, schedule_rules.min_shift_minutes);
    # per role and date it is lowered to what the people who may work it can
    # legally work (a 14-15 year old's 3h school day — STRESS-6).
    min_h = _rules.min_shift_minutes(c) / 60.0
    max_h = min(float(comp.get("max_shift_hours") or DEFAULT_MAX_SHIFT), DEFAULT_MAX_SHIFT + 2)
    hist = history_patterns(list(history_rows or []) + list(last_week_rows or []), family)
    bounds = plan_bounds(context_rows, c)
    closed = set(getattr(c, "closed_dates", None) or ())
    # coverage already on the week (the plan, kept and locked rows)
    fixed_on = defaultdict(lambda: defaultdict(int))
    for r in context_rows or []:
        sp = _rules._span(r)
        if not sp or not (r.get("employee") or "").strip():
            continue
        fam = family(r.get("role"))
        for t in range(sp[0] - sp[0] % SLOT, sp[1], SLOT):
            if _covers(sp[0], sp[1], t):
                fixed_on[(r.get("date"), fam)][t] += 1
    m = _cp.CpModel()
    n = {}
    pats = {}
    obj = []
    roles_by_date = defaultdict(set)
    for (d, role) in curves:
        if d in closed:
            continue
        roles_by_date[d].add(role)
    # every role a floor names, even with no demand row
    floor_codes = {}
    for d in dates:
        if d in closed:
            continue
        day = sq._day_name(d)
        for (fam, part), (need, code) in (_rules.floor_needs(c, day) if c is not None else {}).items():
            floor_codes[(d, fam, part)] = (need, code)
            if not any(family(r) == fam for r in roles_by_date[d]):
                roles_by_date[d].add(code)
    last_shapes = Counter()
    for r in last_week_rows or []:
        sp = _span(r)
        if sp:
            last_shapes[(sq._day_name(r.get("date") or ""), str(r.get("role") or "").strip().lower(),
                         _q(sp[0]), _q(sp[1]))] += 1
    rates = {str(k).strip().lower(): float(v) for k, v in (rates or {}).items() if v}
    blended = float(blended_rate or 0) or (sum(rates.values()) / len(rates) if rates else 15.0)
    dollars = []
    on_at = defaultdict(list)            # (date, family, t) -> [(var, role)]
    for d in sorted(roles_by_date):
        day = sq._day_name(d)
        for role in sorted(roles_by_date[d]):
            demand = curves.get((d, role)) or {}
            lo_h, hi_h = _length_bounds(c, role, d, roster_roles, min_h, max_h)
            ps = patterns_for(role, d, demand, hist, c, bounds, lo_h, hi_h, getattr(c, "role_times", None))
            if not ps:
                continue
            cap_people = _available(c, role, d, roster_roles)
            if cap_people <= 0:
                continue
            pats[(d, role)] = ps
            vars_here = []
            wage = rates.get(role.strip().lower()) or rates.get(family(role)) or blended
            for k, (s, e) in enumerate(ps):
                v = m.new_int_var(0, cap_people, f"n{d}_{role}_{k}")
                n[(d, role, k)] = v
                vars_here.append(v)
                hours = (e - s) / 60.0
                cents = int(round(hours * wage * 100))
                dollars.append(cents * v)
                obj.append((W_DOLLAR * hours * wage + W_SHIFT, v))
                last_n = last_shapes.get((day, role.strip().lower(), s, e), 0)
                if last_n:
                    dev = m.new_int_var(0, max(last_n, cap_people), f"st{d}_{role}_{k}")
                    m.add(dev >= v - last_n)
                    m.add(dev >= last_n - v)
                    obj.append((W_STABILITY, dev))
                for t in range(s - s % SLOT, e, SLOT):
                    if _covers(s, e, t):
                        on_at[(d, family(role), t)].append((v, role))
            m.add(sum(vars_here) <= cap_people)
            # the demand, half hour by half hour
            fam = family(role)
            for t, need in sorted(demand.items()):
                have = [v for k, (s, e) in enumerate(ps) if _covers(s, e, t) for v in [n[(d, role, k)]]]
                fixed = fixed_on[(d, fam)].get(t, 0) if not any(family(r2) == fam and r2 != role
                                                                for r2 in roles_by_date[d]) else 0
                gap = need - fixed
                if gap <= 0:
                    continue
                short = m.new_int_var(0, gap, f"sh{d}_{role}_{t}")
                m.add(sum(have) + short >= gap)
                obj.append((W_DEMAND_SLOT * sq.DEMAND_WEIGHT.get("normal", 1.0), short))
                # one more person over the demand, where it measurably pays
                part_t = "morning" if t < _stime.daypart_split() else "night"
                val = (staffing_value or {}).get((fam, part_t)) or {}
                try:
                    net = float(val.get("per_hour") or 0) if float(val.get("lo") or 0) > 0 else 0.0
                except (TypeError, ValueError):
                    net = 0.0
                if net > 0 and have:
                    extra = m.new_bool_var(f"ex{d}_{role}_{t}")
                    m.add(sum(have) >= gap + extra)
                    # the wage is charged on the pattern's hours already:
                    # credit the gross value, so the net is what one earns
                    obj.append((-(W_DOLLAR * (net + wage) / 2.0), extra))
            # the requirement's count of different people per daypart
            for row in requirements or []:
                if row.get("date") != d or row.get("daypart") not in ("morning", "night"):
                    continue
                for entry in row.get("roles") or []:
                    if str(entry.get("role") or "").strip().lower() != role.lower():
                        continue
                    want = int(entry.get("firm", entry.get("required")) or 0)
                    if want <= 0:
                        continue
                    present = [n[(d, role, k)] for k, (s, e) in enumerate(ps)
                               if row["daypart"] in sq.present_dayparts({"date": d, "shift_start": _fmt(s),
                                                                          "shift_end": _fmt(e)})]
                    if not present:
                        continue
                    sh = m.new_int_var(0, want, f"di{d}_{role}_{row['daypart']}")
                    m.add(sum(present) + sh >= want)
                    obj.append((W_DISTINCT, sh))
    if not n:
        out["stats"] = {"status": "nothing_to_solve", "patterns": 0}
        return out
    # floors: people of the family on at once over the core window
    for (d, fam, part), (need, code) in floor_codes.items():
        day = sq._day_name(d)
        o = _rules.parse_minutes(open_times.get(day) or "")
        cl = _rules.parse_minutes(close_times.get(day) or "")
        for t in sq.floor_slots(part, o, cl):
            have = [v for v, _r in on_at.get((d, fam, t), ())]
            fixed = fixed_on[(d, fam)].get(t, 0)
            gap = need - fixed
            if gap <= 0:
                continue
            short = m.new_int_var(0, gap, f"fl{d}_{fam}_{t}")
            m.add(sum(have) + short >= gap)
            obj.append((W_FLOOR_SLOT, short))
    # the section cap and the owner's most-on-at-once
    cap = int(getattr(c, "section_cap", 0) or 0) if c is not None else 0
    cap_roles = sorted(getattr(c, "foh_roles", None) or {"server"}) if c is not None else ["server"]
    if cap > 0:
        import staffing_curve as _curve
        by_dt = defaultdict(list)
        for (d, fam, t), items in on_at.items():
            for v, role in items:
                if _curve.capped_role(role, cap_roles, getattr(c, "role_families", None)):
                    by_dt[(d, t)].append(v)
        for (d, t), vs in by_dt.items():
            over = m.new_int_var(0, len(vs) * 10, f"cap{d}_{t}")
            m.add(sum(vs) - over <= cap)
            obj.append((W_CAP_SLOT, over))
    for (d, fam, t), items in on_at.items():
        role = items[0][1]
        part = "morning" if t < _stime.daypart_split() else "night"
        try:
            import schedule_requirements as _sreq
            most = _sreq._role_cap(getattr(c, "role_caps", None) or {}, role, sq._day_name(d), part)
        except Exception:
            most = None
        if most is not None:
            over = m.new_int_var(0, 50, f"rc{d}_{fam}_{t}")
            m.add(sum(v for v, _r in items) - over <= int(most))
            obj.append((W_CAP_SLOT, over))
    # closers: somebody of each closing family on until the close + stay
    if c is not None and c.closers_by_role and c.compliance.get("keyholder_until_close", True):
        for d in roles_by_date:
            day = sq._day_name(d)
            for fam in sorted({family(r) for r in roles_by_date[d]}):
                if not (c.closers_by_role or {}).get(fam) or not _rules._closer_rule_runs(c, fam, d):
                    continue
                late_vars = []
                last = 0
                for role in roles_by_date[d]:
                    if family(role) != fam:
                        continue
                    for k, (s, e) in enumerate(pats.get((d, role), ())):
                        last = max(last, e)
                if not last:
                    continue
                need, _until = _rules.closer_need(c, fam, day, last)
                for role in roles_by_date[d]:
                    if family(role) != fam:
                        continue
                    for k, (s, e) in enumerate(pats.get((d, role), ())):
                        if e >= need - 15:
                            late_vars.append(n[(d, role, k)])
                if late_vars:
                    gap = m.new_bool_var(f"cl{d}_{fam}")
                    m.add(sum(late_vars) + gap >= 1)
                    obj.append((W_CLOSER, gap))
    # each family's minimum hours: enough shifts for the people the owner
    # set a minimum for (MIN_HOURS ranks above the requirements and the
    # budget — LOGIC-4), less what the fixed rows already give them
    if c is not None:
        owed = defaultdict(float)
        week = set(dates)
        for name in c.roster_names or []:
            mn = c.min_hours(name)
            if not mn or not _rules.hours_rules_apply(c, name):
                continue
            mine = (c.known_roles or {}).get(c.key(name)) or (
                {str((roster_roles or {}).get(name) or "").lower()} - {""})
            fams = {family(x) for x in mine}
            if len(fams) != 1:
                continue
            given = sum(_rules.row_hours(r) for r in context_rows or []
                        if c.key(r.get("employee")) == c.key(name) and r.get("date") in week)
            owed[next(iter(fams))] += max(0.0, float(mn) - given)
        for fam, hrs in owed.items():
            terms = []
            for (d, role, k), v in n.items():
                if family(role) == fam:
                    s0, e = pats[(d, role)][k]
                    terms.append(int(round((e - s0) / 60.0 * 100)) * v)
            if not terms or hrs <= 0:
                continue
            sh = m.new_int_var(0, int(round(hrs * 100)), f"mh{fam}")
            m.add(sum(terms) + sh >= int(round(hrs * 100)))
            obj.append((W_MIN_HOUR_HUNDREDTH, sh))
    # the budget, priced
    if budget_dollars and dollars:
        fixed_cents = 0
        for r in context_rows or []:
            if (r.get("employee") or "").strip() and c is not None and not c.is_salaried(r.get("employee")):
                fixed_cents += int(round(_rules.row_hours(r) * blended * 100))
        over = m.new_int_var(0, 10 ** 9, "over_budget")
        m.add(100 * over >= sum(dollars) + fixed_cents - int(round(float(budget_dollars) * 100)))
        obj.append((W_BUDGET_DOLLAR, over))
    m.minimize(sum(int(round(w)) * v for w, v in obj))
    s = _cp.CpSolver()
    s.parameters.random_seed = int(seed)
    s.parameters.num_workers = 4
    s.parameters.interleave_search = True
    s.parameters.max_deterministic_time = max(0.5, float(max_seconds))
    s.parameters.max_time_in_seconds = max(1.0, float(max_seconds) * 4)
    st = s.solve(m)
    if st not in (_cp.OPTIMAL, _cp.FEASIBLE):
        out["stats"] = {"status": s.status_name(st).lower(), "patterns": len(n)}
        return out
    rows = []
    for (d, role, k), v in sorted(n.items(), key=lambda kv: (kv[0][0], kv[0][1].lower(), pats[(kv[0][0], kv[0][1])][kv[0][2]])):
        cnt = s.value(v)
        if cnt <= 0:
            continue
        a, b = pats[(d, role)][k]
        for _i in range(cnt):
            r = {"date": d, "day": sq._day_name(d), "employee": "", "role": role, "shift_start": _fmt(a),
                 "shift_end": _fmt(b), "scheduled_hours": "", "notes": "", "_open": True}
            r["scheduled_hours"] = _stime.row_hours_text(r, getattr(c, "tz", None) if c is not None else None)
            rows.append(r)
    _mark_floor(rows, floor_codes, family, open_times, close_times, fixed_on)
    short = []
    for (d, role), demand in curves.items():
        if (d, role) not in pats:
            if d not in closed:
                short.append({"date": d, "role": role, "people": max(demand.values()), "minutes": SLOT * len(demand),
                              "why": "nobody may work it, or no shift fits its hours"})
    out["rows"] = rows
    out["short"] = short
    out["stats"] = {"status": "optimal" if st == _cp.OPTIMAL else "time_limit", "patterns": len(n),
                    "shifts": len(rows), "seconds": round(s.wall_time, 3), "objective": s.objective_value,
                    "bound": s.best_objective_bound}
    return out


def _mark_floor(rows, floor_codes, family, open_times, close_times, fixed_on):
    """`_floor` on the shifts a floor needs: per date, family and daypart,
    the `need` shifts covering most of the core window (less what fixed
    rows already hold) — Stage 2 prices leaving one of them empty at the
    coverage tier, the rest at the requirement's."""
    for (d, fam, part), (need, _code) in floor_codes.items():
        day = sq._day_name(d)
        o = _rules.parse_minutes((open_times or {}).get(day) or "")
        cl = _rules.parse_minutes((close_times or {}).get(day) or "")
        slots = sq.floor_slots(part, o, cl)
        if not slots:
            continue
        held = min(fixed_on[(d, fam)].get(t, 0) for t in slots)
        want = need - held
        if want <= 0:
            continue
        scored = []
        for r in rows:
            if r["date"] != d or family(r["role"]) != fam:
                continue
            sp = _rules._span(r)
            if not sp:
                continue
            k = sum(1 for t in slots if _covers(sp[0], sp[1], t))
            if k:
                scored.append((-k, id(r), r))
        scored.sort(key=lambda t: (t[0], t[1]))
        for _k, _i, r in scored[:want]:
            r["_floor"] = True
