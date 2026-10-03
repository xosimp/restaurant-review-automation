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
  trim      a shift is shortened, or removed, where a day is over its target

Candidates come from the weak dimensions' own facts, costliest first
(demand-weighted, capped shifts before everything), from what the
restaurant's scheduling memory says a move must not undo (signals
["learned"]), and — once those have nothing left — from the what-if's swaps
and replacements, so a better arrangement the comparison would report is
taken here instead (schedule audit 10/3/26 P-33, SQ-19).

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
              does); never a row the manager plan or the owner pinned; the
              hourly hours budget a ceiling (salaried hours are not spent
              from it, P-6); the section count.
  scored      by the same shift_quality.score_rows the owner's number comes
              from, with the rows that will not stand and the hard breaches
              of THAT trial (P-28), less what the move costs in labor
              dollars (schedule_economics.priced_cost: each person's own
              rate, the role's, overtime at its premium, salaried people
              free — P-32) and what it breaks of the scheduling memory (L-3)
              — so a $25/h add no longer costs the same as a $14/h one.

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
# What breaking an active memory of the restaurant's scheduling costs a move
# (schedule audit 10/3/26 L-3, D-35): week points per memory at full
# confidence; a memory the owner made a rule ("hard") many times that. A
# move that puts Bob back on the Tuesday dinner the manager keeps taking him
# off has to buy more than this — a small score gain never does.
LEARNED_POINTS = 1.0
LEARNED_HARD_FACTOR = 6.0
# A row the edit predictor says the manager will change (schedule_learning.
# likely_edit_signals, when the engine passes signals["likely_edits"]):
# week points per unit of its weight for keeping it as it is.
LIKELY_EDIT_POINTS = 0.5
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

_OFF_VALUES = frozenset({"off", "avoid", "never", "not", "no", "remove", "removed", "drop", "false", "0", "apart",
                         "split"})
_ON_VALUES = frozenset({"on", "always", "prefer", "keep", "yes", "true", "1", "add", "together", "with"})
_PARTS = {"morning": "morning", "lunch": "morning", "day": "morning", "am": "morning", "brunch": "morning",
          "night": "night", "dinner": "night", "evening": "night", "pm": "night"}


def _weekday(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw[:2] == "20" and len(raw) >= 10:
        return sq._day_name(raw[:10])
    low = raw.lower()
    for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        if low == d.lower() or low == d.lower()[:3]:
            return d
    return ""


def _pair_names(m: dict) -> list:
    """The two people a pairing memory is about, wherever its shape put them:
    a list under people/persons, "a+b" in person, a value dict, or the key
    ("pair|ana+bo|prefer")."""
    for k in ("people", "persons", "pair"):
        v = m.get(k)
        if isinstance(v, (list, tuple, set, frozenset)) and len(v) >= 2:
            return sorted(_low(x) for x in v)[:2]
    val = m.get("value")
    if isinstance(val, dict):
        names = [val.get(k) for k in ("a", "b", "with", "other") if val.get(k)]
        if m.get("person") and len(names) == 1:
            names = [m["person"]] + names
        if len(names) >= 2:
            return sorted(_low(x) for x in names)[:2]
    for text in (m.get("person"), m.get("key")):
        for part in str(text or "").split("|"):
            if "+" in part:
                bits = [_low(x) for x in part.split("+") if x.strip()]
                if len(bits) == 2:
                    return sorted(bits)
    return []


def learned_items(signals: dict) -> list:
    """signals["learned"] (schedule_memory.enforced_signals: [{kind, key,
    person, day, daypart, role, value, confidence, enforcement, source}]) as
    the person-level facts the solver and this search hold: somebody off a
    slot or on it, the role's opener or closer on a slot, two people kept
    apart or together, and somebody who habitually runs past their shift.
    A memory enforced in the prompt only stays there; "soft" (or "soft_cost")
    is weighed at its confidence, "hard"/"rule" at LEARNED_HARD_FACTOR times
    that. A memory about the week's shape (a headcount, a start time) is not
    a person's and is left to the passes that hold those. Each item:
    {kind: off|on|opener|closer|avoid|prefer|overrun, person, other, day,
    daypart, role, hours, weight, hard, text}."""
    out = []
    for m in (signals or {}).get("learned") or []:
        if not isinstance(m, dict):
            continue
        enforcement = str(m.get("enforcement") or "").strip().lower()
        if enforcement not in ("soft", "soft_cost", "hard", "rule"):
            continue
        try:
            conf = float(m.get("confidence") if m.get("confidence") is not None else 1.0)
        except (TypeError, ValueError):
            conf = 1.0
        conf = max(0.0, min(1.0, conf))
        if conf <= 0:
            continue
        hard = enforcement in ("hard", "rule")
        weight = conf * (LEARNED_HARD_FACTOR if hard else 1.0)
        kind = str(m.get("kind") or "").strip().lower()
        value = m.get("value")
        v = str(value).strip().lower() if not isinstance(value, (dict, list, tuple)) else ""
        person, day = _low(m.get("person")), _weekday(m.get("day") or m.get("date"))
        part = _PARTS.get(str(m.get("daypart") or "").strip().lower(), "")
        role = str(m.get("role") or "").strip()
        text = str(m.get("source") or m.get("key") or kind)[:160]
        base = {"person": person, "other": "", "day": day, "daypart": part, "role": role, "hours": 0.0,
                "weight": weight, "hard": hard, "text": text}
        slotish = kind in ("slot", "person_slot", "person_day", "day_slot")
        if kind in ("moved_off", "slot_off", "person_off", "off", "never_on") or (slotish and v in _OFF_VALUES):
            if person and day:
                out.append(dict(base, kind="off"))
        elif kind in ("moved_on", "slot_on", "person_on", "on", "always_on") or (slotish and v in _ON_VALUES):
            if person and day:
                out.append(dict(base, kind="on"))
        elif kind in ("opener", "opens", "closer", "closes"):
            if person and day:
                out.append(dict(base, kind="opener" if kind.startswith("open") else "closer"))
        elif kind in ("pair", "pairing", "pair_prefer", "pair_avoid", "team", "pair_split"):
            names = _pair_names(m)
            if len(names) == 2 and names[0] != names[1]:
                avoid = kind in ("pair_avoid", "pair_split") or v in _OFF_VALUES
                out.append(dict(base, kind="avoid" if avoid else "prefer", person=names[0], other=names[1]))
        elif kind in ("ot_actual", "overtime_habit", "runs_over", "stayed_late", "overrun"):
            try:
                hours = float(value.get("hours") if isinstance(value, dict) else value)
            except (TypeError, ValueError, AttributeError):
                hours = 0.0
            if person and hours > 0:
                out.append(dict(base, kind="overrun", hours=max(0.0, min(8.0, hours))))
    return out


def week_value(quality: dict, rows: list, pricing=None, base_dollars: float = 0.0, items=None, likely=None,
               families: dict = None) -> float:
    """What a week is worth to the owner, in week points: its Shift Quality
    (objective), less LABOR_POINTS_PER_PCT for each 1% of labor dollars it
    costs over `base_dollars` (the draft's — a saving earns nothing, P-32),
    less what it breaks of the scheduling memory and keeps of the rows the
    manager is expected to change (learned_cost, L-3). The one value the
    repair loop and the solver's judge both choose by."""
    v = objective(quality)
    if pricing and base_dollars > 0:
        over = labor_dollars(rows, pricing) - base_dollars
        if over > 0:
            v -= LABOR_POINTS_PER_PCT * over / base_dollars * 100.0
    if items or likely:
        v -= learned_cost(rows, items or [], likely, families)
    return v


def _slots(rows: list) -> dict:
    """{(weekday, daypart): {date: [rows on the floor for it]}}."""
    out = {}
    for r in rows or []:
        if not (r.get("employee") or "").strip() or not r.get("date"):
            continue
        day = sq._day_name(r["date"], r.get("day", ""))
        for part in sq.present_dayparts(r):
            out.setdefault((day, part), {}).setdefault(r["date"], []).append(r)
    return out


def learned_breaks(rows: list, items: list, families: dict = None) -> list:
    """[(item, date, text)] — each learned fact `rows` break, once per
    shift it breaks it on."""
    if not items:
        return []
    slots = _slots(rows)
    out = []
    for it in items:
        kind, who = it["kind"], it["person"]
        if kind == "overrun":
            continue
        if kind in ("avoid", "prefer"):
            for (day, part), by_date in slots.items():
                for d, rs in by_date.items():
                    on = {_low(r.get("employee")) for r in rs}
                    a, b = who in on, it["other"] in on
                    if kind == "avoid" and a and b:
                        out.append((it, d, f"{it['person'].title()} and {it['other'].title()} are on together"))
                    elif kind == "prefer" and a != b:
                        out.append((it, d, f"{it['person'].title()} and {it['other'].title()} are split"))
            continue
        parts = [it["daypart"]] if it["daypart"] else ["morning", "night"]
        for part in parts:
            for d, rs in (slots.get((it["day"], part)) or {}).items():
                mine = [r for r in rs if _low(r.get("employee")) == who]
                if kind == "off":
                    if mine:
                        out.append((it, d, f"{mine[0].get('employee')} is on {it['day']} "
                                           f"{'lunch' if part == 'morning' else 'dinner'}"))
                    continue
                if kind == "on":
                    if not mine:
                        out.append((it, d, f"{who.title()} is not on {it['day']} "
                                           f"{'lunch' if part == 'morning' else 'dinner'}"))
                    continue
                fam = sq.role_family(it["role"], families) if it["role"] else None
                pool = [r for r in rs if fam is None or sq.role_family(r.get("role"), families) == fam]
                spans = [(sq._row_span(r), r) for r in pool]
                spans = [(sp, r) for sp, r in spans if sp]
                if not spans:
                    continue
                if kind == "opener":
                    edge = min(sp[0] for sp, _r in spans)
                    at = [r for sp, r in spans if sp[0] == edge]
                else:
                    edge = max(sp[1] for sp, _r in spans)
                    at = [r for sp, r in spans if sp[1] == edge]
                if not any(_low(r.get("employee")) == who for r in at):
                    out.append((it, d, f"{who.title()} is not the {kind} on {it['day']}"))
    return out


def learned_cost(rows: list, items: list, likely: list = None, families: dict = None) -> float:
    """Week points `rows` give up against the scheduling memory: LEARNED_POINTS
    per broken fact at its weight, plus LIKELY_EDIT_POINTS per unit of
    weight of each row kept that the edit predictor expects the manager to
    change."""
    pts = sum(LEARNED_POINTS * it["weight"] for it, _d, _t in learned_breaks(rows, items, families))
    if likely:
        here = {(_low(r.get("employee")), r.get("date") or "", r.get("shift_start") or "") for r in rows or []}
        for f in likely:
            try:
                w = float(f.get("weight") or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0 and (_low(f.get("employee")), f.get("date") or "", f.get("shift_start") or "") in here:
                pts += LIKELY_EDIT_POINTS * w
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
        if d.get("score", 100) >= sq.SCORE_MAX:
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


def _learned_problems(rows: list, items: list, quality: dict, families: dict = None) -> list:
    """(cost, shift, pseudo-dimension) for each shift a learned fact is
    broken on, so the moves that put it right are tried first (L-3)."""
    breaks = learned_breaks(rows, items, families)
    if not breaks:
        return []
    shifts = {}
    for s in (quality or {}).get("shifts") or []:
        shifts.setdefault(s.get("date"), []).append(s)
    out = []
    for it, d, text in breaks:
        for s in shifts.get(d) or []:
            if it["daypart"] and s.get("daypart") != it["daypart"]:
                continue
            out.append((150.0 * it["weight"], s, {"key": "learned", "score": 0, "weight": 0,
                                                    "facts": {"item": it, "text": text}}))
            break
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

    def family(self, role) -> str:
        return sq.role_family(role, self.families)

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
        pool = sorted(people, key=lambda n: (-(prefer(n) if prefer else 0), state.index.total_hours(n.lower()), n))
        for name in pool:
            row = _new_row(date, name, role, start, end, why)
            if state.can_add(name, row):
                moves.append((("add", date, name, role, start),
                              f"Added {name} as {role} on {where} ({start}–{end}) — {why}.",
                              lambda rows, row=row: rows + [dict(row)]))
                if len([m for m in moves if m[0][0] == "add"]) >= 3:
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
        order = rank or (lambda n: (state.index.total_hours(n.lower()), n))
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
        it = facts.get("item") or {}
        who = it.get("person") or ""
        name = next((n for n in (state.signals.get("roster") or []) if _low(n) == who), who.title())
        why = f"what the manager keeps doing: {it.get('text') or 'a standing pattern'}"
        if it.get("kind") == "off":
            off_shift(name, why, want=lambda n, w=who: _low(n) != w)
        elif it.get("kind") in ("on", "opener", "closer"):
            fam = state.family(it.get("role")) if it.get("role") else None
            for i in [i for i in range(len(state.rows)) if state.rows[i].get("date") == date
                      and part in sq.present_dayparts(state.rows[i])
                      and (fam is None or state.family(state.rows[i].get("role")) == fam)
                      and _low(state.rows[i].get("employee")) != who][:3]:
                replace_in(i, lambda n, w=who: _low(n) == w, why, limit=1)
                _swap_moves(state, i, moves, why, want=lambda n, w=who: _low(n) == w)
        elif it.get("kind") == "avoid":
            for nm in (it.get("person"), it.get("other")):
                off_shift(next((n for n in (state.signals.get("roster") or []) if _low(n) == nm), nm.title()), why,
                          want=lambda n, a=it.get("person"), b=it.get("other"): _low(n) not in (a, b))
        elif it.get("kind") == "prefer":
            on_here = {_low(n) for n in (shift.get("people") or [])}
            other = it.get("other") if it.get("person") in on_here else it.get("person")
            nm = next((n for n in (state.signals.get("roster") or []) if _low(n) == other), None)
            if nm:
                for i in [i for i in range(len(state.rows)) if state.rows[i].get("date") == date
                          and part in sq.present_dayparts(state.rows[i])][:4]:
                    replace_in(i, lambda n, x=nm: n == x, why, limit=1)

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


def overtime_created(before_rows, after_rows, c) -> list:
    """[(name, payroll week, hours)] for everybody `after_rows` puts past
    their overtime line (schedule_rules.overtime_line — a salaried person's
    weekly cap) by more than `before_rows` did: the owner's rule is that a
    draft never runs overtime while a same-role teammate has room, and no
    quality move is worth an hour of it. [] without Constraints."""
    if c is None:
        return []
    import schedule_rules as _rules

    def per(rs):
        out = {}
        for r in rs or []:
            n = (r.get("employee") or "").strip()
            if not n or not r.get("date"):
                continue
            k = (n.lower(), c.bucket(r["date"]))
            out[k] = out.get(k, 0.0) + _rules.row_hours(r)
        return out
    a, b = per(before_rows), per(after_rows)
    names = {(r.get("employee") or "").strip().lower(): (r.get("employee") or "").strip() for r in after_rows or []}
    out = []
    for (low, bucket), hours in b.items():
        was = a.get((low, bucket), 0.0)
        if hours <= was + 0.01:
            continue
        base = float((c.base_hours.get(low) or {}).get(bucket, 0.0) or 0.0)
        line = _rules.overtime_line(c, names.get(low, low))
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
    items = learned_items(signals)
    likely = [f for f in (signals.get("likely_edits") or []) if isinstance(f, dict)]
    families = signals.get("role_families") or (getattr(c, "role_families", None) if c is not None else None)

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
        """The week's worth: its score less the labor dollars it adds over
        the draft (P-32) and what it breaks of the scheduling memory (L-3)."""
        return week_value(q, rs, pricing, base_dollars, items, likely, families)

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

    def legal(trial_rows):
        """(sweep, profile, signals) of a trial that may stand, else None:
        nothing about a person, the manager every minute, the floors and the
        closer, overtime or minimum hours new or worse than the week as it
        stands after the moves already taken (P-14, E-1); no repaired soft
        breach back; nobody newly past their overtime line; nobody code may
        not choose newly on a date (P-2)."""
        if sweep is None:
            return None, None, signals
        t_viols, t_prof, t_sig = sweep_of(trial_rows)
        if t_sig is None:
            return None
        if _rules.regressions(prof, t_prof, upto=_rules.TIER_BUDGET, hard_only=False):
            return None
        if _soft_repaired(t_viols) - soft_before:
            return None
        if overtime_created(current_rows, trial_rows, c):
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
        if (current.get("score") or 0) >= target and not (items and learned_breaks(current_rows, items, families)):
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
                problems = _learned_problems(current_rows, items, current, families) + _problems(current)[:8]
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
        # Only a change that keeps to what the managers keep doing (the
        # scheduling memory) is ever worth points of the score.
        verdict = (f"Cavnar AI made {len(changes)} change{'s' if len(changes) != 1 else ''} to the draft to keep to "
                   f"what your managers keep doing; Shift Quality moved from {before} to {after}. "
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
