"""
schedule_optimizer.py — the Shift Quality score as the schedule's objective.

The generator writes a week once; the rule sweep repairs hard breaches; and
until this module the score was only ever REPORTED. Replaying real weeks
showed what that cost: one capped peak-night shift (a missing leader, a
half-hour with no pizza cook) took 9-12 points off a week, and nothing went
back to fix it, because neither is a hard-rule breach. The one existing
search (shift_quality.compare_candidates) could only trade two people in the
same role, chose which trades to try by rating gap — all zero at a
restaurant nobody has rated — and its answer was thrown away.

This is a bounded local search over the moves a manager actually makes:

  add       someone legal takes a new shift where a role is short
  extend    a shift starts earlier or ends later to cover a thin half hour
  replace   someone else takes an existing shift (a leader for a busy night,
            a rested person for somebody on day seven, a veteran, somebody
            who turns up, the teammate with room instead of overtime)
  swap      two people in one role trade shifts on different dates
  trade     two people on the same date trade shifts (the other half of the
            day, the role's opening or closing shift, the role the managers
            keep giving one of them)
  trim      a shift is shortened, or removed, where a day is over its target

Candidates come first from what the week breaks of the restaurant's
scheduling memory (the score's learned-patterns measure, shift_quality.
week_learned over signals["learned"] — L-3, D-35), then from the weak
dimensions' own facts, costliest first (demand-weighted, capped shifts
before everything), and — once those have nothing left — from the what-if's
swaps and replacements, so a better arrangement the comparison would report
is taken here instead (schedule audit 10/3/26 P-33, SQ-19).

Every candidate is judged by what it does to the WEEK the owner gets:

  legal       the person-level rules for whoever gains a shift
              (Constraints.can_add — overtime line included — and fillable,
              P-2), then the whole week swept (incrementally, P-37) and
              compared by breach identity with the week as it stands after
              the moves already taken (schedule_rules.regressions up to the
              budget tier: nothing about a person, the manager every minute,
              the floors, the closer, overtime or minimum hours may get new
              or worse — P-14, E-1); never a new hour of overtime for anybody
              (the owner's rule: a same-role teammate takes it or nobody
              does; somebody who habitually runs past their shift has the
              line less that headroom — L-16); never a close the memory says
              runs late ended before it really ends; never a row the manager
              plan or the owner pinned; the hourly hours budget a ceiling
              (salaried hours are not spent from it, P-6); the section count.
  scored      by the same shift_quality.score_rows the owner's number comes
              from, with the rows that will not stand and the hard breaches
              of THAT trial (P-28), less what the move costs in labor
              dollars (schedule_economics.priced_cost: each person's own
              rate, the role's, overtime at its premium, salaried people
              free — P-32) — so a $25/h add no longer costs the same as a
              $14/h one — and what it keeps of the rows the manager is
              expected to change (signals["likely_edits"], L-15). What a
              move breaks of the scheduling memory is in the score itself.

The best improvement is taken and recorded with a sentence saying what
changed and what it bought. The search stops at the target, when nothing
improves, or at its time and evaluation budget.

Nothing here calls a model. Pure over its inputs apart from time.
"""
import time as _time
from datetime import datetime as _dt

import shift_quality as sq

# Stop once the week reaches this, or when no candidate improves it.
DEFAULT_TARGET = 92
# Seconds and evaluations one run may spend. A score is a few milliseconds
# on a 250-row week, so this is well under a minute on the background job.
DEFAULT_SECONDS = 25.0
DEFAULT_EVALUATIONS = 1500
# Moves tried per round, and the smallest week-level gain worth taking.
MOVES_PER_ROUND = 24
MIN_GAIN = 0.15
# A move this good is taken without trying the rest of the round.
TAKE_AT_ONCE = 1.0
# The weekly hours budget is a ceiling (the prompt, the review and the trim
# all treat it as one); a move may not carry the week past it by more than
# the same 2% the review tolerates.
BUDGET_TOLERANCE = 1.02
# How far a shift may be stretched in one move.
MAX_EXTEND_MINUTES = 150
STEP = 30
# A move that takes hours out of a weekday where cutting staffing was taken
# and measured WORSE here (inputs["learned_worse"], rec_learning.
# worsened_levers — memory audit 9/29/26, "what_worked") pays this much of
# its gain per worse result, capped: a small score gain no longer repeats a
# cut that went badly, a large one (a breach fixed) still goes through and
# says what it overrode.
LEARNED_WORSE_STEP = 0.5
LEARNED_WORSE_MAX = 1.5
# What labor dollars are worth against the score (schedule audit 10/3/26
# P-32): week points per 1% of the week's labor dollars a move adds over the
# draft. An 8h add at $25/h on a $15,000 week is 1.3% — 0.67 points — and
# the same add at $14/h 0.37: of two adds that buy the same, the cheaper
# wins, and an add has to buy more than its cost. Dollars a move SAVES earn
# nothing: the search finishes the draft's quality, it never trades the
# score for a cheaper week (the budget trim and labor efficiency own that).
LABOR_POINTS_PER_PCT = 0.5
# A row the edit predictor says the manager will change (schedule_learning.
# likely_edit_signals, when the engine passes signals["likely_edits"]):
# points of its shift per unit of its weight for keeping it as it is.
LIKELY_EDIT_SHIFT_POINTS = 10.0
# The what-if's swaps and replacements tried in a round once the weak
# dimensions have no improving move left (P-33).
WHAT_IF_CANDIDATES = 40

NOTE_TAG = "Cavnar AI:"


# ── time helpers ───────────────────────────────────────────────────────────

def _m(value):
    return sq._slot_minutes(value)


def _fmt(minutes: int) -> str:
    minutes %= 24 * 60
    h, mm = divmod(minutes, 60)
    return f"{h % 12 or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def _span(row):
    s, e = _m(row.get("shift_start")), _m(row.get("shift_end"))
    if s is None or e is None:
        return None, None
    if e <= s:
        e += 24 * 60
    return s, e


def _hours_between(s, e) -> float:
    return round((e - s) / 60.0, 2)


def _day(date: str) -> str:
    return sq._day_name(date)


def _low(name) -> str:
    return (name or "").strip().lower()


# ── the objective ──────────────────────────────────────────────────────────

def objective(quality: dict) -> float:
    """The week's score before rounding. Integer week scores hide a shift
    moving three points; the search needs to see it."""
    scored = [s for s in (quality or {}).get("shifts") or [] if s.get("scored")]
    if not scored:
        return 0.0
    # The week score before rounding, week-level measures (fatigue) included
    # at their share — the shift mean alone no longer sees them.
    if (quality or {}).get("raw_score") is not None:
        return float(quality["raw_score"])
    num = sum(s["score"] * sq.DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0) for s in scored)
    den = sum(sq.DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0) for s in scored)
    return num / (den or 1.0)


# ── labor dollars (P-32) ────────────────────────────────────────────────────

def pricing_inputs(signals: dict, inputs: dict = None, constraints=None):
    """What a week is priced with — the overtime forecast's own inputs the
    engine passes the scorer (signals["overtime"]: the role rates, each
    person's own rate, a role's typical rate, the overtime line, the payroll
    week of each date, the hours already published in it, the daily line)
    and who is salaried (their hours cost no more). None with no rate of any
    kind on file: dollars are then not weighed."""
    ot = (signals or {}).get("overtime") or {}
    rates = ot.get("rates") or {}
    blended = (inputs or {}).get("blended_rate") or ot.get("default_rate") or rates.get("_default")
    person = ot.get("person_rates") or {}
    if not (rates or blended or person):
        return None
    bucket_of = ot.get("bucket_of") or {}
    if bucket_of:
        bucket = lambda d: bucket_of.get(d, "")  # noqa: E731
    else:
        bucket = constraints.bucket if constraints is not None else None
    salaried = (signals or {}).get("salaried")
    if salaried is None and constraints is not None:
        salaried = getattr(constraints, "salaried", None)
    published = ot.get("published") or {}
    return {"rates": rates, "blended": blended, "person_rates": person, "role_typical": ot.get("role_typical") or {},
            "ceiling": float(ot.get("line") or 40.0), "base_hours": {_low(k): v for k, v in published.items()},
            "bucket": bucket, "daily": ot.get("daily_line"), "salaried": set(salaried or ())}


def labor_dollars(rows: list, pricing: dict) -> float:
    """The week's labor dollars, overtime at its premium (schedule_economics.
    priced_cost), unrounded so a half-hour move still moves it."""
    if not pricing:
        return 0.0
    import schedule_economics as _econ
    out = _econ.priced_cost(rows, pricing["rates"], pricing["blended"], ceiling=pricing["ceiling"],
                            base_hours=pricing["base_hours"], bucket=pricing["bucket"],
                            daily_ot_hours=pricing["daily"], salaried=pricing["salaried"],
                            person_rates=pricing["person_rates"], role_typical=pricing["role_typical"],
                            rounded=False)
    return float(out.get("total") or 0.0)


# ── what the restaurant's scheduling memory holds (L-3, D-35) ──────────────

def ot_headroom(signals: dict, constraints=None) -> dict:
    """{person key: hours} — somebody the scheduling memory says habitually
    runs past their shift (schedule_memory's ot_risk, L-16): their overtime
    line is the line less these hours for every check that no move or
    answer puts anybody newly past it."""
    key = constraints.key if (constraints is not None and hasattr(constraints, "key")) else _low
    out = {}
    for m in (signals or {}).get("learned") or []:
        if not isinstance(m, dict) or m.get("kind") != "ot_risk" or not m.get("person"):
            continue
        try:
            v = m.get("value") if isinstance(m.get("value"), dict) else {}
            head = float(v.get("headroom_hours") or 0)
        except (TypeError, ValueError):
            head = 0.0
        if head > 0:
            k = key(m["person"])
            out[k] = max(out.get(k, 0.0), head)
    return out


def memory_misses(quality: dict) -> list:
    """What the week breaks of the scheduling memory, as its score judged it
    (shift_quality.week_learned's facts — schedule_memory.misses, the one
    meaning of every kind): [{key, kind, weight, person, day, daypart, role,
    value, rows, text}]. The memory is IN the score (the learned-patterns
    measure), so the search, the solver's judge and every pass that chooses
    by the score hold it; this is what the moves that put it right read."""
    for d in (quality or {}).get("week_dimensions") or []:
        if d.get("key") == "learned":
            return list((d.get("facts") or {}).get("misses") or [])
    return []


def _overrun_signals(signals: dict) -> list:
    """The scheduling memory's closes that run late (end_overrun, L-16):
    schedule_memory.pad_overruns ended them when they really end before the
    search; no move may shorten one back below it."""
    return [m for m in (signals or {}).get("learned") or []
            if isinstance(m, dict) and m.get("kind") == "end_overrun"]


def _overrun_weight(rows: list, overruns: list, families: dict = None) -> float:
    """How far `rows` fall short of the closes the memory says run late
    (schedule_memory.misses over the end_overrun memories alone)."""
    if not overruns:
        return 0.0
    import schedule_memory as _smem
    return sum(float(x.get("weight") or 0) for x in _smem.misses(rows, overruns, families=families or None))


def week_value(quality: dict, rows: list, pricing=None, base_dollars: float = 0.0, likely=None) -> float:
    """What a week is worth to the owner, in week points: its Shift Quality
    (objective — what it breaks of the scheduling memory is in it, the
    learned-patterns measure, L-3), less LABOR_POINTS_PER_PCT for each 1% of
    labor dollars it costs over `base_dollars` (the draft's — a saving earns
    nothing, P-32), less what it keeps of the rows the manager is expected
    to change (likely_cost, L-15). The one value the repair loop and the
    solver's judge both choose by."""
    v = objective(quality)
    if pricing and base_dollars > 0:
        over = labor_dollars(rows, pricing) - base_dollars
        if over > 0:
            v -= LABOR_POINTS_PER_PCT * over / base_dollars * 100.0
    if likely:
        v -= likely_cost(rows, likely, quality)
    return v


def _demand_shares(rows: list, quality: dict = None):
    """({(date, daypart): demand weight}, the week's total) — from the
    scored shifts when given, else every shift on the rows at weight 1."""
    dw = {}
    for s in (quality or {}).get("shifts") or []:
        if s.get("scored"):
            dw[(s.get("date"), s.get("daypart"))] = sq.DEMAND_WEIGHT.get((s.get("profile") or {}).get("demand"), 1.0)
    if not dw:
        for r in rows or []:
            if r.get("date") and (r.get("employee") or "").strip():
                for part in sq.present_dayparts(r):
                    dw[(r["date"], part)] = 1.0
    return dw, (sum(dw.values()) or 1.0)


def likely_cost(rows: list, likely: list = None, quality: dict = None) -> float:
    """Week points `rows` give up for each row kept that the edit predictor
    expects the manager to change (signals["likely_edits"], L-15):
    LIKELY_EDIT_SHIFT_POINTS of its shift per unit of its weight, the shift
    weighed into the week by its demand (the week score is the demand-
    weighted mean of its shifts)."""
    if not likely:
        return 0.0
    dw, total = _demand_shares(rows, quality)
    here = {}
    for r in rows or []:
        k = (_low(r.get("employee")), r.get("date") or "", r.get("shift_start") or "")
        here[k] = sq.present_dayparts(r)[0]
    pts = 0.0
    for f in likely:
        try:
            w = float(f.get("weight") or 0)
        except (TypeError, ValueError):
            w = 0.0
        k = (_low(f.get("employee")), f.get("date") or "", f.get("shift_start") or "")
        if w > 0 and k in here:
            pts += LIKELY_EDIT_SHIFT_POINTS * w * dw.get((k[1], here[k]), 1.0) / total
    return pts


# ── what is weak ───────────────────────────────────────────────────────────

def _problems(quality: dict) -> list:
    """(cost, shift, dimension) for every dimension under 100, costliest
    first. A capping dimension carries the whole shift's shortfall."""
    out = []
    for s in (quality or {}).get("shifts") or []:
        if not s.get("scored"):
            continue
        w = sq.DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0)
        dims = s.get("dimensions") or []
        total = sum(d["weight"] for d in dims) or 1.0
        for d in dims:
            if d["score"] >= sq.SCORE_MAX:
                continue
            if s.get("capped_by") == d["key"]:
                cost = (sq.SCORE_MAX - s["score"]) * w + 100
            else:
                cost = (sq.SCORE_MAX - d["score"]) * d["weight"] / total * w
            out.append((cost, s, d))
    out.extend(_week_problems(quality))
    out.sort(key=lambda t: -t[0])
    return out


def _week_problems(quality: dict) -> list:
    """(cost, shift, dimension) for a week-level measure under 100 (fatigue
    is judged once for the week, not on each shift): one entry per shift a
    strained person works, so the moves stay the per-date ones _moves_for
    already builds, each costed at the measure's share of the week."""
    out = []
    shifts = [s for s in (quality or {}).get("shifts") or [] if s.get("scored")]
    for d in (quality or {}).get("week_dimensions") or []:
        # The scheduling memory places itself (_learned_problems).
        if d.get("score", 100) >= sq.SCORE_MAX or d.get("key") == "learned":
            continue
        facts = d.get("facts") or {}
        names = set(facts.get("strained") or [])
        # Minimum hours (week_min_hours) names who is short, not strained:
        # the shifts that could be theirs are their role's on any date.
        names |= {x.get("name") for x in (facts.get("short") or []) if isinstance(x, dict) and x.get("name")}
        cost = (sq.SCORE_MAX - d["score"]) * float(d.get("share") or 0)
        for s in shifts:
            if d.get("key") == "min_hours" or names & set(s.get("people") or []):
                out.append((cost, s, d))
    return out


def _learned_problems(quality: dict) -> list:
    """(cost, shift, pseudo-dimension) for each shift a broken memory of the
    restaurant's scheduling sits on (memory_misses), so the moves that put
    it right are tried before anything else (L-3): the shift of each row it
    names; else each shift on its weekday and daypart (somebody the managers
    keep putting on it, one of the people they put on the role's slot);
    else each shift the person works (overtime headroom) or, for a team,
    each shift of the dates it is split on."""
    misses = memory_misses(quality)
    if not misses:
        return []
    shifts = [s for s in (quality or {}).get("shifts") or [] if s.get("scored")]
    at = {(s.get("date"), s.get("daypart")): s for s in shifts}
    out = []
    for x in misses:
        targets = []
        # A miss the score keeps private (the owner's keep-apart — no names
        # in the shared review, shift_quality.week_learned) names only its
        # shifts; the move reads the memory itself (_moves_for, LEARN-1).
        for r in list(x.get("rows") or []) + [{"date": d, "daypart": p} for d, p in x.get("slots") or []]:
            s = at.get((r.get("date"), r.get("daypart")))
            if s is not None and s not in targets:
                targets.append(s)
        if not targets:
            kind = x.get("kind")
            who = sq.name_key(x.get("person"))
            if kind in ("moved_on", "leader_swap", "role_change"):
                targets = [s for s in shifts if s.get("day") == x.get("day") and s.get("daypart") == x.get("daypart")]
            elif kind == "ot_risk":
                targets = [s for s in shifts if who in {sq.name_key(n) for n in s.get("people") or []}]
            elif kind == "pair":
                team = {who} | {sq.name_key(n) for n in ((x.get("value") or {}).get("with") or [])}
                apart = (x.get("value") or {}).get("kind") == "avoid"
                by_date = {}
                for s in shifts:
                    on = team & {sq.name_key(n) for n in s.get("people") or []}
                    if on:
                        by_date.setdefault(s.get("date"), []).append((s, on))
                for _d, here in by_date.items():
                    if apart:
                        # Two the owner keeps apart, on the same shift.
                        targets += [s for s, on in here if len(on) > 1]
                        continue
                    there = set().union(*(on for _s, on in here))
                    if len(there) > 1 and any(len(on) < len(there) for _s, on in here):
                        targets += [s for s, _on in here]
        for s in targets:
            out.append((150.0 * float(x.get("weight") or 0), s,
                        {"key": "learned", "score": 0, "weight": 0, "facts": {"miss": x}}))
    return out


# ── the search ─────────────────────────────────────────────────────────────

class _State:
    """What move generation needs, recomputed after each accepted move."""

    def __init__(self, rows, signals, inputs, constraints=None):
        self.rows = rows
        self.signals = signals
        self.constraints = constraints
        rules = signals.get("rules") or {}
        self.index = sq._SwapIndex(rows, signals.get("availability") or {},
                                   signals.get("constraints") or {}, rules)
        self.scores = signals.get("scores") or {}
        self.families = dict(signals.get("role_families") or
                             (getattr(constraints, "role_families", None) or {} if constraints is not None else {}))
        # A person's score for one role over the overall one (F2, D-12):
        # {name key: {family: score}}.
        self.role_scores = {sq.name_key(n): {str(f).strip().lower(): v for f, v in (fams or {}).items()
                                             if v is not None}
                            for n, fams in (signals.get("role_scores") or {}).items()}
        self.leader_flags = signals.get("leader_flags") or {}
        roster = [n for n in (signals.get("roster") or []) if n]
        roster_set = set(roster)
        roster_roles = (inputs or {}).get("roster_roles") or signals.get("roster_roles") or {}
        cross = signals.get("cross_trained") or {}
        held = {_low(k): set(v or ()) for k, v in (signals.get("held_roles") or {}).items()}
        pool, fam_pool = {}, {}

        def put(role, n):
            role = str(role or "").strip().lower()
            if role and n:
                pool.setdefault(role, set()).add(n)
                fam_pool.setdefault(self.family(role), set()).add(n)
        for n in roster:
            put(roster_roles.get(n), n)
            for role in held.get(_low(n)) or ():
                put(role, n)
        for r in rows:
            n = (r.get("employee") or "").strip()
            if n and (not roster or n in roster_set):
                put(r.get("role"), n)
        for n, roles in cross.items():
            if roster and n not in roster_set:
                continue
            for role in roles or []:
                put(role, n)
        self.pool, self.fam_pool = pool, fam_pool
        self.pending = {str(k).strip().lower(): set(v or ()) for k, v in
                        ((inputs or {}).get("pending_time_off") or {}).items()}
        self.close_times = (signals.get("close_times") or {})
        self.open_times = (signals.get("open_times") or {})
        self.reliability = {_low(k): v or {} for k, v in (signals.get("reliability") or {}).items()}
        self.tenure = {_low(k): int(v or 0) for k, v in (signals.get("tenure") or {}).items() if v is not None}
        self.marked = {_low(n) for n in (signals.get("experienced") or ()) if n}
        self.by_default = {sq.name_key(n) for n in (signals.get("experienced_default") or ()) if n}
        self.cross = {_low(n): {self.family(x) for x in (roles or [])} for n, roles in cross.items()}
        self.held = {k: {self.family(x) for x in v} for k, v in held.items()}
        self.roster_roles = {_low(n): self.family(r) for n, r in roster_roles.items() if r}
        self.pinned = {i for i, r in enumerate(rows) if r.get("_pinned")}
        self._without = {}
        self.pricing = pricing_inputs(signals, inputs, constraints)

    def rate(self, name, role) -> float:
        """What an hour of `name` in `role` costs (labor.person_rate_book's
        order: their own rate, the role's, the role's typical, the blended);
        0 for somebody salaried, and for everybody with no rate on file."""
        p = self.pricing
        if not p:
            return 0.0
        if sq.name_key(name) in {sq.name_key(n) for n in p["salaried"]}:
            return 0.0
        role = str(role or "").strip().lower()
        return float(p["person_rates"].get(" ".join(str(name or "").lower().split())) or p["rates"].get(role)
                     or p["role_typical"].get(role) or p["blended"] or 0.0)

    def family(self, role) -> str:
        return sq.role_family(role, self.families)

    def key(self, name) -> str:
        """One person, one key, as the rules file them (Constraints.key)."""
        c = self.constraints
        return c.key(name) if (c is not None and hasattr(c, "key")) else _low(name)

    def name_of(self, key) -> str:
        """The spelling of the person `key` means on this week's rows, else
        the roster's."""
        for r in self.rows:
            n = (r.get("employee") or "").strip()
            if n and self.key(n) == key:
                return n
        return next((n for n in (self.signals.get("roster") or []) if n and self.key(n) == key), key)

    def score_of(self, name, role=None):
        """Their score for this role's family when the owner rated them in it,
        else their overall score; None when unrated."""
        mine = self.role_scores.get(sq.name_key(name)) or {}
        if mine and role:
            fam = self.family(role)
            if fam in mine:
                return mine[fam]
        return self.scores.get(name)

    def veteran(self, name) -> bool:
        low = _low(name)
        return (low in self.marked or sq.name_key(name) in self.by_default
                or self.tenure.get(low, 0) >= sq.EXPERIENCE_SHIFTS)

    def reliable(self, name) -> bool:
        r = self.reliability.get(_low(name)) or {}
        try:
            rate = float(r.get("no_show_rate") or 0)
        except (TypeError, ValueError):
            rate = 0.0
        return rate < sq.UNRELIABLE_RATE and not r.get("late_risk")

    def flexible(self, name) -> bool:
        low = _low(name)
        fams = set(self.cross.get(low) or ()) | set(self.held.get(low) or ())
        if self.roster_roles.get(low):
            fams.add(self.roster_roles[low])
        return len(fams) > 1

    def role_rows(self, date, role, part=None):
        role = role.strip().lower()
        out = []
        for i, r in enumerate(self.rows):
            if r.get("date") == date and (r.get("role") or "").strip().lower() == role:
                if part is None or part in sq.present_dayparts(r):
                    out.append(i)
        return out

    def family_rows(self, date, fam, part=None):
        out = []
        for i, r in enumerate(self.rows):
            if r.get("date") == date and self.family(r.get("role")) == fam and (r.get("employee") or "").strip():
                if part is None or part in sq.present_dayparts(r):
                    out.append(i)
        return out

    def people_for(self, role) -> list:
        return sorted(self.fam_pool.get(self.family(role)) or ())

    def template(self, date, role, part):
        """Start and end for a new shift: the commonest among this role on
        this date and daypart, else this role's in that daypart any day."""
        def common(idxs):
            counts = {}
            for i in idxs:
                r = self.rows[i]
                # The row's own daypart by what it covers: a 2pm-close
                # shift is a dinner shift and never the template for lunch.
                if sq.present_dayparts(r)[0] != part:
                    continue
                key = (r.get("shift_start"), r.get("shift_end"))
                counts[key] = counts.get(key, 0) + 1
            return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None
        found = common(self.role_rows(date, role))
        if not found:
            role_low = role.strip().lower()
            found = common([i for i, r in enumerate(self.rows)
                            if (r.get("role") or "").strip().lower() == role_low])
        return found

    def _others(self, skip):
        key = tuple(sorted(skip))
        if key not in self._without:
            self._without[key] = [r for j, r in enumerate(self.rows) if j not in skip]
        return self._without[key]

    def legal_for(self, name, row, skip=(), overtime=True) -> bool:
        """Whether `name` may take `row` with the rows `skip` (indices) gone:
        the person-level rules for them (Constraints.can_add — their week
        swept with it, the overtime line included — and fillable: code never
        picks somebody dormant or on a day their own note covers), every
        daypart window and role they hold (P-2). Without Constraints, the
        swap index's own checks."""
        low = _low(name)
        if not low or row.get("date") in self.pending.get(low, ()):
            return False
        if self.constraints is not None and hasattr(self.constraints, "key") and \
                row.get("date") in (self.constraints.pending_off.get(self.constraints.key(name)) or ()):
            return False
        c = self.constraints
        if c is None:
            if not self.index.person_fits(name, row):
                return False
            hours = self.index.total_hours(low) - sum(sq._row_hours(self.rows[j]) for j in skip
                                                      if _low(self.rows[j].get("employee")) == low)
            return hours + sq._row_hours(row) <= self.index.cap(low) + 0.01
        if not c.fillable(name, row.get("date") or "")[0]:
            return False
        if not c.holds(name, row.get("role") or "", row.get("date")):
            return False
        return bool(c.can_add(dict(row, employee=name), self._others(set(skip)), overtime=overtime)[0])

    def can_add(self, name, row) -> bool:
        low = (name or "").strip().lower()
        if not low or (low, row.get("date")) in self.index.working:
            return False
        return self.legal_for(name, row)

    def can_take(self, name, idx) -> bool:
        """`name` takes rows[idx] outright (nobody else moves) — never a
        second shift that date: the automatic passes do not build doubles."""
        row = self.rows[idx]
        low = _low(name)
        if not low or low == _low(row.get("employee")) or idx in self.pinned:
            return False
        if (low, row.get("date")) in self.index.working:
            return False
        if self.constraints is None and not self.index.replacement_legal(idx, name):
            return False
        return self.legal_for(name, row, skip={idx})

    def can_swap(self, i, j) -> bool:
        """rows i and j trade people (different dates): each is legal for
        the person taking it with both rows moved, the overtime line asked
        only of the side whose hours go up."""
        if i in self.pinned or j in self.pinned:
            return False
        if not self.index.legal(i, j, None):
            return False
        if self.constraints is None:
            return True
        a, b = self.rows[i], self.rows[j]
        na, nb = (a.get("employee") or "").strip(), (b.get("employee") or "").strip()
        trial = list(self.rows)
        trial[i], trial[j] = dict(a, employee=nb), dict(b, employee=na)
        c = self.constraints
        up_b = sq._row_hours(a) > sq._row_hours(b)
        for name, row, up in ((nb, trial[i], up_b), (na, trial[j], not up_b)):
            if not c.fillable(name, row.get("date") or "")[0] or not c.holds(name, row.get("role") or "", row.get("date")):
                return False
            if not c.can_add(row, trial, overtime=up)[0]:
                return False
        return True


def _new_row(date, name, role, start, end, why):
    s, e = _m(start), _m(end)
    hours = _hours_between(s, e if e > s else e + 24 * 60) if s is not None and e is not None else 0
    # A string, like every row the engine writes: the iOS ScheduleRow
    # decodes scheduled_hours as String and a bare number failed the whole
    # response.
    return {"date": date, "day": _day(date), "employee": name, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": str(hours),
            "notes": f"{NOTE_TAG} {why}"}


def _tag(row, why):
    note = (row.get("notes") or "").strip()
    row["notes"] = (note + (" — " if note else "") + f"{NOTE_TAG} {why}")[:240]


def _moves_for(problem, state: _State) -> list:
    """Candidate moves for one weak dimension on one shift, most likely first.
    Each move is (signature, description, apply(rows) -> rows)."""
    _cost, shift, dim = problem
    key, facts = dim["key"], dim.get("facts") or {}
    date, part, day = shift["date"], shift["daypart"], shift["day"]
    where = f"{day} {'lunch' if part == 'morning' else 'dinner' if part == 'night' else part}"
    moves = []

    def add_person(role, why, prefer=None, cover_minute=None):
        tpl = state.template(date, role, part)
        if not tpl:
            return
        start, end = tpl
        if cover_minute is not None:
            # The added shift must actually be on at the thin half hour it is
            # added for: the template's own times left "thin at 9:00pm" with
            # a 3-9pm shift and nobody on at nine.
            ts, te = _m(start), _m(end)
            if ts is not None and te is not None:
                if te <= ts:
                    te += 24 * 60
                dur = te - ts
                close = max((e for r in state.rows if r.get("date") == date
                             for _s, e in [_span(r)] if e is not None), default=te)
                if cover_minute >= te:
                    te = min(max(cover_minute + STEP * 2, te), close)
                    ts = te - dur
                elif cover_minute < ts:
                    ts = cover_minute
                    te = min(ts + dur, close)
                if not (ts <= cover_minute < te):
                    return
                start, end = _fmt(ts), _fmt(te)
        people = state.people_for(role) or sorted(state.pool.get(role.strip().lower(), ()))
        # Of people who buy the same, the one who costs less is tried first
        # (P-32): a move this good is taken without trying the rest.
        pool = sorted(people, key=lambda n: (-(prefer(n) if prefer else 0), state.rate(n, role),
                                             state.index.total_hours(n.lower()), n))
        for name in pool:
            row = _new_row(date, name, role, start, end, why)
            if state.can_add(name, row):
                moves.append((("add", date, name, role, start),
                              f"Added {name} as {role} on {where} ({start}–{end}) — {why}.",
                              lambda rows, row=row: rows + [dict(row)]))
                # (a retime or a trade a rule refused is None until the end)
                if len([m for m in moves if m is not None and m[0][0] == "add"]) >= 3:
                    return

    def replace_in(idx, predicate, why, limit=4, rank=None):
        """Somebody else takes rows[idx]: anybody who works the role (its
        family, a role they hold, cross-trained), legal for them and chosen by
        `predicate`, the fewest hours first (or by `rank`). A rated person
        may take an unrated person's shift and the other way round: the
        score judges unrated people as unknown, not as nothing, so it decides
        (SQ-19 — the guard was for a score that counted them 0)."""
        if idx in state.pinned:
            return
        row = state.rows[idx]
        role = (row.get("role") or "").strip()
        cur = (row.get("employee") or "").strip()
        found = 0
        order = rank or (lambda n: (state.rate(n, role), state.index.total_hours(n.lower()), n))
        for name in sorted(state.people_for(role) or state.pool.get(role.lower(), ()), key=order):
            if name == cur or not predicate(name):
                continue
            if not state.can_take(name, idx):
                continue

            def apply(rows, idx=idx, name=name, why=why):
                out = [dict(r) for r in rows]
                out[idx]["employee"] = name
                _tag(out[idx], why + f" (was {cur})")
                return out
            moves.append((("replace", idx, name),
                          f"Put {name} on {row.get('role')} {where} instead of {cur} — {why}.", apply))
            found += 1
            if found >= limit:
                return

    def mine_here(name, fam=None):
        """Indices of `name`'s rows on this shift (in `fam`, when given)."""
        low = _low(name)
        return [i for i, r in enumerate(state.rows)
                if _low(r.get("employee")) == low and r.get("date") == date and part in sq.present_dayparts(r)
                and (fam is None or state.family(r.get("role")) == fam)]

    def off_shift(name, why, want=None):
        """`name` comes off this shift: somebody else takes it, or they trade
        it for a shift on another date."""
        for i in mine_here(name)[:2]:
            replace_in(i, want or (lambda n: True), why)
            _swap_moves(state, i, moves, why, want=want)

    if key == "coverage":
        for role, n in sorted((facts.get("short") or {}).items(), key=lambda kv: -kv[1]):
            add_person(role, f"{role} was short on {where}")
            # Stretch a same-role shift from the other daypart into this one.
            for i in state.role_rows(date, role):
                if i in state.pinned:
                    continue
                r = state.rows[i]
                if part in sq.present_dayparts(r):
                    continue
                s, e = _span(r)
                if s is None:
                    continue
                lo, hi = sq.CORE_WINDOWS.get(part, (None, None))
                if lo is None:
                    continue
                # Through the daypart's core service, not just enough of it
                # to count: an hour into dinner and gone before the rush
                # "covered" nothing anybody would call dinner.
                new_s, new_e = (min(s, lo), e) if part == "morning" else (s, max(e, hi))
                if (new_e - new_s) - (e - s) > MAX_EXTEND_MINUTES:
                    continue
                moves.append(_retime_move(state, i, new_s, new_e, f"to cover {role} through {where}"))

    elif key == "coverage_curve":
        for role, g in (facts.get("gaps") or {}).items():
            t = g.get("worst_minute")
            if t is None:
                continue
            # Extend the nearest same-role shift that day to reach the gap.
            best = []
            for i in state.role_rows(date, role):
                if i in state.pinned:
                    continue
                s, e = _span(state.rows[i])
                if s is None:
                    continue
                if t >= e:
                    best.append((t - e, i, s, t + STEP))
                elif t < s:
                    best.append((s - t, i, t, e))
            for dist, i, ns, ne in sorted(best)[:2]:
                if dist <= MAX_EXTEND_MINUTES:
                    moves.append(_retime_move(state, i, ns, ne, f"{role} was thin at {g.get('worst_at')} on {where}"))
            add_person(role, f"{role} was thin at {g.get('worst_at')} on {where}", cover_minute=t)

    elif key == "leadership":
        for miss in facts.get("misses") or []:
            role = (miss.get("role") or "").strip()
            if not role:
                continue
            if miss.get("attribute"):
                ok = lambda n: bool(state.leader_flags.get(n))  # noqa: E731
            elif miss.get("min_score") is not None:
                ok = lambda n, ms=float(miss["min_score"]), rl=role: (state.score_of(n, rl) or 0) >= ms  # noqa: E731
            else:
                ok = lambda n: True  # noqa: E731
            label = miss.get("rule") or role
            fam = state.family(role)
            weakest = sorted(state.family_rows(date, fam, part),
                             key=lambda i: (bool(ok(state.rows[i].get("employee"))),
                                            state.score_of(state.rows[i].get("employee"), role) or 0))
            for i in weakest[:2]:
                if ok(state.rows[i].get("employee")):
                    continue
                replace_in(i, ok, f"{where} needs {label}")
                _swap_moves(state, i, moves, f"{where} needs {label}", want=ok)
            add_person(role, f"{where} needs {label}", prefer=lambda n, ok=ok: 1 if ok(n) else -1)
        if facts.get("profile_leader_missing"):
            roles = [r for r in (facts.get("leader_roles") or [])] or \
                    sorted({(state.rows[i].get("role") or "") for i in range(len(state.rows))
                            if state.rows[i].get("date") == date})
            ms = float(facts.get("leader_min_score") or 4)
            ok = lambda n: bool(state.leader_flags.get(n)) or (state.score_of(n) or 0) >= ms  # noqa: E731
            for role in roles:
                for i in state.family_rows(date, state.family(role), part)[:2]:
                    replace_in(i, ok, f"{where} needs somebody who can run it")
                    _swap_moves(state, i, moves, f"{where} needs somebody who can run it", want=ok)

    elif key == "operational_strength":
        # Strength is about WHO works, never about adding people (the
        # prompt's own rule): a shortfall is answered by a stronger person
        # in the place of a weaker one, never by another body. The add move
        # here let the search buy strength with hours up to the budget
        # ceiling (schedule audit 10/3/26 SQ-5); the scorer now judges the
        # role's average, so a body only ever helped by being stronger.
        for sf in facts.get("shortfalls") or []:
            roles = sf.get("roles") or [sf.get("role") or ""]
            idxs = sorted({i for role in roles for i in state.role_rows(date, role, part)},
                          key=lambda i: state.score_of(state.rows[i].get("employee"), state.rows[i].get("role")) or 0)
            for i in idxs[:2]:
                role = state.rows[i].get("role") or sf.get("role") or ""
                cur = state.score_of(state.rows[i].get("employee"), role) or 0
                stronger = lambda n, cur=cur, role=role: (state.score_of(n, role) or 0) > cur  # noqa: E731
                replace_in(i, stronger, f"{role} strength was under target on {where}")
                _swap_moves(state, i, moves, f"{role} strength was under target on {where}", want=stronger)

    elif key == "demand_match":
        # The team's average under what this restaurant's own rated people
        # field on a shift this busy (SQ-12): somebody stronger in for the
        # weakest rated person of the role, or traded in from a quieter date
        # (schedule audit 10/3/26 SQ-19 — the dimension had no move at all).
        for fam, info in sorted((facts.get("by_role") or {}).items(),
                                key=lambda kv: (kv[1] or {}).get("average", 0) - (kv[1] or {}).get("wanted", 0)):
            info = info or {}
            if float(info.get("average") or 0) >= float(info.get("wanted") or 0) - 1e-9:
                continue
            want = float(info.get("wanted") or 0)
            idxs = sorted(state.family_rows(date, fam, part),
                          key=lambda i: (state.score_of(state.rows[i].get("employee"), state.rows[i].get("role")) is None,
                                         state.score_of(state.rows[i].get("employee"), state.rows[i].get("role")) or 0))
            for i in idxs[:2]:
                role = state.rows[i].get("role") or ""
                cur = state.score_of(state.rows[i].get("employee"), role)
                floor_ = max(want, float(cur or 0))
                better = lambda n, f=floor_, role=role: (state.score_of(n, role) or 0) > f - 1e-9 and \
                    state.score_of(n, role) is not None  # noqa: E731
                why = f"{where} is a {shift['profile'].get('demand', 'busy')}-demand shift and wanted a stronger team"
                replace_in(i, better, why, rank=lambda n, role=role: (-(state.score_of(n, role) or 0),
                                                                      state.index.total_hours(n.lower()), n))
                _swap_moves(state, i, moves, why, want=better)

    elif key == "experience_balance":
        if float(facts.get("experienced_share") or 0) < float(facts.get("target_share") or 0):
            vets = set(facts.get("veterans") or [])
            newer = [n for n in (facts.get("rookies") or [])] + \
                    [n for n in (shift.get("people") or []) if n not in vets and n not in (facts.get("rookies") or [])]
            why = f"{where} wanted more experienced hands"
            for name in newer[:3]:
                off_shift(name, why, want=lambda n: state.veteran(n))

    elif key == "reliability":
        for o in (facts.get("exposed") or []) + (facts.get("late_exposed") or []):
            name = o.get("name")
            if not name:
                continue
            why = (f"{name} is the only {sq.role_words(o.get('role'))} "
                   f"{'opening' if o.get('edge') == 'opens' else 'closing'} on {where} and is often late"
                   if o.get("edge") else f"{name} has missed shifts and was exposed on {where}")
            off_shift(name, why, want=lambda n: state.reliable(n))

    elif key == "pairings":
        for pair in facts.get("clashes") or []:
            a, b = (list(pair) + ["", ""])[:2]
            why = f"{a} and {b} are kept apart"
            for name in (a, b):
                off_shift(name, why, want=lambda n, a=a, b=b: _low(n) not in (_low(a), _low(b)))
        on_here = {_low(n) for n in (shift.get("people") or [])}
        for pair in facts.get("splits") or []:
            lows = [_low(x) for x in pair]
            absent = [x for x in lows if x not in on_here]
            present = [x for x in lows if x in on_here]
            if len(absent) != 1 or len(present) != 1:
                continue
            other = absent[0]
            name = next((n for n in list(state.signals.get("roster") or [])
                         + [(r.get("employee") or "").strip() for r in state.rows] if _low(n) == other), None)
            if not name:
                continue
            why = f"{name} and {present[0].title()} work best together"
            # Bring the absent one in, in place of somebody of their role here.
            fams = {state.family(r.get("role")) for r in state.rows if _low(r.get("employee")) == other}
            fams |= set(state.cross.get(other) or ()) | set(state.held.get(other) or ())
            if state.roster_roles.get(other):
                fams.add(state.roster_roles[other])
            for fam in sorted(fams):
                for i in state.family_rows(date, fam, part):
                    if _low(state.rows[i].get("employee")) in lows:
                        continue
                    replace_in(i, lambda n, nm=name: n == nm, why, limit=1)
                    _swap_moves(state, i, moves, why, want=lambda n, nm=name: n == nm)
        # A pairing from one of the owner's owner-only rules (schedule audit
        # 10/3/26 D-38) counts in the score and is never named — not in the
        # dimension's facts, and not here: the change list and the row
        # notes are read by the team, so its moves say only that the shift
        # is better arranged.
        pairs = state.signals.get("pairs") or {}
        private = pairs.get("private") or set()
        if private:
            why = f"a better arrangement for {where}"
            names_here = {_low(n): n for n in (shift.get("people") or [])}
            for pair in private:
                members = sorted(pair)
                here = [x for x in members if x in names_here]
                if pair in (pairs.get("avoid") or set()) and len(here) == len(members):
                    for x in here:
                        off_shift(names_here[x], why, want=lambda n, p=pair: _low(n) not in p)
                elif pair in (pairs.get("prefer") or set()) and len(here) == 1:
                    other = next(x for x in members if x not in names_here)
                    name = next((n for n in list(state.signals.get("roster") or [])
                                 + [(r.get("employee") or "").strip() for r in state.rows] if _low(n) == other), None)
                    fams = {state.family(r.get("role")) for r in state.rows if _low(r.get("employee")) == other}
                    for fam in sorted(fams) if name else ():
                        for i in [i for i in state.family_rows(date, fam, part)
                                  if _low(state.rows[i].get("employee")) not in pair][:2]:
                            replace_in(i, lambda n, nm=name: n == nm, why, limit=1)
                            _swap_moves(state, i, moves, why, want=lambda n, nm=name: n == nm)

    elif key == "preferences":
        # Staff preferences, week-level (SQ-24, L-19): a strained person's
        # shift on a daypart they asked not to work, or a slot they keep
        # dropping, goes to somebody else or is traded for one they prefer.
        prefs = state.signals.get("preferences") or {}
        learned = state.signals.get("learned_preferences") or {}
        for name in sorted(set(facts.get("strained") or []) & set(shift.get("people") or [])):
            p = prefs.get(name) or {}
            parts = [x for x in (p.get("preferred_dayparts") or []) if x in ("morning", "night")]
            avoid, _prefer, _w = sq._learned(learned.get(name) or (p.get("learned") or {}))
            wrong_part = bool(parts) and part not in parts
            if wrong_part or (day, part) in avoid:
                why = (f"{name} prefers {sq._parts_words(parts)}" if wrong_part
                       else f"{name} keeps asking to drop {sq._slot_words((day, part))}")
                off_shift(name, why)
            try:
                want_h = float(p.get("desired_hours")) if p.get("desired_hours") else None
            except (TypeError, ValueError):
                want_h = None
            if want_h:
                have = sum(sq._row_hours(r) for r in state.rows if (r.get("employee") or "").strip() == name)
                if have > want_h * (1 + sq.PREFERENCE_HOURS_BAND):
                    for i in mine_here(name)[:1]:
                        replace_in(i, lambda n: True, f"{name} asked for about {want_h:g}h")

    elif key == "cross_training":
        for shown, info in (facts.get("by_role") or {}).items():
            if (info or {}).get("score", 100) >= sq.SCORE_MAX:
                continue
            fam = state.family(shown)
            why = f"{where} wanted somebody who can cover a second station"
            for i in [i for i in state.family_rows(date, fam, part)
                      if not state.flexible(state.rows[i].get("employee"))][:2]:
                replace_in(i, lambda n: state.flexible(n), why)
                _swap_moves(state, i, moves, why, want=lambda n: state.flexible(n))

    elif key == "overtime":
        # Overtime a teammate with room could take (SQ-25; the owner's rule:
        # a draft never runs overtime while a same-role teammate has room):
        # one of the person's shifts in that payroll week goes to the teammate
        # the measure names first, else to anybody in the role with room.
        c = state.constraints
        for o in facts.get("people") or []:
            name = o.get("name")
            if not name or name not in (shift.get("people") or []):
                continue
            if c is not None and o.get("bucket") and c.bucket(date) != o.get("bucket"):
                continue
            mate = o.get("teammate")
            why = f"{name} was {o.get('overtime_hours', 0):g}h into overtime"
            for i in mine_here(name)[:1]:
                if mate:
                    replace_in(i, lambda n, m=mate: n == m, why + f"; {mate} had room", limit=1)
                replace_in(i, lambda n: True, why + " and a teammate had room")

    elif key == "stations":
        cfg = state.signals.get("stations") or {}
        if cfg:
            import kitchen_stations as _ks
            cooks = facts.get("cooks") or []
            assigned = facts.get("assigned") or {}
            for station in facts.get("gaps") or []:
                trained = [p for p in (cfg.get("skills") or {}) if station in (_ks.trained(cfg, p) or set())]
                why = f"{station} had no trained cook on {where}"
                # A cook here holding no station the shift needs gives way to
                # one trained on the gap.
                for i, r in enumerate(state.rows):
                    n = (r.get("employee") or "").strip()
                    if (r.get("date") != date or n not in cooks or n in assigned
                            or not _ks.is_kitchen(cfg, r.get("role")) or part not in _ks.parts_of(r)):
                        continue
                    replace_in(i, lambda x, t=trained: any(_low(x) == _low(p) for p in t), why)
                    _swap_moves(state, i, moves, why, want=lambda x, t=trained: any(_low(x) == _low(p) for p in t))

    elif key == "min_hours":
        # Somebody under the minimum the owner set takes a shift of their role
        # from a teammate (the giver is held to their own minimum by the
        # sweep): the week measure had no move (P-4's score side).
        for o in facts.get("short") or []:
            name = o.get("name")
            if not name or name in (shift.get("people") or []):
                continue
            fams = {state.family(r) for r in state.pool if name in state.pool[r]}
            for fam in sorted(fams):
                for i in state.family_rows(date, fam, part)[:2]:
                    replace_in(i, lambda n, nm=name: n == nm,
                               f"{name} is {float(o.get('min', 0)) - float(o.get('hours', 0)):g}h under the minimum you set",
                               limit=1)

    elif key == "learned":
        # What the restaurant's scheduling memory holds and this shift breaks
        # (L-3, D-35; the kinds as schedule_memory.misses reads them). The
        # words on the row and in the list say only that this is how the
        # managers schedule the shift: staff read the notes.
        x = facts.get("miss") or {}
        if (x.get("value") or {}).get("private"):
            # The owner's keep-apart, read back from the memory by its key
            # (the score's copy names nobody — LEARN-1, LEARN-6).
            m = next((m for m in (state.signals or {}).get("learned") or []
                      if isinstance(m, dict) and m.get("key") == x.get("key")), None)
            x = dict(x, person=m.get("person"), value=dict(m.get("value") or {})) if m else {}
        kind, v = x.get("kind"), x.get("value") or {}
        who = state.key(x.get("person"))
        name = state.name_of(who) if who else ""
        why = f"as your managers usually schedule {where}"
        fam = state.family(x.get("role")) if x.get("role") else None

        def mine_now():
            return [i for i, r in enumerate(state.rows) if state.key(r.get("employee")) == who
                    and r.get("date") == date and part in sq.present_dayparts(r)]
        if kind == "moved_off":
            for i in mine_now()[:2]:
                replace_in(i, lambda n, w=who: state.key(n) != w, why)
                _swap_moves(state, i, moves, why, want=lambda n, w=who: state.key(n) != w)
        elif kind in ("moved_on", "leader_swap"):
            names = {who} if kind == "moved_on" else {state.key(n) for n in (v.get("names") or [])}
            want = lambda n, ns=frozenset(names): state.key(n) in ns  # noqa: E731
            fams = {fam} if fam else ({state.family(r) for r in state.pool if name in state.pool[r]} if name else set())
            for i in [i for i in range(len(state.rows)) if state.rows[i].get("date") == date
                      and sq.present_dayparts(state.rows[i])[0] == part
                      and state.family(state.rows[i].get("role")) in fams
                      and state.key(state.rows[i].get("employee")) not in names][:3]:
                replace_in(i, want, why, limit=1)
                _swap_moves(state, i, moves, why, want=want)
                # Somebody it wants who is on the other half of that day
                # trades their shift for this one.
                for j, r in enumerate(state.rows):
                    if r.get("date") == date and j != i and want(r.get("employee")) and \
                            state.family(r.get("role")) == state.family(state.rows[i].get("role")):
                        moves.append(_trade_move(state, i, j, why))
        elif kind == "role_change":
            role = state.family(v.get("role"))
            for i in mine_now()[:1]:
                for j in [j for j in state.family_rows(date, role, part) if j != i][:3]:
                    moves.append(_trade_move(state, i, j, why))
        elif kind in ("opener", "closer"):
            spans = [(j, _span(state.rows[j])) for j in state.family_rows(date, fam)] if fam else []
            spans = [(j, sp) for j, sp in spans if sp[0] is not None]
            if spans:
                if kind == "opener":
                    first = min(sp[0] for _j, sp in spans)
                    edge = [j for j, sp in spans if sp[0] - first <= 15]
                else:
                    last = max(sp[1] for _j, sp in spans)
                    edge = [j for j, sp in spans if last - sp[1] <= 15]
                mine = [i for i, r in enumerate(state.rows) if state.key(r.get("employee")) == who
                        and r.get("date") == date and state.family(r.get("role")) == fam]
                for i in mine[:1]:
                    for j in edge[:3]:
                        moves.append(_trade_move(state, i, j, why))
        elif kind == "pair" and v.get("kind") == "avoid":
            # Two the owner keeps apart on this shift (LEARN-1): all but one
            # of them trades with somebody of their role on the other half
            # of the day, or gives the shift to somebody outside the pair.
            team = {who} | {state.key(n) for n in (v.get("with") or [])}
            here = [i for i, r in enumerate(state.rows) if r.get("date") == date
                    and state.key(r.get("employee")) in team and sq.present_dayparts(r)[0] == part]
            for i in here[1:]:
                outside = lambda n, t=frozenset(team): state.key(n) not in t  # noqa: E731
                replace_in(i, outside, why)
                _swap_moves(state, i, moves, why, want=outside)
                for j in [j for j, r in enumerate(state.rows) if r.get("date") == date
                          and sq.present_dayparts(r)[0] != part
                          and state.family(r.get("role")) == state.family(state.rows[i].get("role"))
                          and state.key(r.get("employee")) not in team][:3]:
                    moves.append(_trade_move(state, i, j, why))
        elif kind == "pair" and v.get("kind") == "prefer":
            team = {who} | {state.key(n) for n in (v.get("with") or [])}
            here = [i for i, r in enumerate(state.rows) if r.get("date") == date and state.key(r.get("employee")) in team]
            parts_of = {i: sq.present_dayparts(state.rows[i])[0] for i in here}
            for i in here:
                # one of the team on the other half of the day comes across
                # into a teammate's half, trading with somebody of their role
                if parts_of[i] == part:
                    continue
                for j in [j for j in state.family_rows(date, state.family(state.rows[i].get("role")), part)
                          if state.key(state.rows[j].get("employee")) not in team][:3]:
                    moves.append(_trade_move(state, i, j, why))
        elif kind == "ot_risk":
            for i in sorted(mine_now(), key=lambda i: sq._row_hours(state.rows[i]))[:1]:
                replace_in(i, lambda n, w=who: state.key(n) != w, why)
        elif kind in ("retime_start", "retime_end", "end_overrun"):
            target = sq._slot_minutes(v.get("time") if kind != "end_overrun" else v.get("padded_end"))
            if target is not None:
                for r in x.get("rows") or []:
                    i = next((k for k, row in enumerate(state.rows)
                              if (row.get("employee") or "").strip() == r.get("employee") and row.get("date") == r.get("date")
                              and row.get("shift_start") == r.get("shift_start")), None)
                    if i is None:
                        continue
                    s, e = _span(state.rows[i])
                    if s is None:
                        continue
                    if kind == "retime_start":
                        ns, ne = target, e
                        if ns >= ne:
                            continue
                    else:
                        ne = target + (24 * 60 if target <= s else 0)
                        ns = s
                    if abs((ne - ns) - (e - s)) > MAX_EXTEND_MINUTES:
                        continue
                    moves.append(_retime_move(state, i, ns, ne, why))

    elif key == "training_balance":
        for text in facts.get("isolated_names") or []:
            name, _, role = text.partition(" on ")
            fam = state.family(role)
            idxs = [i for i in state.family_rows(date, fam, part)
                    if (state.rows[i].get("employee") or "") != name]
            mentor = lambda n, nm=name, rl=role: (state.score_of(n, rl) or 0) >= (state.score_of(nm, rl) or 0) + 2  # noqa: E731
            for i in idxs[:1]:
                replace_in(i, mentor, f"{name} needed somebody stronger alongside on {where}")
                _swap_moves(state, i, moves, f"{name} needed somebody stronger alongside on {where}", want=mentor)

    elif key == "fatigue":
        strained = {o["name"] for o in facts.get("overloaded") or []} | \
                   {o["name"] for o in facts.get("long_runs") or []} | \
                   {o["name"] for o in facts.get("heavy_weeks") or []} | \
                   {o["name"] for o in facts.get("sustained_busy") or []} | \
                   {o["name"] for o in facts.get("sustained_hours") or []}
        for name in sorted(strained):
            for i, r in enumerate(state.rows):
                if (r.get("employee") or "").strip() == name and r.get("date") == date:
                    replace_in(i, lambda n: True, f"{name} was on too many days, hours or busy shifts")
                    _swap_moves(state, i, moves, f"{name} was on too many busy shifts")
        for ls in facts.get("long_shifts") or []:
            for i, r in enumerate(state.rows):
                if (r.get("employee") or "").strip() == ls["name"] and r.get("date") == date and i not in state.pinned:
                    s, e = _span(r)
                    lim = state.signals.get("max_shift_hours")
                    if s is not None and lim:
                        moves.append(_retime_move(state, i, s, s + int(float(lim) * 60),
                                                  f"{ls['name']}'s shift was over {float(lim):g} hours"))

    elif key == "fairness":
        for kind, info in facts.items():
            if not isinstance(info, dict):
                continue
            for o in info.get("overloaded") or []:
                for i, r in enumerate(state.rows):
                    if (r.get("employee") or "").strip() == o["name"] and r.get("date") == date \
                            and part in sq.present_dayparts(r):
                        replace_in(i, lambda n: True, f"{o['name']} had more than their share of {kind} shifts")
                        _swap_moves(state, i, moves, f"{o['name']} had more than their share of {kind} shifts")
        # The multi-week rotation (schedule_intel.rotation_plan): somebody
        # due a weekend off, or resting from closes, who is on this shift.
        rot = facts.get("rotation") or {}
        for name, why in ([(n, f"{n} was due a weekend off") for n in rot.get("weekend_due_working") or []]
                          + [(n, f"{n} is resting from closes") for n in rot.get("resting_closer_closing") or []]):
            for i, r in enumerate(state.rows):
                if (r.get("employee") or "").strip() == name and r.get("date") == date \
                        and part in sq.present_dayparts(r):
                    replace_in(i, lambda n: True, why)
                    _swap_moves(state, i, moves, why)

    elif key == "labor_efficiency":
        ratio = facts.get("ratio") or 1
        if ratio > 1.02:
            # The longest discretionary shifts that day, trimmed by an hour,
            # or the last-added person on the most-staffed role removed. A
            # salaried person's hours are not the day's hours (D-3): cutting
            # them moves nothing here.
            idxs = sorted([i for i, r in enumerate(state.rows) if r.get("date") == date
                           and i not in state.pinned and not _salaried(state, r)],
                          key=lambda i: -sq._row_hours(state.rows[i]))
            for i in idxs[:3]:
                s, e = _span(state.rows[i])
                if s is not None and e - s > 5 * 60 and _can_cut(state, i, 1.0):
                    moves.append(_retime_move(state, i, s, e - 60, f"{day} was over its hour target"))
            for i in idxs[:3]:
                if _can_cut(state, i, sq._row_hours(state.rows[i]), removing=True):
                    moves.append(_remove_move(state, i, f"{day} was over its hour target"))

    elif key == "splh":
        # More hours on this daypart than its usual sales carry at the
        # target: an hour off its longest shifts, or its longest shift off,
        # each only where a floor and the coverage still allow the cut. Not a
        # salaried person's: their hours are not the daypart's (D-3).
        if (facts.get("ratio") or 1) < 1 - sq.SPLH_TOLERANCE:
            idxs = sorted([i for i, r in enumerate(state.rows) if r.get("date") == date
                           and sq.present_dayparts(r)[0] == part and i not in state.pinned
                           and not _salaried(state, r)],
                          key=lambda i: -sq._row_hours(state.rows[i]))
            why = f"{where} carried more hours than its usual sales at the ${facts.get('target', 0):,.0f} target"
            for i in idxs[:3]:
                s, e = _span(state.rows[i])
                if s is not None and e - s > 5 * 60 and _can_cut(state, i, 1.0):
                    moves.append(_retime_move(state, i, s, e - 60, why))
            for i in idxs[:3]:
                if _can_cut(state, i, sq._row_hours(state.rows[i]), removing=True):
                    moves.append(_remove_move(state, i, why))

    elif key == "stability":
        for name in facts.get("changed") or []:
            for i, r in enumerate(state.rows):
                if (r.get("employee") or "").strip() == name and r.get("date") == date:
                    _swap_moves(state, i, moves, f"{name} is not usually on {where}")
    return [m for m in moves if m is not None]


def _salaried(state, row) -> bool:
    sal = state.signals.get("salaried")
    if sal is None and state.constraints is not None:
        sal = getattr(state.constraints, "salaried", None)
    return bool(sal) and sq.name_key(row.get("employee")) in {sq.name_key(n) for n in sal}


def _can_cut(state, i, hours, removing=False) -> bool:
    """A cut the budget trim would also allow: never somebody's only shift
    of the week, never under the minimum hours they asked for, never a row
    the manager plan or the owner pinned."""
    if i in getattr(state, "pinned", ()):
        return False
    name = (state.rows[i].get("employee") or "").strip()
    low = name.lower()
    mine = [r for r in state.rows if (r.get("employee") or "").strip().lower() == low]
    if removing and len(mine) <= 1:
        return False
    c = getattr(state, "constraints", None)
    if c is not None:
        try:
            mn = c.min_hours(name)
        except Exception:
            mn = None
        if mn and sum(sq._row_hours(r) for r in mine) - hours < float(mn) - 0.05:
            return False
    return True


def _retime_move(state, i, new_s, new_e, why):
    row = state.rows[i]
    if i in getattr(state, "pinned", ()):
        return None
    start, end = _fmt(new_s), _fmt(new_e)
    # The person's own rules for the new times (window, minor's end, rest,
    # the overtime line when the shift grows) — and the owner's start/end
    # rule for the role is the last word on times (L-33): a stretch off it is
    # not offered.
    c = getattr(state, "constraints", None)
    if c is not None:
        new = dict(row, shift_start=start, shift_end=end, scheduled_hours=str(_hours_between(new_s, new_e)))
        grows = _hours_between(new_s, new_e) > sq._row_hours(row) + 0.01
        if not c.can_add(new, state._others({i}), overtime=grows)[0]:
            return None
        try:
            import schedule_rules as _rules
            spec = _rules.role_time_for(c, new)
        except Exception:
            spec = {}
        if spec and ((spec.get("start") is not None and new_s % (24 * 60) != spec["start"] % (24 * 60))
                     or (spec.get("end") is not None and new_e % (24 * 60) != spec["end"] % (24 * 60))):
            return None

    def apply(rows, i=i, start=start, end=end, new_s=new_s, new_e=new_e, why=why):
        out = [dict(r) for r in rows]
        was = f"{out[i].get('shift_start')}–{out[i].get('shift_end')}"
        out[i]["shift_start"], out[i]["shift_end"] = start, end
        out[i]["scheduled_hours"] = str(_hours_between(new_s, new_e))
        _tag(out[i], f"{why} (was {was})")
        return out
    return (("retime", i, start, end),
            f"Moved {row.get('employee')}'s {row.get('day') or _day(row.get('date'))} {row.get('role')} shift to "
            f"{start}–{end} (was {row.get('shift_start')}–{row.get('shift_end')}) — {why}.", apply)


def _remove_move(state, i, why):
    row = state.rows[i]
    if i in getattr(state, "pinned", ()):
        return None

    def apply(rows, i=i):
        return [dict(r) for j, r in enumerate(rows) if j != i]
    return (("remove", i),
            f"Took {row.get('employee')}'s {row.get('day') or _day(row.get('date'))} {row.get('role')} shift "
            f"({row.get('shift_start')}–{row.get('shift_end')}) off — {why}.", apply)


def _trade_move(state, i, j, why):
    """Two people on the same date trade shifts (the other half of the day,
    the role's edge, a role the managers keep making one of them): each is
    legal for the person taking it with both rows moved, the overtime line
    asked only of the side whose hours go up. None when either row is
    pinned or a rule refuses it — never without the week's rules."""
    if i == j or i in state.pinned or j in state.pinned or state.constraints is None:
        return None
    a, b = state.rows[i], state.rows[j]
    na, nb = (a.get("employee") or "").strip(), (b.get("employee") or "").strip()
    if not na or not nb or state.key(na) == state.key(nb) or a.get("date") != b.get("date"):
        return None
    up_b = sq._row_hours(a) > sq._row_hours(b)
    if not state.legal_for(nb, a, skip={i, j}, overtime=up_b) or \
            not state.legal_for(na, b, skip={i, j}, overtime=not up_b):
        return None

    def apply(rows, i=i, j=j, why=why):
        out = [dict(r) for r in rows]
        x, y = out[i]["employee"], out[j]["employee"]
        out[i]["employee"], out[j]["employee"] = y, x
        _tag(out[i], f"{why} (traded with {x})")
        _tag(out[j], f"{why} (traded with {y})")
        return out
    day = a.get("day") or _day(a.get("date"))
    return (("trade", min(i, j), max(i, j)),
            f"{na} and {nb} traded {day} shifts ({a.get('role')} {a.get('shift_start')}–{a.get('shift_end')} and "
            f"{b.get('role')} {b.get('shift_start')}–{b.get('shift_end')}) — {why}.", apply)


def _swap_moves(state, i, moves, why, want=None, limit=3):
    """Trade row i's person with someone in the same role family on another
    date. `want(name)` limits who may come in (a leader, somebody stronger).
    Rated and unrated people may trade: the score decides (SQ-19)."""
    if i in state.pinned:
        return
    row = state.rows[i]
    fam = state.family(row.get("role"))
    found = 0
    for j, other in enumerate(state.rows):
        if j == i or j in state.pinned or state.family(other.get("role")) != fam:
            continue
        if not (other.get("employee") or "").strip() or other.get("date") == row.get("date"):
            continue
        if want is not None and not want((other.get("employee") or "").strip()):
            continue
        other_name = (other.get("employee") or "").strip().lower()
        me = (row.get("employee") or "").strip().lower()
        if row.get("date") in state.pending.get(other_name, ()) or other.get("date") in state.pending.get(me, ()):
            continue
        if not state.can_swap(i, j):
            continue

        def apply(rows, i=i, j=j, why=why):
            out = [dict(r) for r in rows]
            a, b = out[i]["employee"], out[j]["employee"]
            out[i]["employee"], out[j]["employee"] = b, a
            _tag(out[i], f"{why} (swapped with {a})")
            _tag(out[j], f"{why} (swapped with {b})")
            return out
        moves.append((("swap", min(i, j), max(i, j)),
                      f"Swapped {row.get('employee')} ({row.get('day') or _day(row.get('date'))}) and "
                      f"{other.get('employee')} ({other.get('day') or _day(other.get('date'))}) on {row.get('role')} — {why}.",
                      apply))
        found += 1
        if found >= limit:
            return


def _what_if_moves(state: _State, quality: dict, limit: int = WHAT_IF_CANDIDATES) -> list:
    """The what-if's own candidates (shift_quality.compare_candidates), for
    the search to take rather than the panel to report (schedule audit
    10/3/26 P-33): same-role swaps across dates and replacements by
    somebody in the role who is off that date. Ranked the comparison's way
    — the widest rating gap first, a stronger person toward the busier
    shift — with unrated people, who are unknown rather than weak, after
    the rated."""
    demand = {}
    for s in (quality or {}).get("shifts") or []:
        if s.get("scored"):
            w = sq.DEMAND_WEIGHT.get((s.get("profile") or {}).get("demand"), 1.0)
            demand[(s.get("date"), s.get("daypart"))] = w

    def dw(r):
        parts = sq.present_dayparts(r)
        return max((demand.get((r.get("date"), p), 1.0) for p in parts), default=1.0)
    ranked = []
    rows = state.rows
    by_fam = {}
    for i, r in enumerate(rows):
        if (r.get("employee") or "").strip() and i not in state.pinned:
            by_fam.setdefault(state.family(r.get("role")), []).append(i)
    for fam, idxs in by_fam.items():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                ri, rj = rows[i], rows[j]
                if ri.get("date") == rj.get("date"):
                    continue
                si = state.score_of(ri.get("employee"), ri.get("role"))
                sj = state.score_of(rj.get("employee"), rj.get("role"))
                if si is None or sj is None:
                    prio = (1, 0.0)
                else:
                    # moving the stronger person toward the busier shift
                    prio = (0, -abs(float(si) - float(sj)) * max(dw(ri), dw(rj)))
                ranked.append((prio, ("swap", i, j)))
    for i, r in enumerate(rows):
        if i in state.pinned or not (r.get("employee") or "").strip():
            continue
        cur = state.score_of(r.get("employee"), r.get("role"))
        for name in state.people_for(r.get("role")):
            if _low(name) == _low(r.get("employee")) or (_low(name), r.get("date")) in state.index.working:
                continue
            sn = state.score_of(name, r.get("role"))
            if sn is None or cur is None:
                prio = (1, 0.0)
            elif float(sn) <= float(cur):
                continue
            else:
                prio = (0, -(float(sn) - float(cur)) * dw(r))
            ranked.append((prio, ("replace", i, name)))
    ranked.sort(key=lambda t: t[0])
    moves = []
    for _prio, spec in ranked:
        if len(moves) >= limit:
            break
        if spec[0] == "swap":
            i, j = spec[1], spec[2]
            if not state.can_swap(i, j):
                continue
            ri, rj = rows[i], rows[j]

            def apply(rs, i=i, j=j):
                out = [dict(r) for r in rs]
                a, b = out[i]["employee"], out[j]["employee"]
                out[i]["employee"], out[j]["employee"] = b, a
                _tag(out[i], f"a better arrangement of the same people (swapped with {a})")
                _tag(out[j], f"a better arrangement of the same people (swapped with {b})")
                return out
            moves.append((("swap", min(i, j), max(i, j)),
                          f"Swapped {ri.get('employee')} ({ri.get('day') or _day(ri.get('date'))}) and "
                          f"{rj.get('employee')} ({rj.get('day') or _day(rj.get('date'))}) on {ri.get('role')} — "
                          "a better arrangement of the same people.", apply))
        else:
            i, name = spec[1], spec[2]
            if not state.can_take(name, i):
                continue
            r = rows[i]
            cur = (r.get("employee") or "").strip()

            def apply(rs, i=i, name=name, cur=cur):
                out = [dict(x) for x in rs]
                out[i]["employee"] = name
                _tag(out[i], f"a better arrangement of the same shifts (was {cur})")
                return out
            moves.append((("replace", i, name),
                          f"Put {name} on {r.get('day') or _day(r.get('date'))} {r.get('role')} instead of {cur} — "
                          "a better arrangement of the same shifts.", apply))
    return moves


def learned_worse_days(inputs) -> dict:
    """{weekday (lower): {worsened, measured, label}} from
    inputs["learned_worse"], keeping the days with a worse result."""
    return {str(k).strip().lower(): v for k, v in ((inputs or {}).get("learned_worse") or {}).items()
            if isinstance(v, dict) and int(v.get("worsened") or 0) > 0}


def learned_penalty(worse, before_rows, after_rows):
    """(penalty, note) for a move that takes hours out of a weekday whose
    cuts measured worse here (learned_worse_days), else (0.0, None). Pure."""
    if not worse:
        return 0.0, None
    hb, ha = {}, {}
    for r in before_rows or []:
        hb[r.get("date")] = hb.get(r.get("date"), 0.0) + sq._row_hours(r)
    for r in after_rows or []:
        ha[r.get("date")] = ha.get(r.get("date"), 0.0) + sq._row_hours(r)
    hit = set()
    for d, h in hb.items():
        if ha.get(d, 0.0) < h - 0.01:
            try:
                hit.add(_dt.strptime(str(d)[:10], "%Y-%m-%d").strftime("%A").lower())
            except (TypeError, ValueError):
                continue
    hit &= set(worse)
    if not hit:
        return 0.0, None
    n = sum(int(worse[d].get("worsened") or 0) for d in hit)
    pen = min(LEARNED_WORSE_MAX, LEARNED_WORSE_STEP * n)
    labels = ", ".join(sorted(str(worse[d].get("label") or d) for d in hit))
    return pen, f"{labels} measured worse here {n} time{'s' if n != 1 else ''} (before and after, not proof)"


def _pinned_rows(rows) -> list:
    """The rows the manager plan or the owner pinned, as the sweep reads
    them: no move may remove, re-time or hand one on."""
    import schedule_rules as _rules
    return sorted(_rules._sweep_sig(r) + (str(r.get("_pinned")),) for r in rows or [] if r.get("_pinned"))


def overtime_created(before_rows, after_rows, c, headroom: dict = None) -> list:
    """[(name, payroll week, hours)] for everybody `after_rows` puts past
    their overtime line (schedule_rules.overtime_line — a salaried person's
    weekly cap) by more than `before_rows` did: the owner's rule is that a
    draft never runs overtime while a same-role teammate has room, and no
    quality move is worth an hour of it. `headroom` ({person key: hours},
    ot_headroom) moves the line in for somebody who habitually runs past
    their shift (L-16): scheduled to the line, they work overtime. []
    without Constraints."""
    if c is None:
        return []
    import schedule_rules as _rules

    key = c.key if hasattr(c, "key") else (lambda n: (n or "").strip().lower())

    def per(rs):
        out = {}
        for r in rs or []:
            n = (r.get("employee") or "").strip()
            if not n or not r.get("date"):
                continue
            k = (key(n), c.bucket(r["date"]))
            out[k] = out.get(k, 0.0) + _rules.row_hours(r)
        return out
    a, b = per(before_rows), per(after_rows)
    names = {key((r.get("employee") or "").strip()): (r.get("employee") or "").strip() for r in after_rows or []}
    out = []
    for (low, bucket), hours in b.items():
        was = a.get((low, bucket), 0.0)
        if hours <= was + 0.01:
            continue
        base = float((c.base_hours.get(low) or {}).get(bucket, 0.0) or 0.0)
        line = _rules.overtime_line(c, names.get(low, low))
        if line and headroom and low in headroom and not c.is_salaried(names.get(low, low)):
            line = max(0.0, line - headroom[low])
        if line and base + hours > line + 0.05:
            out.append((names.get(low, low), bucket, round(base + hours, 2)))
    return out


def optimize(rows: list, inputs: dict = None, signals: dict = None, weights: dict = None,
             constraints=None, target: int = DEFAULT_TARGET, max_seconds: float = DEFAULT_SECONDS,
             max_evaluations: int = DEFAULT_EVALUATIONS, hours_budget: float = None,
             max_server_overlap: int = None, only_dates=None, what_if: bool = True) -> dict:
    """Improve the week's Shift Quality with legal moves, and say what moved.

    Returns {rows, changes: [{kind, reason, gain, score_after, dollars}],
    before_score, after_score, quality, evaluations, seconds, stopped,
    labor: {before, after} | None}. `rows` is a new list; the input is not
    modified. A week that is already at `target`, or that has nothing
    configured to score, comes back unchanged with the reason. `what_if`
    False keeps to the weak dimensions' own moves.
    """
    t0 = _time.monotonic()
    inputs = inputs or {}
    signals = dict(signals or {})
    profiles = inputs.get("shift_profiles") or None
    constraints = constraints if constraints is not None else inputs.get("constraints")
    c = constraints
    import schedule_rules as _rules
    sweep = _rules.IncrementalSweep(c) if c is not None else None
    pricing = pricing_inputs(signals, inputs, c)
    likely = [f for f in (signals.get("likely_edits") or []) if isinstance(f, dict)]
    families = signals.get("role_families") or (getattr(c, "role_families", None) if c is not None else None)
    # The scheduling memory's overtime headroom and late closes (L-16).
    headroom = ot_headroom(signals, c)
    overruns = _overrun_signals(signals)

    def sweep_of(rs):
        """(violations, breach profile, the scorer's signals) for `rs`: the
        rows that will not stand and the hard breaches are THESE rows'
        (P-28), never the set read before the search began."""
        if sweep is None:
            return None, None, signals
        try:
            viols = sweep.violations(rs)
        except Exception:
            return None, None, None
        prof = _rules.breach_profile(rs, c, viols=viols)
        sig = dict(signals)
        sig["flagged"] = {(_low(v.get("employee")), v.get("date") or "", v.get("shift_start") or "")
                          for v in viols if v.get("no_show")}
        sig["hard_breaches"] = [v for v in viols if v.get("hard")]
        return viols, prof, sig

    def score(rs, sig):
        return sq.score_rows(rs, profiles=profiles, weights=weights, **sig)

    current_rows = [dict(r) for r in rows or []]
    base_dollars = labor_dollars(current_rows, pricing) if pricing else 0.0

    def value(q, rs):
        """The week's worth: its score (the scheduling memory's measure in
        it, L-3) less the labor dollars it adds over the draft (P-32) and the
        rows it keeps that the manager is expected to change (L-15)."""
        return week_value(q, rs, pricing, base_dollars, likely)

    viols, prof, sig = sweep_of(current_rows)
    out = {"rows": current_rows, "changes": [], "before_score": None, "after_score": None,
           "quality": {}, "evaluations": 0, "seconds": 0.0, "stopped": "",
           "labor": ({"before": round(base_dollars, 0), "after": round(base_dollars, 0)} if pricing else None)}
    if c is not None and sig is None:
        out["stopped"] = "the rule check could not run, so nothing was changed"
        out["quality"] = score(current_rows, signals)
        out["before_score"] = out["after_score"] = out["quality"].get("score")
        return out
    current = score(current_rows, sig)
    before = current.get("score")
    out.update(before_score=before, after_score=before, quality=current, evaluations=1)
    if not current.get("checked"):
        out["stopped"] = "nothing to score"
        return out
    # A partial redo: the days the owner kept are not the search's to touch.
    only = set(only_dates) if only_dates else None

    def _kept(rs):
        return sorted(tuple(sorted((k, str(v)) for k, v in r.items())) for r in rs
                      if only is not None and r.get("date") not in only)
    kept_before = _kept(current_rows) if only is not None else None
    pinned_before = _pinned_rows(current_rows)
    budget = float(hours_budget if hours_budget is not None else (inputs.get("hours_budget") or 0) or 0)
    ceiling = budget * BUDGET_TOLERANCE if budget > 0 else None
    salaried = signals.get("salaried")
    if salaried is None and c is not None:
        salaried = getattr(c, "salaried", None)

    def _week_hours(rs):
        # The budget is HOURLY hours: a salaried person's are not spent from
        # it (schedule audit 10/3/26 P-6) — counting them refused every add
        # once the managers were on the floor every minute.
        return _rules.hourly_hours(rs, c) if c is not None else _rules.hourly_hours(rs, salaried=salaried or ())

    # One server per section (restaurants.section_count): the backstop that
    # enforces it runs before this search, so no move may undo it. The roles
    # it counts are front of house by family, as the sweep counts them
    # ("Server AM" and "Server PM" are servers, D-13).
    try:
        cap = int(max_server_overlap if max_server_overlap is not None else (inputs.get("section_count") or 0))
    except (TypeError, ValueError):
        cap = 0
    cap_roles = {sq.role_family(x, families) for x in ((getattr(c, "foh_roles", None) or ()) if c is not None else ())
                 if str(x).strip()} or {"server"}

    def _server_peaks(rs):
        from schedule_engine import _peak_server_overlap
        by = {}
        for r in rs:
            if sq.role_family(r.get("role"), families) in cap_roles:
                by.setdefault(r.get("date"), []).append(r)
        return {d: _peak_server_overlap(v)[0] for d, v in by.items()}
    peaks_before = _server_peaks(current_rows) if cap > 0 else {}

    def _servers_ok(rs):
        """No date's peak goes past the section cap — or, where the draft is
        already past it, any higher than it already is."""
        if cap <= 0:
            return True
        return all(p <= max(cap, peaks_before.get(d, 0)) for d, p in _server_peaks(rs).items())
    worse_days = learned_worse_days(inputs)
    soft_before = _soft_repaired(viols)
    overrun_before = _overrun_weight(current_rows, overruns, families)

    def legal(trial_rows):
        """(sweep, profile, signals) of a trial that may stand, else None:
        nothing about a person, the manager every minute, the floors and the
        closer, overtime or minimum hours new or worse than the week as it
        stands after the moves already taken (P-14, E-1); no repaired soft
        breach back; nobody newly past their overtime line (for somebody
        who habitually runs past their shift, the line less that headroom —
        L-16); no close the memory says runs late ended earlier than it
        really ends; nobody code may not choose newly on a date (P-2)."""
        if sweep is None:
            return None, None, signals
        t_viols, t_prof, t_sig = sweep_of(trial_rows)
        if t_sig is None:
            return None
        if _rules.regressions(prof, t_prof, upto=_rules.TIER_BUDGET, hard_only=False):
            return None
        if _soft_repaired(t_viols) - soft_before:
            return None
        if overtime_created(current_rows, trial_rows, c, headroom=headroom):
            return None
        if overruns and _overrun_weight(trial_rows, overruns, families) > overrun_before + 1e-9:
            return None
        on_before = {(_low(r.get("employee")), r.get("date")) for r in current_rows}
        for r in trial_rows:
            k = (_low(r.get("employee")), r.get("date"))
            if k[0] and k not in on_before and not c.fillable(r.get("employee"), r.get("date") or "")[0]:
                return None
        return t_viols, t_prof, t_sig

    cur_val = value(current, current_rows)
    evaluations, tabu = 1, set()
    stopped = "no improving move"
    while True:
        if (current.get("score") or 0) >= target and not memory_misses(current):
            stopped = "target reached"
            break
        if _time.monotonic() - t0 > max_seconds or evaluations >= max_evaluations:
            stopped = "budget spent"
            break
        state = _State(current_rows, signals, inputs, constraints=c)
        accepted = False
        for phase in ("weak", "what_if"):
            if phase == "what_if" and not what_if:
                break
            candidates, seen = [], set()
            if phase == "weak":
                problems = _learned_problems(current) + _problems(current)[:8]
                for problem in problems:
                    for mv in _moves_for(problem, state):
                        if mv[0] in seen or mv[0] in tabu:
                            continue
                        seen.add(mv[0])
                        candidates.append(mv)
                    if len(candidates) >= MOVES_PER_ROUND:
                        break
                candidates = candidates[:MOVES_PER_ROUND]
            else:
                candidates = [mv for mv in _what_if_moves(state, current) if mv[0] not in tabu]
            if not candidates:
                continue
            ranked = []
            for sig_m, desc, apply in candidates:
                if _time.monotonic() - t0 > max_seconds or evaluations >= max_evaluations:
                    break
                try:
                    trial_rows = apply(current_rows)
                except Exception:
                    tabu.add(sig_m)
                    continue
                if ceiling is not None and _week_hours(trial_rows) > max(ceiling, _week_hours(current_rows)) + 0.01:
                    tabu.add(sig_m)
                    continue
                if not _servers_ok(trial_rows) or _pinned_rows(trial_rows) != pinned_before:
                    tabu.add(sig_m)
                    continue
                if only is not None and _kept(trial_rows) != kept_before:
                    tabu.add(sig_m)
                    continue
                judged = legal(trial_rows)
                if judged is None:
                    tabu.add(sig_m)
                    continue
                t_viols, t_prof, t_sig = judged
                try:
                    trial = score(trial_rows, t_sig)
                except Exception:
                    tabu.add(sig_m)
                    continue
                evaluations += 1
                if not trial.get("checked"):
                    continue
                gain = value(trial, trial_rows) - cur_val
                pen, why_pen = learned_penalty(worse_days, current_rows, trial_rows)
                if pen:
                    gain -= pen
                    desc = f"{desc} Kept despite this: {why_pen}."
                ranked.append((gain, sig_m, desc, trial_rows, trial, t_viols, t_prof))
                if gain >= TAKE_AT_ONCE:
                    break
            ranked.sort(key=lambda t: -t[0])
            if ranked and ranked[0][0] >= MIN_GAIN:
                gain, sig_m, desc, trial_rows, trial, t_viols, t_prof = ranked[0]
                dollars = None
                if pricing:
                    dollars = round(labor_dollars(trial_rows, pricing) - labor_dollars(current_rows, pricing), 0)
                out["changes"].append({"kind": sig_m[0], "reason": desc, "gain": round(gain, 1),
                                       "score_after": trial.get("score"), "dollars": dollars})
                current_rows, current = trial_rows, trial
                # The baseline every later move is held to is the week as it
                # now stands (P-14): a breach this move fixed may not come back.
                viols, prof = t_viols, t_prof
                soft_before = _soft_repaired(viols) if viols is not None else soft_before
                overrun_before = _overrun_weight(current_rows, overruns, families)
                cur_val = value(current, current_rows)
                tabu.add(sig_m)
                accepted = True
                break
        if not accepted:
            break
    out.update(rows=current_rows, after_score=current.get("score"), quality=current,
               evaluations=evaluations, seconds=round(_time.monotonic() - t0, 1), stopped=stopped)
    if pricing:
        out["labor"] = {"before": round(base_dollars, 0), "after": round(labor_dollars(current_rows, pricing), 0)}
    return out


def _soft_repaired(viols) -> set:
    """(person, kind) of the soft breaches a repair pass acts on
    (schedule_rules.FIXABLE_SOFT): no move may bring one back."""
    import schedule_rules as _rules
    return {(_low(v.get("employee")), v["kind"]) for v in (viols or [])
            if not v.get("hard") and v["kind"] in _rules.FIXABLE_SOFT}


def unresolved(result: dict, signals: dict = None, limit: int = 4) -> list:
    """What is still wrong after the search, each with why no legal change
    fixed it — the owner's to decide, not the draft's to hide."""
    signals = signals or {}
    flags = signals.get("leader_flags") or {}
    families = signals.get("role_families") or None
    roster_rows = result.get("rows") or []
    out, seen = [], set()
    for cost, s, d in _problems(result.get("quality") or {}):
        if cost < 5 or len(out) >= limit:
            continue
        where = f"{s['day']} {'lunch' if s['daypart'] == 'morning' else 'dinner'}"
        text = (d.get("weaknesses") or [""])[0]
        if d["key"] == "leadership":
            for miss in (d.get("facts") or {}).get("misses") or []:
                if miss.get("attribute"):
                    role = (miss.get("role") or "").strip()
                    fam = sq.role_family(role, families)
                    roster_roles = signals.get("roster_roles") or {}
                    cross = signals.get("cross_trained") or {}
                    in_role = {n for n, rl in roster_roles.items() if sq.role_family(rl, families) == fam}
                    in_role |= {n for n, rls in cross.items()
                                if fam in {sq.role_family(x, families) for x in rls or []}}
                    in_role |= {(r.get("employee") or "").strip() for r in roster_rows
                                if sq.role_family(r.get("role"), families) == fam}
                    able = sorted(n for n in in_role if flags.get(n))
                    if len(able) <= 1:
                        # Only what was checked: who in the role is authorized
                        # (the whole roster, not only this week's rows), and
                        # that no legal change put them here.
                        text = (f"{where} has no {sq.role_words(role)} authorized to close. "
                                + (f"Only {able[0]} is, and no legal change could put them on this shift "
                                   f"(they may be off that day, at their hours limit, or needed on another close). "
                                   if able else "Nobody in that role is. ")
                                + f"Authorizing another {sq.role_words(role)} to close fixes this.")
        key = (s["date"], s["daypart"], d["key"])
        if key in seen or not text:
            continue
        seen.add(key)
        out.append({"date": s["date"], "day": s["day"], "daypart": s["daypart"], "dimension": d["key"],
                    "text": text, "fixable_by_draft": False})
    return out


def summary(result: dict, signals: dict = None) -> dict:
    """What the owner is shown: before, after, and each change with why."""
    changes = result.get("changes") or []
    before, after = result.get("before_score"), result.get("after_score")
    if not changes:
        verdict = ("The draft already met the quality target." if result.get("stopped") == "target reached"
                   else "No legal change improved the draft.")
    elif before is not None and after is not None and after < before:
        # Only a change where the managers usually change the draft (the edit
        # predictor, L-15) is ever worth points of the score.
        verdict = (f"Cavnar AI made {len(changes)} change{'s' if len(changes) != 1 else ''} to the draft where "
                   f"your managers usually change it; Shift Quality moved from {before} to {after}. "
                   "Each is listed with why.")
    else:
        verdict = (f"Cavnar AI made {len(changes)} change{'s' if len(changes) != 1 else ''} to the draft, "
                   f"raising Shift Quality from {before} to {after}. Each is listed with why.")
    labor = result.get("labor") or None
    return {"ran": True, "applied": bool(changes), "before_score": before, "after_score": after,
            "improvement": (after or 0) - (before or 0) if before is not None and after is not None else 0,
            "changes": [{"kind": c["kind"], "reason": c["reason"], "gain": c["gain"],
                         "dollars": c.get("dollars")} for c in changes],
            "evaluations": result.get("evaluations"), "seconds": result.get("seconds"),
            "stopped": result.get("stopped"), "verdict": verdict,
            # What the changes moved in labor dollars, overtime at its premium
            # (schedule audit 10/3/26 P-32); None with no rate on file.
            "dollars_before": (labor or {}).get("before"), "dollars_after": (labor or {}).get("after"),
            "unresolved": unresolved(result, signals)}
