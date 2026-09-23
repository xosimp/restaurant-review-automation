"""
schedule_solver.py — who works each shift, solved rather than guessed.

The model's draft is judgment: which shifts the week has, when they start
and end, in which role. Who works each of them is a constraint problem, and
the model solved it silently in its head (labor.py asks it to "do the
arithmetic and constraint-solving silently"). The repair loop
(schedule_optimizer) then tries one change at a time. Neither can say the
week it produced is the best arrangement of these people, or that no legal
arrangement exists for a given shift.

This module keeps the draft's shape and re-solves the assignment:

  variables   one per "unit": a draft row, or the rows one person works on
              one date (a double is the model's judgment and stays one unit)
  domains     the people who may legally work it, from the same
              schedule_rules.Constraints the rule sweep uses: roster and
              inactive staff, availability by day and daypart, approved time
              off and sibling-site dates, personal time windows, minors,
              certifications, and the role (primary, cross-trained, or the
              role the draft already had them in)
  hard rules  no overlap and no new doubles, minimum rest between shifts
              (the published tail counts), the weekly hours ceiling per
              payroll week (hours already worked at sibling sites count),
              the longest run of consecutive days (the tail counts), and a
              manager or keyholder on every daypart when the owner asks
  objective   a cost that mirrors the Shift Quality dimensions an assignment
              can move (strength against demand, leadership rules, training
              cover, fairness of closes and weekends, fatigue, reliability,
              stability, stated preferences, pairings) — cheap enough to
              evaluate millions of times. The final choice between the draft
              and the solver's answers is made by shift_quality.score_rows,
              the same judge the owner's number comes from.

The search is complete: backtracking over the most-constrained unit first,
forward checking after every assignment (a person removed from every unit
the assignment makes illegal for them; an emptied domain backtracks at
once), and branch-and-bound on an admissible lower bound (the cheapest
remaining person for each open unit). It runs as limited-discrepancy
iterations so a good week is found early, and the last iteration is plain
depth-first: when it finishes inside the time limit the answer is proved
optimal for the cost model. Independent sub-problems (a role whose people
share no shift, rule or pairing with any other) are solved separately,
which is where most of the proofs come from.

Hard rules are never traded for score. A unit nobody may legally work is
reported with each candidate's reason, and left as the draft had it.

Nothing here calls a model, reads the database or writes anything.
"""
import time as _time
from datetime import datetime
from types import SimpleNamespace

import schedule_rules as _rules
import shift_quality as sq

# The same order of budget the repair loop uses on the background job.
DEFAULT_SECONDS = 12.0
# A week-level gain (objective points, before rounding) worth changing the
# draft for — the repair loop's own threshold.
MIN_GAIN = 0.15
# How many solver answers the judge scores, best first.
JUDGE_CANDIDATES = 3
# Reassignments listed one by one before the rest are counted.
MAX_LISTED_CHANGES = 25
NOTE_TAG = "Cavnar:"
# Discrepancy limits tried when no legal week has been found yet; None is
# the complete pass.
LDS_SCHEDULE = (0, 1, 2, 4, 8, None)
# When a part of the week has no legal arrangement at all: how many times
# the culprit shifts are set aside and the rest re-solved, and how long each
# look for a legal arrangement may take.
RELAX_ATTEMPTS = 6
RELAX_PROBE_SECONDS = 0.6
RELAX_SHARE = 0.35          # of the budget, at most, spent finding what to set aside
# Large-neighbourhood rounds: how many units each re-solves exactly, and the
# node cap on each.
LNS_MAX_UNITS = 14
LNS_NODES = 4000
_CHECK_EVERY = 64

# ── cost coefficients (points of the solver's own objective) ──────────────
# Scaled per restaurant by its dimension weights over the defaults, so an
# owner who weights leadership up gets a solver that does too.
K_STRENGTH = 1.2          # per rating point under 5, times the shift's demand weight
K_RELIABILITY = 2.0       # per unit of no-show rate
K_RELIABILITY_EXPOSED = 3.0
K_EXPERIENCE = 0.8        # somebody not yet experienced, times demand weight
K_STABILITY_DAY = 0.6     # a weekday they do not usually work
K_STABILITY_PART = 0.3    # a daypart they do not usually work
K_PREFERENCE = 1.0        # a daypart they said they do not want
K_CROSS = 0.2             # nobody who can flex to a second station
K_OFF_ROLE = 0.3          # a cross-trained fill-in rather than the role's own
K_PENDING = 40.0          # a day they have asked off (pending, soft)
K_CHANGE = 0.02           # a different person from the draft's: ties keep the draft
K_CLOSE = 0.5             # per close they already have this week
K_WEEKEND = 0.3           # per weekend shift they already have
K_HARD = 0.1              # per busy shift they already have
K_HARD_OVER = 1.5         # a busy shift past the fatigue ceiling
K_AVOID = 3.0             # two people the owner keeps apart, on together
K_DESIRED = 0.1           # per hour past what they asked for (+20%)
K_LEAD_RULE = 12.0        # per missing leader an owner rule asks for
K_LEAD_PROFILE = 4.0      # nobody able to run a shift that wants a leader
K_STRENGTH_GROUP = 20.0   # a role's combined score under its target, by share
K_STRENGTH_FLOOR = 10.0   # ...under the 55% floor that caps a shift
K_TRAINING = 2.0          # somebody weak with nobody stronger alongside
K_DAYS_OFF = 1.5          # fewer consecutive days off than the rule (soft)
K_MIN_HOURS = 0.3         # per hour under somebody's stated minimum (soft)


def _ordinal(date):
    try:
        return datetime.strptime(date, "%Y-%m-%d").toordinal()
    except (ValueError, TypeError):
        return None


def _low(name) -> str:
    return (name or "").strip().lower()


def _where(day: str, part: str) -> str:
    return f"{day} {'lunch' if part == 'morning' else 'dinner' if part == 'night' else part}".strip()


class _Unit:
    __slots__ = ("id", "rows", "idx", "date", "day", "dord", "bucket", "roles", "role_label", "hours",
                 "spans", "parts", "primary", "dw", "demand", "hard", "closing", "weekend", "draft",
                 "fixed", "why_fixed", "sig", "groups", "kgroups", "sym", "sym_pos", "dgroup")

    def __init__(self, uid):
        self.id = uid
        self.groups = []
        self.kgroups = []
        self.sym = None
        self.sym_pos = 0
        self.fixed = False
        self.why_fixed = ""


class Problem:
    """The assignment problem one draft poses, with every table the search
    and the explanation need. Build once; search many times."""

    def __init__(self, rows, constraints, signals=None, weights=None, profiles=None,
                 only_dates=None, roster_roles=None, pending=None, relax=None):
        self.rows = [dict(r) for r in rows or []]
        # {unit id: why} — units a previous attempt showed no legal week can
        # staff; kept as drafted and named (solve's relaxation loop)
        self.relax = dict(relax or {})
        self.draft_overcommitted = set()
        self.c = constraints
        self.signals = signals = dict(signals or {})
        self.profiles = profiles
        w = dict(sq.DEFAULT_WEIGHTS)
        for k, v in (weights or {}).items():
            try:
                if k in w and v is not None:
                    w[k] = float(v)
            except (TypeError, ValueError):
                pass
        self.wn = {k: (w[k] / float(sq.DEFAULT_WEIGHTS[k]) if sq.DEFAULT_WEIGHTS[k] else 0.0) for k in w}
        self.notes = []
        self._build_people(roster_roles, pending)
        if constraints is not None:
            per_person = {"over_max_hours", "long_run", "rest_gap", "overlap", "double_booked"}
            for v in _rules.violations(self.rows, constraints):
                p = self.pidx.get(_low(v.get("employee")))
                if v.get("hard") and v["kind"] in per_person and p is not None:
                    self.draft_overcommitted.add(p)
        self._build_units(only_dates)
        self._build_domains()
        self._build_groups()
        self._build_components()

    # ── people ────────────────────────────────────────────────────────────
    def _build_people(self, roster_roles, pending):
        s, c = self.signals, self.c
        roster_roles = roster_roles if roster_roles is not None else (s.get("roster_roles") or {})
        cross = s.get("cross_trained") or {}
        names = [n for n in (s.get("roster") or list(getattr(c, "roster_names", None) or [])) if n]
        for r in self.rows:
            n = (r.get("employee") or "").strip()
            if n:
                names.append(n)
        self.names, self.pidx = [], {}
        for n in names:
            k = _low(n)
            if k and k not in self.pidx:
                self.pidx[k] = len(self.names)
                self.names.append(n.strip())
        P = len(self.names)
        self.roles = [set() for _ in range(P)]
        self.primary_role = [""] * P
        for n, role in (roster_roles or {}).items():
            p = self.pidx.get(_low(n))
            if p is not None and (role or "").strip():
                self.roles[p].add(_low(role))
                self.primary_role[p] = _low(role)
        for n, rls in cross.items():
            p = self.pidx.get(_low(n))
            if p is not None:
                for role in rls or []:
                    if str(role).strip():
                        self.roles[p].add(_low(str(role)))
        for r in self.rows:
            p = self.pidx.get(_low(r.get("employee")))
            if p is not None and (r.get("role") or "").strip():
                self.roles[p].add(_low(r.get("role")))
                if not self.primary_role[p]:
                    self.primary_role[p] = _low(r.get("role"))
        self.flexible = [len({_low(str(x)) for x in (cross.get(n) or [])}) > 1 for n in self.names]
        self.constrained = {_low(n) for n, note in (s.get("constraints") or {}).items()
                            if n and str(note or "").strip()}
        pend = pending if pending is not None else (getattr(self.c, "pending_off", None) or {})
        self.pending = {_low(k): set(v or ()) for k, v in (pend or {}).items()}
        scores = s.get("scores") or {}
        lscores = {_low(k): v for k, v in scores.items() if v is not None}
        self.score = [lscores.get(_low(n)) for n in self.names]
        self.rated_any = bool(lscores)
        flags = s.get("leader_flags") or {}
        lflags = {_low(k): bool(v) for k, v in flags.items()}
        self.leader = [bool(lflags.get(_low(n))) for n in self.names]
        self.blind = not lscores and not any(lflags.values())
        rel = s.get("reliability") or {}
        lrel = {_low(k): v for k, v in rel.items()}
        self.no_show = []
        for n in self.names:
            try:
                self.no_show.append(float((lrel.get(_low(n)) or {}).get("no_show_rate") or 0))
            except (TypeError, ValueError):
                self.no_show.append(0.0)
        tenure = {_low(k): int(v or 0) for k, v in (s.get("tenure") or {}).items() if v is not None}
        marked = {_low(n) for n in (s.get("experienced") or ()) if n}
        self.experience_on = bool(tenure or marked) and (
            max(tenure.values(), default=0) >= sq.EXPERIENCE_SHIFTS or len(marked) >= sq.EXPERIENCE_MIN_MARKED)
        self.veteran = [(_low(n) in marked) or tenure.get(_low(n), 0) >= sq.EXPERIENCE_SHIFTS for n in self.names]
        pp = {_low(k): v or {} for k, v in (s.get("prior_pattern") or {}).items()}
        self.pattern = []
        for n in self.names:
            pat = pp.get(_low(n))
            if pat:
                self.pattern.append(({_low(d) for d in (pat.get("days") or [])},
                                     {_low(x) for x in (pat.get("dayparts") or [])}))
            else:
                self.pattern.append(None)
        prefs = {_low(k): v or {} for k, v in (s.get("preferences") or {}).items()}
        self.pref_parts, self.desired = [], []
        for n in self.names:
            p = prefs.get(_low(n)) or {}
            self.pref_parts.append({x for x in (p.get("preferred_dayparts") or []) if x in ("morning", "night")})
            try:
                self.desired.append(float(p.get("desired_hours")) if p.get("desired_hours") else None)
            except (TypeError, ValueError):
                self.desired.append(None)
        self.avoid = [set() for _ in range(P)]
        for pair in ((s.get("pairs") or {}).get("avoid") or ()):
            members = [self.pidx.get(_low(x)) for x in pair]
            if len(members) == 2 and None not in members and members[0] != members[1]:
                a, b = members
                self.avoid[a].add(b)
                self.avoid[b].add(a)
        self.unavailable = {_low(k): set(v or ()) for k, v in (s.get("availability") or {}).items()}
        c = self.c
        self.cap = [float(c.max_hours(n)) if c is not None else sq.WEEKLY_HOURS_CEILING for n in self.names]
        self.base_hours = [dict((getattr(c, "base_hours", None) or {}).get(_low(n)) or {}) for n in self.names]
        self.base_dates, self.base_ords = [], []
        for n in self.names:
            ds = set()
            for r in (getattr(c, "base_rows", None) or {}).get(_low(n)) or []:
                if r.get("date"):
                    ds.add(r["date"])
            self.base_dates.append(ds)
            self.base_ords.append({o for o in (_ordinal(d) for d in ds) if o is not None})
        comp = (getattr(c, "compliance", None) or {}) if c is not None else {}
        try:
            self.max_run = int(comp.get("max_consecutive_days") or 0)
        except (TypeError, ValueError):
            self.max_run = 0
        try:
            self.need_rest = float(comp.get("min_rest_hours") or 0)
        except (TypeError, ValueError):
            self.need_rest = 0.0
        self.keyholders = {self.pidx[k] for k in (getattr(c, "keyholders", None) or set()) if k in self.pidx}
        self.manager_rule = bool(comp.get("manager_on_duty")) and bool(getattr(c, "keyholders", None))
        self.min_hours = [c.min_hours(n) if c is not None else None for n in self.names]
        self.employment = [(getattr(c, "employment", None) or {}).get(_low(n)) for n in self.names]
        self.days_off_rule = (comp.get("min_consecutive_days_off"), comp.get("part_time_days_off"))
        self.week_dates = list(getattr(c, "week_dates", None) or [])

    # ── units ─────────────────────────────────────────────────────────────
    def _build_units(self, only_dates):
        editable = set(only_dates) if only_dates else None
        groups, order = {}, []
        for i, r in enumerate(self.rows):
            name = (r.get("employee") or "").strip()
            key = (_low(name), r.get("date")) if name else (f"#{i}", r.get("date"))
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(i)
        # Two legs that overlap are a double-booking, not a double: each is
        # its own shift for somebody to take.
        tz = getattr(self.c, "tz", None) if self.c is not None else None
        split_order = []
        for key in order:
            idx = groups[key]
            spans = [_rules.shift_span(self.rows[i], tz) for i in idx]
            clash = any(spans[a][0] is not None and spans[b][0] is not None
                        and spans[b][0] < spans[a][1] and spans[a][0] < spans[b][1]
                        for a in range(len(idx)) for b in range(a + 1, len(idx)))
            if clash:
                for i in idx:
                    groups[(key, i)] = [i]
                    split_order.append((key, i))
            else:
                split_order.append(key)
        order = split_order
        # Which bucket closes each date, exactly as build_contexts reads it.
        primary, latest_end = {}, {}
        for r in self.rows:
            if not ((r.get("employee") or "").strip() and r.get("date")):
                continue
            part = sq.present_dayparts(r)[0]
            primary.setdefault((r["date"], part), []).append(r)
        closes_on = {}
        for (date, part), rs in primary.items():
            latest = max((sq._end_minutes(r.get("shift_end")) for r in rs), default=-1)
            latest_end[date] = max(latest_end.get(date, -1), latest)
            if latest > closes_on.get(date, (-1, None))[0]:
                closes_on[date] = (latest, part)
        self.closes_on = {d: v[1] for d, v in closes_on.items()}
        s = self.signals
        dbd = s.get("demand_by_day") or {}
        dbdate = s.get("demand_by_date") or {}
        self._profile_cache = {}

        def profile(date, day, part):
            k = (date, part)
            if k not in self._profile_cache:
                self._profile_cache[k] = sq.profile_for_shift(day, part, self.profiles, dbd,
                                                              (dbdate.get(date) or {}).get("lift_pct"))
            return self._profile_cache[k]
        self.profile = profile
        self.units = []
        for key in order:
            idx = groups[key]
            rs = [self.rows[i] for i in idx]
            u = _Unit(len(self.units))
            u.idx, u.rows = idx, rs
            u.date = rs[0].get("date") or ""
            u.day = sq._day_name(u.date, rs[0].get("day", ""))
            try:
                u.dord = datetime.strptime(u.date, "%Y-%m-%d").toordinal()
            except (ValueError, TypeError):
                u.dord = None
            u.bucket = self.c.bucket(u.date) if (self.c is not None and u.date) else ""
            u.roles = {_low(r.get("role")) for r in rs if (r.get("role") or "").strip()}
            u.role_label = (rs[0].get("role") or "").strip()
            u.hours = sum(_rules.row_hours(r) for r in rs)
            tz = getattr(self.c, "tz", None) if self.c is not None else None
            u.spans = [_rules.shift_span(r, tz) for r in rs]
            u.parts = sorted({(u.date, p) for r in rs for p in sq.present_dayparts(r) if p != "unknown"})
            u.primary = [sq.present_dayparts(r)[0] for r in rs]
            demands = [profile(u.date, u.day, p).demand for p in u.primary if p != "unknown"] or ["normal"]
            u.demand = max(demands, key=lambda d: sq.DEMAND_RANK.get(d, 1))
            u.dw = sq.DEMAND_WEIGHT.get(u.demand, 1.0)
            u.hard = sq.DEMAND_RANK.get(u.demand, 1) >= sq.DEMAND_RANK[sq.HARD_DEMAND]
            u.closing = any(p == self.closes_on.get(u.date) or
                            (sq._end_minutes(r.get("shift_end")) >= 0 and sq._end_minutes(r.get("shift_end")) == latest_end.get(u.date))
                            for r, p in zip(rs, u.primary))
            u.weekend = u.day in ("Friday", "Saturday", "Sunday")
            name = (rs[0].get("employee") or "").strip()
            u.draft = self.pidx.get(_low(name)) if name else None
            u.sig = (u.date, tuple(sorted(u.roles)),
                     tuple(sorted((r.get("shift_start") or "", r.get("shift_end") or "") for r in rs)))
            if editable is not None and u.date not in editable:
                u.fixed, u.why_fixed = True, "a day the owner kept"
            elif name and _low(name) in self.constrained:
                u.fixed, u.why_fixed = True, "has a written note the solver cannot read"
            elif not u.date or u.dord is None or any(sp[0] is None for sp in u.spans) or not u.roles:
                u.fixed, u.why_fixed = True, "times or date could not be read"
            self.units.append(u)

    # ── feasibility ───────────────────────────────────────────────────────
    def fits(self, u, p):
        """None when person p may legally work unit u on their own; else the
        reason, in the rule sweep's words."""
        c, name = self.c, self.names[p]
        low = _low(name)
        if not u.roles <= self.roles[p]:
            return "does not work " + "/".join(sorted(u.roles))
        if low in self.constrained and u.draft != p:
            return "has a written note the solver cannot read"
        if u.day in self.unavailable.get(low, ()):
            return _rules.LABELS["unavailable_day"]
        for r in u.rows:
            for part in sq.present_dayparts(r):
                ok, why = c.can_work(name, u.date, part)
                if not ok:
                    return why
            ok, why = c.window_ok(name, u.date, r.get("shift_start", ""), r.get("shift_end", ""))
            if not ok:
                return why
            ok, why = c.cert_ok(name, r.get("role", ""))
            if not ok:
                return why
            if low in c.minors:
                latest = _rules.parse_minutes(c.compliance.get("minor_latest_end") or "")
                e_m = _rules.parse_minutes(r.get("shift_end", ""))
                s_m = _rules.parse_minutes(r.get("shift_start", ""))
                if latest is not None and e_m is not None and s_m is not None and (e_m > latest or e_m < s_m):
                    return _rules.LABELS["minor_late"]
                mm = c.compliance.get("minor_max_daily_hours")
                if mm and _rules.row_hours(r) > float(mm) + 0.01:
                    return _rules.LABELS["minor_hours"]
            ok, why = c.rest_ok(name, r, [])
            if not ok:
                return why
        if u.hours + float(self.base_hours[p].get(u.bucket, 0.0)) > self.cap[p] + 0.05:
            return _rules.LABELS["over_max_hours"]
        if self.max_run and self._run_with(self.base_ords[p], u.dord) > self.max_run:
            return _rules.LABELS["long_run"]
        return None

    @staticmethod
    def _run_with(ords, d) -> int:
        """Consecutive days through day-ordinal `d` if it were added to
        `ords` (a set or dict of date ordinals)."""
        if d is None:
            return 0
        run = 1
        k = d - 1
        while k in ords:
            run += 1
            k -= 1
        k = d + 1
        while k in ords:
            run += 1
            k += 1
        return run

    def _conflict(self, a, b) -> bool:
        """Whether one person may not work both units: the same date (no new
        doubles — the model decides where doubles go), an overlap, or less
        than the minimum rest between them."""
        if a.date == b.date:
            return True
        for s1, e1 in a.spans:
            for s2, e2 in b.spans:
                if s1 is None or s2 is None:
                    continue
                if s2 < e1 and s1 < e2:
                    return True
                gap = (s2 - e1).total_seconds() / 3600 if s2 >= e1 else (s1 - e2).total_seconds() / 3600
                if self.need_rest and gap < self.need_rest - 0.01:
                    return True
        return False

    def _build_domains(self):
        U, P = len(self.units), len(self.names)
        self.dom = [set() for _ in range(U)]
        self.reasons = [dict() for _ in range(U)]
        self.infeasible = []
        for u in self.units:
            if u.fixed:
                continue
            for p in range(P):
                why = self.fits(u, p)
                if why is None:
                    self.dom[u.id].add(p)
                elif u.roles <= self.roles[p]:
                    self.reasons[u.id][self.names[p]] = why
        for uid, why in sorted(self.relax.items()):
            u = self.units[uid]
            if not u.fixed:
                u.fixed, u.why_fixed = True, why
                e = self._infeasible_entry(u)
                e["text"] = why
                self.infeasible.append(e)
        # Fixed units hold their person: that person's other units must
        # leave room for them (a unit nobody can legally work is fixed too,
        # as the draft had it, and named).
        changed = True
        while changed:
            changed = False
            for u in self.units:
                if not u.fixed and not self.dom[u.id]:
                    u.fixed, u.why_fixed = True, "nobody may legally work it"
                    self.infeasible.append(self._infeasible_entry(u))
                    changed = True
            # More shifts of a role on a date than people who may legally
            # work any of them (Hall's condition): the excess cannot be
            # staffed, whoever works the rest. Kept as drafted, preferring the
            # ones whose drafted person was not legal there anyway.
            by_day = {}
            for u in self.units:
                if not u.fixed:
                    by_day.setdefault((u.dord, tuple(sorted(u.roles))), []).append(u)
            for members in by_day.values():
                pool = set()
                for u in members:
                    pool |= self.dom[u.id]
                excess = len(members) - len(pool)
                if excess > 0:
                    worst = sorted(members, key=lambda u: (u.draft in self.dom[u.id], len(self.dom[u.id]), u.id))
                    for u in worst[:excess]:
                        u.fixed, u.why_fixed = True, "more shifts than people who may legally work them"
                        e = self._infeasible_entry(u)
                        e["text"] = (f"{len(members)} {u.role_label or 'shifts'} shifts on {u.day} and only "
                                     f"{len(pool)} {'person' if len(pool) == 1 else 'people'} who may legally work them.")
                        self.infeasible.append(e)
                    changed = True
            self._fixed_state()
            changed = self._weekly_capacity() or changed
            for u in self.units:
                if u.fixed:
                    continue
                for p in list(self.dom[u.id]):
                    why = self._fixed_blocks(u, p)
                    if why:
                        self.dom[u.id].discard(p)
                        self.reasons[u.id][self.names[p]] = why
                        changed = True
        live = [u for u in self.units if not u.fixed]
        self.conf = {u.id: [] for u in live}
        by_day = {}
        for u in live:
            by_day.setdefault(u.dord, []).append(u)
        span = 2
        for u in live:
            for d in range(u.dord - span, u.dord + span + 1):
                for v in by_day.get(d, ()):
                    if v.id <= u.id:
                        continue
                    if (self.dom[u.id] & self.dom[v.id]) and self._conflict(u, v):
                        self.conf[u.id].append(v.id)
                        self.conf[v.id].append(u.id)
        # Units of one role on one date: nobody may work two of them.
        self.dgroups = {}
        for u in live:
            u.dgroup = (u.dord, tuple(sorted(u.roles)))
            self.dgroups.setdefault(u.dgroup, []).append(u.id)
        for u in self.units:
            if u.fixed:
                u.dgroup = None
        # Units nobody could tell apart (same date, role and times): any
        # arrangement of the same people among them is the same week, so
        # only the one with people in ascending order is searched.
        sym = {}
        for u in live:
            sym.setdefault(u.sig, []).append(u)
        self.sym_groups = []
        for members in sym.values():
            if len(members) < 2:
                continue
            gid = len(self.sym_groups)
            members.sort(key=lambda x: x.id)
            for pos, u in enumerate(members):
                u.sym, u.sym_pos = gid, pos
            self.sym_groups.append([u.id for u in members])
        self.sym_draft = [{u_.draft for u_ in (self.units[i] for i in g) if u_.draft is not None}
                          for g in self.sym_groups]

    def _days_cap(self, p, avail) -> int:
        """Most of the week's dates in `avail` person p can work, with at
        most one unit a date and no run past the limit (the published tail
        and p's kept units count toward runs but not toward the total)."""
        ords = sorted({o for o in (_ordinal(d) for d in self.week_dates) if o is not None} | set(avail))
        if not ords:
            return 0
        forced = set(self.fx_ords[p])
        limit = self.max_run or 10 ** 6
        run = 0
        k = ords[0] - 1
        while k in forced:
            run += 1
            k -= 1
        best = {run: 0}
        for o in ords:
            nxt = {}
            for r, n in best.items():
                if o in forced:
                    if r + 1 <= limit:
                        nxt[r + 1] = max(nxt.get(r + 1, -1), n)
                    continue
                nxt[0] = max(nxt.get(0, -1), n)
                if o in avail and r + 1 <= limit:
                    nxt[r + 1] = max(nxt.get(r + 1, -1), n + 1)
            best = nxt or {0: max(best.values())}
        return max(best.values())

    def _weekly_capacity(self) -> bool:
        """A role's shifts this week against an upper bound on what its
        people can carry: the days each may work (one shift a date, the run
        limit) and the hours under their ceiling. Past it, the excess cannot
        be staffed by anybody; that many are kept as drafted and named."""
        changed = False
        by_role = {}
        for u in self.units:
            if not u.fixed:
                by_role.setdefault(tuple(sorted(u.roles)), []).append(u)
        for role, members in by_role.items():
            people = set()
            for u in members:
                people |= self.dom[u.id]
            shortest = min(u.hours for u in members) or 1.0
            longest = max(u.hours for u in members) or 1.0
            capacity, supply = 0, 0.0
            for p in people:
                avail = {u.dord for u in members if p in self.dom[u.id]}
                days = self._days_cap(p, avail)
                buckets = {u.bucket for u in members if p in self.dom[u.id]}
                room = sum(max(0.0, self.cap[p] + 0.05 - self.fx_hours[p].get(b, 0.0)) for b in buckets)
                capacity += min(days, sum(int(max(0.0, self.cap[p] + 0.05 - self.fx_hours[p].get(b, 0.0)) // shortest)
                                          for b in buckets))
                supply += min(room, days * longest)
            excess = len(members) - capacity
            demand = sum(u.hours for u in members)
            if excess <= 0 and demand > supply + 0.05:
                # enough days, not enough hours under everyone's ceiling
                excess = max(1, int(-(-(demand - supply) // longest)))
            if excess <= 0:
                continue
            label = members[0].role_label or "that role"
            # A shift kept as drafted still occupies its drafted person, so
            # setting single shifts aside cannot close the gap. Whoever the
            # draft already overcommitted (their own hours, rest or days in a
            # row broken) keeps their drafted week in this role exactly as it
            # was — the same breach the draft had, no new one — and the rest
            # is solved around them, most-drafted first.
            offenders = {}
            for u in members:
                if u.draft is not None and u.draft in self.draft_overcommitted:
                    offenders[u.draft] = offenders.get(u.draft, 0) + 1
            if offenders:
                who = max(offenders.items(), key=lambda kv: (kv[1], -kv[0]))[0]
                for u in members:
                    if u.draft == who:
                        u.fixed, u.why_fixed = True, "kept with an overcommitted person"
                        e = self._infeasible_entry(u)
                        e["text"] = (f"{len(members)} {label} shifts this week and the {len(people)} "
                                     f"{'person' if len(people) == 1 else 'people'} who may work them can carry at most "
                                     f"{capacity} (days in a row, one shift a day, hours ceiling); "
                                     f"{self.names[who]}'s week is kept as drafted.")
                        self.infeasible.append(e)
                changed = True
                continue
            worst = sorted(members, key=lambda u: (u.draft in self.dom[u.id], len(self.dom[u.id]), -u.dord, u.id))
            for u in worst[:excess]:
                u.fixed, u.why_fixed = True, "more shifts than the role's people can carry this week"
                e = self._infeasible_entry(u)
                e["text"] = (f"{len(members)} {label} shifts this week and the {len(people)} "
                             f"{'person' if len(people) == 1 else 'people'} who may work them can carry at most "
                             f"{capacity} (days in a row, one shift a day, hours ceiling).")
                self.infeasible.append(e)
            changed = True
        return changed

    def _fixed_state(self):
        P = len(self.names)
        self.fx_hours = [dict(h) for h in self.base_hours]
        self.fx_dates = [dict.fromkeys(d, 1) for d in self.base_dates]
        self.fx_ords = [dict.fromkeys(o, 1) for o in self.base_ords]
        self.fx_units = [[] for _ in range(P)]
        for u in self.units:
            if u.fixed and u.draft is not None:
                p = u.draft
                self.fx_hours[p][u.bucket] = self.fx_hours[p].get(u.bucket, 0.0) + u.hours
                self.fx_dates[p][u.date] = self.fx_dates[p].get(u.date, 0) + 1
                self.fx_ords[p][u.dord] = self.fx_ords[p].get(u.dord, 0) + 1
                self.fx_units[p].append(u)

    def _fixed_blocks(self, u, p):
        for f in self.fx_units[p]:
            if self._conflict(u, f):
                return "already on another shift that day" if f.date == u.date else _rules.LABELS["rest_gap"]
        if u.hours + self.fx_hours[p].get(u.bucket, 0.0) > self.cap[p] + 0.05:
            return _rules.LABELS["over_max_hours"]
        if self.max_run and self._run_with(self.fx_ords[p], u.dord) > self.max_run:
            return _rules.LABELS["long_run"]
        return None

    def _infeasible_entry(self, u):
        r = u.rows[0]
        why = self.reasons[u.id]
        common = {}
        for reason in why.values():
            common[reason] = common.get(reason, 0) + 1
        if not why:
            text = f"Nobody on the roster works {u.role_label or 'that role'}."
        else:
            parts = [f"{', '.join(n for n, rr in why.items() if rr == reason)[:80]} — {reason}"
                     for reason, _ in sorted(common.items(), key=lambda kv: -kv[1])[:3]]
            text = "Nobody may legally work it: " + "; ".join(parts) + "."
        return {"index": u.idx[0], "rows": list(u.idx), "date": u.date, "day": u.day,
                "role": u.role_label, "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"),
                "kept": self.names[u.draft] if u.draft is not None else None,
                "reasons": dict(sorted(why.items())[:12]), "text": text}

    # ── groups the week-level costs and the keyholder rule read ────────────
    def _build_groups(self):
        s = self.signals
        unmeetable = sq._unmeetable_rules(self.rows, s)
        self.rules = [r for r in (s.get("leader_rules") or []) if id(r) not in unmeetable]
        self.groups = {}          # key -> {"units": [...], "kind", ...}
        for u in self.units:
            for (date, part) in u.parts:
                for role in u.roles:
                    k = ("r", date, part, role)
                    self.groups.setdefault(k, {"units": [], "date": date, "part": part, "role": role})["units"].append(u.id)
                prof = self.profile(date, u.day, part)
                if prof.requires_leader and not self.blind:
                    wanted = {_low(x) for x in (prof.leader_roles or [])}
                    if not wanted or (u.roles & wanted):
                        k = ("L", date, part)
                        self.groups.setdefault(k, {"units": [], "date": date, "part": part, "role": None})["units"].append(u.id)
        # Only groups with something the solver decides, and something to
        # judge in them, are kept: a constant is no help to the search.
        keep = {}
        for k, g in self.groups.items():
            if all(self.units[i].fixed for i in g["units"]):
                continue
            if k[0] == "r" and not self._role_group_active(g):
                continue
            keep[k] = g
        self.groups = keep
        for k, g in self.groups.items():
            g["units"] = sorted(set(g["units"]))
            for i in g["units"]:
                self.units[i].groups.append(k)
        self.kgroups = {}
        if self.manager_rule:
            for u in self.units:
                for r in u.rows:
                    part = _rules.daypart_of(r.get("shift_start", ""))
                    if part == "unknown":
                        continue
                    self.kgroups.setdefault((u.date, part), set()).add(u.id)
            enforce = {}
            for k, members in self.kgroups.items():
                members = sorted(members)
                if all(self.units[i].fixed for i in members):
                    continue
                sat = any(self.units[i].fixed and self.units[i].draft in self.keyholders for i in members)
                possible = sat or any((not self.units[i].fixed) and (self.dom[i] & self.keyholders) for i in members)
                if not possible:
                    self.notes.append(f"No keyholder can legally work {_where(sq._day_name(k[0]), k[1])}, "
                                      "so that rule could not be held there.")
                    continue
                enforce[k] = members
                for i in members:
                    self.units[i].kgroups.append(k)
            self.kgroups = enforce

    def _role_group_active(self, g) -> bool:
        day = sq._day_name(g["date"])
        prof = self.profile(g["date"], day, g["part"])
        if self.rated_any and any(_low(r) == g["role"] and v for r, v in (prof.min_strength or {}).items()):
            return True
        if self.rated_any:
            return True        # training balance reads any rated role
        return bool(self._rules_for(g["date"], g["part"], g["role"]))

    def _rules_for(self, date, part, role):
        ctx = SimpleNamespace(day=sq._day_name(date), daypart=part, is_closing=(self.closes_on.get(date) == part))
        out = []
        for rule in self.rules:
            if _low(rule.get("role")) != role or not sq._rule_applies(rule, ctx):
                continue
            if self.blind and rule.get("min_score") is not None and not rule.get("attribute"):
                continue
            out.append(rule)
        return out

    # ── components ────────────────────────────────────────────────────────
    def _build_components(self):
        parent = list(range(len(self.units)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra
        live = [u.id for u in self.units if not u.fixed]
        first_of = {}
        for i in live:
            for p in self.dom[i]:
                if p in first_of:
                    union(first_of[p], i)
                else:
                    first_of[p] = i
        for p, partners in enumerate(self.avoid):
            for q in partners:
                if p in first_of and q in first_of:
                    union(first_of[p], first_of[q])
        for g in list(self.groups.values()) + [{"units": m} for m in self.kgroups.values()]:
            ids = [i for i in g["units"] if not self.units[i].fixed]
            for i in ids[1:]:
                union(ids[0], i)
        comps = {}
        for i in live:
            comps.setdefault(find(i), []).append(i)
        self.components = sorted(comps.values(), key=len)

    # ── costs ─────────────────────────────────────────────────────────────
    def components_of(self, u, p) -> dict:
        """The static cost of person p on unit u, by part, for the search
        and for saying why a change was made."""
        wn = self.wn
        out = {}
        low = _low(self.names[p])
        if u.date in self.pending.get(low, ()):
            out["pending"] = K_PENDING
        if self.rated_any:
            s = self.score[p]
            if s is None:
                s = self._neutral(u)
            out["strength"] = K_STRENGTH * u.dw * max(0.0, 5.0 - float(s)) * (
                wn.get("operational_strength", 1) + wn.get("demand_match", 1)) / 2.0
        rate = self.no_show[p]
        if rate:
            k = K_RELIABILITY_EXPOSED if (rate >= sq.UNRELIABLE_RATE and u.hard) else K_RELIABILITY * rate
            out["reliability"] = k * u.dw * wn.get("reliability", 1)
        if self.experience_on and not self.veteran[p]:
            out["experience"] = K_EXPERIENCE * u.dw * wn.get("experience_balance", 1)
        pat = self.pattern[p]
        if pat is not None:
            days, parts = pat
            if days and _low(u.day) not in days:
                out["stability_day"] = K_STABILITY_DAY * wn.get("stability", 1)
            if parts and any(x not in parts for x in u.primary if x != "unknown"):
                out["stability_part"] = K_STABILITY_PART * wn.get("stability", 1)
        if self.pref_parts[p] and any(x not in self.pref_parts[p] for x in u.primary if x != "unknown"):
            out["preference"] = K_PREFERENCE * wn.get("preferences", 1)
        if self.signals.get("cross_trained") and not self.flexible[p]:
            out["cross"] = K_CROSS * wn.get("cross_training", 1)
        if self.primary_role[p] and self.primary_role[p] not in u.roles:
            out["role"] = K_OFF_ROLE
        drafted = self.sym_draft[u.sym] if u.sym is not None else ({u.draft} if u.draft is not None else set())
        if p not in drafted:
            out["change"] = K_CHANGE
        return out

    def _neutral(self, u):
        """An unrated person costs what the role's rated people average: never
        benched for not being rated, never preferred for it either."""
        key = tuple(sorted(u.roles))
        cache = self.__dict__.setdefault("_neutral_cache", {})
        if key not in cache:
            vals = [float(self.score[p]) for p in range(len(self.names))
                    if self.score[p] is not None and (u.roles & self.roles[p])]
            vals = vals or [float(v) for v in self.score if v is not None] or [3.0]
            cache[key] = sum(vals) / len(vals)
        return cache[key]

    def static(self):
        if not hasattr(self, "_static"):
            self._static = [None] * len(self.units)
            for u in self.units:
                if not u.fixed:
                    self._static[u.id] = {p: sum(self.components_of(u, p).values()) for p in self.dom[u.id]}
        return self._static

    def group_penalty(self, key, assign) -> float:
        g = self.groups[key]
        people_by_role, everyone = {}, []
        for i in g["units"]:
            p = assign[i]
            if p is None:
                continue
            u = self.units[i]
            everyone.append(p)
            for role in u.roles:
                people_by_role.setdefault(role, []).append(p)
        date, part = g["date"], g["part"]
        day = sq._day_name(date)
        prof = self.profile(date, day, part)
        dw = sq.DEMAND_WEIGHT.get(prof.demand, 1.0)
        wn = self.wn
        pen = 0.0
        if key[0] == "L":
            ok = any(self.leader[p] or (self.score[p] or 0) >= float(prof.leader_min_score) for p in everyone)
            return 0.0 if ok else K_LEAD_PROFILE * dw * wn.get("leadership", 1)
        role = g["role"]
        pool = people_by_role.get(role, [])
        for rule in self._rules_for(date, part, role):
            need = int(rule.get("count") or 1)
            if rule.get("attribute"):
                found = sum(1 for p in pool if self.leader[p])
            elif rule.get("min_score") is None:
                found = len(pool)
            else:
                found = sum(1 for p in pool if (self.score[p] or 0) >= float(rule["min_score"]))
            if found < need:
                pen += K_LEAD_RULE * (need - found) * dw * wn.get("leadership", 1)
        if self.rated_any and pool:
            target = next((float(v) for r, v in (prof.min_strength or {}).items() if _low(r) == role and v), None)
            rated = [p for p in pool if self.score[p] is not None]
            if target and rated:
                strength = sum(float(self.score[p]) for p in pool if self.score[p] is not None)
                ratio = min(1.0, strength / target)
                if ratio < 1.0:
                    pen += K_STRENGTH_GROUP * (1.0 - ratio) * dw * wn.get("operational_strength", 1)
                    if ratio < 0.55:
                        pen += K_STRENGTH_FLOOR * dw * wn.get("operational_strength", 1)
            if rated:
                best = max(float(self.score[p]) for p in rated)
                for p in rated:
                    if float(self.score[p]) <= 2 and best < float(self.score[p]) + 2:
                        pen += K_TRAINING * wn.get("training_balance", 1)
        return pen

    def dynamic(self, u, p, st) -> float:
        """What p on u adds given what p already carries this week. Never
        negative, so the static lower bound stays admissible."""
        wn = self.wn
        c = 0.0
        if u.closing and st.closes[p]:
            c += K_CLOSE * st.closes[p] * wn.get("fairness", 1)
        if u.weekend and st.weekends[p]:
            c += K_WEEKEND * st.weekends[p] * wn.get("fairness", 1)
        if u.hard and st.hards[p]:
            c += K_HARD * st.hards[p] * wn.get("fairness", 1)
            if st.hards[p] >= sq.HARD_SHIFT_CEILING:
                c += K_HARD_OVER * wn.get("fatigue", 1)
        if self.avoid[p]:
            for key in u.parts:
                on = st.on.get(key)
                if on:
                    for q in self.avoid[p]:
                        if on.get(q):
                            prof = self.profile(key[0], u.day, key[1])
                            c += K_AVOID * sq.DEMAND_WEIGHT.get(prof.demand, 1.0) * wn.get("pairings", 1)
        want = self.desired[p]
        if want:
            band = want * (1 + sq.PREFERENCE_HOURS_BAND)
            have = st.total[p]
            over = max(0.0, have + u.hours - band) - max(0.0, have - band)
            if over > 0:
                c += K_DESIRED * over * wn.get("preferences", 1)
        return c

    def leaf_penalty(self, persons, st) -> float:
        """Soft rules that are only known once a person's week is complete."""
        pen = 0.0
        full_req, part_req = self.days_off_rule
        for p in persons:
            mn = self.min_hours[p]
            if mn and st.total[p] + 0.05 < mn:
                pen += K_MIN_HOURS * (mn - st.total[p])
            req = part_req if self.employment[p] == "part" else full_req
            if req and self.week_dates:
                worked = st.dates[p]
                if sum(1 for d in self.week_dates if worked.get(d)) >= 2:
                    best = run = 0
                    for d in self.week_dates:
                        if worked.get(d):
                            run = 0
                        else:
                            run += 1
                            best = max(best, run)
                    if best < int(req):
                        pen += K_DAYS_OFF
        return pen

    def evaluate(self, assign) -> float:
        """The full cost of a complete assignment, from scratch — the search
        keeps the same number incrementally; tests hold the two together."""
        st = _State(self)
        static = self.static()
        total = 0.0
        for u in self.units:
            if u.fixed:
                continue
            p = assign[u.id]
            total += static[u.id][p] + self.dynamic(u, p, st)
            st.add(u, p)
        for k in self.groups:
            total += self.group_penalty(k, assign)
        return total + self.leaf_penalty(self.persons(), st)

    def persons(self) -> list:
        return sorted({p for u in self.units if not u.fixed for p in self.dom[u.id]})

    def feasible(self, assign) -> bool:
        """Every hard rule the search holds, checked from scratch."""
        by_p = {}
        for u in self.units:
            p = assign[u.id]
            if u.fixed:
                continue
            if p is None or p not in self.dom[u.id]:
                return False
            by_p.setdefault(p, []).append(u)
        for p, us in by_p.items():
            for a in range(len(us)):
                for b in range(a + 1, len(us)):
                    if self._conflict(us[a], us[b]):
                        return False
            hours = dict(self.fx_hours[p])
            ords = set(self.fx_ords[p])
            for u in us:
                hours[u.bucket] = hours.get(u.bucket, 0.0) + u.hours
                ords.add(u.dord)
            if any(h > self.cap[p] + 0.05 for h in hours.values()):
                return False
            if self.max_run and any(self._run_with(ords - {x}, x) > self.max_run for x in ords):
                return False
        for members in self.kgroups.values():
            if not any(assign[i] in self.keyholders for i in members):
                return False
        return True


class _State:
    """Per-person tallies the dynamic cost and the hard rules read."""

    def __init__(self, prob):
        P = len(prob.names)
        self.hours = [dict(h) for h in prob.fx_hours]
        self.dates = [dict(d) for d in prob.fx_dates]
        self.ords = [dict(o) for o in prob.fx_ords]
        self.total = [0.0] * P
        self.closes = [0] * P
        self.weekends = [0] * P
        self.hards = [0] * P
        self.on = {}
        for u in prob.units:
            if u.fixed and u.draft is not None:
                self._count(u, u.draft, +1)

    def _count(self, u, p, sign):
        self.total[p] += sign * u.hours
        if u.closing:
            self.closes[p] += sign
        if u.weekend:
            self.weekends[p] += sign
        if u.hard:
            self.hards[p] += sign
        for key in u.parts:
            on = self.on.setdefault(key, {})
            on[p] = on.get(p, 0) + sign

    def add(self, u, p):
        self.hours[p][u.bucket] = self.hours[p].get(u.bucket, 0.0) + u.hours
        self.dates[p][u.date] = self.dates[p].get(u.date, 0) + 1
        self.ords[p][u.dord] = self.ords[p].get(u.dord, 0) + 1
        self._count(u, p, +1)

    def remove(self, u, p):
        self.hours[p][u.bucket] -= u.hours
        self.dates[p][u.date] -= 1
        if not self.dates[p][u.date]:
            del self.dates[p][u.date]
        self.ords[p][u.dord] -= 1
        if not self.ords[p][u.dord]:
            del self.ords[p][u.dord]
        self._count(u, p, -1)


class _Search:
    """Backtracking with forward checking, most-constrained unit first, and
    branch-and-bound on an admissible bound, one component at a time."""

    def __init__(self, prob, deadline):
        self.prob = prob
        self.deadline = deadline
        self.static = prob.static()
        U = len(prob.units)
        self.assign = [None] * U
        for u in prob.units:
            if u.fixed:
                self.assign[u.id] = u.draft
        self.dom = [set(d) for d in prob.dom]
        self.minc = [min(self.static[i][p] for p in self.dom[i]) if (self.static[i] and self.dom[i]) else 0.0
                     for i in range(U)]
        self.st = _State(prob)
        self.left = {k: sum(1 for i in g["units"] if not prob.units[i].fixed) for k, g in prob.groups.items()}
        self.kh = {k: sum(1 for i in m if prob.units[i].fixed and prob.units[i].draft in prob.keyholders)
                   for k, m in prob.kgroups.items()}
        self.p_units = {}
        for u in prob.units:
            if not u.fixed:
                for p in self.dom[u.id]:
                    self.p_units.setdefault(p, []).append(u.id)
        # Each interchangeable group's people ranked by what they cost on it
        # (the members are identical, so any member's costs are the group's).
        self.rank = []
        for members in prob.sym_groups:
            first = members[0]
            order = sorted(self.dom[first], key=lambda q: (self.static[first][q], q))
            self.rank.append({q: k for k, q in enumerate(order)})
        self.trail = []
        self.nodes = 0
        self.fails = {}
        self.stop = self.timed_out = self.cut = False
        self.node_cap = None
        self.lns_rounds = 0

    def _tick(self):
        self.nodes += 1
        if self.nodes % _CHECK_EVERY == 0 and _time.monotonic() > self.deadline:
            self.stop = self.timed_out = True
        if self.node_cap is not None and self.nodes >= self.node_cap:
            self.stop = True

    def _remove(self, v, p):
        d = self.dom[v]
        d.discard(p)
        old = self.minc[v]
        self.trail.append((v, p, old))
        if d and self.static[v][p] <= old + 1e-12:
            self.minc[v] = min(self.static[v][q] for q in d)
            self.lb += self.minc[v] - old
        return bool(d)

    def _undo_to(self, mark):
        while len(self.trail) > mark:
            v, p, old = self.trail.pop()
            if self.dom[v]:
                self.lb += old - self.minc[v]
            else:
                self.lb += 0.0
            self.dom[v].add(p)
            self.minc[v] = old

    def _assign(self, u, p, marginal):
        """Place p on unit u and propagate. Returns (ok, undo record)."""
        prob = self.prob
        unit = prob.units[u]
        rec = [len(self.trail), marginal, []]
        self.assign[u] = p
        self.lb -= self.minc[u]
        self.g += marginal
        self.st.add(unit, p)
        for k in unit.groups:
            self.left[k] -= 1
            if self.left[k] == 0:
                pen = prob.group_penalty(k, self.assign)
                rec[2].append((k, pen))
                self.g += pen
        if p in prob.keyholders:
            for k in unit.kgroups:
                self.kh[k] += 1
        ok = True
        touched = set(unit.kgroups)
        # the same person may not also work anything this unit conflicts with
        for v in prob.conf.get(u, ()):
            if self.assign[v] is None and p in self.dom[v]:
                if not self._remove(v, p):
                    ok = False
                    self.fails[v] = self.fails.get(v, 0) + 1
                    break
                touched.update(prob.units[v].kgroups)
        # Interchangeable units are filled in position order with people in
        # ascending order of the group's own cost rank (so the order the
        # heuristic wants anyway), and the positions still open must have
        # at least as many people left as there are positions.
        if ok and unit.sym is not None:
            rank = self.rank[unit.sym]
            r = rank[p]
            later = [v for v in prob.sym_groups[unit.sym]
                     if self.assign[v] is None and prob.units[v].sym_pos > unit.sym_pos]
            for v in later:
                for q in list(self.dom[v]):
                    if rank.get(q, -1) <= r and not self._remove(v, q):
                        ok = False
                        break
                if not ok:
                    self.fails[v] = self.fails.get(v, 0) + 1
                    break
            if ok and later and len(self.dom[later[0]]) < len(later):
                ok = False
                self.fails[later[0]] = self.fails.get(later[0], 0) + 1
        # hours ceiling and the run of consecutive days for p's other units
        if ok:
            room = prob.cap[p] + 0.05
            ords = self.st.ords[p]
            for v in self.p_units.get(p, ()):
                if self.assign[v] is not None or p not in self.dom[v]:
                    continue
                vu = prob.units[v]
                bad = vu.hours + self.st.hours[p].get(vu.bucket, 0.0) > room
                if not bad and prob.max_run and vu.dord is not None and abs(vu.dord - unit.dord) <= prob.max_run:
                    bad = prob._run_with(ords, vu.dord) > prob.max_run
                if bad:
                    if not self._remove(v, p):
                        ok = False
                        self.fails[v] = self.fails.get(v, 0) + 1
                        break
                    touched.update(vu.kgroups)
        # Nobody works two units on one date, so the open units of a role on
        # a date need at least as many distinct people between them as there
        # are units (Hall's condition on that set). Checked for the dates
        # this assignment narrowed.
        if ok:
            dates = {unit.dgroup}
            for k in range(rec[0], len(self.trail)):
                dates.add(prob.units[self.trail[k][0]].dgroup)
            for key in dates:
                open_ = [v for v in prob.dgroups.get(key, ()) if self.assign[v] is None]
                if len(open_) > 1:
                    pool = set()
                    for v in open_:
                        pool |= self.dom[v]
                        if len(pool) >= len(open_):
                            break
                    if len(pool) < len(open_):
                        ok = False
                        self.fails[open_[0]] = self.fails.get(open_[0], 0) + 1
                        break
        # a manager or keyholder must still be possible on every daypart
        if ok:
            for k in touched:
                if self.kh.get(k, 1):
                    continue
                if not any(self.assign[i] is None and (self.dom[i] & prob.keyholders) for i in prob.kgroups[k]):
                    ok = False
                    break
        return ok, rec

    def _unassign(self, u, p, rec):
        prob = self.prob
        unit = prob.units[u]
        self._undo_to(rec[0])
        for k, pen in rec[2]:
            self.g -= pen
        for k in unit.groups:
            self.left[k] += 1
        if p in prob.keyholders:
            for k in unit.kgroups:
                self.kh[k] -= 1
        self.st.remove(unit, p)
        self.g -= rec[1]
        self.lb += self.minc[u]
        self.assign[u] = None

    def _select(self, comp):
        best, key = None, None
        dom = self.dom
        for i in comp:
            if self.assign[i] is not None:
                continue
            k = (len(dom[i]), -self.prob.units[i].dw, i)
            if key is None or k < key:
                best, key = i, k
        if best is not None and self.prob.units[best].sym is not None:
            # an interchangeable group is filled from its first open position
            for i in self.prob.sym_groups[self.prob.units[best].sym]:
                if self.assign[i] is None:
                    return i
        return best

    def _record(self, cost):
        snap = {i: self.assign[i] for i in self.full}
        self.best, self.best_assign = cost, snap
        self.incumbents.append((cost, snap))

    def _dfs(self, free, persons, disc):
        """Depth-first branch-and-bound over the open units in `free`. Stops
        gracefully (every frame undoes its own assignment) when the clock or
        a node cap says so; `disc` limits discrepancies, None is complete."""
        self._tick()
        if self.stop:
            return
        u = self._select(free)
        if u is None:
            total = self.g + self.prob.leaf_penalty(persons, self.st)
            if total < self.best - 1e-9:
                self._record(total)
            return
        unit = self.prob.units[u]
        vals = sorted(((self.static[u][p] + self.prob.dynamic(unit, p, self.st), p) for p in self.dom[u]))
        rest_lb = self.lb - self.minc[u]
        taken = 0              # values that survived propagation; the first is the heuristic's
        for c, p in vals:
            if self.stop:
                break
            if self.g + c + rest_lb >= self.best - 1e-9:
                break               # sorted: nothing after this can do better
            ok, rec = self._assign(u, p, c)
            if not ok:
                # refuted by forward checking: no branch is skipped, so it
                # costs no discrepancy
                self._unassign(u, p, rec)
                continue
            if taken and disc is not None and disc <= 0:
                self._unassign(u, p, rec)
                self.cut = True     # a live branch left unexplored: no proof this pass
                break
            if self.g + self.lb < self.best - 1e-9:
                self._dfs(free, persons, None if disc is None else disc - (1 if taken else 0))
            taken += 1
            self._unassign(u, p, rec)

    def _fix(self, units, values):
        """Place `values` on `units` in order; (ok, undo list)."""
        done = []
        for u in units:
            p = values.get(u)
            if p is None or p not in self.dom[u]:
                return False, done
            c = self.static[u][p] + self.prob.dynamic(self.prob.units[u], p, self.st)
            good, rec = self._assign(u, p, c)
            done.append((u, p, rec))
            if not good:
                return False, done
        return True, done

    def _release(self, done):
        for u, p, rec in reversed(done):
            self._unassign(u, p, rec)

    def _pass(self, free, persons, disc, until, node_cap=None):
        """One search pass; True when it ran to completion (nothing cut,
        clock not out), which is a proof for the units it searched."""
        self.deadline = until
        self.stop = self.timed_out = self.cut = False
        self.node_cap = (self.nodes + node_cap) if node_cap else None
        self._dfs(free, persons, disc)
        finished = not (self.stop or self.cut)
        self.stop, self.node_cap = False, None
        return finished

    def _neighbourhood(self, rng):
        """A handful of units to re-solve exactly, the rest held where the
        best week has them: one role over one to three days, or the whole
        week of two to four people in one role."""
        prob, inc = self.prob, self.best_assign
        role = rng.choice(self._roles)
        members = self._by_role[role]
        if rng.random() < 0.5:
            d0 = rng.choice(self._dords)
            width = rng.choice((1, 2, 3))
            free = [i for i in members if d0 <= prob.units[i].dord < d0 + width]
        else:
            people = sorted({inc[i] for i in members})
            rng.shuffle(people)
            pick = set(people[:rng.choice((2, 3, 4))])
            free = [i for i in members if inc[i] in pick]
        if len(free) > LNS_MAX_UNITS:
            free = rng.sample(free, LNS_MAX_UNITS)
        return sorted(free)

    def _lns(self, comp, persons, until):
        """Large-neighbourhood rounds: each frees a few units of the best
        week and re-solves them exactly under the same propagation and
        bound (capped in nodes), keeping any better week found. Seeded, so
        the same draft gives the same week."""
        import random as _random
        rng = _random.Random(7919 + len(comp))
        self._by_role = {}
        for i in comp:
            self._by_role.setdefault(tuple(sorted(self.prob.units[i].roles)), []).append(i)
        self._roles = sorted(self._by_role)
        self._dords = sorted({self.prob.units[i].dord for i in comp})
        while _time.monotonic() < until and self.best_assign is not None:
            free = self._neighbourhood(rng)
            self.lns_rounds += 1
            if not free:
                continue
            held = set(free)
            ok, done = self._fix([i for i in sorted(comp) if i not in held], self.best_assign)
            if ok:
                self._pass(free, persons, None, until, node_cap=LNS_NODES)
            self._release(done)

    def solve_component(self, comp, deadline):
        prob = self.prob
        start = _time.monotonic()
        self.full = comp
        self.g = 0.0
        self.lb = sum(self.minc[i] for i in comp)
        self.best, self.best_assign, self.incumbents = float("inf"), None, []
        self.draft_cost = None
        self.lns_rounds = 0
        persons = sorted({p for i in comp for p in self.dom[i]})
        # the draft, with interchangeable units' people put in order, as the
        # first incumbent when it keeps every hard rule the search holds
        target = {i: prob.units[i].draft for i in comp}
        for g, members in enumerate(prob.sym_groups):
            if members and members[0] in target:
                ps = [target[i] for i in members if target[i] is not None]
                if len(ps) == len(members) and all(q in self.rank[g] for q in ps):
                    for i, q in zip(members, sorted(ps, key=lambda q: self.rank[g][q])):
                        target[i] = q
        ok, done = self._fix(sorted(comp), target)
        if ok:
            total = self.g + prob.leaf_penalty(persons, self.st)
            self._record(total)
            self.draft_cost = total
        self._release(done)
        # 1. a first week, fast: the heuristic's own dive
        proved = self._pass(comp, persons, 0, deadline)
        if not proved and not self.timed_out:
            # 2. a short complete pass, which is where small parts get proved
            span = deadline - _time.monotonic()
            proved = self._pass(comp, persons, None, _time.monotonic() + max(0.05, 0.2 * span))
            if not proved and self.best_assign is None:
                # nothing legal found yet: widen the dive before anything else
                for disc in LDS_SCHEDULE[1:]:
                    proved = self._pass(comp, persons, disc, deadline)
                    if proved or self.timed_out or self.best_assign is not None:
                        break
            if not proved and self.best_assign is not None and _time.monotonic() < deadline:
                # 3. improve the week, 4. then try to prove it with what is left
                span = deadline - _time.monotonic()
                self._lns(comp, persons, _time.monotonic() + 0.8 * span)
                proved = self._pass(comp, persons, None, deadline)
        return {"units": len(comp), "proved": proved, "timed_out": not proved,
                "feasible": self.best_assign is not None, "cost": self.best if self.best_assign else None,
                "draft_cost": self.draft_cost, "assign": self.best_assign,
                "incumbents": list(self.incumbents), "seconds": round(_time.monotonic() - start, 3),
                "lns_rounds": self.lns_rounds}

    def commit(self, comp, assign):
        """Keep a component's answer placed, so later components see those
        people's hours, dates and conflicts (components share no person, so
        this only matters for bookkeeping, and is cheap)."""
        for i in sorted(comp):
            p = assign[i]
            self.assign[i] = p
            self.st.add(self.prob.units[i], p)
            for k in self.prob.units[i].groups:
                self.left[k] -= 1


def solve(rows, constraints, signals=None, weights=None, profiles=None, only_dates=None,
          max_seconds=DEFAULT_SECONDS, roster_roles=None, pending=None) -> dict:
    """Re-solve who works each of the draft's shifts.

    Returns {status, proved_optimal, seconds, slots, rows_in, people,
    components, components_proved, nodes, cost, draft_cost, infeasible,
    notes, rows, candidates}. `rows` is the best assignment found (the draft
    where a component found nothing); `candidates` are up to
    JUDGE_CANDIDATES distinct complete weeks, best first, for the judge.
    status is 'optimal' (every component proved), 'time_limit' (a feasible
    week, not proved best), 'infeasible' (some unit nobody may legally
    work, or a component with no legal arrangement), or 'nothing_to_solve'.
    """
    t0 = _time.monotonic()
    deadline = t0 + max(0.05, float(max_seconds))
    out = {"status": "nothing_to_solve", "proved_optimal": False, "seconds": 0.0, "slots": 0,
           "rows_in": len(rows or []), "people": 0, "components": 0, "components_proved": 0, "nodes": 0,
           "cost": None, "draft_cost": None, "infeasible": [], "notes": [], "rows": [dict(r) for r in rows or []],
           "candidates": [], "optimal_for": "the solver's cost model; Shift Quality judges the final choice"}
    if constraints is None or not rows:
        out["notes"].append("No rule set to solve against." if constraints is None else "No shifts.")
        return out
    relax = {}
    probe_until = t0 + RELAX_SHARE * max(0.05, float(max_seconds))
    for attempt in range(RELAX_ATTEMPTS):
        prob = Problem(rows, constraints, signals=signals, weights=weights, profiles=profiles,
                       only_dates=only_dates, roster_roles=roster_roles, pending=pending, relax=relax)
        if attempt == RELAX_ATTEMPTS - 1 or _time.monotonic() > probe_until:
            break
        culprits = _first_failures(prob, probe_until, attempt)
        if not culprits:
            break
        # Keep what no legal week can staff as drafted, named, and solve
        # everything else.
        relax.update(culprits)
    out["relaxed"] = len(relax)
    out["problem"] = prob
    out["people"] = len(prob.names)
    out["slots"] = sum(1 for u in prob.units if not u.fixed)
    out["notes"] = list(prob.notes)
    kept_for_note = sum(1 for u in prob.units if u.fixed and u.why_fixed == "has a written note the solver cannot read")
    if kept_for_note:
        out["notes"].append(f"{kept_for_note} shift{'s' if kept_for_note != 1 else ''} kept with people who have a "
                            "written note the solver cannot read.")
    out["infeasible"] = list(prob.infeasible)
    search = _Search(prob, deadline)
    comps = prob.components
    out["components"] = len(comps)
    final = [u.draft for u in prob.units]
    alt = []                     # per component: incumbents, best first
    proved_all, any_found, unsolved_proved, unsolved_timed = True, False, 0, 0
    unsolved = []
    remaining_units = sum(len(c) for c in comps) or 1
    for comp in comps:
        now = _time.monotonic()
        share = (deadline - now) * (len(comp) / float(remaining_units))
        remaining_units -= len(comp)
        res = search.solve_component(comp, min(deadline, now + max(0.02, share)))
        if res["feasible"]:
            any_found = True
            for i, p in res["assign"].items():
                final[i] = p
            search.commit(comp, res["assign"])
            alt.append((comp, sorted(res["incumbents"], key=lambda t: t[0])))
            out["cost"] = (out["cost"] or 0.0) + res["cost"]
            if res["draft_cost"] is not None:
                out["draft_cost"] = (out["draft_cost"] or 0.0) + res["draft_cost"]
            if res["proved"]:
                out["components_proved"] += 1
            else:
                proved_all = False
        else:
            proved_all = False
            unsolved.append(comp)
            # nothing found: the draft stays for these units, and they are
            # named — as infeasible when the search was complete
            if res["proved"]:
                unsolved_proved += 1
                worst = sorted(comp, key=lambda i: -search.fails.get(i, 0))[:3]
                for i in worst:
                    e = prob._infeasible_entry(prob.units[i])
                    e["text"] = ("No legal arrangement of the people who can work these shifts exists "
                                 "(hours, rest, days in a row and time off together).")
                    out["infeasible"].append(e)
            else:
                unsolved_timed += 1
                out["notes"].append(f"{len(comp)} shift{'s' if len(comp) != 1 else ''} kept as drafted: "
                                    "no legal arrangement was found in time.")
            for i in comp:
                search.assign[i] = prob.units[i].draft
    out["nodes"] = search.nodes
    out["seconds"] = round(_time.monotonic() - t0, 2)
    # every row left exactly as drafted because nothing legal could replace
    # it: set aside as infeasible, or in a part with no legal week found
    kept = {i for e in out["infeasible"] for i in e["rows"]}
    for comp in unsolved:
        for u in comp:
            kept.update(prob.units[u].idx)
    out["kept_rows"] = sorted(kept)
    # The status is the search's, over what it was given; shifts no legal
    # week can staff are listed in `infeasible` (kept as drafted) either way.
    if not comps:
        out["status"] = "infeasible" if out["infeasible"] else "nothing_to_solve"
    elif unsolved_proved:
        out["status"] = "infeasible"
    elif unsolved_timed:
        out["status"] = "no_solution_in_time"
    elif proved_all:
        out["status"] = "optimal"
    elif any_found:
        out["status"] = "time_limit"
    else:
        out["status"] = "no_solution_in_time"
    out["proved_optimal"] = out["status"] == "optimal"
    out["assignment"] = final
    out["rows"] = _rows_for(prob, final)
    # Distinct complete weeks for the judge: the best, then each component's
    # runner-up swapped in on its own.
    cands, seen = [], set()

    def _add(assign):
        key = tuple(assign)
        if key not in seen:
            seen.add(key)
            cands.append(_rows_for(prob, assign))
    _add(final)
    for comp, incs in sorted(alt, key=lambda t: -len(t[0])):
        for _cost, snap in incs[1:]:
            if len(cands) >= JUDGE_CANDIDATES:
                break
            a = list(final)
            for i, p in snap.items():
                a[i] = p
            _add(a)
    out["candidates"] = cands
    return out


def _first_failures(prob, deadline, attempt) -> dict:
    """{unit id: why} to keep as drafted so the rest of the week can be
    solved. For each component a quick look cannot staff: when the draft
    already overcommits somebody there (their hours, rest or days in a row
    broken), that person's drafted week in the most-failing role — kept
    exactly as it was, so no new breach; otherwise, once the look proves
    nothing legal exists, the units it emptied most often. {} when every
    component has a legal week or nothing more can be said."""
    out = {}
    search = _Search(prob, deadline)
    for comp in prob.components:
        now = _time.monotonic()
        budget = min(deadline - now, RELAX_PROBE_SECONDS)
        if budget <= 0:
            break
        search.full = comp
        search.g, search.lb = 0.0, sum(search.minc[i] for i in comp)
        search.best, search.best_assign, search.incumbents = float("inf"), None, []
        persons = sorted({p for i in comp for p in search.dom[i]})
        search.fails = {}
        # the heuristic's dive, then the complete pass; a pass that finishes
        # with nothing found is a proof that nothing legal exists
        finished = search._pass(comp, persons, 0, now + budget)
        if search.best_assign is not None:
            continue
        if not finished:
            finished = search._pass(comp, persons, None, now + budget)
        if search.best_assign is not None:
            continue
        by_role = {}
        for i in comp:
            key = tuple(sorted(prob.units[i].roles))
            by_role[key] = by_role.get(key, 0) + search.fails.get(i, 0)
        worst_role = max(by_role.items(), key=lambda kv: (kv[1], kv[0]))[0]
        in_role = [i for i in comp if tuple(sorted(prob.units[i].roles)) == worst_role]
        drafted = {}
        for i in in_role:
            d = prob.units[i].draft
            if d is not None and d in prob.draft_overcommitted:
                drafted.setdefault(d, []).append(i)
        if drafted:
            who, units = max(drafted.items(), key=lambda kv: (len(kv[1]), -kv[0]))
            why = (f"Kept as drafted: {prob.names[who]}'s drafted week is past their own limits and no legal "
                   "arrangement of the rest was found that takes enough of it.")
            for i in units:
                out[i] = why
            continue
        if not finished:
            continue           # unknown, and nobody overcommitted: leave it to the real search
        ranked = sorted(in_role, key=lambda i: (-search.fails.get(i, 0), i))
        for i in ranked[:max(1, len(comp) // 40) * (2 ** attempt)]:
            out[i] = ("Kept as drafted: no legal arrangement of the week has anybody here "
                      "(hours, rest, days in a row and time off together).")
    return out


def _rows_for(prob, assign) -> list:
    """The draft's rows with the assignment written in, interchangeable
    units matched back to the draft's people where the same people work
    them, so an identical rearrangement never reads as a change."""
    assign = list(assign)
    for members in prob.sym_groups:
        drafted = [prob.units[i].draft for i in members]
        got = [assign[i] for i in members]
        pool = list(got)
        placed = [None] * len(members)
        for k, d in enumerate(drafted):
            if d is not None and d in pool:
                placed[k] = d
                pool.remove(d)
        for k in range(len(members)):
            if placed[k] is None:
                placed[k] = pool.pop(0)
        for i, p in zip(members, placed):
            assign[i] = p
    rows = [dict(r) for r in prob.rows]
    for u in prob.units:
        p = assign[u.id]
        if p is None:
            continue
        for i in u.idx:
            rows[i]["employee"] = prob.names[p]
    return rows


# ── the judge ───────────────────────────────────────────────────────────────

def _flagged(rows, constraints):
    if constraints is None:
        return set()
    return {(_low(v.get("employee")), v.get("date") or "", v.get("shift_start") or "")
            for v in _rules.violations(rows, constraints) if v.get("no_show")}


def _hard(rows, constraints) -> set:
    if constraints is None:
        return set()
    return {(v["index"], v["kind"]) for v in _rules.violations(rows, constraints) if v.get("hard")}


def _objective(quality) -> float:
    scored = [s for s in (quality or {}).get("shifts") or [] if s.get("scored")]
    if not scored:
        return 0.0
    num = sum(s["score"] * sq.DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0) for s in scored)
    den = sum(sq.DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0) for s in scored)
    return num / (den or 1.0)


def improve(rows, inputs=None, signals=None, weights=None, constraints=None, only_dates=None,
            max_seconds=DEFAULT_SECONDS, min_gain=MIN_GAIN) -> dict:
    """Solve the draft's assignment and keep the answer only when Shift
    Quality scores the week higher and it breaks no hard rule the draft did
    not already break (judged row by row by the full rule sweep).

    Returns {applied, rows, before_score, after_score, quality, changes,
    stats, reason}. The input rows are never modified.
    """
    t0 = _time.monotonic()
    inputs = inputs or {}
    signals = dict(signals or {})
    constraints = constraints if constraints is not None else inputs.get("constraints")
    profiles = inputs.get("shift_profiles") or None
    base = [dict(r) for r in rows or []]

    def score(rs):
        sig = dict(signals)
        if constraints is not None:
            sig["flagged"] = _flagged(rs, constraints)
        return sq.score_rows(rs, profiles=profiles, weights=weights, **sig)

    before_q = score(base)
    out = {"applied": False, "rows": base, "before_score": before_q.get("score"), "after_score": before_q.get("score"),
           "quality": before_q, "changes": [], "stats": {}, "reason": ""}
    if not before_q.get("checked"):
        out["reason"] = "nothing to score"
        return out
    if constraints is None:
        out["reason"] = "no rule set"
        return out
    # The judge needs about a second per candidate on a big week; the search
    # gets what is left of the budget.
    judge_reserve = min(max_seconds * 0.25, 0.25 + 0.004 * len(base) * JUDGE_CANDIDATES)
    solved = solve(base, constraints, signals=signals, weights=weights, profiles=profiles, only_dates=only_dates,
                   max_seconds=max(0.1, max_seconds - (_time.monotonic() - t0) - judge_reserve),
                   roster_roles=inputs.get("roster_roles") or signals.get("roster_roles"),
                   pending=inputs.get("pending_time_off") or getattr(constraints, "pending_off", None))
    prob = solved.pop("problem", None)
    stats = {k: solved[k] for k in ("status", "proved_optimal", "seconds", "slots", "rows_in", "people",
                                    "components", "components_proved", "nodes", "cost", "draft_cost",
                                    "infeasible", "notes", "optimal_for")}
    out["stats"] = stats
    before_hard = _hard(base, constraints)
    base_obj = _objective(before_q)
    best = None
    judged = 0
    for cand in solved.get("candidates") or []:
        if cand == base:
            continue
        new_hard = _hard(cand, constraints) - before_hard
        if new_hard:
            continue            # never: a better score is not worth a breach
        q = score(cand)
        judged += 1
        if not q.get("checked"):
            continue
        obj = _objective(q)
        if best is None or obj > best[0]:
            best = (obj, cand, q)
    stats["judged"] = judged
    stats["seconds_total"] = round(_time.monotonic() - t0, 2)
    if best is None or best[0] < base_obj + min_gain:
        out["reason"] = ("the draft's own assignment was already the best the solver found"
                         if best is None or best[0] <= base_obj else "the solver's week scored no better")
        stats["kept"] = "draft"
        stats["after_score"] = best[2].get("score") if best else before_q.get("score")
        return out
    obj, cand, q = best
    stats["kept"] = "solver"
    rows_out = [dict(r) for r in cand]
    out.update(applied=True, rows=rows_out, after_score=q.get("score"), quality=q,
               changes=_describe(prob, base, rows_out, before_q, q, obj - base_obj))
    stats["after_score"] = q.get("score")
    return out


def _dims(q) -> dict:
    return {d["key"]: d for d in (q or {}).get("dimensions") or []}


def _describe(prob, before_rows, after_rows, before_q, after_q, gain) -> list:
    """The changes in the optimizer's shape ({kind, reason, gain}): one line
    for the week, then each reassignment with why."""
    changed = []
    for u in prob.units:
        i0 = u.idx[0]
        old = (before_rows[i0].get("employee") or "").strip()
        new = (after_rows[i0].get("employee") or "").strip()
        if _low(old) != _low(new):
            changed.append((u, old, new))
    for idx_u, old, new in changed:
        for i in idx_u.idx:
            note = (after_rows[i].get("notes") or "").strip()
            tag = f"{NOTE_TAG} solver put {new} here (was {old or 'nobody'})"
            after_rows[i]["notes"] = (note + (" — " if note else "") + tag)[:240]
    bd, ad = _dims(before_q), _dims(after_q)
    moved = sorted(((ad[k]["score"] - bd[k]["score"], ad[k]["label"], bd[k]["score"], ad[k]["score"])
                    for k in ad if k in bd and ad[k]["score"] != bd[k]["score"]), key=lambda t: -t[0])
    ups = [f"{label.lower()} {b} → {a}" for d, label, b, a in moved if d > 0][:3]
    head = (f"Cavnar re-solved who works each shift ({len(changed)} change{'s' if len(changed) != 1 else ''}), "
            f"Shift Quality {before_q.get('score')} → {after_q.get('score')}"
            + (f": {', '.join(ups)}" if ups else "") + ".")
    out = [{"kind": "solve", "reason": head, "gain": round(gain, 1)}]
    st = _State(prob)
    after_assign = [prob.pidx.get(_low(after_rows[u.idx[0]].get("employee"))) for u in prob.units]
    for u in prob.units:
        if after_assign[u.id] is not None:
            st.add(u, after_assign[u.id])
    for u, old, new in changed[:MAX_LISTED_CHANGES]:
        r = after_rows[u.idx[0]]
        why = _why(prob, u, prob.pidx.get(_low(old)), prob.pidx.get(_low(new)), st)
        out.append({"kind": "reassign",
                    "reason": f"Put {new} on {_where(u.day, u.primary[0])} {u.role_label} "
                              f"({r.get('shift_start')}–{r.get('shift_end')}) instead of {old or 'nobody'} — {why}.",
                    "gain": None})
    if len(changed) > MAX_LISTED_CHANGES:
        out.append({"kind": "reassign", "reason": f"…and {len(changed) - MAX_LISTED_CHANGES} more reassignments "
                                                  "of the same kind.", "gain": None})
    return out


_WHY = {
    "pending": lambda P, u, o, n: f"{P.names[o]} has asked for that day off",
    "strength": lambda P, u, o, n: (f"{P.names[n]} is rated {P.score[n]:g}"
                                    + (f" to {P.names[o]}'s {P.score[o]:g}" if P.score[o] is not None else "")
                                    + f" on a {u.demand}-demand shift") if P.score[n] is not None else
                                   f"it is a {u.demand}-demand shift",
    "reliability": lambda P, u, o, n: f"{P.names[o]} has missed {int(round(P.no_show[o] * 100))}% of scheduled shifts",
    "experience": lambda P, u, o, n: f"{P.names[n]} is one of the experienced hands",
    "stability_day": lambda P, u, o, n: f"{P.names[n]} usually works {u.day}s",
    "stability_part": lambda P, u, o, n: f"{P.names[n]} usually works that daypart",
    "preference": lambda P, u, o, n: f"{P.names[o]} asked for {'nights' if 'night' in P.pref_parts[o] else 'days'}",
    "cross": lambda P, u, o, n: f"{P.names[n]} can cover a second station",
    "role": lambda P, u, o, n: f"it is {P.names[n]}'s own role",
}


def _why(prob, u, old, new, st) -> str:
    if new is None:
        return "a better week overall"
    if old is None or old not in range(len(prob.names)):
        return "the draft's person could not legally work it"
    if prob.fits(u, old):
        return f"{prob.names[old]} could not legally work it ({prob.fits(u, old)})"
    co, cn = prob.components_of(u, old), prob.components_of(u, new)
    diffs = sorted(((co.get(k, 0) - cn.get(k, 0), k) for k in set(co) | set(cn) if k != "change"), reverse=True)
    if diffs and diffs[0][0] > 0.05 and diffs[0][1] in _WHY:
        try:
            return _WHY[diffs[0][1]](prob, u, old, new)
        except Exception:
            pass
    if u.closing and st.closes[old] > st.closes[new]:
        return f"spreads the closes ({prob.names[old]} has {st.closes[old]} this week, {prob.names[new]} {st.closes[new]})"
    if u.weekend and st.weekends[old] > st.weekends[new]:
        return f"spreads the weekend shifts ({prob.names[old]} has {st.weekends[old]}, {prob.names[new]} {st.weekends[new]})"
    if u.groups:
        return "so the shift's leadership, strength and training cover add up"
    return "part of a better week overall"


def summary(result: dict) -> dict:
    """What travels with the week: whether the solver ran and was kept, its
    changes, and its stats. Owner-facing text says what changed; the stats
    are for the admin and the replay script."""
    stats = dict(result.get("stats") or {})
    return {"ran": True, "applied": bool(result.get("applied")), "before_score": result.get("before_score"),
            "after_score": result.get("after_score"), "changes": list(result.get("changes") or []),
            "reason": result.get("reason") or "", "status": stats.get("status"),
            "proved_optimal": bool(stats.get("proved_optimal")), "seconds": stats.get("seconds_total", stats.get("seconds")),
            "slots": stats.get("slots"), "people": stats.get("people"), "components": stats.get("components"),
            "components_proved": stats.get("components_proved"), "nodes": stats.get("nodes"),
            "infeasible": [{k: e.get(k) for k in ("date", "day", "role", "shift_start", "shift_end", "kept", "text")}
                           for e in (stats.get("infeasible") or [])][:10],
            "notes": list(stats.get("notes") or [])[:6], "kept": stats.get("kept"),
            "optimal_for": stats.get("optimal_for")}


def merge_into_optimizer(optimizer: dict, solver: dict) -> dict:
    """Fold the solver's changes into the optimizer's summary, which is what
    both clients render as "what Cavnar changed": the solver's lines first,
    the week's before-score from before the solver ran."""
    opt = dict(optimizer or {"ran": False})
    if not (solver and solver.get("ran")):
        return opt
    opt["solver"] = {k: solver.get(k) for k in ("applied", "status", "proved_optimal", "seconds", "slots", "people",
                                                "components", "components_proved", "nodes", "kept", "before_score",
                                                "after_score", "infeasible", "notes")}
    if not solver.get("applied"):
        return opt
    changes = list(solver.get("changes") or []) + list(opt.get("changes") or [])
    before = solver.get("before_score")
    after = opt.get("after_score") if opt.get("ran") and opt.get("after_score") is not None else solver.get("after_score")
    opt.update(ran=True, applied=True, changes=changes, before_score=before, after_score=after,
               improvement=(after or 0) - (before or 0) if before is not None and after is not None else 0)
    n = len([c for c in changes if c.get("kind") != "solve"])
    opt["verdict"] = (f"Cavnar made {n} change{'s' if n != 1 else ''} to the draft, raising Shift Quality "
                      f"from {before} to {after}. Each is listed with why.")
    opt.setdefault("unresolved", [])
    return opt
