"""
schedule_shapes.py — which shifts the week has, and a first answer to who
can work each: Stage 1 of the solver-first draft (person-aware).

Patterns P(role, date) — enumerated, best first:
  - the restaurant's own start/end pairs for the role family on that
    weekday (its published weeks and punches, on the quarter hour),
  - the owner's role times and each role's window (role_window_ok),
  - starts on each step of the role's demand curve, lengths from the one
    minimum shift (schedule_rules.min_shift_minutes, contract #2) to the
    longest, ends where the curve falls, at the close and at the close plus
    the role's stay,
  - with no history of the family: the open-to-mid and mid-to-close
    templates,
  kept to the day's opening (a role's lead before it), the close plus the
  role's stay (the owner's, else learned from punches, else the stated
  default — never a hard cap at the close), and the planned managers' span.
  A restaurant open 24 hours has no close: its business day turns over at
  DAY_START and a shift may run across it.

Variables z[p, d, role, k] — person p works pattern k of `role` on date d —
for the people who may legally work it (the role they hold, availability by
daypart and personal window, approved time off, a minor's band and caps,
certificates, whom code may choose), and n[d, role, k] = how many work it.
So capacity is per person, per daypart and per week: a lunch-only server is
capacity for lunch shapes only, nobody works two shifts a day (or two legs
with a break where they work doubles), and nobody passes their own maximum.

The objective is schedule_objective's, in dollars (contract #1), with the
owner's rank order above it as constraints and tier-sized costs:
  coverage   floors (people on at once over the core window), the section
             cap and most-on-at-once, a closer until close;
  policy     the restaurant's weekly ceiling, days in a row, rest;
  overtime   hours past a person's overtime line;
  min hours  hours under a person's minimum;
  demand     each person-half-hour short of the half-hour need x its
             slot_value (the half hour's share of the week's demand x the
             role's criticality — a busy Friday dinner's hole costs far
             more than a quiet Monday afternoon's);
  budget     dollars past the hourly budget x BUDGET_OVER_FACTOR — so the
             LOWEST-value half hours are the ones cut when the budget
             binds;
  dollars    each person's own rate, a shift's overhead, past the role's
             usual length squared and a double where the history has
             none, a day past somebody's usual days, their usual weekday
             and daypart credited, hours under their usual week, last
             published week's person on the same slot (stability), the
             restaurant's own shapes.
The requirement's count of different people is priced only where no
half-hour curve exists (the curve is the requirement).

Deterministic: everything iterated in sorted order, a fixed seed and worker
count and a deterministic work budget; the wall clock only an emergency
cap. Nothing here calls a model, reads the database or writes.
"""
import math
from datetime import datetime
import os
from collections import Counter, defaultdict

import schedule_objective as _obj
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
PERSON_PATTERNS = 60                   # the best patterns of a role-date a person is offered
HISTORY_MIN_SHARE = 0.05
HISTORY_TOP = 12
DEFAULT_MAX_SHIFT = 10.0
DEFAULT_SECONDS = 8.0
DAY_START = _stime.NIGHT_TAIL_BEFORE   # a 24-hour restaurant's business day turns over here (4am)
DOUBLE_BREAK_MINUTES = 60              # the least break between the two legs of a double
WORKERS = max(1, int(float(os.getenv("SCHEDULE_CPSAT_WORKERS", "4") or 4)))

# weights (integer units shared with schedule_cpsat's tiers; dollars are cents)
W_FLOOR_SLOT = 10 ** 10 // 2         # half an hour of a floor short (coverage)
W_CAP_SLOT = 10 ** 10 // 2           # half an hour over the section cap / role ceiling (coverage)
W_CLOSER = 4 * 10 ** 10
W_POLICY_HUNDREDTH = 3 * 10 ** 6     # a hundredth of an hour past the weekly ceiling (policy)
W_POLICY_RUN_DAY = 2 * 10 ** 9       # a day past the days-in-a-row rule (policy)
W_POLICY_REST = 3 * 10 ** 8          # a turnaround shorter than the rest rule (policy)
W_OT_HUNDREDTH = 10 ** 6             # a hundredth of an hour past a person's overtime line (overtime)
W_MIN_HOUR_HUNDREDTH = 3 * 10 ** 5   # a hundredth of an hour short of someone's minimum (min hours)
W_DEMAND_SLOT = 10 ** 7 // 2         # kept for callers: the requirement tier's old flat price
W_FIRM_SLOT = 10 ** 7 // 2           # the first person of a role the restaurant is never without (requirement)
FIRM_PRESENCE = _obj.FIRM_PRESENCE    # "never without" (one definition: schedule_objective)
W_DISTINCT = 10 ** 7                 # kept for callers: the old distinct-count price
W_BUDGET_DOLLAR = 300                # kept for callers
W_DOLLAR = 100                       # cents in a dollar: every price below is in cents
W_SHIFT = 400
W_STABILITY = 1500


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


def _cents(usd) -> int:
    return int(round(float(usd) * 100))


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


def is_24h(c, day) -> bool:
    """Whether the restaurant is open around the clock on `day` (open and
    close on file, 23 hours or more apart — 12:00am-12:00am, 12:00am-11:59pm,
    6:00am-6:00am): no close to cap a shift at."""
    if c is None:
        return False
    o, cl = _rules.open_hours(c, day)
    return o is not None and cl is not None and cl - o >= 23 * 60


def day_start(c, day):
    """Where a 24-hour day's business day starts: its open when that is
    past DAY_START, else DAY_START (the small hours belong to the night
    before)."""
    o, _cl = _rules.open_hours(c, day)
    return o if (o is not None and o >= DAY_START) else DAY_START


def _window(c, role, d, day, plan_bounds, stay=0, max_minutes=12 * 60):
    """(earliest start, latest end) a pattern of `role` on date `d` may use:
    four hours before the open, to the close plus the most the role may stay
    (the owner's cap, else `stay` — what the role's punches show, else the
    stated default: never a hard cap at the close), inside the owner's role
    window and the planned managers' span. Open 24 hours: the business day
    from day_start, a shift ending up to `max_minutes` past its last start."""
    if c is not None and is_24h(c, day):
        lo = day_start(c, day)
        hi = lo + 24 * 60 + max_minutes
    else:
        o, cl = _rules.open_hours(c, day) if c is not None else (None, None)
        close_cap = _rules.close_cap_minutes(c, {"date": d, "role": role, "employee": ""}) if c is not None else None
        lo = 0 if o is None else max(0, o - 4 * 60)
        hi = 30 * 60 if close_cap is None else close_cap
        if cl is not None and stay:
            hi = max(hi, cl + int(stay))
    w = c.role_window_for(role, d) if c is not None else {}
    if w.get("earliest") is not None:
        lo = max(lo, int(w["earliest"]))
    if w.get("latest") is not None:
        hi = min(hi, int(w["latest"]))
    b = plan_bounds.get(d)
    if not b and plan_bounds and c is not None and not is_24h(c, day):
        # The plan has no manager on this date: nobody is drafted outside
        # the open hours, where each minute could only add to the stretch
        # with no manager (the manager rule outranks the stay and the prep).
        o, cl = _rules.open_hours(c, day)
        if o is not None:
            lo = max(lo, o)
        if cl is not None:
            hi = min(hi, cl)
    if b and not (c is not None and is_24h(c, day)):
        # (open around the clock the managers run across the turnover, so a
        # date's own manager rows bound nothing — the manager tier holds it)
        mlo, mhi, prep_ok = b
        if not prep_ok:
            lo = max(lo, mlo)
        hi = min(hi, mhi + _rules.closing_stay_minutes(c))
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
        lead = _rules.opening_lead_minutes(c)
        out[d] = (lo, hi, lead > 0 and o is not None and lo <= o - lead)
    return out


def patterns_for(role, d, demand, hist, c, bounds, min_h, max_h, role_times=None, stay=0, templates=False) -> list:
    """[(start, end)] business minutes, best first: history pairs, the
    owner's role times, curve steps x lengths, and (`templates`, a family
    with no history) the open-to-mid and mid-to-close shifts — all inside
    the role's window."""
    day = sq._day_name(d)
    lo, hi = _window(c, role, d, day, bounds, stay=stay, max_minutes=int(max_h * 60))
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
    cl = None
    if c is not None and not is_24h(c, day):
        cl = _rules.close_minutes(c, day)
        if cl is not None and lo < cl <= hi:
            ends.add(cl)
    if templates and c is not None:
        # open-to-mid and mid-to-close: a new restaurant's first week is
        # drafted on the shifts a GM would write, not 4h fragments
        o, _c2 = _rules.open_hours(c, day)
        if o is not None and cl is not None and cl - o >= 2 * min_h * 60:
            mid = _q(o + (cl - o) / 2.0)
            for s, e in ((o, mid), (mid, cl), (o, min(hi, mid + 60)), (max(lo, mid - 60), cl), (mid, hi),
                         (max(lo, mid - 60), hi)):
                cand[(int(s), int(e))] += 500
    for s in sorted(steps):
        for length in range(int(min_h * 60), int(max_h * 60) + 1, 60):
            cand[(s, s + length)] += 1
        for e in ends:
            if min_h * 60 <= e - s <= max_h * 60:
                cand[(s, e)] += 5
    out = []
    for (s, e), w in sorted(cand.items(), key=lambda kv: (-kv[1], kv[0])):
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
                  roles=None, sources=None) -> dict:
    """{(date, role): {minute: people on at once}} — the requirement row's
    own half-hour need, else the restaurant's measured people-on-at-once
    (`concurrent`) scaled by the row's demand factor, else the requirement's
    half-hour ramp, else its count spread over the core window. Only roles
    the requirement row asks for that daypart. `sources`, a dict, is filled
    with {(date, role): "half_need" | "concurrent" | "ramp" | "spread"}."""
    import staffing_curve as _curve
    out = {}
    src = sources if sources is not None else {}
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
                    src[(d, role)] = "half_need"
                    continue
                figs = low_conc.get(role.lower())
                slots = (figs or {}).get("slots") or {}
                if slots:
                    for m, n in slots.items():
                        m = int(m)
                        if lo_p <= m < hi_p:
                            v = int(math.floor(float(n) * factor + 0.5))
                            cur[m] = max(cur.get(m, 0), v)
                    src.setdefault((d, role), "concurrent")
                    continue
                ramp = (row.get("half_hours") or {}).get(role)
                if ramp:
                    pts = sorted((int(m), int(n)) for m, n in ramp)
                    o_end = hi_p
                    for i, (m, n) in enumerate(pts):
                        nxt = pts[i + 1][0] if i + 1 < len(pts) else min(o_end, m + 4 * 60)
                        for t in range(m, nxt, SLOT):
                            cur[t] = max(cur.get(t, 0), n)
                    src.setdefault((d, role), "ramp")
                    continue
                win = sq.floor_window(part, _rules.parse_minutes((open_times or {}).get(day) or ""),
                                      _rules.parse_minutes((close_times or {}).get(day) or ""))
                if not win:
                    continue
                each = max(1, int(math.ceil(_curve.PEAK_SHARE * max(need, int(entry.get("floor") or 0)) - 1e-9)))
                for t in range(win[0] - win[0] % SLOT, win[1], SLOT):
                    cur[t] = max(cur.get(t, 0), each)
                src.setdefault((d, role), "spread")
    return {k: v for k, v in out.items() if v}


SHOULDER_HISTORY_MAX_MINUTES = 6 * 60      # how far before the open / after the close history is read


def history_shoulders(hist_rows, c) -> dict:
    """{(role lower, weekday): {minute: people}} — the median, over the
    restaurant's own history dates of that weekday, of how many of each role
    were on at each half hour BEFORE the open and AFTER the close (P — found
    by the schedule benchmark once SP let the solver run at 120+ people):
    the staffing baseline is read between the open and the close, so a prep
    crew of five on from 7am for an 11am open was "measured" as nobody and
    drafted as half the first service hour's count from at most three hours
    out (8am, three cooks), 94 of 132 hours short of the usual crew. By
    role, never family: a prep cook's 7am is not a line cook's."""
    if c is None or not hist_rows:
        return {}
    by_date = defaultdict(list)
    for r in hist_rows:
        sp = _span(r)
        d = str(r.get("date") or "")[:10]
        if sp and len(d) == 10 and (r.get("employee") or "").strip() and (r.get("role") or "").strip():
            by_date[d].append((str(r["role"]).strip().lower(), sp))
    counts = defaultdict(lambda: defaultdict(list))        # (role, weekday) -> minute -> [people per date]
    dates_of = defaultdict(int)
    roles_of = defaultdict(set)
    for d, items in by_date.items():
        day = sq._day_name(d)
        o, cl = _rules.open_hours(c, day)
        if o is None or cl is None or is_24h(c, day):
            continue
        dates_of[day] += 1
        on = defaultdict(lambda: defaultdict(int))
        for role, (s, e) in items:
            roles_of[day].add(role)
            for t in range(s - s % SLOT, e, SLOT):
                if _covers(s, e, t) and (o - SHOULDER_HISTORY_MAX_MINUTES <= t < o
                                         or cl <= t < cl + SHOULDER_HISTORY_MAX_MINUTES):
                    on[role][t] += 1
        for role, cur in on.items():
            for t, n in cur.items():
                counts[(role, day)][t].append(n)
    out = {}
    for (role, day), cur in counts.items():
        n_dates = dates_of[day]
        med = {}
        for t, vals in cur.items():
            vals = sorted(vals + [0] * (n_dates - len(vals)))
            v = vals[len(vals) // 2]
            if v > 0:
                med[t] = v
        if med:
            out[(role, day)] = med
    return out


def _shoulders(curves, c, concurrent, requirements, profile, family, measured_history=None):
    """The half hours either side of service each role really works, in place
    on `curves`: the measured people on before the open and after the close —
    the concurrent baseline's slots outside service, and the restaurant's own
    history at each such half hour (history_shoulders, `measured_history`) —
    else the family's learned lead or stay (its punches), else the stated
    default (cooks an hour before the doors, servers half an hour, the dish
    pit and the closers 45 minutes after the close); and a role on in the
    last service hour is on until the close (a dishwasher no longer leaves at
    9:30 on a 10pm close). {(date, role): stay minutes}."""
    stays = {}
    if c is None:
        return stays
    fams = (profile or {}).get("families") or {}
    factor = {}
    for row in requirements or []:
        if row.get("daypart") in ("morning", "night"):
            factor[(row["date"], row["daypart"])] = float(row.get("factor") or 1.0)
    for (d, role), cur in sorted(curves.items()):
        if not cur:
            continue
        day = sq._day_name(d)
        if is_24h(c, day):
            continue
        o, cl = _rules.open_hours(c, day)
        fam = family(role)
        fp = fams.get(fam) or {}
        learned = getattr(c, "learned_stays", None) or {}
        lead_learned = getattr(c, "learned_leads", None) or {}
        measured = False
        for part in ("morning", "night"):
            figs = next((v for k, v in ((concurrent or {}).get((day, part)) or {}).items()
                         if str(k).strip().lower() == role.lower()), None) or {}
            f = factor.get((d, part), 1.0)
            for m, n in ((figs.get("slots") or {}) if isinstance(figs, dict) else {}).items():
                m = int(m)
                if (o is not None and m < o) or (cl is not None and m >= cl):
                    v = int(math.floor(float(n) * f + 0.5))
                    if v > 0:
                        cur[m] = max(cur.get(m, 0), v)
                        measured = True
        for m, v in ((measured_history or {}).get((role.lower(), day)) or {}).items():
            if (o is not None and m < o) or (cl is not None and m >= cl):
                cur[m] = max(cur.get(m, 0), int(v))
                measured = True
        first, last = min(cur), max(cur)
        if o is not None and not measured and first <= o + SLOT and first >= o:
            lead = lead_learned.get(fam)
            if lead is None:
                lead = fp.get("lead") if fp.get("lead") is not None else _obj.default_lead(role)
            lead = max(0, min(180, int(lead or 0)))
            n0 = cur[first]
            for t in range(o - (lead - lead % SLOT), o, SLOT):
                cur[t] = max(cur.get(t, 0), max(1, int(math.ceil(n0 / 2.0))))
        if cl is not None and last + SLOT >= cl - 60 and last < cl:
            for t in range(last + SLOT, cl, SLOT):
                cur[t] = max(cur.get(t, 0), 1)
        if cl is not None and max(cur) + SLOT >= cl - 60:
            closer = bool((getattr(c, "closers_by_role", None) or {}).get(fam))
            stay = learned.get(fam)
            if stay is None:
                stay = fp.get("stay") if fp.get("stay") is not None and fp.get("stay") > 0 else \
                    _obj.default_stay(role, closer=closer)
            stay = max(0, min(120, int(stay or 0)))
            stays[(d, role)] = stay
            if not measured:
                for t in range(cl, cl + stay - SLOT + 1, SLOT):
                    cur[t] = max(cur.get(t, 0), 1)
    return stays


def _round_the_clock(curves, c, dates, hist_rows, family):
    """A 24-hour day's people on at once over its whole business day, from
    the restaurant's own shifts: the staffing baseline is read between the
    open and midnight, so the graveyard after midnight had no need and one
    person was drafted on 10pm-6am. For each 24-hour date and role, the
    median over the history's same weekdays of the people on at each half
    hour from day_start for 24 hours, a shift from the night before
    counted into its small hours. In place on `curves`."""
    if c is None or not hist_rows:
        return
    by_date = defaultdict(list)
    for r in hist_rows:
        sp = _span(r)
        d = str(r.get("date") or "")[:10]
        if sp and d and (r.get("employee") or "").strip():
            by_date[d].append((sp, str(r.get("role") or "").strip()))
    from datetime import date as _date, timedelta as _td
    for d in dates or []:
        day = sq._day_name(d)
        if not is_24h(c, day):
            continue
        ds = day_start(c, day)
        hist_days = sorted(x for x in by_date if sq._day_name(x) == day)
        if not hist_days:
            continue
        counts = defaultdict(lambda: defaultdict(list))      # role -> t -> [n per week]
        roles = set()
        for hd in hist_days:
            prev = (_date.fromisoformat(hd) - _td(days=1)).isoformat()
            items = [(sp, role) for sp, role in by_date[hd]] + \
                [((sp[0] - 1440, sp[1] - 1440), role) for sp, role in by_date.get(prev, ())]
            here = defaultdict(lambda: defaultdict(int))
            for (s, e), role in items:
                roles.add(role)
                for t in range(ds, ds + 1440, SLOT):
                    if _covers(s, e, t):
                        here[role][t] += 1
            for role in roles:
                for t in range(ds, ds + 1440, SLOT):
                    counts[role][t].append(here[role][t])
        for role in sorted(roles):
            if _rules.manager_role_kind(role) in ("owner", "manager"):
                continue
            key = next((k for k in curves if k[0] == d and family(k[1]) == family(role)), (d, role))
            cur = curves.setdefault(key, {})
            for t, ns in counts[role].items():
                ns = sorted(ns + [0] * (len(hist_days) - len(ns)))
                med = ns[len(ns) // 2]
                if med > 0:
                    cur[t] = max(cur.get(t, 0), med)


def _presence(hist_rows, family) -> dict:
    """{(family, weekday, minute): share of that weekday's history dates the
    family had somebody on at that half hour} — where the restaurant is
    never without a role (its firm part). A shift past midnight counts on
    its own date's timeline. One reading with every pass after the draft:
    schedule_objective.presence."""
    return _obj.presence(hist_rows, family=family)


def _assumed_roles(dates, curves, c, roster_roles, profile, family, open_times, close_times):
    """Roles on the roster with no history and no requirement: a new
    restaurant's dishwasher was never staffed. Each gets one person on
    through each daypart it is open, said in `assumed` so the owner sets its
    floor. [(family, role)]; in place on `curves`."""
    if c is None:
        return []
    fams_hist = set(((profile or {}).get("families") or {}))
    present = {family(r) for (_d, r) in curves}
    roles = {}
    for n, r in sorted((roster_roles or {}).items()):
        if (r or "").strip():
            roles.setdefault(family(r), r.strip())
    for k, rs in sorted((getattr(c, "known_roles", None) or {}).items()):
        for r in sorted(rs or ()):
            if str(r).strip():
                roles.setdefault(family(r), (getattr(c, "role_names", None) or {}).get(r, str(r).strip().title()))
    managers = {family(r) for r in (getattr(c, "managers", None) or {}).values() if r}
    out = []
    for fam, role in sorted(roles.items()):
        if not fam or fam in present or fam in fams_hist or fam in managers:
            continue
        if _rules.manager_role_kind(role) in ("owner", "manager", "trainee"):
            continue
        added = False
        for d in dates or []:
            day = sq._day_name(d)
            if d in (getattr(c, "closed_dates", None) or ()):
                continue
            o, cl = _rules.open_hours(c, day)
            if o is None or cl is None:
                continue
            cur = curves.setdefault((d, role), {})
            for t in range(o - o % SLOT, cl, SLOT):
                cur[t] = max(cur.get(t, 0), 1)
            added = True
        if added:
            out.append((fam, role))
    return out


def _available(c, role, d, roster_roles) -> int:
    """How many people may legally work `role` on `d` (can_work, fillable,
    holds) — the count the shortfall's reason names."""
    if c is None:
        return 99
    n = 0
    for name in c.roster_names or []:
        if not _holds_family(c, name, role, d, roster_roles):
            continue
        if c.can_work(name, d)[0] and c.fillable(name, d)[0]:
            n += 1
    return n


def _holds_family(c, name, role, d, roster_roles) -> bool:
    if not c.holds(name, role, d):
        return False
    mine = (c.known_roles or {}).get(c.key(name))
    if not mine and (roster_roles or {}).get(name):
        return c.family((roster_roles or {}).get(name)) == c.family(role)
    if mine:
        return c.family(role) in {c.family(x) for x in mine}
    return False


def _nobody_reason(c, role, d, roster_roles, rows=None, part=None) -> str:
    """Why nobody may work `role` on `d`: the reasons of the people who hold
    it, counted. With the week's `rows` and the shift's `part`, a holder who
    could legally take a shift of it over the core window that day
    (Constraints.can_add) is named instead — the role was left empty, not
    unfillable: EJ's Saturday 11/7/26 read "nobody who works it can take a
    shift" while its barback had 11.5 hours that week and a free night."""
    why = Counter()
    holders = 0
    free = []
    for name in sorted(c.roster_names or []):
        if not _holds_family(c, name, role, d, roster_roles):
            continue
        holders += 1
        ok, w = c.can_work(name, d)
        if ok:
            ok, w = c.fillable(name, d)
        if not ok:
            why[str(w or "unavailable")] += 1
        else:
            free.append(name)
    if not holders:
        return f"nobody on the roster works {role}"
    if free and rows is not None and part in ("morning", "night"):
        day = sq._day_name(d)
        win = sq.floor_window(part, _rules.parse_minutes((getattr(c, "open_times", None) or {}).get(day) or ""),
                              _rules.parse_minutes((getattr(c, "close_times", None) or {}).get(day) or ""))
        if win:
            probe = {"date": d, "day": day, "role": role, "shift_start": _fmt(win[0] % 1440),
                     "shift_end": _fmt(win[1] % 1440), "notes": ""}
            probe["scheduled_hours"] = _stime.row_hours_text(probe, getattr(c, "tz", None))
            could = [n for n in free if c.can_add(dict(probe, employee=n), rows)[0]]
            if could:
                return (f"{_names_said(could)} could work it — the draft left it empty "
                        f"(the labor budget or a costlier gap came first)")
    if not why:
        return "nobody who works it can take a shift that fits its hours"
    bits = [f"{n} {w}" for w, n in sorted(why.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    return f"all {holders} who work {role}: " + "; ".join(bits)


def uncovered_roles(rows, requirements, c, roster_roles=None) -> list:
    """[{date, day, role, needed, why}] — each open date a role the week's
    requirements need (people or a floor) has NOBODY of its family on at
    all, with why nobody can work it (a week with no server any day and no
    cook on four read as complete because a role nobody could work dropped
    out of the model). Any draft source; the rows as they stand."""
    if c is None:
        return []
    fam = c.family
    on = {(r.get("date"), fam(r.get("role"))) for r in rows or [] if (r.get("employee") or "").strip()}
    need = {}
    for row in requirements or []:
        d = row.get("date")
        if not d or d in (getattr(c, "closed_dates", None) or ()):
            continue
        for e in row.get("roles") or []:
            n = max(int(e.get("required") or 0), int(e.get("floor") or 0))
            role = str(e.get("role") or "").strip()
            if n > 0 and role and _rules.manager_role_kind(role) not in ("owner", "manager"):
                k = (d, fam(role))
                if k not in need or n > need[k][1]:
                    need[k] = (role, n, row.get("daypart"))
    out = []
    for (d, f), (role, n, part) in sorted(need.items()):
        if (d, f) in on:
            continue
        out.append({"date": d, "day": sq._day_name(d), "role": role, "needed": n,
                    "why": _nobody_reason(c, role, d, roster_roles, rows=list(rows or []), part=part)})
    return out


def _names_said(names) -> str:
    """'Ana', 'Ana and Bo', 'Ana, Bo and 2 more'."""
    names = list(names)
    if len(names) <= 2:
        return " and ".join(names)
    return f"{names[0]}, {names[1]} and {len(names) - 2} more"


def uncovered_line(items) -> str:
    """The review's "⚠" line for uncovered_roles — one per week, the days
    named, so the publish gate holds the week until the owner has seen it."""
    if not items:
        return ""
    from time_utils import mdy
    bits = []
    for it in items[:6]:
        bits.append(f"{it['day']} {mdy(it['date'])}: no {it['role']} on at all (needs {it['needed']}) — {it['why']}")
    more = len(items) - 6
    return ("⚠ Roles with nobody on: " + "; ".join(bits) + (f"; and {more} more" if more > 0 else "")
            + ". Find cover, move time off, or lower that role's need for those days.")


# ── the people ───────────────────────────────────────────────────────────

class _Person:
    __slots__ = ("i", "name", "key", "fams", "rates", "cap", "line", "ceil", "min_h", "salaried", "minor",
                 "rules", "rest", "run", "doubles", "busy", "fixed_h", "fixed_week", "tail_ords", "law_rest",
                 "law_run", "usual_h", "days_target", "rate")


def _people(c, roster_roles, dates, ctx, context_rows, exclude, comp):
    """The people Stage 1 may choose, with what binds each."""
    out = []
    if c is None:
        return out
    law_rest = _rules.law_backed(c, "min_rest_hours")
    law_run = _rules.law_backed(c, "max_consecutive_days")
    try:
        need_rest = float(comp.get("min_rest_hours") or 0) if _rules.rest_mode(comp) != "off" else 0.0
    except (TypeError, ValueError):
        need_rest = 0.0
    try:
        max_run = int(comp.get("max_consecutive_days") or 0)
    except (TypeError, ValueError):
        max_run = 0
    week = set(dates or [])
    ctx_by = defaultdict(list)
    for r in context_rows or []:
        name = (r.get("employee") or "").strip()
        if name:
            ctx_by[c.key(name)].append(r)
    seen = set()
    for name in sorted(c.roster_names or []):
        k = c.key(name)
        if not k or k in seen or k in (exclude or ()):
            continue
        seen.add(k)
        mine = (c.known_roles or {}).get(k) or ({(roster_roles or {}).get(name)} - {None, ""})
        fams = {c.family(x) for x in mine if str(x or "").strip()}
        if not fams:
            continue
        p = _Person()
        p.i = len(out)
        p.name, p.key, p.fams = name, k, fams
        p.salaried = bool(c.is_salaried(name))
        p.rate = 0.0 if p.salaried else None
        if p.salaried or c.own_max(name):
            p.cap = float(c.max_hours(name))
        else:
            p.cap = float(_rules.SALARIED_HOURS_MAX)
        p.minor = k in (c.minors or {})
        if p.minor:
            br = _rules.minor_rules((c.minor_bands or {}).get(k), c.jurisdiction)
            if br.get("max_weekly_school_week"):
                p.cap = min(p.cap, float(br["max_weekly_school_week"]))
        try:
            p.line = float(_rules.overtime_line(c, name))
        except Exception:
            p.line = 40.0
        p.rules = _rules.hours_rules_apply(c, name)
        p.ceil = (float(comp.get("weekly_hours_ceiling") or _rules.DEFAULTS["weekly_hours_ceiling"])
                  if (p.rules and not p.salaried and not c.own_max(name)) else None)
        mn = c.min_hours(name)
        p.min_h = float(mn) if (mn and p.rules) else 0.0
        p.rest = need_rest if (need_rest and _rules.rest_rule_applies(c, name)) else 0.0
        p.run = max_run if (max_run and p.rules) else 0
        p.law_rest, p.law_run = law_rest, law_run
        p.doubles = ctx.doubles_ok(name, c) if ctx is not None else False
        p.busy = defaultdict(list)
        p.fixed_h = defaultdict(float)
        p.fixed_week = 0.0
        for r in ctx_by.get(k, ()):
            sp = _rules._span(r)
            if sp and r.get("date"):
                p.busy[r["date"]].append(sp)
            if r.get("date") in week:
                h = _rules.row_hours(r)
                p.fixed_h[c.bucket(r["date"])] += h
                p.fixed_week += h
        for b, h in ((getattr(c, "base_hours", None) or {}).get(k) or {}).items():
            p.fixed_h[b] += float(h or 0)
        p.tail_ords = set()
        for r in (getattr(c, "base_rows", None) or {}).get(k) or []:
            o = _ordinal(r.get("date"))
            if o is not None:
                p.tail_ords.add(o)
        p.usual_h = ctx.usual_hours(name) if ctx is not None else 0.0
        p.days_target = ctx.days_target(name) if ctx is not None else _obj.DEFAULT_DAYS_A_WEEK
        out.append(p)
    return out


def _ordinal(d):
    try:
        from datetime import datetime as _dt
        return _dt.strptime(str(d)[:10], "%Y-%m-%d").toordinal()
    except (TypeError, ValueError):
        return None


def _day_ok(c, p, d, cache):
    """{daypart: True} the person may work on `d` at all (can_work,
    fillable by part), or {} for none."""
    key = (p.key, d)
    if key in cache:
        return cache[key]
    ok_day, _w = c.can_work(p.name, d)
    parts = {}
    if ok_day:
        for part in ("morning", "night"):
            a, _w1 = c.can_work(p.name, d, part)
            b, _w2 = c.fillable(p.name, d, part)
            if a and b:
                parts[part] = True
    cache[key] = parts
    return parts


def _fits(c, p, d, role, s, e, parts_ok, txt):
    """Whether person p may legally work `role` s-e on `d` (beyond the
    day's dayparts, checked by the caller)."""
    if not c.holds(p.name, role, d):
        return False
    ok, _w = c.cert_ok(p.name, role, d)
    if not ok:
        return False
    if (c.time_windows or {}).get(p.key) or ((c.blocked_parts or {}).get(p.key) or {}).get(d):
        ok, _w = c.window_ok(p.name, d, txt[0], txt[1])
        if not ok:
            return False
    if getattr(c, "trainees", None) and c.training_row({"date": d, "role": role, "employee": p.name,
                                                       "shift_start": txt[0], "shift_end": txt[1]}):
        return False
    if p.minor:
        latest, _l = _rules.minor_latest(c, p.key, d)
        if latest is not None and e > latest:
            return False
        earliest = _rules.minor_earliest(c, p.key)
        if earliest is not None and s < earliest:
            return False
        mcap, _t = _rules.minor_daily_cap(c, p.key, d)
        if mcap and (e - s) / 60.0 > float(mcap) + 0.01:
            return False
    for bs, be in p.busy.get(d, ()):
        if s < be and bs < e:
            return False
        if not p.doubles:
            return False
        if s < be + DOUBLE_BREAK_MINUTES and bs < e + DOUBLE_BREAK_MINUTES:
            return False
    if p.rest and p.law_rest:
        o = _ordinal(d)
        for dd, spans in p.busy.items():
            od = _ordinal(dd)
            if od is None or o is None or abs(od - o) != 1:
                continue
            for bs, be in spans:
                gap = (s + 1440 - be) if od < o else (bs + 1440 - e)
                if gap < p.rest * 60:
                    return False
    return True

def _length_bounds(c, role, d, roster_roles, min_h, max_h) -> tuple:
    """(shortest, longest) hours a shape of `role` on `d` may be, from the
    people who may legally work it: the one shortest shift lowered to a
    minor's legal day (a 14-15 year old's 3h school-day cap is a shift, not
    nothing — schedule_rules.shortest_for), and no shape longer than the
    longest day anybody available may work (three 16-17 servers capped at 8h
    got 0h when every shape was 11am-10pm)."""
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
               demand_curve=None, max_seconds=DEFAULT_SECONDS, seed=7, staffing_value=None, person_rates=None,
               objective=None, exclude=None, workers=None, wall_cap=None, owner_person_rates=None,
               person_job_rates=None, role_typical=None) -> dict:
    """The week's shift shapes: {"rows": [slot rows, no employee, `_open`,
    `_floor` where a floor needs them, `_s1` the person Stage 1 chose — the
    hint Stage 2 starts from], "short": [{date, role, minutes, people, why,
    kind: "nobody" | "short", usd}], "assumed": [{family, role, text}],
    "stats": {...}}. `context_rows` (the manager plan, a redo's kept days,
    locked rows) count toward coverage, bound the span and keep their people
    busy. `staffing_value` — {(family, daypart): {"per_hour", "lo", "hi"}}
    (staffing_elasticity.marginal_values): one person over the demand earns
    that net value back where it is measurably clear of zero. `objective` a
    schedule_objective.ObjectiveContext (built here from the same inputs
    when not passed); `exclude` people code may not choose (an unread note).
    `max_seconds` is a deterministic work budget; `wall_cap` the emergency
    wall-clock cap. A person's wage is the forward chain
    (schedule_objective.forward_rate_source) when the context carries its
    maps (`owner_person_rates` / `person_job_rates`, audit #23 A3-02)."""
    c = constraints
    out = {"rows": [], "short": [], "assumed": [], "stats": {"status": "nothing_to_solve"}}
    if not available() or not dates:
        out["stats"]["status"] = "unavailable" if not available() else "nothing_to_solve"
        return out
    family = (c.family if c is not None else sq.role_family)
    open_times = getattr(c, "open_times", None) or {}
    close_times = getattr(c, "close_times", None) or {}
    sources = {}
    curves = demand_curves(dates, requirements, concurrent, demand_curve, open_times, close_times, sources=sources)
    comp = getattr(c, "compliance", None) or {}
    hist_rows = list(history_rows or []) + list(last_week_rows or [])
    ctx = objective
    if ctx is None:
        ctx = _obj.ObjectiveContext.build(None, dates, budget=budget_dollars, last_week_rows=last_week_rows,
                                          requirements=requirements, demand_curve=demand_curve,
                                          history_rows=hist_rows, rates=rates, person_rates=person_rates,
                                          blended_rate=blended_rate, constraints=c,
                                          owner_person_rates=owner_person_rates,
                                          person_job_rates=person_job_rates, role_typical=role_typical)
    prof = ctx.profile
    # The one shortest shift (contract #2, schedule_rules.min_shift_minutes); per
    # role and date it is lowered to what the people who may work it can
    # legally work (a 14-15 year old's 3h school day).
    min_h = _rules.min_shift_minutes(c) / 60.0
    max_h = min(float(comp.get("max_shift_hours") or DEFAULT_MAX_SHIFT), DEFAULT_MAX_SHIFT + 2)
    hist = history_patterns(hist_rows, family)
    bounds = plan_bounds(context_rows, c)
    closed = set(getattr(c, "closed_dates", None) or ())
    _round_the_clock(curves, c, dates, hist_rows, family)
    stays = _shoulders(curves, c, concurrent, requirements, prof, family,
                       measured_history=history_shoulders(hist_rows, c))
    assumed = _assumed_roles(dates, curves, c, roster_roles, prof, family, open_times, close_times)
    for fam_a, role_a in assumed:
        out["assumed"].append({"family": fam_a, "role": role_a,
                               "text": f"{role_a}: no shift history and no floor — drafted one on through the "
                                       "open hours; set its floor so the next draft knows"})
    week_ords = {d: _ordinal(d) for d in dates}
    base_ord = min(o for o in week_ords.values() if o is not None) if any(week_ords.values()) else 0

    def absm(d, t):
        return ((week_ords.get(d) or base_ord) - base_ord) * 1440 + int(t)

    # A 24-hour day's small hours are the night before's.
    for (d, role), cur in list(curves.items()):
        day = sq._day_name(d)
        if c is not None and is_24h(c, day):
            ds = day_start(c, day)
            for t in sorted(t for t in cur if t < ds):
                n0 = cur.pop(t)
                prev = [x for x in dates if week_ords.get(x) == (week_ords.get(d) or 0) - 1]
                key = (prev[0], role) if prev else (d, role)
                tgt = curves.setdefault(key, {})
                # the week's first small hours are last week's night: the
                # first date's own stand in for its tail
                tgt[t + 1440] = max(tgt.get(t + 1440, 0), n0)
    # coverage already on the week (the plan, kept and locked rows)
    fixed_on = defaultdict(lambda: defaultdict(int))
    for r in context_rows or []:
        sp = _rules._span(r)
        if not sp or not (r.get("employee") or "").strip() or r.get("date") not in week_ords:
            continue
        fam = family(r.get("role"))
        for t in range(sp[0] - sp[0] % SLOT, sp[1], SLOT):
            if _covers(sp[0], sp[1], t):
                fixed_on[fam][absm(r.get("date"), t)] += 1
    # The managers are the manager plan's (schedule_skeleton, in
    # context_rows): Stage 1 shapes everybody else.
    for k in [k for k in curves if _rules.manager_role_kind(k[1]) in ("owner", "manager")]:
        curves.pop(k)
    # The demand Stage 1 shapes to, half hour by half hour — the one
    # objective's need for this week: the trim, the top-up and the
    # optimizer price a hole against the same people-on-at-once the shifts
    # were drawn for, never only the requirement rows' smaller need (the
    # trim used to cut 24-hour-diner coverage Stage 1 had bought at the
    # same prices).
    out["need"] = {(d, role): {int(t): int(n) for t, n in cur.items() if n}
                   for (d, role), cur in curves.items() if d not in closed and cur}
    roles_by_date = defaultdict(set)
    for (d, role) in curves:
        if d not in closed:
            roles_by_date[d].add(role)
    floor_codes = {}
    for d in dates:
        if d in closed:
            continue
        day = sq._day_name(d)
        for (fam, part), (need, code) in (_rules.floor_needs(c, day) if c is not None else {}).items():
            floor_codes[(d, fam, part)] = (need, code)
            if not any(family(r) == fam for r in roles_by_date[d]):
                roles_by_date[d].add(code)
    exclude = {c.key(n) for n in (exclude or ())} if c is not None else set()
    people = _people(c, roster_roles, dates, ctx, context_rows, exclude, comp)
    rates_l = {str(k).strip().lower(): float(v) for k, v in (rates or {}).items() if v and k != "_default"}
    blended = float(blended_rate or 0) or (sum(rates_l.values()) / len(rates_l) if rates_l else 15.0)
    prates = {" ".join(str(k).lower().split()): float(v) for k, v in (person_rates or {}).items() if v}
    # The forward chain when the objective carries its maps (rate_maps).
    forward = (getattr(ctx, "owner_person_rates", None) is not None
               or getattr(ctx, "person_job_rates", None) is not None)

    def wage(p, role):
        if p is None:
            return rates_l.get(role.strip().lower()) or rates_l.get(family(role)) or blended
        if p.salaried:
            return 0.0
        if forward:
            r_, f_ = role.strip().lower(), family(role)
            return _obj.forward_rate(p.name, role, people=ctx.person_rates, owner=ctx.owner_person_rates,
                                     jobs=ctx.person_job_rates,
                                     role_rate=ctx.owner_role_rates.get(r_) or ctx.owner_role_rates.get(f_) or 0.0,
                                     typical_rate=ctx.role_typical.get(r_) or ctx.role_typical.get(f_) or 0.0,
                                     fallback=blended)
        return prates.get(" ".join(p.name.lower().split())) or rates_l.get(role.strip().lower()) or \
            rates_l.get(family(role)) or blended
    last_shapes = Counter()
    for r in last_week_rows or []:
        sp = _span(r)
        if sp:
            last_shapes[(sq._day_name(r.get("date") or ""), str(r.get("role") or "").strip().lower(),
                         _q(sp[0]), _q(sp[1]))] += 1
    last_slots = defaultdict(list)
    for r in last_week_rows or []:
        sp = _span(r)
        name = (r.get("employee") or "").strip()
        if sp and name and c is not None:
            last_slots[(c.key(name), sq._day_name(r.get("date") or ""), family(r.get("role")))].append(sp[0])
    # Every job code in use: a shape is drafted under its family's code for
    # the half of the day it is mainly on (sq.code_for_daypart) — an evening
    # host shift is a Host PM, never a Host AM because Host AM priced lower
    # (EJ's week of 11/4/26: three Host AM shifts at dinner and no Host PM).
    fold = sq.fold_table({role for roles in roles_by_date.values() for role in roles}
                         | {str(v) for v in (roster_roles or {}).values()}
                         | {(r.get("role") or "").strip() for r in hist_rows}, getattr(c, "role_families", None))
    m = _cp.CpModel()
    obj = []
    pats = {}
    pat_roles = {}                       # (date, role) -> [the code each pattern is drafted under]
    n = {}
    z = {}
    z_by_pd = defaultdict(list)          # (person i, date) -> [(var, s, e, hours, role, k)]
    on_at = defaultdict(list)            # (family, abs t) -> [(n var, role)]
    late_by = defaultdict(list)          # (date, family) -> [(person, var, end)]
    dollars = []
    obj_dollars = []                     # (cents, var): the wage terms, read back for the budget's verdict
    day_cache = {}
    short_meta = []
    at_meta = {}
    for d in sorted(roles_by_date):
        day = sq._day_name(d)
        for role in sorted(roles_by_date[d]):
            demand = curves.get((d, role)) or {}
            fam = family(role)
            templates = fam not in (prof.get("families") or {})
            lo_h, hi_h = _length_bounds(c, role, d, roster_roles, min_h, max_h)
            ps = patterns_for(role, d, demand, hist, c, bounds, lo_h, hi_h, getattr(c, "role_times", None),
                              stay=stays.get((d, role), 0), templates=templates)
            ps = ps[:PERSON_PATTERNS]
            if not ps:
                continue
            txt = [(_fmt(s), _fmt(e)) for s, e in ps]
            parts_of = [set(sq.touched_dayparts({"shift_start": a, "shift_end": b})) - {"unknown"} for a, b in txt]
            codes_k = [sq.code_for_daypart(role, (sq.present_dayparts({"date": d, "shift_start": a, "shift_end": b})
                                                  or [None])[0], table=fold) for a, b in txt]
            hist_here = hist.get((fam, day)) or Counter()
            htot = float(sum(hist_here.values()) or 1)
            kvars = defaultdict(list)
            for p in people:
                if fam not in p.fams or c is None:
                    continue
                ok_parts = _day_ok(c, p, d, day_cache)
                if not ok_parts:
                    continue
                if not p.doubles and p.busy.get(d):
                    continue
                for k, (s, e) in enumerate(ps):
                    if not parts_of[k] <= set(ok_parts):
                        continue
                    if not _fits(c, p, d, codes_k[k], s, e, ok_parts, txt[k]):
                        continue
                    w_ = wage(p, codes_k[k])
                    v = m.new_bool_var(f"z{p.i}_{d}_{role}_{k}")
                    z[(p.i, d, role, k)] = v
                    kvars[k].append(v)
                    hours = (e - s) / 60.0
                    z_by_pd[(p.i, d)].append((v, s, e, hours, role, k))
                    cents = _cents(hours * w_)
                    if cents:
                        dollars.append(cents * v)
                        obj_dollars.append((cents, v))
                        obj.append((cents, v))
                    shape_c = _cents(ctx.length_usd(role, s, e))
                    shape_c += _cents(ctx.usual_slot_usd(p.name, d, sq.present_dayparts(
                        {"shift_start": txt[k][0], "shift_end": txt[k][1]})))
                    hn = hist_here.get((s, e), 0)
                    if hn:
                        shape_c -= _cents(_obj.HISTORY_PATTERN_USD * min(1.0, 3.0 * hn / htot))
                    if any(abs(s0 - s) <= _obj.STABILITY_START_MINUTES
                           for s0 in last_slots.get((p.key, day, fam), ())):
                        shape_c -= _cents(_obj.STABILITY_USD)
                    if shape_c:
                        obj.append((shape_c, v))
                    if c.closers_by_role and p.key in ((c.closers_by_role or {}).get(fam) or ()):
                        late_by[(d, fam)].append((p, v, e))
            if not kvars:
                if d not in closed and demand:
                    short_meta.append({"date": d, "role": role, "people": max(demand.values()),
                                       "minutes": SLOT * len(demand), "kind": "nobody",
                                       "why": _nobody_reason(c, role, d, roster_roles) if c is not None else
                                       "nobody may work it"})
                continue
            pats[(d, role)] = ps
            pat_roles[(d, role)] = codes_k
            for k in sorted(kvars):
                s, e = ps[k]
                v = m.new_int_var(0, len(kvars[k]), f"n{d}_{role}_{k}")
                m.add(v == sum(kvars[k]))
                n[(d, role, k)] = v
                # last published week's shapes, whoever worked them: a shape
                # changed costs STABILITY_USD (the week is stable even where
                # the people move)
                last_n = last_shapes.get((day, role.strip().lower(), s, e), 0)
                if last_n:
                    dev = m.new_int_var(0, max(last_n, len(kvars[k])), f"st{d}_{role}_{k}")
                    m.add(dev >= v - last_n)
                    m.add(dev >= last_n - v)
                    obj.append((_cents(_obj.STABILITY_USD), dev))
                for t in range(s - s % SLOT, e, SLOT):
                    if _covers(s, e, t):
                        a = absm(d, t)
                        on_at[(fam, a)].append((v, role))
                        at_meta.setdefault((fam, a), (d, t))
    if not n:
        out["short"] = short_meta
        out["stats"] = {"status": "nothing_to_solve", "patterns": 0, "people": len(people)}
        return out
    # the demand, half hour by half hour, by family (a family's job codes
    # are one crew at a moment): short x slot_value
    need_by = defaultdict(int)
    role_at = {}
    for (d, role), cur in curves.items():
        if d in closed:
            continue
        fam = family(role)
        for t, k in cur.items():
            a = absm(d, t)
            need_by[(fam, a)] += int(k)
            role_at.setdefault((fam, a), (d, t, role))
    presence = _presence(hist_rows, family)
    shorts = {}
    for (fam, a), need in sorted(need_by.items()):
        have = [v for v, _r in on_at.get((fam, a), ())]
        gap = need - fixed_on[fam].get(a, 0)
        if gap <= 0:
            continue
        d, t, role = role_at[(fam, a)]
        sh = m.new_int_var(0, gap, f"sh{fam}_{a}")
        if have:
            m.add(sum(have) + sh >= gap)
        else:
            m.add(sh >= gap)
        shorts[(fam, a)] = (sh, d, t, role)
        obj.append((max(1, _cents(_obj.slot_value(ctx, d, t, role))), sh))
        # The firm part: where the restaurant is never without the role at
        # this half hour (on in FIRM_PRESENCE of its same weekdays), its
        # first person is the requirement tier — above the budget — and only
        # the rest of the demand is priced against it.
        if fixed_on[fam].get(a, 0) == 0 and presence.get((fam, sq._day_name(d), t), 0.0) >= FIRM_PRESENCE:
            first = m.new_bool_var(f"fm{fam}_{a}")
            m.add(sh <= gap - 1 + first)
            obj.append((W_FIRM_SLOT, first))
        part_t = _stime.daypart_at(t)
        val = (staffing_value or {}).get((fam, part_t)) or {}
        try:
            net = float(val.get("per_hour") or 0) if float(val.get("lo") or 0) > 0 else 0.0
        except (TypeError, ValueError):
            net = 0.0
        if net > 0 and have:
            extra = m.new_bool_var(f"ex{fam}_{a}")
            m.add(sum(have) >= gap + extra)
            w_ = wage(None, role)
            obj.append((-_cents((net + w_) / 2.0), extra))
    # the requirement's distinct count — only where no half-hour curve
    # exists, and only shapes mainly in that daypart
    for row in requirements or []:
        d = row.get("date")
        if row.get("daypart") not in ("morning", "night") or d not in roles_by_date:
            continue
        for entry in row.get("roles") or []:
            role = str(entry.get("role") or "").strip()
            if sources.get((d, role)) != "spread" or (d, role) not in pats:
                continue
            want = int(entry.get("firm", entry.get("required")) or 0)
            if want <= 0:
                continue
            present = [n[(d, role, k)] for k, (s, e) in enumerate(pats[(d, role)]) if (d, role, k) in n
                       and sq.present_dayparts({"date": d, "shift_start": _fmt(s), "shift_end": _fmt(e)})[0]
                       == row["daypart"]]
            if not present:
                continue
            sh = m.new_int_var(0, want, f"di{d}_{role}_{row['daypart']}")
            m.add(sum(present) + sh >= want)
            obj.append((_cents(_obj.SHORT_PERSON_HOUR_USD * 2 * ctx.criticality(role)), sh))
    # floors: people of the family on at once over the core window
    for (d, fam, part), (need, code) in sorted(floor_codes.items()):
        day = sq._day_name(d)
        o = _rules.parse_minutes(open_times.get(day) or "")
        cl = _rules.parse_minutes(close_times.get(day) or "")
        for t in sq.floor_slots(part, o, cl):
            a = absm(d, t)
            have = [v for v, _r in on_at.get((fam, a), ())]
            gap = need - fixed_on[fam].get(a, 0)
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
        by_t = defaultdict(list)
        for (fam, a), items in sorted(on_at.items()):
            for v, role in items:
                if _curve.capped_role(role, cap_roles, getattr(c, "role_families", None)):
                    by_t[a].append(v)
        for a, vs in sorted(by_t.items()):
            over = m.new_int_var(0, len(vs) * 10, f"cap{a}")
            m.add(sum(vs) - over <= cap)
            obj.append((W_CAP_SLOT, over))
    for (fam, a), items in sorted(on_at.items()):
        role = items[0][1]
        d, t = at_meta[(fam, a)]
        part = _stime.daypart_at(t)
        try:
            import schedule_requirements as _sreq
            most = _sreq._role_cap(getattr(c, "role_caps", None) or {}, role, sq._day_name(d), part)
        except Exception:
            most = None
        if most is not None:
            over = m.new_int_var(0, 50, f"rc{fam}_{a}")
            m.add(sum(v for v, _r in items) - over <= int(most))
            obj.append((W_CAP_SLOT, over))
    # closers: one of the family's closers on until the close + stay
    if c is not None and c.closers_by_role and c.compliance.get("keyholder_until_close", True):
        for d in sorted(roles_by_date):
            day = sq._day_name(d)
            for fam in sorted({family(r) for r in roles_by_date[d]}):
                if not (c.closers_by_role or {}).get(fam) or not _rules._closer_rule_runs(c, fam, d):
                    continue
                last = max((e for role in roles_by_date[d] if family(role) == fam
                            for (s, e) in pats.get((d, role), ())), default=0)
                if not last:
                    continue
                need, _until = _rules.closer_need(c, fam, day, last)
                if any(sp[1] >= need - 15 and r.get("date") == d and family(r.get("role")) == fam
                       and c.key(r.get("employee")) in (c.closers_by_role or {}).get(fam, ())
                       for r in context_rows or [] for sp in [_rules._span(r)] if sp):
                    continue
                late = [v for p, v, e in late_by.get((d, fam), ()) if e >= need - 15]
                gap = m.new_bool_var(f"cl{d}_{fam}")
                m.add(sum(late) + gap >= 1)
                obj.append((W_CLOSER, gap))
    # each person: shifts a day, hours, overtime, minimum, days, rest, runs
    daily_line = 0.0
    try:
        daily_line = float(comp.get("daily_ot_hours") or 0)
    except (TypeError, ValueError):
        daily_line = 0.0
    worked = {}
    for p in people:
        items_week = []
        by_bucket = defaultdict(list)
        for d in sorted(dates):
            items = z_by_pd.get((p.i, d)) or []
            busy = 1 if p.busy.get(d) else 0
            if not items:
                if busy:
                    worked[(p.i, d)] = 1
                continue
            vs = [it[0] for it in items]
            if p.doubles:
                # two legs at most, never overlapping, a break between them
                m.add(sum(vs) <= 2 - busy)
                for t0 in sorted({it[1] for it in items}):
                    clique = [it[0] for it in items if it[1] <= t0 < it[2] + DOUBLE_BREAK_MINUTES]
                    if len(clique) > 1:
                        m.add(sum(clique) <= 1)
                w = m.new_bool_var(f"w{p.i}_{d}")
                for v in vs:
                    m.add_implication(v, w)
                m.add(w <= sum(vs))
                worked[(p.i, d)] = w if not busy else 1
            else:
                m.add(sum(vs) <= 1)
                worked[(p.i, d)] = sum(vs) if not busy else 1
            if daily_line and p.rules:
                tot = sum(int(round(it[3] * 100)) * it[0] for it in items)
                top = sum(int(round(it[3] * 100)) for it in items)
                if top > daily_line * 100:
                    ot_d = m.new_int_var(0, top, f"dot{p.i}_{d}")
                    m.add(ot_d >= tot - int(round(daily_line * 100)))
                    obj.append((W_OT_HUNDREDTH, ot_d))
            for it in items:
                by_bucket[c.bucket(d)].append(it)
                items_week.append(it)
        if not items_week:
            continue
        for b, its in sorted(by_bucket.items()):
            base = int(round(p.fixed_h.get(b, 0.0) * 100))
            expr = sum(int(round(it[3] * 100)) * it[0] for it in its) + base
            top = base + sum(int(round(it[3] * 100)) for it in its)
            capv = int(round(p.cap * 100))
            if top > capv:
                m.add(expr <= capv)
            if not p.salaried:
                line = int(round(p.line * 100))
                if top > line:
                    ot = m.new_int_var(0, top - line, f"ot{p.i}_{b}")
                    m.add(ot >= expr - line)
                    obj.append((W_OT_HUNDREDTH, ot))
                if p.ceil:
                    ce = int(round(p.ceil * 100))
                    if top > ce:
                        ov = m.new_int_var(0, top - ce, f"ce{p.i}_{b}")
                        m.add(ov >= expr - ce)
                        obj.append((W_POLICY_HUNDREDTH, ov))
        wexpr = sum(int(round(it[3] * 100)) * it[0] for it in items_week)
        wfix = int(round(p.fixed_week * 100))
        if p.min_h:
            need = int(round(p.min_h * 100))
            if wfix < need:
                sh = m.new_int_var(0, need - wfix, f"mn{p.i}")
                m.add(sh >= need - wexpr - wfix)
                obj.append((W_MIN_HOUR_HUNDREDTH, sh))
        if p.usual_h:
            want = int(round(p.usual_h * (1 - _obj.USUAL_HOURS_BAND) * 100))
            if wfix < want:
                under = m.new_int_var(0, want - wfix, f"uh{p.i}")
                m.add(under >= want - wexpr - wfix)
                obj.append((max(1, _cents(_obj.USUAL_HOURS_USD / 100.0)), under))
            # past half their usual week, the objective's starvation price
            # (schedule_objective.STARVED_HOURS_USD — one objective)
            half = int(round(p.usual_h * _obj.STARVED_SHARE * 100))
            if p.usual_h >= _obj.STARVED_MIN_USUAL_HOURS and wfix < half:
                starved = m.new_int_var(0, half - wfix, f"st{p.i}")
                m.add(starved >= half - wexpr - wfix)
                obj.append((max(1, _cents(_obj.starved_hour_usd(ctx, p.name) / 100.0)), starved))
        wds = [worked[(p.i, d)] for d in sorted(dates) if (p.i, d) in worked]
        if len(wds) > p.days_target:
            extra = m.new_int_var(0, len(wds), f"xd{p.i}")
            m.add(extra >= sum(wds) - p.days_target)
            obj.append((_cents(_obj.EXTRA_DAY_USD), extra))
        # days in a row (the published tail counts)
        if p.run:
            ords = {week_ords[d]: worked[(p.i, d)] for d in dates if (p.i, d) in worked and week_ords[d] is not None}
            if ords:
                lo_o, hi_o = min(ords) - p.run, max(ords)
                for s0 in range(lo_o, hi_o + 1):
                    win = range(s0, s0 + p.run + 1)
                    live = [ords[o] for o in win if o in ords]
                    fx = sum(1 for o in win if o in p.tail_ords and o not in ords)
                    if not live or fx + len(live) <= p.run:
                        continue
                    if p.law_run:
                        m.add(sum(live) + fx <= p.run)
                    else:
                        over = m.new_int_var(0, len(live), f"run{p.i}_{s0}")
                        m.add(over >= sum(live) + fx - p.run)
                        obj.append((W_POLICY_RUN_DAY, over))
        # rest between consecutive days: one bucketed constraint per end
        if p.rest:
            need_m = int(round(p.rest * 60))
            ds = sorted(dates)
            for d0, d1 in zip(ds, ds[1:]):
                if (week_ords[d1] or 0) - (week_ords[d0] or 0) != 1:
                    continue
                a_items = z_by_pd.get((p.i, d0)) or []
                b_items = z_by_pd.get((p.i, d1)) or []
                if not a_items or not b_items:
                    continue
                for e0 in sorted({it[2] for it in a_items}):
                    later = [it[0] for it in b_items if it[1] + 1440 < e0 + need_m]
                    if not later:
                        continue
                    ending = [it[0] for it in a_items if it[2] >= e0]
                    if p.doubles and len(ending) > 1:
                        ind = m.new_bool_var(f"le{p.i}_{d0}_{e0}")
                        for v in ending:
                            m.add_implication(v, ind)
                        lhs = ind + sum(later)
                    else:
                        lhs = sum(ending) + sum(later)
                    if p.law_rest:
                        m.add(lhs <= 1)
                    else:
                        br = m.new_bool_var(f"rs{p.i}_{d0}_{e0}")
                        m.add(lhs <= 1 + br)
                        obj.append((W_POLICY_REST, br))
    # the budget, priced: past it, each dollar costs BUDGET_OVER_FACTOR more
    over = None
    if budget_dollars and dollars:
        fixed_cents = 0
        for r in context_rows or []:
            if (r.get("employee") or "").strip() and c is not None and not c.is_salaried(r.get("employee")) \
                    and r.get("date") in week_ords:
                fixed_cents += _cents(_rules.row_hours(r) * blended)
        over = m.new_int_var(0, 10 ** 10, "over_budget")
        m.add(over >= sum(dollars) + fixed_cents - _cents(float(budget_dollars)))
        obj.append((max(1, int(round(getattr(ctx, "budget_over_factor", None) or _obj.BUDGET_OVER_FACTOR))), over))
    m.minimize(sum(int(w) * v for w, v in obj if int(w)))
    _usual_hint(m, z, z_by_pd, people, pats, hist_rows, c, family)
    # The deterministic budget scaled to the model (schedule_cpsat.work_budget
    # — a fixed 7.5 s found no week at all at 250 people), the memory-bounded
    # portfolio on a large model, and the solve in a child process whose
    # memory goes back to the OS when it returns (schedule_cpsat.run).
    import schedule_cpsat_runtime as _rt
    n_vars = len(m.proto.variables)
    budget = _rt.work_budget(max(0.5, float(max_seconds)), n_vars)
    s = _cp.CpSolver()
    s.parameters.random_seed = int(seed)
    s.parameters.num_workers = int(workers or WORKERS)
    s.parameters.interleave_search = s.parameters.num_workers > 1
    s.parameters.max_deterministic_time = budget
    _rt.lean(s, n_vars)
    cap_s = max(1.0, budget * 4)
    if wall_cap:
        cap_s = min(cap_s, max(0.5, float(wall_cap)))
    s.parameters.max_time_in_seconds = cap_s
    st = _rt.run(s, m)
    info = getattr(s, "sp_info", None) or {}
    memory = {"isolated": bool(info.get("isolated")), "peak_mb": info.get("peak_mb"),
              "fallback": bool(info.get("fallback")),
              "memory_capped": bool(info.get("memory_capped")), "notes": [info["note"]] if info.get("note") else []}
    if st not in (_cp.OPTIMAL, _cp.FEASIBLE):
        out["short"] = short_meta
        out["stats"] = {"status": s.status_name(st).lower(), "patterns": len(n), "people": len(people),
                        "variables": n_vars, "budget": budget, "memory": memory,
                        "memory_capped": memory["memory_capped"], "wall_capped": _rt._wall_capped(s, st)}
        return out
    rows = []
    chosen = []
    for (pi, d, role, k), v in z.items():
        if s.value(v):
            chosen.append((d, role.lower(), pats[(d, role)][k], people[pi].name, pat_roles[(d, role)][k]))
    chosen.sort()
    for d, _rl, (a, b), who, role in chosen:
        r = {"date": d, "day": sq._day_name(d), "employee": "", "role": role, "shift_start": _fmt(a),
             "shift_end": _fmt(b), "scheduled_hours": "", "notes": "", "_open": True, "_s1": who}
        r["scheduled_hours"] = _stime.row_hours_text(r, getattr(c, "tz", None) if c is not None else None)
        rows.append(r)
    _mark_floor(rows, floor_codes, family, open_times, close_times, fixed_on, absm)
    short = list(short_meta)
    agg = defaultdict(lambda: [0, 0.0, 0, None])
    for (fam, a), (sh, d, t, role) in sorted(shorts.items()):
        k = s.value(sh)
        if k > 0:
            cell = agg[(d, role)]
            cell[0] += SLOT
            cell[1] += k * _obj.slot_value(ctx, d, t, role)
            cell[2] = max(cell[2], k)
    nobody = {(e["date"], e["role"]) for e in short_meta}
    # Short where the budget bound (spent to within 2% of it, or past) is the
    # budget's cut of the lowest-value half hours, never "more than the
    # people can cover" — EJ's Saturday 11/7/26 barback was said to be
    # unfillable while its barback had a free night.
    budget_bound = bool(over is not None and (s.value(over) > 0 or sum(
        cents * s.value(v) for cents, v in obj_dollars) >= 0.98 * _cents(float(budget_dollars))))
    why_short = ("left short to hold the labor budget — the lowest-value half hours are cut first"
                 if budget_bound else "more than the people who can work it can cover inside their hours and the rules")
    for (d, role), (mins, usd, most, _x) in sorted(agg.items()):
        if (d, role) in nobody:
            continue
        short.append({"date": d, "role": role, "people": most, "minutes": mins, "kind": "short",
                      "usd": round(usd, 2), "why": why_short, "budget": budget_bound})
    out["rows"] = rows
    out["short"] = short
    out["stats"] = {"status": "optimal" if st == _cp.OPTIMAL else "time_limit", "patterns": len(n),
                    "choices": len(z), "people": len(people), "shifts": len(rows),
                    "seconds": round(s.wall_time, 3),
                    "deterministic_time": round(float(s.response_proto.deterministic_time), 3),
                    "objective": s.objective_value, "bound": s.best_objective_bound, "seed": int(seed),
                    "variables": n_vars, "budget": budget, "memory": memory,
                    "memory_capped": memory["memory_capped"], "wall_capped": _rt._wall_capped(s, st),
                    "workers": int(workers or WORKERS), "min_shift_minutes": int(round(min_h * 60)),
                    "assumed": [a["family"] for a in out["assumed"]]}
    return out


USUAL_HINT_MIN_WEEKS = 2             # a weekday is somebody's usual once it recurs in two weeks


def _usual_hint(m, z, z_by_pd, people, pats, hist_rows, c, family) -> int:
    """A starting week for Stage 1's search (P): each person on the pattern
    nearest their own usual shift on each weekday they usually work (their
    punches and published weeks, USUAL_HINT_MIN_ WEEKS of them), nobody else
    on. On a 120-person week the model with no hint spent its whole
    deterministic budget without a first solution, so the solver-first draft
    silently gave way to the v0 code draft (large_120 and high_volume_250 in
    the schedule benchmark); with the usual week as the hint the search
    starts from a week the restaurant really works. Only a hint: every rule
    and price still decides. Returns the number of choices hinted on."""
    if c is None or not z:
        return 0
    seen = defaultdict(set)                 # (key, weekday, family) -> weeks
    spans = defaultdict(list)               # (key, weekday, family) -> [(start, end)]
    for r in hist_rows or []:
        name = (r.get("employee") or "").strip()
        sp = _span(r)
        d = str(r.get("date") or "")[:10]
        if not name or not sp or len(d) != 10:
            continue
        try:
            wk = datetime.strptime(d, "%Y-%m-%d").strftime("%G-%V")
        except ValueError:
            continue
        key = (c.key(name), sq._day_name(d), family(r.get("role")))
        seen[key].add(wk)
        spans[key].append(sp)
    usual = {}
    for key, weeks in seen.items():
        if len(weeks) >= USUAL_HINT_MIN_WEEKS:
            ss = sorted(spans[key])
            usual[key] = ss[len(ss) // 2]
    on = 0
    for (pi, d), opts in sorted(z_by_pd.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        p = people[pi]
        day = sq._day_name(d)
        best = None
        for v, s, e, _h, role, _k in opts:
            u = usual.get((p.key, day, family(role)))
            if u is None:
                continue
            dist = abs(s - u[0]) + abs(e - u[1])
            if best is None or dist < best[0]:
                best = (dist, v)
        if best is None:
            continue                         # no usual here: the search is free (a 0 hint anchors it empty)
        for v, *_rest in opts:
            m.add_hint(v, 1 if v is best[1] else 0)
        on += 1
    return on


def _mark_floor(rows, floor_codes, family, open_times, close_times, fixed_on, absm=None):
    """`_floor` on the shifts a floor needs: per date, family and daypart,
    the `need` shifts covering most of the core window (less what fixed
    rows already hold) — Stage 2 prices leaving one of them empty at the
    coverage tier, the rest at the requirement's."""
    absm = absm or (lambda d, t: t)
    for (d, fam, part), (need, _code) in sorted(floor_codes.items()):
        day = sq._day_name(d)
        o = _rules.parse_minutes((open_times or {}).get(day) or "")
        cl = _rules.parse_minutes((close_times or {}).get(day) or "")
        slots = sq.floor_slots(part, o, cl)
        if not slots:
            continue
        fx = fixed_on.get(fam, {}) if isinstance(fixed_on, dict) else fixed_on[fam]
        held = min(fx.get(absm(d, t), 0) for t in slots)
        want = need - held
        if want <= 0:
            continue
        scored = []
        for i, r in enumerate(rows):
            if r["date"] != d or family(r["role"]) != fam:
                continue
            sp = _rules._span(r)
            if not sp:
                continue
            k = sum(1 for t in slots if _covers(sp[0], sp[1], t))
            if k:
                # Ties by the row's place in the (sorted) week, never by
                # id(): a memory address chose which of two equal shifts
                # was the floor's, so the same inputs priced a different
                # shift at the coverage tier and drafted a different week
                # ('s property test).
                scored.append((-k, i, r))
        scored.sort(key=lambda t: (t[0], t[1]))
        for _k, _i, r in scored[:want]:
            r["_floor"] = True
