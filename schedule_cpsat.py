"""
schedule_cpsat.py — who works each shift, solved exactly (OR-Tools CP-SAT).

Stage 2 of the solver-first draft (scheduler audit 10/10/26 LOGIC-24, RM-11):
schedule_shapes decides which shifts the week has (Stage 1); this module
decides who works each of them, as one joint model with no pass order
(LOGIC-28). The repair loop's solver stage uses it to re-solve a draft's
assignment, and the Studio's re-solve (MARKET-13) uses it to answer "Maria
is out, re-solve Friday" in seconds. schedule_rules.violations() stays the
independent oracle: whatever this returns is swept by it like any draft.

Variables: x[u, p] — person p works unit u (a shift, or the rows one person
works on one date, as schedule_solver.Problem reads them), for the people
the rule set lets work it (CpProblem.fits: roster, availability by day and
daypart, approved time off and partial days, personal windows, a minor's
band and caps, certificates, the roles a person holds, whom code may choose
— Constraints.fillable — and never a training shift handed out); and
unfilled[u], so the model is never infeasible.

Hard (never traded for cost): one person per shift, one shift a date per
person, no overlap (the published tail counts), each person's OWN weekly
maximum (the one the owner set, a salaried cap, a minor's week), and rest
and days in a row where the jurisdiction makes them law. A row the manager
plan or the owner pinned or LOCKED (contract #4) is kept exactly.

Soft, every one a named cost the owner can be shown, in the owner's rank
order (schedule_rules.TIER_TABLE: legal > manager > coverage > policy >
overtime > min hours > requirements > budget > quality), each tier's weight
far above everything below it:

  manager      a stretch with no manager on the floor (per minute)
  floor        a shift a staffing floor needs left unfilled (per hour)
  closer       a role's close without one of its closers
  policy       hours past the restaurant's weekly ceiling, rest shorter
               than its rule, a day past its days-in-a-row rule (where they
               are policy, not law — LOGIC-1)
  overtime     hours past a person's overtime line, priced at 1.5x their
               rate on top — so it happens only where no teammate with room
               can take the shift (the owner's teammate rule falls out of
               the cost)
  min_hours    hours under someone's minimum
  requirement  a SHIFT REQUIREMENTS shift left unfilled (per hour)
  budget       dollars past the hourly labor budget
  quality      labor dollars at each person's rate; STABILITY against the
               last PUBLISHED week per slot (same person, weekday, daypart,
               role family, start within 30 min — RM-3, about a tenth of
               the quality objective, LOGIC-18); stated and learned
               preferences; fairness of closes, weekends and busy shifts;
               leader rules; strength (one rating measure — LOGIC-30); the
               scheduling memory's per-slot costs; reliability; the role's
               own people first

Shift Quality is the owner's report, not this objective (LOGIC-19): its
caps and renormalisation plateau a search; every term here is a separable
linear piece, so each assignment's reason is its marginal cost ("Kim, not
Lee: Lee would hit overtime +$27" — RM-12), and when a requirement cannot
be met the smallest set of the owner's rules that conflict is found with
assumption literals (explain_conflicts, LOGIC-32).

Deterministic: a fixed seed, a deterministic time budget and interleaved
workers (SCHEDULE_CPSAT_DETERMINISTIC, on by default) — same inputs, same
week. Warm-started from last published week, else the draft. Nothing here
calls a model, reads the database or writes anything.
"""
import math
import os
import time as _time
from datetime import datetime

import schedule_objective as _obj
import schedule_rules as _rules
import schedule_solver as _solver
import shift_quality as sq

try:                                    # the dependency is locked; a box without it falls back
    from ortools.sat.python import cp_model as _cp
except Exception:                       # pragma: no cover - exercised only without ortools
    _cp = None


def _env_float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return float(default)


DEFAULT_SECONDS = _env_float("SCHEDULE_CPSAT_SECONDS", 20.0)
WORKERS = max(1, int(_env_float("SCHEDULE_CPSAT_WORKERS", 4)))
SEED = 7
DETERMINISTIC = str(os.getenv("SCHEDULE_CPSAT_DETERMINISTIC", "1")).strip() != "0"

# ── the weights: one integer unit, tiers far apart ───────────────────────
# Q units are one Shift Quality point of one shift of demand weight 1 (the
# old solver's unit, schedule_solver), at hundredth-of-a-point precision.
Q = 100
W_MANAGER_MIN = 10 ** 11            # a minute with no manager on the floor
W_CLOSER = 4 * 10 ** 10              # a role's close without a closer
W_COVER_HOUR = 10 ** 10              # an hour of a floor shift left unfilled
W_POLICY_HOUR = 3 * 10 ** 8         # an hour past the restaurant's weekly ceiling (or a salaried limit)
W_POLICY_REST_HOUR = 3 * 10 ** 8    # an hour of rest short of the restaurant's rule
W_POLICY_RUN_DAY = 2 * 10 ** 9      # a day past the restaurant's days-in-a-row rule
W_OT_HOUR = 10 ** 8                 # an hour past a person's overtime line (plus its premium in dollars)
W_MIN_HOUR = 3 * 10 ** 7            # an hour under someone's minimum
W_REQ_HOUR = 10 ** 7                # an hour of a requirement shift left unfilled, at an average half hour
REQ_VALUE_BOUNDS = (0.1, 8.0)       # its demand x criticality against average: kept below the overtime tier
# The lexicographic order of the solve (LOGIC-22, LOGIC-1, LOGIC-10): every
# rule tier and the owner's words first; then keeping last published week's
# (else the usual) person on each slot; then every quality tie-break —
# fairness can never again decide which shift goes empty.
PHASE_RULES = frozenset({"manager", "floor", "closer", "policy_hours", "policy_rest", "policy_run", "overtime",
                         "min_hours", "requirement", "budget", "leadership", "pending", "memory", "commitment"})
PHASE_STABILITY = frozenset({"stability", "change", "usual"})
PHASE_SHARES = (0.6, 0.15, 0.25)    # each phase's share of the deterministic work budget
W_CHANGE_RESOLVE = 5 * 10 ** 6      # a re-solve changing a person on a shift that stays
BUDGET_OVER_FACTOR = 3              # a dollar past the budget costs this many dollars of quality
K_CHANGE_IMPROVE = 0.02             # points: the repair loop's tie-break toward the draft
K_CLOSE, K_WEEKEND, K_HARD = _solver.K_CLOSE, _solver.K_WEEKEND, _solver.K_HARD
STABILITY_START_MINUTES = 30        # "the same slot": start within this of last week's
H = 100                             # hours are hundredths in the model

TERM_LABELS = {
    "manager": "a stretch with no manager on the floor",
    "floor": "a shift a staffing floor needs, unfilled",
    "closer": "a close without one of the role's closers",
    "policy_hours": "hours past the restaurant's weekly ceiling",
    "policy_rest": "rest shorter than the restaurant's rule",
    "policy_run": "days past the days-in-a-row rule",
    "overtime": "overtime",
    "min_hours": "hours under someone's minimum",
    "requirement": "a requirement shift, unfilled",
    "budget": "dollars past the hourly budget",
    "dollars": "labor dollars",
    "stability": "a shift changed from last published week",
    "change": "a shift changed from the week as it stands",
    "fairness": "closes, weekends and busy shifts spread",
    "leadership": "leader rules",
    "strength": "strength against target",
    "preferences": "staff preferences",
    "memory": "what the scheduling memory learned",
    "reliability": "reliability",
    "role": "a fill-in from another role",
    "pending": "a day someone asked off",
    "hours_wanted": "the hours people asked for",
    "standby": "a standby on a shift likely to come up short",
    "commitment": "a shift staff were told is committed, moved",
    "retention_hours": "hours taken from somebody at risk of leaving",
    "retention_slot": "somebody at risk of leaving on a slot they keep dropping",
    "rotation_tips": "the rotation of the best-tipped shifts",
    "usual": "a shift off somebody's usual days and slots",
    "days": "days in past somebody's usual days a week",
}
DOUBLE_BREAK_MINUTES = 60           # the least break between the two legs of a double
_STATIC_TERMS = {"pending": "pending", "reliability": "reliability", "rotation_weekend": "fairness",
                 "rotation_close": "fairness", "preference": "preferences", "learned_preference": "preferences",
                 "role": "role", "learned": "memory", "likely_edit": "memory",
                 # a published shift handed to somebody else, at its commitment
                 # level (J3's commitment ladder, MARKET-27)
                 "commitment": "commitment"}


_OWN_TERMS = frozenset({"dollars", "stability", "change"})


def available() -> bool:
    """Whether OR-Tools is importable here (the old backtracking solver is
    the fallback when it is not, or when CP-SAT finds nothing in time)."""
    return _cp is not None


def _low(s) -> str:
    return " ".join(str(s or "").lower().split())


def _ordinal(d):
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").toordinal()
    except (TypeError, ValueError):
        return None


def _minute_of(dt) -> int:
    return int(round((dt - datetime(2000, 1, 1)).total_seconds() / 60.0))


def is_locked(row) -> bool:
    """A row the owner pinned (contract #4: `locked`), the manager plan's
    (`_pinned`): kept exactly by every solve."""
    if not row:
        return False
    if row.get("_pinned"):
        return True
    try:
        import schedule_rows as _srows
        return bool(_srows.is_locked(row))
    except ImportError:                     # pragma: no cover - the one row format is part of the app
        v = row.get("locked")
        return v is True or str(v).strip().lower() in ("1", "true", "yes")


# ── the problem: legality from the rule set, nothing else hard ────────────

class CpProblem(_solver.Problem):
    """schedule_solver.Problem with only what is LEGAL kept hard: a person's
    own maximum (never the restaurant's ceiling, never the overtime line),
    rest and days in a row only where the jurisdiction makes them law; the
    rest are the model's priced terms. No unit is fixed for want of people —
    the model's `unfilled` says so instead."""

    def __init__(self, rows, constraints, **kw):
        rows = [dict(r, _pinned=True) if is_locked(r) else dict(r) for r in rows or []]
        self.law_rest = bool(constraints is not None and _rules.law_backed(constraints, "min_rest_hours"))
        self.law_run = bool(constraints is not None and _rules.law_backed(constraints, "max_consecutive_days"))
        super().__init__(rows, constraints, **kw)

    def _build_caps(self):
        c = self.c
        self.draft_hours = [dict(h) for h in self.base_hours]
        self.capb = []
        for p, n in enumerate(self.names):
            if c is None:
                cap = _rules.SALARIED_HOURS_MAX
            elif c.is_salaried(n) or c.own_max(n):
                cap = float(c.max_hours(n))
            else:
                cap = float(_rules.SALARIED_HOURS_MAX)
            caps = {}
            for b in {u.bucket for u in self.units} | set(self.base_hours[p]):
                cb = cap
                if c is not None and self.key(n) in c.minors:
                    br = _rules.minor_rules(c.minor_bands.get(self.key(n)), c.jurisdiction)
                    wk = br.get("max_weekly_school_week")
                    if wk:
                        cb = min(cb, float(wk))
                caps[b] = cb
            self.capb.append(caps)

    def capf(self, p, b) -> float:
        return self.capb[p].get(b, self.maxh[p])

    def rest_need(self, p) -> float:
        """Hours of rest the restaurant's rule asks of p (0: none)."""
        if not self.need_rest or self.c is None or not _rules.rest_rule_applies(self.c, self.names[p]):
            return 0.0
        return float(self.need_rest)

    def run_limit(self, p) -> int:
        if not self.max_run or self.c is None or not _rules.hours_rules_apply(self.c, self.names[p]):
            return 0
        return int(self.max_run)

    def fits(self, u, p):
        """None when p may legally work u; else why, in the sweep's words.
        Hours, rest and days in a row are the model's (rest and a run here
        only where they are law, against the published tail)."""
        c, name = self.c, self.names[p]
        low = self.key(name)
        if not u.roles <= self.roles[p]:
            return "does not work " + "/".join(sorted(u.roles))
        if low in self.constrained and u.draft != p:
            return "has a written note the solver cannot read"
        if u.day in self.unavailable.get(low, ()):
            return _rules.LABELS["unavailable_day"]
        drafted = p == u.draft
        if c is None:
            return None
        if not drafted:
            ok, why = c.fillable(name, u.date)
            if not ok:
                return why
        for r in u.rows:
            for part in sq.touched_dayparts(r):
                ok, why = c.can_work(name, u.date, part)
                if not ok:
                    return why
            ok, why = c.window_ok(name, u.date, r.get("shift_start", ""), r.get("shift_end", ""))
            if not ok:
                return why
            ok, why = c.cert_ok(name, r.get("role", ""), u.date)
            if not ok:
                return why
            if not c.holds(name, r.get("role", ""), u.date):
                return _rules.LABELS["role_not_held"]
            if not drafted and c.training_row(dict(r, employee=name)):
                return "would be a training shift"
            if low in c.minors:
                latest, _label = _rules.minor_latest(c, low, u.date)
                e_m, s_m = _rules.end_minutes(r), _rules.start_minutes(r)
                if latest is not None and e_m is not None and e_m > latest:
                    return _rules.LABELS["minor_late"]
                earliest = _rules.minor_earliest(c, low)
                if earliest is not None and s_m is not None and s_m < earliest:
                    return _rules.LABELS["minor_early"]
            ok, why = c.rest_ok(name, r, [])
            if not ok and (why == _rules.LABELS["overlap"] or self.law_rest):
                return why
        if low in c.minors:
            mcap, _t = _rules.minor_daily_cap(c, low, u.date)
            if mcap and u.hours > mcap + 0.01:
                return _rules.LABELS["minor_hours"]
        if u.hours + float(self.base_hours[p].get(u.bucket, 0.0)) > self.capf(p, u.bucket) + 0.05:
            return _rules.LABELS["over_max_hours"]
        if self.law_run and self.run_limit(p) and self._run_with(self.base_ords[p], u.dord) > self.run_limit(p):
            return _rules.LABELS["long_run"]
        return None

    def _build_domains(self):
        U, P = len(self.units), len(self.names)
        self.dom = [set() for _ in range(U)]
        self.reasons = [dict() for _ in range(U)]
        self.infeasible = []
        self._fixed_state()
        for u in self.units:
            if u.fixed:
                continue
            for p in range(P):
                why = self.fits(u, p)
                if why is None and u.hours + float(self.fx_hours[p].get(u.bucket, 0.0) or 0.0) > \
                        self.capf(p, u.bucket) + 0.05:
                    why = _rules.LABELS["over_max_hours"]
                if why is None:
                    for f in self.fx_units[p]:
                        if f.date == u.date or self._overlaps(u, f):
                            why = "already on another shift that day" if f.date == u.date else _rules.LABELS["overlap"]
                            break
                if why is None:
                    self.dom[u.id].add(p)
                elif u.roles <= self.roles[p]:
                    self.reasons[u.id][self.names[p]] = why
        self.conf = {}
        self.dgroups = {}
        self.sym_groups, self.sym_draft = [], []

    @staticmethod
    def _overlaps(a, b) -> bool:
        for s1, e1 in a.spans:
            for s2, e2 in b.spans:
                if s1 is not None and s2 is not None and s2 < e1 and s1 < e2:
                    return True
        return False

    def _manager_groups(self):
        return None

    def _closer_groups(self):
        return None

    def _build_components(self):
        self.components = []


# ── last published week, slot by slot (RM-3) ─────────────────────────────

def last_week_slots(prob, last_rows) -> dict:
    """{unit id: set(person)} — who worked each unit's slot in the last
    PUBLISHED week: the same weekday, the same role family, the same
    daypart, and a start within STABILITY_START_MINUTES (LOGIC-18, RM-3)."""
    by = {}
    for r in last_rows or []:
        name = (r.get("employee") or "").strip()
        p = prob.pidx.get(prob.key(name)) if name else None
        if p is None:
            continue
        day = sq._day_name(r.get("date") or "", r.get("day") or "")
        s = _rules.start_minutes(r)
        if not day or s is None:
            continue
        part = (sq.present_dayparts(r) or ["unknown"])[0]
        by.setdefault((day, prob.family(r.get("role")), part), []).append((s, p))
    out = {}
    for u in prob.units:
        if u.fixed or not u.rows:
            continue
        r = u.rows[0]
        s = _rules.start_minutes(r)
        key = (u.day, prob.family(r.get("role")), u.primary[0] if u.primary else "unknown")
        got = {p for s0, p in by.get(key, ()) if s is not None and abs(s0 - s) <= STABILITY_START_MINUTES}
        if got:
            out[u.id] = got
    return out


# ── the model ──────────────────────────────────────────────────────────────

class _Book:
    """Every objective term, by name, so the solution's cost can be said
    term by term (what the owner is shown) and each tier weighed apart."""

    def __init__(self, m):
        self.m = m
        self.terms = []                 # (name, var/expr, coef, meta)
        self.const = {}

    def add(self, name, expr, coef, meta=None):
        coef = int(round(coef))
        if coef:
            self.terms.append((name, expr, coef, meta))

    def constant(self, name, value):
        self.const[name] = self.const.get(name, 0) + int(round(value))

    def objective(self):
        return sum(coef * expr for _n, expr, coef, _m in self.terms) + sum(self.const.values())


def _rate_cents(prob, p, u) -> int:
    role = u.role_at.get(u.primary[0], "") if u.primary else ""
    return int(round(prob.rate(p, role) * 100))


def _ot_rate_cents(prob, p) -> int:
    rate = prob.person_rates.get(" ".join(prob.names[p].lower().split())) or prob.blended or 15.0
    return int(round(rate * 100))


class _Model:
    def __init__(self, prob, mode, last_week, budget_dollars, change_cost, objective=None, usual=None,
                 doubles=None):
        self.prob, self.mode = prob, mode
        self.m = _cp.CpModel()
        self.book = _Book(self.m)
        self.x = {}
        self.unf = {}
        self.last = last_week or {}
        # {unit id: set(person)} — who USUALLY works the slot (their own
        # history), where there is no published week to keep (STRESS-15)
        self.usual = usual or {}
        self.budget = float(budget_dollars or 0)
        self.change_cost = change_cost
        self.live = [u for u in prob.units if not u.fixed]
        self.manages = {}
        self.notes = []
        self.ctx = objective
        # people who work doubles: two legs a date with a break (LOGIC-19)
        self.doubles = set(doubles or ())

    def mgr(self, p, d) -> bool:
        k = (p, d)
        if k not in self.manages:
            self.manages[k] = bool(self.prob.manages(p, d))
        return self.manages[k]

    def value_rel(self, u) -> float:
        """A requirement shift's value against an average one: the mean of
        its half hours' demand weight x the role's criticality
        (schedule_objective.slot_value's own factors), held inside
        REQ_VALUE_BOUNDS so the requirement tier stays below overtime."""
        role = u.role_label
        ctx = self.ctx
        vals = []
        for r in u.rows:
            sp = _rules._span(r)
            if not sp:
                continue
            for t in range(sp[0] - sp[0] % 30, sp[1], 30):
                if sp[0] <= t and t + 30 <= sp[1]:
                    if ctx is not None:
                        vals.append(ctx.demand_weight(u.date, t) * ctx.criticality(role))
                    else:
                        vals.append(float(u.dw or 1.0) * _obj.default_criticality(role))
        v = sum(vals) / len(vals) if vals else float(u.dw or 1.0) * _obj.default_criticality(role)
        return max(REQ_VALUE_BOUNDS[0], min(REQ_VALUE_BOUNDS[1], v))

    # quality points per dollar: LABOR_POINTS_PER_PCT for 1% of the week's labor
    def _per_cent(self) -> float:
        import schedule_optimizer as _opt
        prob = self.prob
        est = 0.0
        for u in prob.units:
            ps = [u.draft] if u.draft is not None else sorted(prob.dom[u.id])[:5]
            rates = [prob.rate(p, u.role_at.get(u.primary[0], "") if u.primary else "") for p in ps if p is not None]
            rates = [r for r in rates if r > 0]
            if rates:
                est += u.hours * (sum(rates) / len(rates))
        if est <= 0:
            return 0.0
        return _opt.LABOR_POINTS_PER_PCT * 100.0 / est * prob.sigma_dw * Q / 100.0

    def build(self):
        prob, m, book = self.prob, self.m, self.book
        x, unf = self.x, self.unf
        # assignment
        for u in self.live:
            for p in sorted(prob.dom[u.id]):
                x[(u.id, p)] = m.new_bool_var(f"x{u.id}_{p}")
            unf[u.id] = m.new_bool_var(f"u{u.id}")
            m.add_exactly_one([x[(u.id, p)] for p in sorted(prob.dom[u.id])] + [unf[u.id]])
        self.by_person = {}
        for (uid, p), v in x.items():
            self.by_person.setdefault(p, []).append((prob.units[uid], v))
        self._unfilled()
        self._per_person()
        self._manager()
        self._closer()
        self._leaders()
        self._static_and_stability()
        self._slot_memory()
        self._fairness()
        self._dollars()
        self._days()
        m.minimize(book.objective())
        self._hints()

    def _unfilled(self):
        prob = self.prob
        per_cent = self._per_cent()
        for u in self.live:
            hours = max(0.25, u.hours)
            if any(r.get("_standby") for r in u.rows):
                # worth twice its wage in the quality terms: filled while the
                # week is under budget, left when it would push it over
                # (a dollar over costs BUDGET_OVER_FACTOR)
                cents = hours * (prob.blended or 15.0) * 100
                self.book.add("standby", self.unf[u.id], max(1.0, 2 * cents * per_cent), {"unit": u.id})
                continue
            if self.mode == "draft":
                floor = any(r.get("_floor") for r in u.rows)
                # a requirement shift's hole weighs its half hours' demand
                # and the role's criticality (LOGIC-1, LOGIC-24): a Friday
                # dinner dishwasher is never the cheap one to leave empty
                rel = 1.0 if floor else self.value_rel(u)
                self.book.add("floor" if floor else "requirement", self.unf[u.id],
                              hours * (W_COVER_HOUR if floor else W_REQ_HOUR * rel), {"unit": u.id, "rel": rel})
            else:
                # A shift on the week as it stands: dropping it is never a
                # saving; kept as drafted when nobody can legally take it.
                self.book.add("floor", self.unf[u.id], hours * W_COVER_HOUR * 2, {"unit": u.id})

    def _per_person(self):
        prob, m, book, c = self.prob, self.m, self.book, self.prob.c
        self.hours_expr = {}
        self.worked = {}
        for p, items in self.by_person.items():
            name = prob.names[p]
            rules_apply = c is None or _rules.hours_rules_apply(c, name)
            # one shift a date — or, for somebody who works doubles, two legs
            # that never overlap with a break between them (LOGIC-19)
            dbl = p in self.doubles
            by_date = {}
            for u, v in items:
                by_date.setdefault(u.dord, []).append((u, v))
            for o, its in sorted(by_date.items(), key=lambda kv: (kv[0] is None, kv[0] or 0)):
                if dbl and len(its) > 1:
                    m.add(sum(v for _u, v in its) <= 2)
                    spans = [(min(_minute_of(sp[0]) for sp in u.spans), max(_minute_of(sp[1]) for sp in u.spans), v)
                             for u, v in its]
                    for t0 in sorted({s for s, _e, _v in spans}):
                        clique = [v for s, e, v in spans if s <= t0 < e + DOUBLE_BREAK_MINUTES]
                        if len(clique) > 1:
                            m.add_at_most_one(clique)
                    w = m.new_bool_var(f"wd{p}_{o}")
                    for _u, v in its:
                        m.add_implication(v, w)
                    m.add(w <= sum(v for _u, v in its))
                    self.worked[(p, o)] = w
                    if prob.daily_line and rules_apply:
                        line_d = int(round(prob.daily_line * H))
                        top_d = sum(int(round(u.hours * H)) for u, _v in its)
                        if top_d > line_d:
                            od = m.new_int_var(0, top_d - line_d, f"dot{p}_{o}")
                            m.add(od >= sum(int(round(u.hours * H)) * v for u, v in its) - line_d)
                            book.add("overtime", od, W_OT_HOUR / H, {"person": p, "date": o, "daily": True})
                    continue
                if len(its) > 1:
                    m.add_at_most_one([v for _u, v in its])
                w = sum(v for _u, v in its) if len(its) > 1 else its[0][1]
                self.worked[(p, o)] = w
            # hours per payroll bucket
            by_b = {}
            for u, v in items:
                by_b.setdefault(u.bucket, []).append((u, v))
            for b, its in by_b.items():
                # the published tail and the kept (fixed) shifts of the bucket
                base = int(round(float(prob.fx_hours[p].get(b, 0.0) or 0.0) * H))
                expr = sum(int(round(u.hours * H)) * v for u, v in its) + base
                top = base + sum(int(round(u.hours * H)) for u, _v in its)
                self.hours_expr[(p, b)] = (expr, base, top)
                cap = int(round(prob.capf(p, b) * H))
                if top > cap:
                    m.add(expr <= cap)
                if not prob.salaried[p]:
                    line = int(round(prob.line[p] * H))
                    if top > line:
                        ot = m.new_int_var(0, max(0, top - line), f"ot{p}_{b}")
                        m.add(ot >= expr - line)
                        book.add("overtime", ot, W_OT_HOUR / H, {"person": p, "bucket": b})
                        self.ot_vars = getattr(self, "ot_vars", {})
                        self.ot_vars[(p, b)] = ot
                    if rules_apply and c is not None and not c.own_max(name):
                        ceil_h = float(c.compliance.get("weekly_hours_ceiling") or _rules.DEFAULTS["weekly_hours_ceiling"])
                        ceil = int(round(ceil_h * H))
                        if top > ceil:
                            ov = m.new_int_var(0, max(0, top - ceil), f"ceil{p}_{b}")
                            m.add(ov >= expr - ceil)
                            book.add("policy_hours", ov, W_POLICY_HOUR / H, {"person": p, "bucket": b})
                elif c is not None and not c.salaried_cap_set(name):
                    lim = int(round(c.salaried_limit(name) * H))
                    if top > lim:
                        ov = m.new_int_var(0, max(0, top - lim), f"sal{p}_{b}")
                        m.add(ov >= expr - lim)
                        book.add("policy_hours", ov, W_POLICY_HOUR / H, {"person": p, "bucket": b})
            # the week's own hours (the sweep's under_min_hours and the
            # scorer's preferences read the schedule week, not the payroll
            # bucket): every live shift plus the kept ones of this week
            fixed_week = sum(float(prob.fx_hours[p].get(b, 0.0) or 0.0) - float(prob.base_hours[p].get(b, 0.0) or 0.0)
                             for b in prob.fx_hours[p])
            wbase = int(round(max(0.0, fixed_week) * H))
            wexpr = sum(int(round(u.hours * H)) * v for u, v in items) + wbase
            wtop = wbase + sum(int(round(u.hours * H)) for u, _v in items)
            mn = prob.min_hours[p]
            if mn and rules_apply:
                need = int(round(float(mn) * H))
                if wbase < need:
                    sh = m.new_int_var(0, need - wbase, f"min{p}")
                    m.add(sh >= need - wexpr)
                    book.add("min_hours", sh, W_MIN_HOUR / H, {"person": p})
            want = prob.desired[p] or prob.usual[p]
            if want and rules_apply:
                item = prob.week_unit("preferences") / prob.n_pref * 100.0 * Q
                lo = int(round(want * (1 - sq.PREFERENCE_HOURS_BAND) * H))
                hi = int(round(want * (1 + sq.PREFERENCE_HOURS_BAND) * H))
                dev = m.new_int_var(0, max(lo, wtop), f"want{p}")
                m.add(dev >= lo - wexpr)
                m.add(dev >= wexpr - hi)
                book.add("hours_wanted", dev, item / max(1.0, want * H) * 2, {"person": p})
            # rest and overlap between consecutive dates, one linear
            # constraint per pair of days: the next start less the last end,
            # whenever both days are worked (an overlap is always refused;
            # rest short of the rule is law, or a priced policy breach)
            need_r = prob.rest_need(p)
            ords = sorted(o for o in by_date if o is not None)
            for o in ords:
                nxt = by_date.get(o + 1)
                if not nxt:
                    continue
                if dbl:
                    # two legs a day: the turnaround pair by pair
                    want = int(round(need_r * 60)) if need_r else 0
                    for ua, va in by_date[o]:
                        ea = max(_minute_of(sp[1]) for sp in ua.spans)
                        for ub, vb in nxt:
                            gap = min(_minute_of(sp[0]) for sp in ub.spans) - ea
                            if gap < 0 or (want and gap < want and prob.law_rest):
                                m.add_bool_or([va.Not(), vb.Not()])
                            elif want and gap < want:
                                br = m.new_bool_var(f"rd{p}_{ua.id}_{ub.id}")
                                m.add(va + vb <= 1 + br)
                                book.add("policy_rest", br, W_POLICY_REST_HOUR * (want - gap) / 60.0,
                                         {"person": p, "date": o})
                    continue
                ends = [(max(_minute_of(sp[1]) for sp in ua.spans), va) for ua, va in by_date[o]]
                starts = [(min(_minute_of(sp[0]) for sp in ub.spans), vb) for ub, vb in nxt]
                worst = min(sb for sb, _v in starts) - max(ea for ea, _v in ends)
                want = int(round(need_r * 60)) if need_r else 0
                if worst >= want:
                    continue
                base_t = min(ea for ea, _v in ends)
                e_expr = sum((ea - base_t) * va for ea, va in ends)
                s_expr = sum((sb - base_t) * vb for sb, vb in starts)
                w_a, w_b = self.worked[(p, o)], self.worked[(p, o + 1)]
                big = 4 * 24 * 60 + want
                if worst < 0:
                    m.add(s_expr - e_expr >= -big * (2 - w_a - w_b))
                if want and worst < want:
                    if prob.law_rest:
                        m.add(s_expr - e_expr >= want - big * (2 - w_a - w_b))
                    else:
                        short = m.new_int_var(0, want, f"rest{p}_{o}")
                        m.add(s_expr - e_expr + short >= want - big * (2 - w_a - w_b))
                        book.add("policy_rest", short, W_POLICY_REST_HOUR / 60.0, {"person": p, "date": o})
            # a fixed or published-tail shift beside a live one: priced rest
            if need_r and not prob.law_rest and prob.c is not None:
                for u, v in items:
                    for r in u.rows:
                        ok, why = prob.c.rest_ok(name, r, [f_r for f in prob.fx_units[p] for f_r in f.rows])
                        if not ok:
                            book.add("policy_rest", v, W_POLICY_REST_HOUR * 2, {"person": p, "units": (u.id,)})
                            break
            # days in a row (the published tail counts)
            lim = prob.run_limit(p)
            if lim and ords:
                fixed = set(prob.fx_ords[p])
                lo_o, hi_o = min(ords) - lim, max(ords)
                for s in range(lo_o, hi_o + 1):
                    window = range(s, s + lim + 1)
                    live_w = [self.worked[(p, o)] for o in window if (p, o) in self.worked]
                    if not live_w:
                        continue
                    fx = sum(1 for o in window if o in fixed and (p, o) not in self.worked)
                    if fx + len(live_w) <= lim:
                        continue
                    if prob.law_run:
                        m.add(sum(live_w) + fx <= lim)
                    else:
                        over = m.new_int_var(0, len(live_w), f"run{p}_{s}")
                        m.add(over >= sum(live_w) + fx - lim)
                        book.add("policy_run", over, W_POLICY_RUN_DAY, {"person": p})

    def _manager(self):
        prob, m, c, book = self.prob, self.m, self.prob.c, self.book
        if c is None or not (c.managers or c.acting_managers):
            return
        only = None if c.managers else {d for ds in (c.acting_managers or {}).values() for d in (ds or ())}
        by_date = {}
        for u in prob.units:
            if not u.date or u.date in (c.closed_dates or set()) or (only is not None and u.date not in only):
                continue
            if u.fixed and u.draft is None:
                continue
            by_date.setdefault(u.date, []).append(u)
        for d, units in sorted(by_date.items()):
            items = [(s, e, u) for u in units for s, e in u.bspans]
            if not items:
                continue
            fixed_mgr = [(s, e) for s, e, u in items if u.fixed and u.draft is not None
                         and any(_rules.counts_as_manager(c, dict(r, employee=prob.names[u.draft])) for r in u.rows)]
            day = sq._day_name(d)
            o, _cl = _rules.open_hours(c, day)
            open_need = _rules.manager_need(c, d, [])
            cuts = sorted({t for s, e, _u in items for t in (s, e)} | {t for s, e in open_need for t in (s, e)})
            last_end = max(e for _s, e, _u in items)
            mgr_end = max((e for _s, e in fixed_mgr), default=None)
            mgr_starts = {s for s, _e in fixed_mgr}
            for a, b in zip(cuts, cuts[1:]):
                if b <= a:
                    continue
                if any(s <= a and b <= e for s, e in fixed_mgr):
                    continue
                cover = [u for s, e, u in items if s <= a and b <= e]
                open_req = any(s <= a and b <= e for s, e in open_need)
                fixed_on = any(u.fixed for u in cover)
                live = [u for u in cover if not u.fixed]
                if not (open_req or fixed_on or live):
                    continue
                # the owner's two allowances (the closing manager's stay,
                # the prep before an opening manager) — the verdict's own
                if mgr_end is not None and a >= mgr_end and b <= last_end and last_end - mgr_end <= \
                        _rules.CLOSING_MANAGER_STAYS_MINUTES:
                    continue
                if _rules.opening_prep((a, b), mgr_starts, o) or (
                        mgr_starts and b <= min(mgr_starts) and o is not None
                        and min(mgr_starts) <= o - _rules.OPENING_MANAGER_LEAD_MINUTES):
                    continue
                lits = [self.x[(u.id, p)] for u in live for p in prob.dom[u.id] if self.mgr(p, d)]
                gap = m.new_bool_var(f"mg{d}_{a}")
                book.add("manager", gap, W_MANAGER_MIN * (b - a), {"date": d, "start": a, "end": b})
                if open_req or fixed_on:
                    m.add(sum(lits) + gap >= 1)
                else:
                    for u in live:
                        m.add(sum(lits) + gap + self.unf[u.id] >= 1)

    def _closer(self):
        prob, m, c, book = self.prob, self.m, self.prob.c, self.book
        if c is None or not c.closers_by_role or not c.compliance.get("keyholder_until_close", True):
            return
        by = {}
        for u in prob.units:
            if not u.date or u.date in (c.closed_dates or set()) or (u.fixed and u.draft is None):
                continue
            for r, sp in zip(u.rows, (_rules._span(x) for x in u.rows)):
                if sp and not (u.fixed and c.training_row(r)):
                    by.setdefault((u.date, prob.family(r.get("role"))), []).append((sp[1], u))
        for (d, fam), items in sorted(by.items(), key=lambda kv: kv[0]):
            closers = (c.closers_by_role or {}).get(fam)
            if not closers or not _rules._closer_rule_runs(c, fam, d):
                continue
            ok = {prob.pidx[k] for k in closers if k in prob.pidx}
            e_last = max(e for e, _u in items)
            need, _until = _rules.closer_need(c, fam, sq._day_name(d), e_last)
            full = max(need - 15, e_last - 15)
            late = [u for e, u in items if e >= full]
            if any(u.fixed and u.draft in ok for u in late):
                continue
            live = [u for u in late if not u.fixed]
            if not live:
                continue
            lits = [self.x[(u.id, p)] for u in live for p in prob.dom[u.id] if p in ok]
            self.closer_units = getattr(self, "closer_units", {})
            for u in live:
                self.closer_units[u.id] = ok
            gap = m.new_bool_var(f"cl{d}_{fam}")
            book.add("closer", gap, W_CLOSER, {"date": d, "family": fam})
            if any(u.fixed for u in late):
                m.add(sum(lits) + gap >= 1)
            else:
                for u in live:
                    m.add(sum(lits) + gap + self.unf[u.id] >= 1)

    def _leaders(self):
        """Leader rules and strength (one rating measure — LOGIC-30) on each
        shift, as linear shortfalls."""
        prob, m, book = self.prob, self.m, self.book
        for key, g in prob.groups.items():
            if g.get("kind") != "S":
                continue
            part, dw, prof = g["part"], g["dw"], g["prof"]
            units = [prob.units[i] for i in prob._all_units(key)]
            for k, rule in enumerate(g.get("rules") or []):
                if not prob.rated_any and rule.get("min_score") is not None and not rule.get("attribute"):
                    continue
                fam = prob.family(rule.get("role"))
                pool = [u for u in units if prob.family(u.role_at.get(part) or "") == fam]
                if not pool:
                    continue
                try:
                    need = max(1, int(rule.get("count") or 1))
                except (TypeError, ValueError):
                    need = 1
                need = min(need, len(pool))

                def qual(p, rule=rule):
                    if rule.get("attribute"):
                        return prob.leader[p]
                    if rule.get("min_score") is None:
                        return True
                    return (prob._sc(p, {_low(rule.get("role"))}) or 0) >= float(rule["min_score"])
                have = sum(1 for u in pool if u.fixed and u.draft is not None and qual(u.draft))
                if have >= need:
                    continue
                lits = [self.x[(u.id, p)] for u in pool if not u.fixed for p in prob.dom[u.id] if qual(p)]
                short = m.new_int_var(0, need - have, f"ld{len(book.terms)}")
                m.add(sum(lits) + short >= need - have)
                per = (100 - sq.LEADER_MISS_SCORE) / float(need) * prob.weights.get("leadership", 0) / 100.0 * dw
                book.add("leadership", short, max(1.0, per * 3) * Q, {"shift": key, "rule": k})
            if not prob.rated_any:
                continue
            for role, target in (prof.min_strength or {}).items():
                if not target or sq.job_code_daypart(role) not in (None, part):
                    continue
                fam = prob.family(role)
                hit = prob.crews.get((fam, float(target)))
                crew = int(hit[0]) if hit and int(hit[0] or 0) > 0 else max(1, int(math.ceil(float(target) / 5 - 1e-9)))
                bar = max(1.0, min(5.0, float(target) / crew))
                terms, fixed = [], 0
                for u in units:
                    if prob.family(u.role_at.get(part) or "") != fam:
                        continue
                    if u.fixed:
                        if u.draft is not None:
                            sc = prob._sc(u.draft, {u.role_at.get(part)})
                            if sc is not None:
                                fixed += int(round((float(sc) - bar) * 100))
                        continue
                    for p in prob.dom[u.id]:
                        sc = prob._sc(p, {u.role_at.get(part)})
                        if sc is not None:
                            terms.append(int(round((float(sc) - bar) * 100)) * self.x[(u.id, p)])
                if not terms:
                    continue
                short = m.new_int_var(0, 500 * max(1, crew) * 3, f"st{len(book.terms)}")
                m.add(sum(terms) + fixed + short >= 0)
                per = prob.weights.get("operational_strength", 0) / 100.0 * dw * (100.0 / bar) / 100.0 / max(1, crew)
                book.add("strength", short, per * Q / 100.0 * 100, {"shift": key, "role": role})
            # the level this restaurant's own ratings want for a shift this
            # busy (the former demand match, folded into strength — LOGIC-30)
            every = list(prob.ratings.get("*") or [])
            if len(every) < sq.DEMAND_MIN_RATED:
                continue
            offset = sq.DEMAND_BAR_OFFSET.get(prof.demand or "normal", 0.0)
            fams = {}
            for u in units:
                fams.setdefault(prob.family(u.role_at.get(part) or ""), []).append(u)
            for fam, fus in fams.items():
                own = sorted((float(v) for v in (prob.ratings.get(fam) or [])), reverse=True)
                pool = own if len(own) >= sq.DEMAND_MIN_RATED else sorted(every, reverse=True)
                if not pool:
                    continue
                mean = sum(pool) / len(pool)
                best = (own or pool)[:max(1, len(fus))]
                want = max(1.0, min(mean + offset, sum(best) / len(best)))
                terms, fixed = [], 0
                for u in fus:
                    if u.fixed:
                        if u.draft is not None:
                            sc = prob._sc(u.draft, {u.role_at.get(part)})
                            if sc is not None:
                                fixed += int(round((float(sc) - want) * 100))
                        continue
                    for p in prob.dom[u.id]:
                        sc = prob._sc(p, {u.role_at.get(part)})
                        if sc is not None:
                            terms.append(int(round((float(sc) - want) * 100)) * self.x[(u.id, p)])
                if not terms:
                    continue
                short = m.new_int_var(0, 500 * len(fus), f"dm{len(book.terms)}")
                m.add(sum(terms) + fixed + short >= 0)
                per = prob.weights.get("operational_strength", 0) / 100.0 * dw * sq.DEMAND_POINT_STEP / 100.0 / len(fus)
                book.add("strength", short, per * Q, {"shift": key, "family": fam, "basis": "demand"})

    def _slot_memory(self):
        """The scheduling memory about who is on a slot at all (schedule_
        memory.misses' moved_on and leader_swap): somebody the managers keep
        putting on a weekday's daypart, one of the people they keep putting
        on a role's slot — a cost while they are not on it."""
        prob, m, book = self.prob, self.m, self.book
        if not prob.k_miss:
            return
        for key, g in prob.groups.items():
            if g.get("kind") != "S" or not g.get("learned"):
                continue
            part = g["part"]
            units = [prob.units[i] for i in prob._all_units(key)]
            first = [u for u in units if part in u.primary]
            for mem in g["learned"]:
                if mem["kind"] == "moved_on":
                    want = {prob.pidx.get(prob.key(mem.get("person")))} - {None}
                    fams = None
                else:
                    want = {prob.pidx.get(prob.key(n)) for n in (_solver._val(mem).get("names") or [])} - {None}
                    fams = {prob.family(mem.get("role"))} if mem.get("role") else None
                if not want:
                    continue
                pool = [u for u in first if fams is None or prob.family(u.role_at.get(part) or "") in fams]
                if any(u.fixed and u.draft in want for u in pool):
                    continue
                lits = [self.x[(u.id, p)] for u in pool if not u.fixed for p in prob.dom[u.id] if p in want]
                miss = m.new_bool_var(f"mem{len(book.terms)}")
                m.add(sum(lits) + miss >= 1)
                book.add("memory", miss, mem["confidence"] * prob.k_miss * Q, {"shift": key, "kind": mem["kind"]})

    def _static_and_stability(self):
        prob, book = self.prob, self.book
        drafted = {u.id: u.draft for u in self.live}
        for u in self.live:
            n_here = 1
            for (d, part) in u.parts[:1]:
                n_here = max(1, len((prob.groups.get(("S", d, part)) or {}).get("units") or ()) or 1)
            unit_stab = sum(prob.dim_cost("stability", 100.0 / n_here, prob.shift_dw.get(dp, 1.0))
                            for dp in u.parts) or prob.dim_cost("stability", 100.0 / n_here, u.dw)
            stab, ref, term_s = 0.0, set(), "stability"
            if u.id in self.last:
                stab, ref = unit_stab, self.last[u.id]
            elif u.id in self.usual:
                # no published week: the person who usually works the slot
                # (their own punches) is kept the same way (STRESS-15)
                stab, ref, term_s = unit_stab, self.usual[u.id], "usual"
            if stab:
                book.constant(term_s, stab * Q)
            parts_u = [x for x in u.primary if x != "unknown"]
            for p in prob.dom[u.id]:
                v = self.x[(u.id, p)]
                comp = prob.components_of(u, p)
                for k, val in comp.items():
                    # every per-assignment cost the solver's table knows,
                    # under its own name where no term groups it (J3's
                    # retention and tip rotation, and whatever comes next);
                    # dollars and change are this model's own; the usual-
                    # pattern reading is the stability phase's "usual"
                    if k == "stability":
                        term = "usual" if self.ctx is None else None
                    else:
                        term = None if k in _OWN_TERMS else _STATIC_TERMS.get(k, k)
                    if term and val:
                        book.add(term, v, val * Q, {"unit": u.id, "person": p})
                if stab and p in ref:
                    book.add(term_s, v, -stab * Q, {"unit": u.id, "person": p})
                if self.ctx is not None:
                    # a weekday and daypart they usually work, credited; a
                    # weekday they almost never work, a cost — at the same
                    # scale as one slot kept (schedule_objective's rates)
                    usd = self.ctx.usual_slot_usd(prob.names[p], u.date, parts_u)
                    if usd:
                        book.add("usual", v, usd / _obj.STABILITY_USD * unit_stab * Q,
                                 {"unit": u.id, "person": p})
                d0 = drafted.get(u.id)
                if d0 is not None and p != d0 and self.change_cost:
                    book.add("change", v, self.change_cost, {"unit": u.id, "person": p})

    def _fairness(self):
        """Closes, weekends and busy shifts spread: the k-th one a person
        carries costs k-1 times the step (the old solver's own increments,
        as a convex sum)."""
        prob, m, book = self.prob, self.m, self.book
        wf = prob.wn.get("fairness", 1) * Q
        for p, items in self.by_person.items():
            for kind, step, attr in (("close", K_CLOSE, "closing"), ("weekend", K_WEEKEND, "weekend"),
                                     ("hard", K_HARD, "hard")):
                lits = [v for u, v in items if getattr(u, attr)]
                fixed = sum(1 for f in prob.fx_units[p] if getattr(f, attr))
                if len(lits) + fixed < 2:
                    continue
                n = sum(lits) + fixed
                most = min(len(lits), len({u.dord for u, v in items if getattr(u, attr)})) + fixed
                for k in range(1, most):
                    e = m.new_int_var(0, len(lits) + fixed, f"f{kind}{p}_{k}")
                    m.add(e >= n - k)
                    book.add("fairness", e, step * wf, {"person": p, "kind": kind})

    def _dollars(self):
        prob, m, book = self.prob, self.m, self.book
        per_cent = self._per_cent()
        self.per_cent = per_cent
        total = []
        fixed_cents = 0
        for u in prob.units:
            if u.fixed and u.draft is not None and not prob.salaried[u.draft]:
                fixed_cents += int(round(u.hours * _rate_cents(prob, u.draft, u)))
        for u in self.live:
            for p in prob.dom[u.id]:
                if prob.salaried[p]:
                    continue
                cents = int(round(u.hours * _rate_cents(prob, p, u)))
                if cents:
                    total.append(cents * self.x[(u.id, p)])
                    if per_cent:
                        book.add("dollars", self.x[(u.id, p)], cents * per_cent, {"unit": u.id, "person": p})
        prem = []
        for (p, b), ot in (getattr(self, "ot_vars", None) or {}).items():
            cents_h = _ot_rate_cents(prob, p) * sq.OVERTIME_PREMIUM
            prem.append(int(round(cents_h)) * ot)          # ot is hundredths of an hour: cents x100
            if per_cent:
                book.add("dollars", ot, cents_h / H * per_cent, {"person": p, "overtime": True})
        self.total_cents = (sum(total) if total else 0) + fixed_cents
        self.prem_hcents = prem
        if self.budget > 0 and (total or prem):
            # dollars past the hourly budget: hundredth-cents on both sides
            spend = H * self.total_cents + (sum(prem) if prem else 0)
            over = m.new_int_var(0, 10 ** 9, "over_budget")
            m.add(100 * H * over >= spend - int(round(self.budget * 100 * H)))
            book.add("budget", over, max(1.0, 100 * per_cent * BUDGET_OVER_FACTOR), {})

    def _days(self):
        """A day in past somebody's usual days a week (their history, else
        schedule_objective.DEFAULT_DAYS_A_WEEK) at EXTRA_DAY_USD (LOGIC-5):
        six 4h days are not a part-timer's four 6h ones."""
        prob, m, book = self.prob, self.m, self.book
        per_cent = getattr(self, "per_cent", 0.0) or 0.0
        if not per_cent:
            return
        for p in sorted(self.by_person):
            target = self.ctx.days_target(prob.names[p]) if self.ctx is not None else _obj.DEFAULT_DAYS_A_WEEK
            fixed = len({f.dord for f in prob.fx_units[p] if f.dord in prob.week_ord_set})
            live = [w for (q, o), w in sorted(self.worked.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0))
                    if q == p and o not in {f.dord for f in prob.fx_units[p]}]
            if fixed + len(live) <= target:
                continue
            extra = m.new_int_var(0, len(live) + fixed, f"xd{p}")
            m.add(extra >= sum(live) + fixed - target)
            book.add("days", extra, _obj.EXTRA_DAY_USD * 100 * per_cent, {"person": p})

    def _hints(self):
        """A complete starting week for the search: last published week's
        person on each slot, else the draft's, else a greedy legal pick
        (the most constrained shift first, the person with the most room
        under their overtime line) — CP-SAT repairs what the hint breaks."""
        prob = self.prob
        used, hours = set(), {}
        for p in range(len(prob.names)):
            for b, h in prob.fx_hours[p].items():
                hours[(p, b)] = float(h or 0)
            for f in prob.fx_units[p]:
                used.add((p, f.dord))
        pick = {}

        def room(p, u):
            line = prob.line[p] if not prob.salaried[p] else prob.capf(p, u.bucket)
            return line - hours.get((p, u.bucket), 0.0) - u.hours

        def owed(p, u):
            mn = prob.min_hours[p]
            if not mn:
                return 0.0
            have = sum(h for (q, _b), h in hours.items() if q == p) - sum(prob.base_hours[p].values())
            return max(0.0, float(mn) - have)

        def rank(p, u):
            return (room(p, u) >= 0, owed(p, u), room(p, u), -p)

        def take(u, p):
            pick[u.id] = p
            used.add((p, u.dord))
            hours[(p, u.bucket)] = hours.get((p, u.bucket), 0.0) + u.hours

        closer = getattr(self, "closer_units", {})

        def fits_run(p, u):
            lim = prob.run_limit(p)
            if not lim or u.dord is None:
                return True
            ords = {o for (q, o) in used if q == p}
            return prob._run_with(ords, u.dord) <= lim

        def ok(p, u):
            return ((p, u.dord) not in used and fits_run(p, u)
                    and hours.get((p, u.bucket), 0.0) + u.hours <= prob.capf(p, u.bucket) + 0.05)

        # the closes first (one of the role's closers on each), then the
        # most constrained shifts
        order = sorted(self.live, key=lambda u: (u.id not in closer, len(prob.dom[u.id]), -u.hours, u.id))
        done_close = set()
        for u in order:
            if u.id not in closer:
                break
            key = (u.date, tuple(sorted(closer[u.id])))
            if key in done_close:
                continue
            cands = [p for p in prob.dom[u.id] if p in closer[u.id] and ok(p, u)]
            if cands:
                take(u, max(cands, key=lambda p: rank(p, u)))
                done_close.add(key)
        for u in order:
            if u.id in pick:
                continue
            # Stage 1's own person (its person-aware shapes), then last
            # week's, the usual one's, the draft's
            s1 = prob.pidx.get(prob.key(u.rows[0].get("_s1") or "")) if u.rows and u.rows[0].get("_s1") else None
            first = [s1] if s1 is not None and s1 in prob.dom[u.id] else []
            first += [p for p in sorted(self.last.get(u.id, ()) or self.usual.get(u.id, ())) if p in prob.dom[u.id]]
            if u.draft is not None and u.draft in prob.dom[u.id]:
                first.append(u.draft)
            for p in first:
                if ok(p, u) and room(p, u) >= 0:
                    take(u, p)
                    break
        for u in order:
            if u.id in pick:
                continue
            cands = [p for p in prob.dom[u.id] if ok(p, u)]
            if cands:
                take(u, max(cands, key=lambda p: rank(p, u)))
        self.greedy = dict(pick)
        for u in self.live:
            want = pick.get(u.id)
            self.m.add_hint(self.unf[u.id], 1 if want is None else 0)
            for p in prob.dom[u.id]:
                self.m.add_hint(self.x[(u.id, p)], 1 if p == want else 0)


def _solver_for(max_seconds, seed=SEED, workers=None, deterministic=None, wall_cap=None):
    s = _cp.CpSolver()
    det = DETERMINISTIC if deterministic is None else deterministic
    secs = max(0.2, float(max_seconds))
    s.parameters.random_seed = int(seed)
    s.parameters.num_workers = int(workers or WORKERS)
    if det:
        # Same inputs, same week: a deterministic work budget instead of the
        # wall clock, workers interleaved in a fixed order. The wall clock is
        # only the emergency cap — 4x the budget, or `wall_cap` (what the
        # repair loop has left) when that is less (scheduler audit 10/10/26
        # ARCH-26); _wall_capped says when it, not the budget, ended a solve.
        s.parameters.interleave_search = s.parameters.num_workers > 1
        s.parameters.max_deterministic_time = secs
        cap = secs * 4
        if wall_cap:
            cap = min(cap, max(0.2, float(wall_cap)))
        s.parameters.max_time_in_seconds = cap
    else:
        s.parameters.max_time_in_seconds = secs
    return s


def _phase_exprs(mdl) -> list:
    """[rules, stability, quality] — each phase's part of the objective, or
    None where it has no term."""
    groups = [[], [], []]
    for name, expr, coef, _meta in mdl.book.terms:
        k = 0 if name in PHASE_RULES else 1 if name in PHASE_STABILITY else 2
        groups[k].append(coef * expr)
    return [sum(g) if g else None for g in groups]


def _phase_values(mdl, solver) -> list:
    """The phase costs of a solved model, in PHASE order (a week is better
    than another when this list is lexicographically smaller)."""
    out = [0, 0, 0]
    for name, expr, coef, _meta in mdl.book.terms:
        k = 0 if name in PHASE_RULES else 1 if name in PHASE_STABILITY else 2
        out[k] += coef * solver.value(expr)
    return out


def _solve_phases(mdl, max_seconds, seed, workers, deterministic, wall_left=None):
    """Solve `mdl` phase by phase (LOGIC-22): each phase minimised with
    its share of the deterministic budget (PHASE_SHARES; what a phase did
    not use carries to the next, read from the solver's own deterministic
    time — so still the same on every run), then held at what it reached
    while the next one is optimised from that week. (solver, status,
    [{phase, status, value, seconds}], wall capped, proved, status name);
    solver None when the first phase found nothing."""
    exprs = _phase_exprs(mdl)
    det = DETERMINISTIC if deterministic is None else deterministic
    carry = 0.0
    best, best_st, log, capped, proved, last_name = None, None, [], False, True, "UNKNOWN"
    for i, expr in enumerate(exprs):
        if expr is None:
            continue
        secs = max(0.2, float(max_seconds) * PHASE_SHARES[i] + carry)
        if hasattr(mdl.m, "clear_objective"):
            mdl.m.clear_objective()
        mdl.m.minimize(expr)
        s = _solver_for(secs, seed=seed, workers=workers, deterministic=deterministic,
                        wall_cap=wall_left() if wall_left else None)
        st = s.solve(mdl.m)
        last_name = s.status_name(st)
        if st not in (_cp.OPTIMAL, _cp.FEASIBLE):
            proved = False
            log.append({"phase": ("rules", "stability", "quality")[i], "status": last_name.lower(), "value": None,
                        "seconds": round(s.wall_time, 3)})
            break
        capped = capped or _wall_capped(s, st)
        proved = proved and st == _cp.OPTIMAL
        val = int(round(s.objective_value))
        log.append({"phase": ("rules", "stability", "quality")[i], "status": last_name.lower(), "value": val,
                    "seconds": round(s.wall_time, 3)})
        if det:
            try:
                carry = max(0.0, secs - float(s.response_proto.deterministic_time))
            except Exception:
                carry = 0.0
        mdl.m.add(expr <= val)
        mdl.m.clear_hints()
        for j in range(len(mdl.m.proto.variables)):
            v = mdl.m.get_int_var_from_proto_index(j)
            mdl.m.add_hint(v, s.value(v))
        best, best_st = s, st
    return best, best_st, log, capped, proved, last_name


def usual_week(history_rows, key, family, share=None) -> list:
    """The restaurant's usual week as rows (STRESS-15): for each person, each
    weekday, role family and daypart they worked in at least `share`
    (schedule_objective.USUAL_SHARE) of the history's weeks — with at least
    USUAL_MIN_WEEKS of them — one row at their median start. The stability
    reference when no week has been published."""
    share = _obj.USUAL_SHARE if share is None else share
    weeks = set()
    by = {}
    for r in history_rows or []:
        name = (r.get("employee") or "").strip()
        s = _rules.start_minutes(r)
        d = str(r.get("date") or "")[:10]
        if not name or s is None or not d:
            continue
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        wk = dt.isocalendar()[:2]
        weeks.add(wk)
        part = (sq.present_dayparts(r) or ["unknown"])[0]
        k = (key(name), dt.strftime("%A"), family(r.get("role")), part)
        by.setdefault(k, {"name": name, "role": r.get("role"), "date": d, "weeks": set(), "starts": []})
        by[k]["weeks"].add(wk)
        by[k]["starts"].append(s)
        by[k].setdefault("rows", []).append(r)
    if len(weeks) < _obj.USUAL_MIN_WEEKS:
        return []
    out = []
    for k in sorted(by):
        v = by[k]
        if len(v["weeks"]) < share * len(weeks):
            continue
        mid = sorted(v["rows"], key=lambda x: (_rules.start_minutes(x) or 0, str(x.get("shift_end"))))[
            len(v["rows"]) // 2]
        out.append({"date": v["date"], "day": k[1], "employee": v["name"], "role": v["role"],
                    "shift_start": mid.get("shift_start"), "shift_end": mid.get("shift_end"), "_usual": True})
    return out


def _wall_capped(solver, status) -> bool:
    """Whether the wall clock (the emergency cap), not the deterministic work
    budget, ended a solve that did not prove its answer optimal."""
    if status == _cp.OPTIMAL or not solver.parameters.max_deterministic_time:
        return False
    try:
        det = float(solver.response_proto.deterministic_time)
    except Exception:
        return False
    return det < 0.95 * float(solver.parameters.max_deterministic_time) and \
        float(solver.wall_time) >= 0.95 * float(solver.parameters.max_time_in_seconds)


def assign(rows, constraints, signals=None, weights=None, profiles=None, only_dates=None, last_week=None,
           max_seconds=None, seed=SEED, workers=None, mode="draft", budget_dollars=None, roster_roles=None,
           pending=None, deterministic=None, reasons=True, explain=True, wall_cap=None, objective=None,
           history_rows=None, doubles=None) -> dict:
    """Solve who works each of `rows` (a row with no employee is a slot to
    staff; a pinned or locked row is kept; with `only_dates` every other
    date is kept) — the whole week as one model, solved lexicographically
    (the rule tiers, then stability, then quality — PHASE_RULES,
    PHASE_STABILITY). `objective` a schedule_objective.ObjectiveContext
    (a requirement hole weighed by its demand and the role's criticality,
    the usual days); `history_rows` the punches the usual person on each
    slot is read from when `last_week` is empty; `doubles` the people (by
    index) who may work two legs a date — read from the context / the
    Constraints' doubles_ok when None.

    mode: "draft" (Stage 2 of the solver-first draft: an unfilled slot is
    left out, at the floor's or the requirement's cost), "improve" (the
    repair loop's solver stage: the draft's people are a tie-break, a shift
    nobody legal can take stays as drafted) or "resolve" (MARKET-13: the
    week as it stands, a person changed on a shift priced so only what has
    to move moves).

    Returns {status, proved_optimal, seconds, rows, unfilled, kept_as_drafted,
    terms: {name: {label, cost, count}}, objective, bound, assignment,
    reasons, conflicts, notes, problem}."""
    t0 = _time.monotonic()
    out = {"status": "nothing_to_solve", "proved_optimal": False, "seconds": 0.0, "rows": [dict(r) for r in rows or []],
           "unfilled": [], "kept_as_drafted": [], "terms": {}, "objective": None, "bound": None, "assignment": [],
           "reasons": [], "conflicts": [], "notes": [], "engine": "cp-sat"}
    if not available():
        out["status"] = "unavailable"
        out["notes"].append("OR-Tools is not installed here.")
        return out
    if constraints is None or not rows:
        return out
    max_seconds = DEFAULT_SECONDS if max_seconds is None else float(max_seconds)
    prob = CpProblem(rows, constraints, signals=signals, weights=weights, profiles=profiles, only_dates=only_dates,
                     roster_roles=roster_roles, pending=pending)
    out["problem"] = prob
    live = [u for u in prob.units if not u.fixed]
    out["slots"] = len(live)
    out["people"] = len(prob.names)
    if not live:
        out["status"] = "nothing_to_solve"
        out["seconds"] = round(_time.monotonic() - t0, 3)
        return out
    last = last_week_slots(prob, last_week) if last_week else {}
    # No published week to keep: the person who usually works each slot,
    # from their own punches, is the reference instead (STRESS-15).
    usual = last_week_slots(prob, usual_week(history_rows, prob.key, prob.family)) \
        if (history_rows and not last) else {}
    if doubles is None:
        said = getattr(constraints, "doubles_ok", None) or {}
        doubles = {p for p, n in enumerate(prob.names)
                   if (objective.doubles_ok(n, constraints) if objective is not None else bool(said.get(prob.key(n))))}
    change = {"improve": K_CHANGE_IMPROVE * Q, "resolve": W_CHANGE_RESOLVE}.get(mode, 0)
    mdl = _Model(prob, mode, last, budget_dollars, change, objective=objective, usual=usual, doubles=doubles)
    mdl.build()
    # the model with its whole objective and no phase bound: what a fixed
    # week is evaluated on, term by term
    mdl.base = mdl.m.clone()
    out["build_seconds"] = round(_time.monotonic() - t0, 3)
    # A complete starting week first: the greedy legal pick (last week's
    # person, the draft's, else the most room) with every cost variable
    # settled at its least — a full hint the search starts from, and the
    # answer if the search finds nothing better in its budget. A large week
    # with only the people hinted spent its whole deterministic budget
    # looking for a first solution.
    greedy = [u.draft for u in prob.units]
    for u in live:
        greedy[u.id] = mdl.greedy.get(u.id)
    first = _fixed_eval(mdl, prob, greedy, seconds=min(5.0, max(1.0, max_seconds * 0.2)))
    if first is not None:
        mdl.m.clear_hints()
        for i in range(len(mdl.m.proto.variables)):
            v = mdl.m.get_int_var_from_proto_index(i)
            mdl.m.add_hint(v, first.value(v))
    # The whole deterministic budget, whatever building the model took on the
    # wall clock (ARCH-26: the same inputs, the same week), spent phase by
    # phase — the rule tiers, then stability, then quality, each holding the
    # one before at its optimum (LOGIC-22); what the wall clock has left is
    # only the emergency cap.
    solver, st, phases, capped, proved, name = _solve_phases(
        mdl, max_seconds, seed, workers, deterministic,
        (lambda: max(0.2, float(wall_cap) - (_time.monotonic() - t0))) if wall_cap else None)
    out["wall_capped"] = capped
    out["phases"] = phases
    out["seed"] = int(seed)
    out["workers"] = int(workers or WORKERS)
    out["seconds"] = round(_time.monotonic() - t0, 3)
    out["solve_seconds"] = round(sum(x.get("seconds", 0.0) for x in phases), 3)
    if solver is None:
        if first is None:
            out["status"] = "no_solution_in_time" if name == "UNKNOWN" else str(name).lower()
            return out
        # nothing better in the budget: the complete starting week stands
        solver = first
        out["start_only"] = True
    out["status"] = "optimal" if proved and not out.get("start_only") else "time_limit"
    out["proved_optimal"] = out["status"] == "optimal"
    out["objective"] = solver.objective_value
    out["bound"] = solver.best_objective_bound if not out.get("start_only") else None
    assignment = [u.draft for u in prob.units]
    for u in live:
        got = None
        for p in prob.dom[u.id]:
            if solver.boolean_value(mdl.x[(u.id, p)]):
                got = p
                break
        assignment[u.id] = got
    out["assignment"] = assignment
    exact = _fixed_eval(mdl, prob, assignment) if not out.get("start_only") else first
    if exact is not None:
        solver = exact
        out["objective"] = exact.objective_value
        out["phase_cost"] = _phase_values(mdl, exact)
    # the terms, as the owner reads them
    terms = {}
    for tname, expr, coef, _meta in mdl.book.terms:
        val = solver.value(expr)
        if val:
            t = terms.setdefault(tname, {"label": TERM_LABELS.get(tname, tname), "cost": 0, "count": 0})
            t["cost"] += coef * val
            t["count"] += 1
    for tname, val in mdl.book.const.items():
        if val:
            t = terms.setdefault(tname, {"label": TERM_LABELS.get(tname, tname), "cost": 0, "count": 0})
            t["cost"] += val
    out["terms"] = {k: v for k, v in terms.items() if v["cost"]}
    out["dollars"] = round((solver.value(mdl.total_cents) if not isinstance(mdl.total_cents, int)
                            else mdl.total_cents) / 100.0 + sum(solver.value(e) for e in mdl.prem_hcents) / 100.0 / H, 2)
    # rows out
    rows_out, unfilled, kept = [], [], []
    by_unit = {}
    for u in prob.units:
        for i in u.idx:
            by_unit[i] = u
    for i, r in enumerate(prob.rows):
        u = by_unit.get(i)
        x = {k: v for k, v in r.items() if k not in ("_pinned",)} if not rows[i].get("_pinned") else dict(r)
        if u is None or u.fixed:
            rows_out.append(x)
            continue
        p = assignment[u.id]
        if p is None:
            if mode == "draft" or u.draft is None:
                if i == u.idx[0]:
                    unfilled.append(_unfilled_entry(prob, u))
                continue
            kept.append(_unfilled_entry(prob, u))
            rows_out.append(x)
            continue
        x["employee"] = prob.names[p]
        rows_out.append(x)
    out["rows"] = rows_out
    out["unfilled"] = unfilled
    out["kept_as_drafted"] = kept
    out["model"] = mdl
    if reasons:
        try:
            out["reasons"] = assignment_reasons(prob, mdl, assignment)
        except Exception as e:           # never costs the week
            out["notes"].append(f"reasons not computed: {e!r}")
    if explain and (unfilled or "manager" in out["terms"]):
        try:
            out["conflicts"] = explain_conflicts(prob, assignment, unfilled, mdl, seed=seed)
        except Exception as e:
            out["notes"].append(f"conflicts not computed: {e!r}")
    out["seconds"] = round(_time.monotonic() - t0, 3)
    return out


def _unfilled_entry(prob, u) -> dict:
    r = u.rows[0]
    why = prob.reasons[u.id]
    common = {}
    for reason in why.values():
        common[reason] = common.get(reason, 0) + 1
    if not prob.dom[u.id]:
        text = (f"Nobody on the roster works {u.role_label or 'that role'}." if not why else
                "Nobody may legally work it: " + "; ".join(
                    f"{', '.join(n for n, rr in why.items() if rr == reason)[:80]} — {reason}"
                    for reason, _ in sorted(common.items(), key=lambda kv: -kv[1])[:3]) + ".")
    else:
        text = "Everyone who may work it is already on another shift that day or would break a higher rule."
    return {"index": u.idx[0], "rows": list(u.idx), "date": u.date, "day": u.day, "role": u.role_label,
            "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"), "hours": round(u.hours, 2),
            "floor": any(rr.get("_floor") for rr in u.rows), "text": text}


# ── why each assignment (RM-12) ─────────────────────────────────────────────

def _money(cents) -> str:
    return f"${int(round(cents / 100.0)):,}"


def assignment_reasons(prob, mdl, assignment, limit=400) -> list:
    """[{date, day, role, shift_start, shift_end, employee, instead, text}]:
    for each shift the solve staffed, the marginal cost of the next-best
    person who could legally take it — "Kim, not Lee: Lee would hit
    overtime (+$27)". Read from the solution, term by term."""
    hours = {}
    for u in prob.units:
        p = assignment[u.id] if u.id < len(assignment) else None
        if p is None:
            continue
        hours[(p, u.bucket)] = hours.get((p, u.bucket), 0.0) + u.hours
    for p in range(len(prob.names)):
        for b, h in (prob.base_hours[p] or {}).items():
            hours[(p, b)] = hours.get((p, b), 0.0) + float(h or 0)
    on_date = {}
    counts = {}
    for u in prob.units:
        p = assignment[u.id] if u.id < len(assignment) else None
        if p is not None:
            on_date.setdefault((p, u.date), []).append(u)
            c_ = counts.setdefault(p, {"closing": 0, "weekend": 0, "hard": 0, "hours": 0.0})
            for k in ("closing", "weekend", "hard"):
                c_[k] += 1 if getattr(u, k) else 0
            c_["hours"] += u.hours
    mdl._counts = counts
    out = []
    for u in mdl.live:
        p = assignment[u.id]
        if p is None:
            continue
        r = u.rows[0]
        base = {"date": u.date, "day": u.day, "role": u.role_label, "shift_start": r.get("shift_start"),
                "shift_end": r.get("shift_end"), "employee": prob.names[p]}
        alts = []
        for q in prob.dom[u.id]:
            if q == p:
                continue
            why, cost = _marginal(prob, mdl, u, p, q, hours, on_date)
            alts.append((cost, prob.names[q], why))
        if not alts:
            blocked = sorted(prob.reasons[u.id].items())[:2]
            text = f"{prob.names[p]}: the only one who could legally work it"
            if blocked:
                text += " (" + "; ".join(f"{n} — {w}" for n, w in blocked) + ")"
            out.append(dict(base, instead=None, text=text + "."))
        else:
            alts.sort(key=lambda t: (t[0], t[1]))
            cost, qn, why = alts[0]
            out.append(dict(base, instead=qn, text=f"{prob.names[p]}, not {qn}: {why}."))
        if len(out) >= limit:
            break
    return out


def _marginal(prob, mdl, u, p, q, hours, on_date):
    """(why, cost) of q working u instead of p, holding everything else:
    the largest named difference, and its cost in the model's units."""
    parts = []
    c = prob.c
    if on_date.get((q, u.date)):
        other = on_date[(q, u.date)][0]
        return f"{prob.names[q]} is already on {other.role_label} that day", W_COVER_HOUR
    hq = hours.get((q, u.bucket), 0.0)
    hp = hours.get((p, u.bucket), 0.0) - u.hours
    if not prob.salaried[q]:
        line = prob.line[q]
        ot_q = max(0.0, hq + u.hours - line) - max(0.0, hq - line)
        if ot_q > 0.05:
            cents = ot_q * _ot_rate_cents(prob, q) * (1 + sq.OVERTIME_PREMIUM)
            parts.append((W_OT_HOUR * ot_q, f"{prob.names[q]} would hit overtime (+{_money(cents)})"))
        if c is not None and not c.own_max(prob.names[q]) and _rules.hours_rules_apply(c, prob.names[q]):
            ceil = float(c.compliance.get("weekly_hours_ceiling") or _rules.DEFAULTS["weekly_hours_ceiling"])
            over = max(0.0, hq + u.hours - ceil) - max(0.0, hq - ceil)
            if over > 0.05:
                parts.append((W_POLICY_HOUR * over, f"{prob.names[q]} would pass your {ceil:g}h weekly ceiling"))
    if not prob.salaried[p]:
        line = prob.line[p]
        ot_p = max(0.0, hp + u.hours - line) - max(0.0, hp - line)
        if ot_p > 0.05:
            parts.append((-W_OT_HOUR * ot_p, ""))
    mn = prob.min_hours[p]
    if mn and hp < mn - 0.05:
        parts.append((W_MIN_HOUR * min(u.hours, mn - hp), f"{prob.names[p]} needs the hours toward their {mn:g}h minimum"))
    if u.id in mdl.last and p in mdl.last[u.id] and q not in mdl.last[u.id]:
        parts.append((Q * 2, f"{prob.names[p]} had this shift last week"))
    rq, rp = _rate_cents(prob, q, u), _rate_cents(prob, p, u)
    if rq > rp and u.hours:
        parts.append(((rq - rp) * u.hours * (mdl.per_cent or 0.001),
                      f"{prob.names[q]} costs more for the same shift (+{_money((rq - rp) * u.hours)})"))
    cp_, cq = prob.components_of(u, p), prob.components_of(u, q)
    words = {"pending": f"{prob.names[q]} has asked for that day off",
             "preference": f"{prob.names[q]} prefers the other half of the day",
             "learned_preference": f"{prob.names[q]} keeps dropping that shift",
             "learned": f"it is what the managers keep doing on that shift",
             "reliability": f"{prob.names[q]} has a reliability risk on it",
             "role": f"it is {prob.names[p]}'s own role",
             "rotation_weekend": f"{prob.names[q]} is due a weekend off",
             "rotation_close": f"{prob.names[q]} is due a rest from closing"}
    for k, w in words.items():
        diff = cq.get(k, 0.0) - cp_.get(k, 0.0)
        if diff > 0.01:
            parts.append((diff * Q, w))
    counts = getattr(mdl, "_counts", {}) or {}
    cp0, cq0 = counts.get(p) or {}, counts.get(q) or {}
    for k, word in (("closing", "closes"), ("weekend", "weekend shifts"), ("hard", "busy shifts")):
        if getattr(u, k) and cq0.get(k, 0) >= cp0.get(k, 0):
            parts.append((K_CLOSE * Q * (1 + cq0.get(k, 0) - cp0.get(k, 0)),
                          f"spreads the {word} ({prob.names[q]} has {cq0.get(k, 0)} this week, "
                          f"{prob.names[p]} {cp0.get(k, 0) - 1})"))
            break
    hq_w, hp_w = cq0.get("hours", 0.0), cp0.get("hours", 0.0) - u.hours
    if hq_w > hp_w + 2:
        parts.append((Q * 0.5, f"{prob.names[q]} already has {hq_w:g}h this week, {prob.names[p]} {hp_w:g}h"))
    named = [t for t in parts if t[1]]
    total = sum(t[0] for t in parts)
    ot = [t for t in named if "overtime" in t[1]]
    if ot and any("weekly ceiling" in t[1] for t in named):
        return ot[0][1], total
    if not named:
        return "an equal fit — the rest of the week decides who takes it", max(0.0, total)
    best = max(named, key=lambda t: t[0])
    return best[1], total


# ── the smallest set of rules that conflict (LOGIC-32, RM-12) ───────────────

def explain_conflicts(prob, assignment, unfilled, mdl=None, seed=SEED, max_items=6, seconds=2.0) -> list:
    """For each floor shift the solve left unfilled, and each stretch left
    with no manager: the smallest set of the owner's rules that cannot all
    hold together — "Tuesday dinner can't have this Server shift with these
    rules together: your floor; Kim's 40h maximum; Lee unavailable
    Tuesdays. Relax one of them." Each candidate's blocking rule is an
    assumption literal; CP-SAT's sufficient assumptions are shrunk to a
    minimal set by deletion. [] when nothing is unmet."""
    out = []
    items = [e for e in unfilled or [] if e.get("floor")][:max_items]
    for e in items:
        u = next((x for x in prob.units if x.idx and x.idx[0] == e["index"]), None)
        if u is None:
            continue
        got = _conflict_for_unit(prob, assignment, u, seed, seconds)
        if got:
            out.append(got)
    if mdl is not None:
        gaps = {}
        for tname, expr, coef, meta in mdl.book.terms:
            if tname == "manager" and meta:
                gaps.setdefault(meta["date"], []).append((meta["start"], meta["end"], expr))
        for d, spans in sorted(gaps.items())[:max_items]:
            got = _conflict_for_manager(prob, d, spans)
            if got:
                out.append(got)
    return out


_RELAX = {
    "unavailable_day": "availability", "unavailable_daypart": "availability", "approved_time_off": "time_off",
    "over_max_hours": "max_hours", "outside_window": "window", "note_unavailable": "note",
}


def _blocker(prob, u, q, week_hours=None) -> tuple:
    """(kind, text) of the owner's rule keeping q off u, or None when it is
    a fact no owner can relax (a minor's hours, a certificate, a role they
    do not hold, off the roster) or nothing keeps them off. `week_hours`
    {(person, bucket): hours} is the solved week: their own maximum binds
    on what they already work."""
    name = prob.names[q]
    why = None
    if q not in prob.dom[u.id]:
        why = prob.reasons[u.id].get(name) or prob.fits(u, q)
    elif week_hours is not None and week_hours.get((q, u.bucket), 0.0) + u.hours > prob.capf(q, u.bucket) + 0.05:
        why = _rules.LABELS.get("over_max_hours")
    if why is None:
        return None
    hard = (_rules.LABELS.get("minor_late"), _rules.LABELS.get("minor_early"), _rules.LABELS.get("minor_hours"),
            _rules.LABELS.get("role_not_held"), _rules.LABELS.get("off_roster"), _rules.LABELS.get("inactive"))
    if why in hard or str(why).startswith("does not work") or str(why).startswith(_rules.LABELS.get("missing_cert", "~")):
        return None
    if why == "already on another shift that day":
        # a kept shift of the same role that day is the same need, not a
        # rule; one of another role is the owner's to move
        fam = prob.family(u.role_label)
        other = next((f for f in prob.fx_units[q] if f.date == u.date), None)
        if other is None or prob.family(other.role_label) == fam:
            return None
        return ("other_shift", f"{name} kept on {other.role_label} that day")
    if why == _rules.LABELS.get("over_max_hours"):
        mx = prob.maxh[q]
        return ("max_hours", f"{name}'s {mx:g}h maximum")
    if why in (_rules.LABELS.get("unavailable_day"), _rules.LABELS.get("unavailable_daypart")):
        return ("availability", f"{name} unavailable {u.day}s")
    if "time off" in str(why).lower():
        return ("time_off", f"{name}'s approved time off {u.day}")
    return ("rule", f"{name}: {why}")


def _conflict_for_unit(prob, assignment, u, seed, seconds):
    """A local model of u's date and role family: every unit of the family
    that date must be staffed (the floor), each candidate's blocking rule an
    assumption; the solved week elsewhere held."""
    fam = prob.family(u.role_label)
    units = [v for v in prob.units if v.date == u.date and not v.fixed and prob.family(v.role_label) == fam]
    cands = [q for q in range(len(prob.names)) if u.roles <= prob.roles[q]]
    if not cands:
        return {"date": u.date, "day": u.day, "what": f"{u.day} {u.role_label}", "relax_one_of": [],
                "text": f"Nobody on the roster works {u.role_label or 'that role'}."}
    m = _cp.CpModel()
    lits, meaning = [], []
    floor_lit = m.new_bool_var("floor")
    lits.append(floor_lit)
    part = "lunch" if (u.primary or ["night"])[0] == "morning" else "dinner"
    meaning.append(("floor", f"your floor for {u.day} {part} {u.role_label or ''}".strip()))
    rule_lit = {}
    xv = {}
    others_day = {}
    for v in prob.units:
        q = assignment[v.id] if v.id < len(assignment) else None
        if q is not None and v.date == u.date and prob.family(v.role_label) != fam:
            others_day[q] = v
    week_hours = {}
    for w in prob.units:
        q = assignment[w.id] if w.id < len(assignment) else None
        if q is not None:
            week_hours[(q, w.bucket)] = week_hours.get((q, w.bucket), 0.0) + w.hours
    for q in range(len(prob.names)):
        for b_, h in (prob.base_hours[q] or {}).items():
            week_hours[(q, b_)] = week_hours.get((q, b_), 0.0) + float(h or 0)
    for v in units:
        for q in cands:
            if not v.roles <= prob.roles[q]:
                continue
            if assignment[v.id] == q:
                b = None
            else:
                b = _blocker(prob, v, q, week_hours)
                if b is None and q not in prob.dom[v.id]:
                    continue
            var = m.new_bool_var(f"x{v.id}_{q}")
            xv[(v.id, q)] = var
            if b is not None:
                key = (q, b[0])
                if key not in rule_lit:
                    rule_lit[key] = m.new_bool_var(f"r{q}_{b[0]}")
                    lits.append(rule_lit[key])
                    meaning.append(b)
                m.add_implication(var, rule_lit[key].Not())
            if q in others_day:
                key = (q, "other_shift")
                if key not in rule_lit:
                    o = others_day[q]
                    rule_lit[key] = m.new_bool_var(f"o{q}")
                    lits.append(rule_lit[key])
                    meaning.append(("other_shift", f"{prob.names[q]} kept on {o.role_label} that day"))
                m.add_implication(var, rule_lit[key].Not())
    for v in units:
        vs = [xv[(v.id, q)] for q in cands if (v.id, q) in xv]
        m.add(sum(vs) <= 1)
        m.add(sum(vs) == 1).only_enforce_if(floor_lit)
    for q in cands:
        vs = [xv[(v.id, q)] for v in units if (v.id, q) in xv]
        if len(vs) > 1:
            m.add_at_most_one(vs)
    s = _solver_for(seconds, seed=seed, workers=1, deterministic=True)
    core = _minimal_core(m, s, lits)
    if core is None:
        return None
    relax = [{"kind": meaning[i][0], "text": meaning[i][1]} for i in core]
    names = "; ".join(r["text"] for r in relax)
    return {"date": u.date, "day": u.day, "what": f"{u.day} {part}: {u.role_label}", "relax_one_of": relax,
            "text": f"{u.day} {part} can't have this {u.role_label or ''} shift ({u.rows[0].get('shift_start')}–"
                    f"{u.rows[0].get('shift_end')}) with these rules together: {names}. Relax one of them."}


def _minimal_core(m, s, lits):
    """Indices into `lits` of a minimal set that cannot all hold, or None
    when they can (nothing conflicts)."""
    m.clear_assumptions()
    m.add_assumptions(lits)
    st = s.solve(m)
    if st != _cp.INFEASIBLE:
        return None
    idx = {lit.index: i for i, lit in enumerate(lits)}
    core = [idx[k] for k in s.sufficient_assumptions_for_infeasibility() if k in idx]
    if not core:
        return None
    keep = list(core)
    for i in list(core):
        trial = [j for j in keep if j != i]
        m.clear_assumptions()
        m.add_assumptions([lits[j] for j in trial])
        if s.solve(m) == _cp.INFEASIBLE:
            keep = trial
    m.clear_assumptions()
    return keep


def _conflict_for_manager(prob, d, spans):
    """Who could have managed a stretch, and the one rule keeping each off
    it: relaxing any one of them closes the gap."""
    c = prob.c
    if c is None:
        return None
    a = min(s for s, _e, _x in spans)
    b = max(e for _s, e, _x in spans)
    day = sq._day_name(d)
    relax = []
    for k in sorted(c.managers or {}):
        name = _rules._display_name(c, k)
        ok, why = c.can_work(name, d)
        if not ok:
            kind = "time_off" if "time off" in str(why).lower() else "availability"
            relax.append({"kind": kind, "text": f"{name}: {why}"})
            continue
        p = prob.pidx.get(prob.key(name))
        if p is not None and prob.salaried[p] is False and c.own_max(name):
            relax.append({"kind": "max_hours", "text": f"{name}'s {prob.maxh[p]:g}h maximum"})
        else:
            relax.append({"kind": "plan", "text": f"{name} isn't planned on then"})
    when = f"{_rules._fmt_minutes(a % 1440)}–{_rules._fmt_minutes(b % 1440)}"
    return {"date": d, "day": day, "what": f"{day} {when}: a manager on the floor", "relax_one_of": relax[:6],
            "text": f"{day} {when} has nobody to manage the floor with these together: "
                    + "; ".join(r["text"] for r in relax[:6]) + ". Relax one of them."}


# ── a re-solve in seconds: "Maria is out, re-solve Friday" (MARKET-13) ──────

RESOLVE_SECONDS = _env_float("SCHEDULE_RESOLVE_SECONDS", 6.0)


def resolve(rows, constraints, dates, out=(), add=(), signals=None, weights=None, profiles=None, last_week=None,
            max_seconds=None, seed=SEED) -> dict:
    """The week as it stands re-solved on `dates` only, with no model call:
    each person in `out` ({"employee", "dates"?}) off those dates (their
    shifts opened, nobody else's moved unless it has to be), each shift in
    `add` ({"date", "role", "shift_start", "shift_end"}) staffed, every
    other date and every locked or pinned row kept exactly. A person on a
    shift that stays is changed only when the rules leave no other way —
    a change costs more than any preference — so the answer is the
    smallest edit that holds every rule.

    {"ok", "rows" (the whole week), "changes": [{index, date, day, role,
    shift_start, shift_end, from, to, why}], "unfilled", "conflicts",
    "seconds", "status"}. Nothing is saved."""
    import copy as _copy
    t0 = _time.monotonic()
    res = {"ok": False, "rows": [dict(r) for r in rows or []], "changes": [], "unfilled": [], "conflicts": [],
           "seconds": 0.0, "status": "nothing_to_solve"}
    if not available():
        res["status"] = "unavailable"
        return res
    if constraints is None or not dates:
        return res
    want = set(dates)
    c = _copy.copy(constraints)
    c.blocked_dates = {k: dict(v or {}) for k, v in (constraints.blocked_dates or {}).items()}
    gone = set()
    for o in out or []:
        name = str((o or {}).get("employee") or "").strip()
        if not name:
            continue
        k = c.key(name)
        for d in (o.get("dates") or sorted(want)):
            if d in want:
                gone.add((k, d))
                c.blocked_dates.setdefault(k, {})[d] = "out — re-solved without them"
    work, origin = [], []
    for i, r in enumerate(rows or []):
        x = dict(r)
        name = (x.get("employee") or "").strip()
        if x.get("date") not in want:
            x["_pinned"] = True
        elif name and (c.key(name), x.get("date")) in gone:
            if is_locked(x):
                x.pop("locked", None)
                x.pop("_pinned", None)
            x["employee"] = ""
        work.append(x)
        origin.append(i)
    for a in add or []:
        if not isinstance(a, dict) or a.get("date") not in want or not a.get("role"):
            continue
        x = {"date": a["date"], "day": sq._day_name(a["date"]), "employee": "", "role": str(a["role"]),
             "shift_start": str(a.get("shift_start") or ""), "shift_end": str(a.get("shift_end") or ""),
             "scheduled_hours": "", "notes": str(a.get("notes") or "")}
        x["scheduled_hours"] = _stime_hours(x, c)
        work.append(x)
        origin.append(None)
    secs = RESOLVE_SECONDS if max_seconds is None else float(max_seconds)
    got = assign(work, c, signals=signals, weights=weights, profiles=profiles, only_dates=sorted(want),
                 last_week=last_week, max_seconds=secs, mode="resolve", seed=seed, explain=True)
    res["status"] = got["status"]
    res["seconds"] = round(_time.monotonic() - t0, 3)
    if got["status"] not in ("optimal", "time_limit"):
        return res
    prob = got.get("problem")
    by_unit = {}
    for u in prob.units:
        for i in u.idx:
            by_unit[i] = u
    why = {(r["date"], r.get("shift_start"), _low(r["employee"])): r["text"] for r in got.get("reasons") or []}
    final, changes = [], []
    for i, x in enumerate(prob.rows):
        u = by_unit.get(i)
        p = got["assignment"][u.id] if u is not None and u.id < len(got["assignment"]) else None
        name = prob.names[p] if p is not None else (x.get("employee") or "").strip()
        src = origin[i]
        before = (rows[src].get("employee") or "").strip() if src is not None else ""
        if not name:
            continue                    # nobody could take it: listed in unfilled
        r = {k: v for k, v in x.items() if k not in ("_pinned", "_open")}
        if src is not None and rows[src].get("_pinned"):
            r["_pinned"] = rows[src]["_pinned"]
        r["employee"] = name
        final.append(r)
        if _low(name) != _low(before):
            changes.append({"index": src, "date": r["date"], "day": r.get("day") or sq._day_name(r["date"]),
                            "role": r.get("role"), "shift_start": r.get("shift_start"), "shift_end": r.get("shift_end"),
                            "from": before or None, "to": name,
                            "why": why.get((r["date"], r.get("shift_start"), _low(name)), "")})
    res.update(ok=True, rows=final, changes=changes, unfilled=got.get("unfilled") or [],
               conflicts=got.get("conflicts") or [], terms=got.get("terms") or {})
    res["seconds"] = round(_time.monotonic() - t0, 3)
    return res


# ── the quality gate, exactly (LOGIC-26) ────────────────────────────────────

def _probe_row(c, v, rows):
    """The shift that would put hard breach `v` right — a manager over the
    unmanaged stretch, one more of the role over the floor's core window —
    as a slot for the solver, or None for a breach no added shift answers."""
    d = v.get("date")
    if not d:
        return None
    day = sq._day_name(d)
    kind = v.get("kind")
    if kind == "no_manager":
        gs, ge = v.get("gap_start"), v.get("gap_end")
        if gs is None or ge is None or not (c.managers or c.acting_managers):
            return None
        roles = [r for r in (c.managers or {}).values() if r]
        role = max(set(roles), key=roles.count) if roles else "Manager"
        a, b = int(gs), int(ge)
        if b - a < 60:
            a = max(0, b - 60)
        return {"date": d, "day": day, "employee": "", "role": role, "shift_start": _rules._fmt_minutes(a % 1440),
                "shift_end": _rules._fmt_minutes(b % 1440), "scheduled_hours": "", "notes": "", "_floor": True,
                "_probe": "manager"}
    if kind == "coverage_floor":
        part = v.get("daypart") or "night"
        role = v.get("role") or v.get("floor_role") or ""
        if not role:
            return None
        o, cl = _rules.open_hours(c, day)
        win = sq.floor_window(part, o, cl)
        if not win:
            return None
        return {"date": d, "day": day, "employee": "", "role": role, "shift_start": _rules._fmt_minutes(win[0] % 1440),
                "shift_end": _rules._fmt_minutes(win[1] % 1440), "scheduled_hours": "", "notes": "", "_floor": True,
                "_probe": "floor"}
    return None


def gate_exact(rows, constraints, dates, signals=None, weights=None, viols=None, max_seconds=8.0) -> dict:
    """The quality gate's days solved exactly before any model call
    (scheduler audit 10/10/26 LOGIC-26): for each hard breach on `dates` an
    added shift could answer (a stretch with no manager, a floor short),
    the shift is offered to the solver over the week as it stands. Filled:
    it is a fix ({"fixes": [rows]}) the gate's code fill takes in. Not
    filled: no legal arrangement of these people exists, and the smallest
    set of the owner's rules in the way is said ({"conflicts": [...]}) —
    a sampled rewrite by the model cannot change that, so none is paid
    for. {"fixes", "conflicts", "probed", "unprobed"} — `unprobed` the
    breaches no added shift answers (left to the fill and the rewrite)."""
    out = {"fixes": [], "conflicts": [], "probed": 0, "unprobed": 0}
    if not available() or constraints is None or not dates:
        return out
    want = set(dates)
    viols = _rules.violations(rows, constraints) if viols is None else viols
    probes, seen = [], set()
    for v in viols:
        if not v.get("hard") or v.get("date") not in want:
            continue
        key = (v.get("kind"), v.get("date"), v.get("daypart") or v.get("gap_start"), v.get("floor_role") or v.get("role"))
        if key in seen:
            continue
        seen.add(key)
        p = _probe_row(constraints, v, rows)
        if p is None:
            out["unprobed"] += 1
            continue
        p["scheduled_hours"] = _stime_hours(p, constraints)
        probes.append(p)
    if not probes:
        return out
    out["probed"] = len(probes)
    kept = [dict(r, _pinned=True) for r in rows]
    got = assign(kept + probes, constraints, signals=signals, weights=weights, only_dates=sorted(want),
                 max_seconds=max_seconds, mode="draft", reasons=False, explain=True)
    if got["status"] not in ("optimal", "time_limit"):
        return out
    for r in got["rows"][len(kept):]:
        if (r.get("employee") or "").strip():
            out["fixes"].append({k: v for k, v in r.items() if k not in ("_open", "_pinned", "_floor", "_probe")})
    out["conflicts"] = list(got.get("conflicts") or [])
    if got.get("unfilled") and not out["conflicts"]:
        out["conflicts"] = [{"date": e["date"], "day": e["day"], "what": f"{e['day']} {e['role']}",
                             "relax_one_of": [], "text": e["text"]} for e in got["unfilled"]]
    return out


def _stime_hours(row, c):
    import schedule_time as _st
    return _st.row_hours_text(row, getattr(c, "tz", None))


# ── the repair loop's stage (replaces schedule_solver.improve when it can) ──

def improve(rows, inputs=None, signals=None, weights=None, constraints=None, only_dates=None,
            max_seconds=None, min_gain=0.0, hours_budget=None, last_week=None, wall_cap=None) -> dict:
    """schedule_solver.improve's contract, solved with CP-SAT over the joint
    objective (LOGIC-19: the objective is the model's separable costs, never
    the Shift Quality score — the score is computed for the owner's report
    only). Kept only when the model's own cost of the answer is lower than
    the draft's under the same model and it puts nothing above the budget
    tier new or worse (schedule_rules.regressions — the repair loop's judge
    holds it again). Falls back to the backtracking solver when CP-SAT is
    unavailable or finds nothing in time."""
    inputs = inputs or {}
    signals = dict(signals or {})
    constraints = constraints if constraints is not None else inputs.get("constraints")
    base = [dict(r) for r in rows or []]
    t0 = _time.monotonic()
    if not available() or constraints is None:
        res = _solver.improve(rows, inputs, signals=signals, weights=weights, constraints=constraints,
                              only_dates=only_dates, max_seconds=max_seconds or _solver.DEFAULT_SECONDS,
                              hours_budget=hours_budget, wall_cap=wall_cap)
        res.setdefault("stats", {})["engine"] = "backtracking"
        return res
    secs = float(max_seconds or DEFAULT_SECONDS)
    solved = assign(base, constraints, signals=signals, weights=weights, profiles=inputs.get("shift_profiles"),
                    only_dates=only_dates, last_week=last_week or signals.get("last_published_rows"),
                    max_seconds=secs, mode="improve", budget_dollars=inputs.get("labor_budget_dollars"),
                    roster_roles=inputs.get("roster_roles") or signals.get("roster_roles"),
                    pending=inputs.get("pending_time_off") or getattr(constraints, "pending_off", None),
                    explain=False, wall_cap=wall_cap)
    if solved["status"] not in ("optimal", "time_limit"):
        # The backtracking fallback on its own work budget (WorkClock); the
        # wall clock still left is its emergency cap (ARCH-26).
        res = _solver.improve(rows, inputs, signals=signals, weights=weights, constraints=constraints,
                              only_dates=only_dates, max_seconds=_solver.DEFAULT_SECONDS, hours_budget=hours_budget,
                              wall_cap=(max(0.5, float(wall_cap) - (_time.monotonic() - t0)) if wall_cap else None))
        res.setdefault("stats", {})["engine"] = "backtracking"
        res["stats"]["cpsat_status"] = solved["status"]
        return res
    prob, mdl = solved.get("problem"), solved.get("model")
    draft_cost = _draft_objective(mdl, prob)
    draft_eval = _fixed_eval(mdl, prob, [u.draft for u in prob.units]) if mdl is not None else None
    draft_phases = _phase_values(mdl, draft_eval) if draft_eval is not None else None
    stats = {"engine": "cp-sat", "status": solved["status"], "proved_optimal": solved["proved_optimal"],
             "wall_capped": bool(solved.get("wall_capped")),
             "seconds": solved["seconds"], "slots": solved.get("slots"), "people": solved.get("people"),
             "cost": solved["objective"], "draft_cost": draft_cost, "bound": solved["bound"],
             "terms": solved["terms"], "infeasible": solved["kept_as_drafted"][:10], "notes": solved["notes"],
             "optimal_for": "the solver's separable cost; Shift Quality is the owner's report"}
    out = {"applied": False, "rows": base, "before_score": None, "after_score": None, "quality": None,
           "changes": [], "stats": stats, "reason": "", "reasons": solved.get("reasons") or []}
    changed = [i for i, (a, b) in enumerate(zip(base, solved["rows"]))
               if _low(a.get("employee")) != _low(b.get("employee"))] if len(base) == len(solved["rows"]) else None
    if changed is None or not changed:
        out["reason"] = "the draft's own assignment was already the best the solver found"
        stats["kept"] = "draft"
        return out
    # Better is lexicographic, as the solve is (the rule tiers first, then
    # stability, then quality): a week no lower in that order is no better.
    got_phases = solved.get("phase_cost")
    no_better = (list(got_phases) >= list(draft_phases[:2]) + [draft_phases[2] - min_gain]
                 if (got_phases is not None and draft_phases is not None) else
                 (draft_cost is not None and solved["objective"] is not None
                  and solved["objective"] >= draft_cost - min_gain))
    stats["phase_cost"], stats["draft_phase_cost"] = got_phases, draft_phases
    if no_better:
        out["reason"] = "the solver's week cost no less under its own objective"
        stats["kept"] = "draft"
        return out
    if constraints is not None:
        before = _rules.breach_profile(base, constraints)
        after = _rules.breach_profile(solved["rows"], constraints)
        if _rules.regressions(before, after, upto=_rules.TIER_BUDGET, hard_only=False):
            out["reason"] = "the solver's week would have made a rule above the budget worse"
            stats["kept"] = "draft"
            return out
    rows_out = [dict(r) for r in solved["rows"]]
    lines = []
    reason_by = {(r["date"], r.get("shift_start"), _low(r["employee"])): r["text"] for r in out["reasons"]}
    for i in changed[:_solver.MAX_LISTED_CHANGES]:
        r = rows_out[i]
        why = reason_by.get((r.get("date"), r.get("shift_start"), _low(r.get("employee"))), "")
        note = (r.get("notes") or "").strip()
        tag = f"{_solver.NOTE_TAG} solver put {r['employee']} here (was {base[i].get('employee') or 'nobody'})"
        r["notes"] = (note + (" — " if note else "") + tag)[:240]
        lines.append({"kind": "reassign", "reason": f"Put {r['employee']} on {r.get('day') or ''} {r.get('role')} "
                                                     f"({r.get('shift_start')}–{r.get('shift_end')}) instead of "
                                                     f"{base[i].get('employee') or 'nobody'} — {why or 'a better week overall'}",
                      "gain": None})
    head = {"kind": "solve", "reason": f"Cavnar AI re-solved who works each shift ({len(changed)} change"
                                       f"{'s' if len(changed) != 1 else ''}) — each with what it saved.",
            "gain": round((draft_cost or 0) - (solved["objective"] or 0), 1)}
    stats["kept"] = "solver"
    out.update(applied=True, rows=rows_out, changes=[head] + lines)
    # Shift Quality before and after, for the owner's report only — it chose
    # nothing here (LOGIC-19)
    try:
        def _q(rs):
            viols = _rules.violations(rs, constraints)
            sig = dict(signals, flagged=_solver._flagged(rs, constraints, viols),
                       hard_breaches=[v for v in viols if v.get("hard")])
            return sq.score_rows(rs, profiles=inputs.get("shift_profiles"), weights=weights, **sig)
        qb, qa = _q(base), _q(rows_out)
        out.update(before_score=qb.get("score"), after_score=qa.get("score"), quality=qa)
        stats["after_score"] = qa.get("score")
    except Exception as e:
        stats.setdefault("notes", []).append(f"score not computed: {e!r}")
    return out


def _fixed_eval(mdl, prob, assignment, seconds=3.0):
    """The model with every shift held to `assignment` (None: unfilled) and
    only its auxiliaries left to settle — each slack at its least — so a
    week's cost is exact term by term: the search's own last solution may
    carry a slack it had not yet driven down when time ran out. A solved
    CpSolver, or None."""
    if mdl is None or prob is None:
        return None
    # the whole objective with no phase held (_solve_phases bounds mdl.m)
    m = (getattr(mdl, "base", None) or mdl.m).clone()
    m.clear_hints()
    for u in mdl.live:
        p = assignment[u.id] if u.id < len(assignment) else None
        for q in prob.dom[u.id]:
            m.add(mdl.x[(u.id, q)] == (1 if q == p else 0))
        m.add(mdl.unf[u.id] == (1 if (p is None or p not in prob.dom[u.id]) else 0))
    s = _solver_for(seconds, workers=1, deterministic=True)
    st = s.solve(m)
    return s if st in (_cp.OPTIMAL, _cp.FEASIBLE) else None


def _draft_objective(mdl, prob):
    """The model's cost of the draft's own assignment."""
    if mdl is None or prob is None:
        return None
    s = _fixed_eval(mdl, prob, [u.draft for u in prob.units])
    return s.objective_value if s is not None else None


def summary(result: dict) -> dict:
    """schedule_solver.summary's shape, plus the cost terms and the engine."""
    out = _solver.summary(result)
    stats = result.get("stats") or {}
    out["engine"] = stats.get("engine")
    out["terms"] = stats.get("terms") or {}
    out["cost"], out["draft_cost"] = stats.get("cost"), stats.get("draft_cost")
    out["reasons"] = list(result.get("reasons") or [])[:60]
    return out


# ── the solver-first draft: shapes, then people (RM-8 + RM-11) ──────────────


def solver_draft(dates, constraints, requirements, concurrent=None, history_rows=None, context_rows=None,
                 last_week_rows=None, signals=None, weights=None, profiles=None, roster_roles=None,
                 budget_dollars=None, max_seconds=None, seed=SEED, standby_slots=None, wall_cap=None,
                 demand_curve=None, objective=None) -> dict:
    """The week drafted with no model call: schedule_shapes (Stage 1) picks
    each role's shifts — and a first person for each — to cover its
    people-on-at-once demand, floors, the section cap and the closers, with
    the budget priced against the least valuable half hours; assign (Stage
    2) picks who works each one with `context_rows` (the manager plan, a
    redo's kept days, locked rows) held as they are. Both price the week
    from one schedule_objective.ObjectiveContext (contract #1).

    `max_seconds` is a DETERMINISTIC work budget, split by size and never by
    the clock (LOGIC-9: Stage 2 used to get whatever the wall clock had
    left, so the same inputs drafted different weeks on a busy box);
    `wall_cap` the emergency wall-clock cap.

    {"rows" (staffed shifts — the context rows are NOT among them),
    "unassigned" (shifts nobody could legally take, left out), "short"
    (demand no shift could cover, and roles nobody may work that day, with
    why), "assumed" (roles drafted with no history and no floor),
    "conflicts", "reasons", "terms", "stats" (with each stage's status, seed
    and deterministic budget, the week priced, the usual days kept)} — the
    shape schedule_fallback.solver_rows returns, so the engine's code-draft
    path takes either."""
    import schedule_shapes as _shapes
    t0 = _time.monotonic()
    secs = float(max_seconds or DEFAULT_SECONDS)
    sig = dict(signals or {})
    ot = sig.get("overtime") or {}
    rates = dict(ot.get("role_typical") or {})
    rates.update({k: v for k, v in (ot.get("rates") or {}).items() if k != "_default"})
    blended = ot.get("default_rate") or (ot.get("rates") or {}).get("_default")
    hist_all = list(history_rows or []) + list(last_week_rows or [])
    ctx = objective
    if ctx is None:
        ctx = _obj.ObjectiveContext.build(getattr(constraints, "restaurant_id", None), dates, budget=budget_dollars,
                                          last_week_rows=last_week_rows, requirements=requirements,
                                          demand_curve=demand_curve or sig.get("demand_curve"),
                                          history_rows=hist_all, rates=rates,
                                          person_rates=ot.get("person_rates"), blended_rate=blended,
                                          ot_line=ot.get("line"), constraints=constraints)
    read = {_low(n) for n in (sig.get("notes_read") or ())}
    exclude = sorted(n for n, note in (sig.get("constraints") or {}).items()
                     if n and str(note or "").strip() and _low(n) not in read)
    s1_secs = max(1.0, min(_shapes.DEFAULT_SECONDS, secs * 0.3))
    s2_secs = max(1.0, secs - s1_secs)

    def wall_left():
        return max(0.5, float(wall_cap) - (_time.monotonic() - t0)) if wall_cap else None
    shaped = _shapes.shape_week(dates, constraints, requirements, concurrent=concurrent, history_rows=history_rows,
                                context_rows=context_rows, last_week_rows=last_week_rows, rates=rates,
                                blended_rate=blended, budget_dollars=budget_dollars, roster_roles=roster_roles,
                                max_seconds=s1_secs, seed=seed, staffing_value=sig.get("staffing_value"),
                                person_rates=ot.get("person_rates"), objective=ctx, exclude=exclude,
                                wall_cap=wall_left())
    stats = {"shapes": shaped["stats"], "engine": "shapes+cp-sat", "seed": int(seed),
             "budget": {"shapes": round(s1_secs, 2), "assign": round(s2_secs, 2), "unit": "deterministic"}}
    # A standby on a busy shift likely to come up short (RM-18, G2's
    # attendance_risk.standby_needed): one more of the role over the
    # daypart's core window, staffed only while the budget has room.
    slots = shaped["rows"] + [dict(r, _standby=True, employee="") for r in standby_slots or []]
    if not slots:
        return {"rows": [], "unassigned": [], "short": shaped["short"], "assumed": shaped.get("assumed") or [],
                "conflicts": [], "reasons": [], "terms": {},
                "stats": dict(stats, status="nothing_to_solve", seconds=round(_time.monotonic() - t0, 3))}
    ctx_rows = [dict(r, _pinned=True) for r in context_rows or []]
    got = assign(ctx_rows + slots, constraints, signals=sig, weights=weights, profiles=profiles,
                 only_dates=list(dates), last_week=last_week_rows, max_seconds=s2_secs, mode="draft",
                 budget_dollars=budget_dollars, roster_roles=roster_roles,
                 pending=getattr(constraints, "pending_off", None), seed=seed, wall_cap=wall_left(),
                 objective=ctx, history_rows=history_rows)
    rows = list(got["rows"][len(ctx_rows):]) if got["status"] in ("optimal", "time_limit") else []
    clean = [{k: v for k, v in r.items() if k not in ("_open", "_pinned", "_s1")} for r in rows
             if (r.get("employee") or "").strip()]
    unassigned = [dict(e) for e in got.get("unfilled") or []]
    priced = None
    try:
        priced = _obj.price_week(list(context_rows or []) + clean, ctx)
        priced.pop("by_slot", None)
    except Exception as e:                   # never costs the week
        got.setdefault("notes", []).append(f"week not priced: {e!r}")
    stats.update(status=got["status"], proved_optimal=got.get("proved_optimal"), slots=got.get("slots"),
                 people=got.get("people"), objective=got.get("objective"), bound=got.get("bound"),
                 phases=got.get("phases"), phase_cost=got.get("phase_cost"), wall_capped=got.get("wall_capped"),
                 workers=got.get("workers"), assign_seconds=got.get("seconds"), build_seconds=got.get("build_seconds"),
                 seconds=round(_time.monotonic() - t0, 3), dollars=got.get("dollars"), notes=got.get("notes"),
                 priced=priced, usual_days=usual_days_kept(list(context_rows or []) + clean, ctx),
                 history_weeks=int((ctx.profile or {}).get("weeks") or 0))
    return {"rows": clean, "unassigned": unassigned, "short": shaped["short"], "assumed": shaped.get("assumed") or [],
            "conflicts": got.get("conflicts") or [], "reasons": got.get("reasons") or [],
            "terms": got.get("terms") or {}, "stats": stats}


def usual_days_kept(rows, ctx) -> dict:
    """{"kept", "usual", "pct", "off_usual"} — of each person's usual working
    weekdays (worked in at least schedule_objective.USUAL_SHARE of their
    history's weeks), how many this week keeps, and the days placed on a
    weekday they almost never work: measured, for the review line, never
    claimed without it (STRESS-15). pct None without history."""
    people = (getattr(ctx, "profile", None) or {}).get("people") or {}
    worked = {}
    for r in rows or []:
        n = (r.get("employee") or "").strip()
        if n:
            worked.setdefault(ctx.key(n), set()).add(_obj._weekday(r.get("date")))
    kept = usual = off = 0
    for k, pu in people.items():
        if (pu.get("seen_weeks") or 0) < _obj.USUAL_MIN_WEEKS:
            continue
        days = {d for d, s in (pu.get("days") or {}).items() if s >= _obj.USUAL_SHARE}
        got = worked.get(k, set())
        usual += len(days)
        kept += len(days & got)
        off += sum(1 for d in got if float((pu.get("days") or {}).get(d) or 0) <= _obj.RARE_SHARE)
    return {"kept": kept, "usual": usual, "pct": round(100.0 * kept / usual, 1) if usual else None, "off_usual": off}
