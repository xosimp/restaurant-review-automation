"""The Shift Quality Engine — what is the strongest team we can build for this shift?

The scheduler used to answer a narrower question. Availability said who
could work, the labor target said how much it could cost, and typical
headcount said how many bodies to put on the floor. Three good answers to
three small questions, and nothing that judged the result as a whole.

Erik's Saturday made the gap concrete: two bartenders were available, the
labor maths worked, coverage was satisfied, and the schedule put his two
weakest people on his busiest night. Every individual rule passed. The
shift was still wrong.

This module scores a finished shift the way an experienced operator reads
one. It is deliberately a PURE evaluation layer — no database, no network,
no model call — because the same evaluation has to serve four callers that
have nothing else in common:

  * generation, scoring the schedule the model just wrote
  * a manager mid-edit, who needs the number to move as they drag a shift
  * what-if comparison, scoring dozens of candidate variants in a loop
  * anything later that wants to read a past schedule back

Three shapes carry all of it. ShiftContext holds every signal a dimension
may consult. A dimension is a function from context to DimensionResult.
DIMENSIONS is the registry. Adding reservations, punctuality or guest
satisfaction later is one field and one function; aggregation, explanation
and both surfaces are untouched.

Two rules run through the whole file and are easy to break by accident:

  A dimension with nothing to judge returns None, never a zero. Weights
  renormalise over the dimensions that actually applied, so a restaurant
  with no tenure history does not quietly score worse than one with it.
  This is the same discipline as an unrated employee contributing nothing
  to shift strength rather than being imputed a middle 3.

  A critical dimension under its floor CAPS the total instead of being
  averaged away. Coverage at 40% is not a B+ that eight 95s can outvote,
  and no operator reads it as one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

# The scale every dimension reports on. Percentages, because "Coverage 100%"
# is a sentence an owner can act on and "Coverage 1.0" is not.
SCORE_MAX = 100

# Demand levels, weakest to strongest. Profiles name one of these; the
# engine also derives one from the restaurant's own sales history when a
# profile does not pin it.
DEMAND_LEVELS = ("low", "normal", "high", "peak")
DEMAND_RANK = {level: i for i, level in enumerate(DEMAND_LEVELS)}

# How much a shift at each demand level counts toward the week's overall
# quality. A weak Saturday dinner is a worse week than a weak Monday lunch,
# and averaging them flat says otherwise.
DEMAND_WEIGHT = {"low": 0.6, "normal": 1.0, "high": 1.5, "peak": 2.0}


@dataclass
class ShiftProfile:
    """What this particular shift is supposed to be.

    Not every shift is judged the same way, which is the whole point of
    profiles: Monday lunch and Saturday dinner are different jobs and a
    single global threshold flattens them into one.

    Every field has a defensible default so a restaurant that never opens
    the profile editor still gets a real evaluation.
    """
    key: str = "default"
    label: str = "Standard shift"
    days: list = field(default_factory=list)          # empty = every day
    daypart: str | None = None                        # None = both
    demand: str = "normal"
    min_quality: int = 70
    # {role: combined Operational Score required on this shift}
    min_strength: dict = field(default_factory=dict)
    # {role: how many people must be on}
    critical_positions: dict = field(default_factory=dict)
    # Somebody on this shift has to be able to run it.
    requires_leader: bool = False
    leader_roles: list = field(default_factory=list)
    leader_min_score: float = 4.0
    # What share of the shift should be experienced hands, 0-1.
    experience_mix: float = 0.4
    # Whether a deliberately weaker, mentored team is acceptable here.
    training_allowed: bool = False
    # Per-profile weight overrides, merged over DEFAULT_WEIGHTS.
    weights: dict = field(default_factory=dict)
    # Higher wins when two profiles both match a shift.
    priority: int = 0
    source: str = "default"

    def matches(self, day: str, daypart: str) -> bool:
        if self.days and (day or "").strip().lower() not in {
                d.strip().lower() for d in self.days if d}:
            return False
        if self.daypart and (self.daypart or "").strip().lower() != (daypart or "").lower():
            return False
        return True

    def specificity(self) -> int:
        """How narrowly this profile is aimed, for tie-breaking.

        A profile naming Saturday night beats one naming every night, which
        beats the catch-all — without the admin having to hand-number them.
        """
        return (2 if self.days else 0) + (1 if self.daypart else 0)


# Built-in profiles. Deliberately generic — a real restaurant's demand
# levels get overwritten from its OWN sales history in derive_profiles(),
# because guessing that every restaurant's Friday is busy is exactly the
# kind of invented fact this codebase keeps having to remove.
BUILTIN_PROFILES = [
    ShiftProfile(key="default", label="Standard shift", demand="normal", min_quality=70),
    ShiftProfile(key="weekday_lunch", label="Weekday lunch", daypart="morning",
                 days=["Monday", "Tuesday", "Wednesday", "Thursday"],
                 demand="low", min_quality=65, training_allowed=True,
                 experience_mix=0.3, priority=1),
    ShiftProfile(key="weekday_dinner", label="Weekday dinner", daypart="night",
                 days=["Monday", "Tuesday", "Wednesday", "Thursday"],
                 demand="normal", min_quality=70, experience_mix=0.4, priority=1),
    ShiftProfile(key="friday_dinner", label="Friday dinner", daypart="night",
                 days=["Friday"], demand="high", min_quality=78,
                 requires_leader=True, experience_mix=0.5, priority=2),
    ShiftProfile(key="saturday_dinner", label="Saturday dinner", daypart="night",
                 days=["Saturday"], demand="peak", min_quality=82,
                 requires_leader=True, experience_mix=0.6, priority=2),
    ShiftProfile(key="brunch", label="Weekend brunch", daypart="morning",
                 days=["Saturday", "Sunday"], demand="high", min_quality=75,
                 experience_mix=0.45, priority=2),
]


def resolve_profile(day: str, daypart: str, profiles: list = None) -> ShiftProfile:
    """The profile governing one shift. Most specific active match wins."""
    candidates = [p for p in (profiles if profiles is not None else BUILTIN_PROFILES)
                  if p.matches(day, daypart)]
    if not candidates:
        return ShiftProfile()
    return sorted(candidates, key=lambda p: (p.priority, p.specificity()))[-1]


@dataclass
class ShiftContext:
    """Everything a dimension is allowed to consult about one shift.

    New signals arrive here. A dimension that wants reservations or
    punctuality gets a field on this dataclass and reads it; nothing else
    in the engine changes. Fields default to empty rather than required so
    that adding one never breaks an existing caller.
    """
    date: str = ""
    day: str = ""
    daypart: str = ""
    rows: list = field(default_factory=list)
    profile: ShiftProfile = field(default_factory=ShiftProfile)
    scores: dict = field(default_factory=dict)         # {name: 1-5}
    tenure: dict = field(default_factory=dict)         # {name: shifts worked}
    leader_flags: dict = field(default_factory=dict)   # {name: authorised to close}
    leader_rules: list = field(default_factory=list)
    cross_trained: dict = field(default_factory=dict)  # {name: [roles]}
    typical_headcount: dict = field(default_factory=dict)   # {role: people}
    role_minimums: dict = field(default_factory=dict)       # {role: floor}
    demand_pct: float | None = None                    # vs an average day
    target_hours: float | None = None                  # this DAY's hour target
    day_hours: float = 0.0                             # hours scheduled this day
    week_assignments: dict = field(default_factory=dict)   # {name: [assignment]}
    prior_pattern: dict = field(default_factory=dict)  # {name: {"days": [...], "dayparts": [...]}}
    availability: dict = field(default_factory=dict)   # {name: set(unavailable days)}
    # Free-text staff constraints, which the generator's prompt calls the
    # highest priority rule of all. The engine cannot parse them, but it can
    # refuse to move somebody who has one.
    constraints: dict = field(default_factory=dict)    # {name: note}
    # Rows the repair pass could not vouch for. A double-booked or
    # off-roster row must not be counted as coverage.
    flagged: set = field(default_factory=set)          # {(employee, date, start)}
    # True for the last shift to end on this date, so a closing requirement
    # can name the shift it actually means.
    is_closing: bool = False
    # {name: [{date, location}]} — the same person already on a schedule at
    # another site in this group on this date.
    elsewhere: dict = field(default_factory=dict)
    # Where a dimension leaves a note on its way OUT. A dimension that
    # withdraws for want of data takes its findings with it, and the owner
    # would never learn that rating somebody unlocks the check — so the
    # reason is left here instead and folded into the shift's blind spots.
    notes: list = field(default_factory=list)
    # ── the hour-by-hour view (coverage_curve) ─────────────────────────
    # Opening and closing minutes for this date, every row on this date
    # (not only this daypart's), and the per-role floor that applies to
    # this daypart. A gap between 2pm and 4pm is invisible to two blocks a
    # day; it is not invisible to a 30-minute sweep.
    open_minutes: int | None = None
    close_minutes: int | None = None
    day_rows: list = field(default_factory=list)
    role_floors: dict = field(default_factory=dict)    # {role: people required on this daypart}
    demand_curve: dict = field(default_factory=dict)   # {hour: share of the day's sales}
    # ── people facts (reliability, pairings, limits) ───────────────────
    reliability: dict = field(default_factory=dict)    # {name: {"no_show_rate", "shifts"}}
    pairs: dict = field(default_factory=dict)          # {"prefer": {frozenset}, "avoid": {frozenset}}
    max_shift_hours: float | None = None
    weekly_ceiling: float | None = None
    hours_limits: dict = field(default_factory=dict)   # {name: (min, max)}

    # ── Derived views every dimension wants ────────────────────────────
    @property
    def people(self) -> list:
        seen, out = set(), []
        for r in self.rows:
            n = (r.get("employee") or "").strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(n)
        return out

    @property
    def by_role(self) -> dict:
        """{role: [names]}, each person counted ONCE across the whole shift.

        Somebody listed as both Cook and Bartender at five o'clock is one
        person who cannot be in two places. Counting them in both buckets
        scored a physically impossible shift as fully covered — which is
        exactly what it did before this deduplicated globally rather than
        per role. They are credited to the first role they appear in and
        reported through `role_conflicts`.
        """
        out, claimed = {}, {}
        for r in self.rows:
            n = (r.get("employee") or "").strip()
            role = (r.get("role") or "").strip()
            if not (n and role) or self._is_flagged(r):
                continue
            key = n.lower()
            if key in claimed:
                continue
            claimed[key] = role
            out.setdefault(role, []).append(n)
        return out

    @property
    def role_conflicts(self) -> list:
        """People the schedule puts in more than one role on this shift."""
        seen, clashing = {}, {}
        for r in self.rows:
            n = (r.get("employee") or "").strip()
            role = (r.get("role") or "").strip()
            if not (n and role):
                continue
            key = n.lower()
            if key in seen and seen[key] != role:
                clashing.setdefault(n, {seen[key]}).add(role)
            seen.setdefault(key, role)
        return [{"name": n, "roles": sorted(rs)} for n, rs in sorted(clashing.items())]

    def _is_flagged(self, row: dict) -> bool:
        if not self.flagged:
            return False
        return ((row.get("employee") or "").strip().lower(),
                row.get("date") or "", row.get("shift_start") or "") in self.flagged

    def rated(self, names) -> list:
        return [n for n in names if self.scores.get(n) is not None]

    def unrated(self, names=None) -> list:
        names = self.people if names is None else names
        return [n for n in names if self.scores.get(n) is None]


@dataclass
class DimensionResult:
    """One dimension's verdict, with the numbers it was reached from.

    `facts` exists so the explanation layer never has to recompute anything
    the dimension already knew, and so a future UI can drill in without a
    second pass.
    """
    key: str
    label: str
    score: int
    weight: float
    strengths: list = field(default_factory=list)
    weaknesses: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    # Under this, the dimension caps the whole shift rather than averaging in.
    floor: int | None = None
    # What the dimension could not see. Feeds confidence, never the score.
    blind_spots: list = field(default_factory=list)


DEFAULT_WEIGHTS = {
    "coverage": 20,
    "operational_strength": 18,
    "leadership": 15,
    "demand_match": 10,
    "labor_efficiency": 10,
    "coverage_curve": 8,
    "experience_balance": 8,
    "training_balance": 7,
    "fatigue": 7,
    "fairness": 6,
    "reliability": 5,
    "pairings": 4,
    "stability": 2,
    "cross_training": 2,
}


def _pct(part: float, whole: float) -> int:
    """A ratio as a 0-100 score. A whole of zero means nothing was asked."""
    if whole <= 0:
        return SCORE_MAX
    return max(0, min(SCORE_MAX, int(round(part / whole * 100))))


def _names(people: list, scores: dict = None) -> str:
    if not people:
        return "nobody"
    if scores:
        return ", ".join(f"{n} ({scores[n]})" if scores.get(n) else n for n in people)
    return ", ".join(people)


def _plural(n: int, one: str, many: str = None) -> str:
    return one if n == 1 else (many or one + "s")


# ── Dimensions ─────────────────────────────────────────────────────────────
#
# Each takes a ShiftContext and returns a DimensionResult, or None when
# there is nothing to judge. None is not zero: it removes the dimension
# from the weighted mean entirely rather than dragging the shift down for
# a fact nobody has supplied yet.


def dim_coverage(ctx: ShiftContext) -> DimensionResult | None:
    """Is every required position filled?

    Requirements come from the profile first, then the restaurant's own
    role minimums, then what it typically runs on this shift. A restaurant
    that has configured nothing still gets judged against its own history
    rather than against a number this file invented.
    """
    required = dict(ctx.profile.critical_positions or {})
    source = "profile"
    if not required and ctx.role_minimums:
        # The owner's own floors, narrowed to the roles this daypart
        # actually runs. "Minimum 2 bartenders" is a statement about a
        # service day; a restaurant whose bar opens at five should not read
        # as two bartenders short every lunch, which is what applying the
        # figure to both halves of the day produced.
        runs_here = {r.strip().lower() for r in (ctx.typical_headcount or {})}
        required = {r: c for r, c in ctx.role_minimums.items()
                    if c and (not runs_here or r.strip().lower() in runs_here)}
        source = "your role minimums"
    if not required:
        required = {r: c for r, c in (ctx.typical_headcount or {}).items() if c}
        source = "your usual staffing"
    if not required:
        return None

    on = ctx.by_role
    filled = missing = 0
    gaps = []
    for role, need in required.items():
        need = int(need or 0)
        if need <= 0:
            continue
        have = 0
        for r, people in on.items():
            if r.strip().lower() == role.strip().lower():
                have = len(people)
                break
        filled += min(have, need)
        if have < need:
            missing += need - have
            gaps.append(f"{role} short {need - have} of {need}")

    total_required = sum(int(c or 0) for c in required.values() if int(c or 0) > 0)
    if total_required <= 0:
        return None

    score = _pct(filled, total_required)
    res = DimensionResult(
        key="coverage", label="Coverage", score=score,
        weight=DEFAULT_WEIGHTS["coverage"], floor=70,
        facts={"required": total_required, "filled": filled, "missing": missing,
               "gaps": gaps, "requirement_source": source},
    )
    if missing:
        res.weaknesses.append(
            f"{missing} {_plural(missing, 'position')} unfilled — " + "; ".join(gaps) + ".")
    else:
        res.strengths.append("Every required position is filled.")
    # Somebody already working another site in this group tonight is not
    # coverage here, whatever the row says.
    for name in ctx.people:
        for entry in (ctx.elsewhere.get(name) or []):
            if entry.get("date") == ctx.date:
                res.weaknesses.append(
                    f"{name} is also on the schedule at {entry['location']} on this date.")
                res.score = min(res.score, 60)
    for clash in ctx.role_conflicts:
        res.weaknesses.append(
            f"{clash['name']} is down for {' and '.join(r.lower() for r in clash['roles'])} "
            "at the same time — only one of them is counted.")
    res.facts["role_conflicts"] = ctx.role_conflicts
    return res


def dim_operational_strength(ctx: ShiftContext) -> DimensionResult | None:
    """Combined Operational Score per role, against this shift's target.

    An unrated person genuinely contributes nothing here, because inventing
    a middle 3 for them would hide the missing rating forever. That costs
    the shift real score, so it is also reported as a blind spot and drags
    confidence rather than passing silently.
    """
    targets = {r: v for r, v in (ctx.profile.min_strength or {}).items() if v}
    if not targets or not ctx.scores:
        return None

    on = ctx.by_role
    ratios, shorts, mets = [], [], []
    unrated_here = []
    for role, target in targets.items():
        people = []
        for r, names in on.items():
            if r.strip().lower() == role.strip().lower():
                people = names
                break
        if not people:
            continue  # coverage owns an empty role, not strength
        strength = sum(ctx.scores.get(n) or 0 for n in people)
        unrated_here.extend(ctx.unrated(people))
        ratios.append(min(1.0, strength / float(target)) if target else 1.0)
        if strength < float(target):
            shorts.append((role, strength, float(target), people))
        else:
            mets.append((role, strength, float(target)))

    if not ratios:
        return None

    # The weakest role sets the tone rather than the mean. A kitchen at
    # half strength is not rescued by an over-strong bar, and an operator
    # reading "82%" would never guess one station was in trouble.
    score = int(round(min(ratios) * 100))
    res = DimensionResult(
        key="operational_strength", label="Operational strength", score=score,
        weight=DEFAULT_WEIGHTS["operational_strength"], floor=55,
        facts={"targets": {r: float(v) for r, v in targets.items()},
               "shortfalls": [{"role": r, "strength": s, "target": t} for r, s, t, _ in shorts],
               "met": [{"role": r, "strength": s, "target": t} for r, s, t in mets]},
    )
    for role, strength, target, people in shorts:
        res.weaknesses.append(
            f"{role} strength {strength:g} against a target of {target:g} — "
            f"{_names(people, ctx.scores)}.")
    for role, strength, target in mets[:2]:
        res.strengths.append(f"{role} at {strength:g}, clear of its {target:g} target.")
    if unrated_here:
        res.blind_spots.append(
            f"{_names(sorted(set(unrated_here)))} " +
            _plural(len(set(unrated_here)), "has", "have") +
            " no Operational Score, so nothing was counted for them.")
    return res


def dim_leadership(ctx: ShiftContext) -> DimensionResult | None:
    """Is somebody on this shift able to run it?

    Two sources, both optional. The profile can require a leader generally;
    a shift leader rule can name one precisely ("Saturday dinner needs a
    bartender at 5"). Neither configured means the question is not being
    asked of this restaurant, which is different from failing it.
    """
    rules = [r for r in (ctx.leader_rules or []) if _rule_applies(r, ctx)]
    if not rules and not ctx.profile.requires_leader:
        return None

    people = ctx.people
    on_role = ctx.by_role
    satisfied, missed, unanswerable = [], [], []

    # A requirement phrased in scores cannot be judged by a restaurant that
    # has rated nobody. Answering it "not met" would be a zero, and
    # leadership carries a floor, so one unconfigured built-in profile
    # capped a perfectly good Saturday at nothing on the strength of a fact
    # the owner never supplied. Unanswerable requirements are set aside and
    # named; the dimension withdraws if none are left.
    blind = not ctx.scores and not ctx.leader_flags
    answerable = []
    for rule in rules:
        if blind and rule.get("min_score") is not None and not rule.get("attribute"):
            unanswerable.append(f"{(rule.get('role') or 'somebody').lower()} scoring "
                                f"{float(rule['min_score']):g} or above")
        else:
            answerable.append(rule)
    rules = answerable
    check_profile = ctx.profile.requires_leader and not blind
    if ctx.profile.requires_leader and blind:
        unanswerable.append("somebody able to run the shift")
    if not rules and not check_profile:
        if unanswerable:
            ctx.notes.append(
                "Leadership was not checked — nobody is rated yet, so "
                + ", ".join(sorted(set(unanswerable))) + " could not be identified.")
        return None

    for rule in rules:
        role = (rule.get("role") or "").strip()
        need = int(rule.get("count") or 1)
        min_score = rule.get("min_score")
        attribute = (rule.get("attribute") or "").strip()
        pool = []
        for r, names in on_role.items():
            if r.strip().lower() == role.lower():
                pool = names
                break
        # Three ways a rule can be answered, in the order an operator would
        # think of them: a named capability, a minimum score, or simply
        # being on the shift. The capability branch is what makes "every
        # closing shift needs somebody authorised to close" real rather
        # than documented.
        if attribute:
            qualified = [n for n in pool if ctx.leader_flags.get(n)]
        elif min_score is None:
            qualified = list(pool)
        else:
            qualified = [n for n in pool if (ctx.scores.get(n) or 0) >= float(min_score)]
        label = (f"{need} {role.lower()}{'' if need == 1 else 's'}" +
                 (" authorised to close" if attribute
                  else (f" scoring {float(min_score):g} or above" if min_score is not None else "")))
        if rule.get("closing"):
            label += " on the closing shift"
        if len(qualified) >= need:
            satisfied.append(label)
        else:
            missed.append({"rule": label, "found": len(qualified),
                           "scheduled": _names(pool, ctx.scores)})

    # The profile's own softer requirement: anybody authorised to close, or
    # anybody clearing the leader score, in one of the leader roles.
    profile_ok = True
    if check_profile:
        pool = people
        if ctx.profile.leader_roles:
            wanted = {r.strip().lower() for r in ctx.profile.leader_roles}
            pool = [n for role, names in on_role.items() if role.strip().lower() in wanted
                    for n in names]
        leaders = [n for n in pool
                   if ctx.leader_flags.get(n)
                   or (ctx.scores.get(n) or 0) >= ctx.profile.leader_min_score]
        profile_ok = bool(leaders)

    total = len(rules) + (1 if check_profile else 0)
    met = len(satisfied) + (1 if (check_profile and profile_ok) else 0)
    score = _pct(met, total)

    # The floor (which caps the whole shift) is earned only by a rule the
    # owner wrote. A built-in profile's "somebody able to run it" is a
    # preference: it costs the dimension its points, never the shift its
    # score — a fully staffed Saturday is not worth 0 because nobody on it
    # has been rated 4 yet.
    res = DimensionResult(
        key="leadership", label="Leadership", score=score,
        weight=DEFAULT_WEIGHTS["leadership"], floor=60 if rules else None,
        facts={"rules_checked": total, "rules_met": met, "misses": missed},
    )
    for miss in missed:
        res.weaknesses.append(
            f"Needs {miss['rule']}, found {miss['found']}. On this shift: {miss['scheduled']}.")
    if check_profile and not profile_ok:
        res.weaknesses.append(
            f"Nobody on this shift is authorised to close or rated "
            f"{ctx.profile.leader_min_score:g} or above.")
    if met == total and total:
        res.strengths.append("Leadership requirements met.")
    if unanswerable:
        res.blind_spots.append(
            "Nobody is rated yet, so " + ", ".join(sorted(set(unanswerable)))
            + " could not be checked on this shift.")
    return res


def _rule_applies(rule: dict, ctx: ShiftContext) -> bool:
    if not (rule.get("role") or "").strip():
        return False
    # "Every closing shift" means the last shift to end that day, not every
    # shift. Applied to all of them it demanded a closer at breakfast.
    if rule.get("closing") and not ctx.is_closing:
        return False
    days = {d.strip().lower() for d in (rule.get("days") or []) if d}
    if days and (ctx.day or "").strip().lower() not in days:
        return False
    part = (rule.get("daypart") or "").strip().lower()
    if part and part != (ctx.daypart or "").lower():
        return False
    return True


# Somebody is "experienced" once they have worked this many shifts in the
# history the restaurant has uploaded. Deliberately a count of real shifts
# rather than a hire date, because shift data is what this product actually
# has — a hire date would be a field nobody fills in.
EXPERIENCE_SHIFTS = 20
# Below this, somebody is still learning and should not be the only person
# holding a station on a busy shift.
DEVELOPING_SHIFTS = 6


def dim_experience_balance(ctx: ShiftContext) -> DimensionResult | None:
    """Enough hands who have done this before.

    Separate from Operational Score on purpose: a strong new hire and a
    steady veteran are different kinds of useful, and a shift made entirely
    of the first kind goes wrong in ways no rating predicts.
    """
    if not ctx.tenure:
        return None
    people = ctx.people
    known = [n for n in people if n in ctx.tenure]
    if not known:
        return None

    veterans = [n for n in known if ctx.tenure[n] >= EXPERIENCE_SHIFTS]
    rookies = [n for n in known if ctx.tenure[n] < DEVELOPING_SHIFTS]
    want = float(ctx.profile.experience_mix or 0)
    have = len(veterans) / float(len(known))
    score = SCORE_MAX if want <= 0 else _pct(have, want)

    res = DimensionResult(
        key="experience_balance", label="Experience", score=score,
        weight=DEFAULT_WEIGHTS["experience_balance"],
        facts={"veterans": veterans, "rookies": rookies,
               "experienced_share": round(have, 2), "target_share": want,
               "unknown_tenure": [n for n in people if n not in ctx.tenure]},
    )
    if have >= want:
        res.strengths.append(
            f"{len(veterans)} experienced {_plural(len(veterans), 'hand')} on, "
            f"{int(round(have * 100))}% of the shift.")
    else:
        res.weaknesses.append(
            f"Only {len(veterans)} of {len(known)} have worked {EXPERIENCE_SHIFTS}+ shifts here; "
            f"this shift usually wants about {int(round(want * 100))}%.")
    if len(rookies) > 1 and len(known) - len(rookies) <= 1:
        res.weaknesses.append(
            f"{_names(rookies)} are all still new, with little experienced cover.")
    unknown = res.facts["unknown_tenure"]
    if unknown:
        res.blind_spots.append(
            f"No shift history for {_names(unknown)}, so their experience is unknown.")
    return res


def dim_training_balance(ctx: ShiftContext) -> DimensionResult | None:
    """Is anybody weak left holding a station alone?

    The point is not to keep developing people off the floor. It is the
    opposite: pair them with someone stronger so the shift works AND they
    get better. A schedule that maximises rating benches the weaker half
    permanently, which is how a team stops improving and how people leave.
    """
    if not ctx.scores:
        return None
    on_role = ctx.by_role
    checked = isolated = mentored = 0
    isolated_names, mentored_pairs = [], []

    for role, names in on_role.items():
        rated = ctx.rated(names)
        if not rated:
            continue
        weak = [n for n in rated if (ctx.scores.get(n) or 0) <= 2]
        if not weak:
            continue
        checked += len(weak)
        strongest = max((ctx.scores.get(n) or 0) for n in rated)
        for w in weak:
            # A mentor is somebody two full points clear in the same role.
            if strongest >= (ctx.scores.get(w) or 0) + 2:
                mentored += 1
                mentor = max(rated, key=lambda n: ctx.scores.get(n) or 0)
                mentored_pairs.append(f"{w} with {mentor} on {role.lower()}")
            else:
                isolated += 1
                isolated_names.append(f"{w} on {role.lower()}")

    if not checked:
        return None
    score = _pct(mentored, checked)
    res = DimensionResult(
        key="training_balance", label="Training balance", score=score,
        weight=DEFAULT_WEIGHTS["training_balance"],
        facts={"developing": checked, "mentored": mentored, "isolated": isolated,
               "pairs": mentored_pairs, "isolated_names": isolated_names},
    )
    if mentored:
        res.strengths.append("Paired " + "; ".join(mentored_pairs[:2]) + ".")
    for name in isolated_names[:2]:
        res.weaknesses.append(f"{name} has nobody stronger alongside them.")
    if ctx.profile.training_allowed and isolated:
        res.weaknesses.append(
            "This shift allows training, but a mentor still has to be on it.")
    return res


def dim_demand_match(ctx: ShiftContext) -> DimensionResult | None:
    """Does the quality of the team track how busy the shift will be?

    Only penalises under-quality on a busy shift. Putting a strong team on
    a quiet Tuesday is not a fault this dimension should punish — that is
    labor efficiency's question, and double-counting it would push the
    scheduler toward deliberately weak quiet shifts.
    """
    rated = ctx.rated(ctx.people)
    if not rated:
        return None
    demand = ctx.profile.demand or "normal"
    avg = sum(ctx.scores.get(n) or 0 for n in rated) / float(len(rated))
    # What an average team strength ought to be at each demand level.
    wanted = {"low": 2.6, "normal": 3.0, "high": 3.6, "peak": 4.0}.get(demand, 3.0)
    score = SCORE_MAX if avg >= wanted else _pct(avg, wanted)

    res = DimensionResult(
        key="demand_match", label="Demand match", score=score,
        weight=DEFAULT_WEIGHTS["demand_match"],
        facts={"demand": demand, "average_score": round(avg, 2), "wanted": wanted,
               "vs_average_day_pct": ctx.demand_pct},
    )
    if avg >= wanted:
        res.strengths.append(
            f"Team averages {avg:.1f} against the {demand}-demand bar of {wanted:g}.")
    else:
        busy = "your busiest" if demand == "peak" else f"a {demand}-demand"
        res.weaknesses.append(
            f"Team averages {avg:.1f} on {busy} shift, under the {wanted:g} this profile expects.")
    if ctx.demand_pct is None:
        res.blind_spots.append("No sales history yet for this day, so demand is the profile's.")
    return res


def dim_labor_efficiency(ctx: ShiftContext) -> DimensionResult | None:
    """Hours against this day's own target.

    Symmetric but not equal: over budget costs money, well under usually
    means the floor is thin. Over is penalised harder, because the labor
    target is a ceiling the rest of the module treats as one.
    """
    if not ctx.target_hours or ctx.target_hours <= 0 or ctx.day_hours <= 0:
        return None
    ratio = ctx.day_hours / float(ctx.target_hours)
    if 0.9 <= ratio <= 1.02:
        score = SCORE_MAX
    elif ratio > 1.02:
        score = max(0, int(round(100 - (ratio - 1.02) * 220)))
    else:
        score = max(0, int(round(100 - (0.9 - ratio) * 130)))

    res = DimensionResult(
        key="labor_efficiency", label="Labor efficiency", score=score,
        weight=DEFAULT_WEIGHTS["labor_efficiency"],
        facts={"scheduled_hours": round(ctx.day_hours, 1),
               "target_hours": round(float(ctx.target_hours), 1),
               "ratio": round(ratio, 2)},
    )
    over = ctx.day_hours - float(ctx.target_hours)
    if 0.9 <= ratio <= 1.02:
        res.strengths.append(f"{ctx.day_hours:.0f}h against a {float(ctx.target_hours):.0f}h target.")
    elif over > 0:
        res.weaknesses.append(f"{over:.0f}h over this day's {float(ctx.target_hours):.0f}h target.")
    else:
        res.weaknesses.append(
            f"{abs(over):.0f}h under target — cheap, but check the floor can carry it.")
    return res


# A shift at this demand level or above is a hard one to work, and is what
# fatigue and fairness both count.
HARD_DEMAND = "high"
# Hard shifts in one week before somebody is carrying too many of them.
HARD_SHIFT_CEILING = 4
# Consecutive days worked before the run itself is the problem.
CONSECUTIVE_DAY_CEILING = 6


def dim_fatigue(ctx: ShiftContext) -> DimensionResult | None:
    """Are the same people carrying every hard shift?

    This is the dimension that stops the engine degenerating into "put the
    best people on everything". A rating system can only ever push in that
    direction; burning out the strong half is the predictable result, and
    it shows up as turnover months later rather than as a bad schedule
    anybody could point at.
    """
    if not ctx.week_assignments:
        return None
    people = ctx.people
    tracked = [n for n in people if n in ctx.week_assignments]
    if not tracked:
        return None

    overloaded, long_runs, long_shifts, heavy_weeks = [], [], [], []
    for name in tracked:
        assignments = ctx.week_assignments.get(name) or []
        hard = sum(1 for a in assignments
                   if DEMAND_RANK.get(a.get("demand", "normal"), 1) >= DEMAND_RANK[HARD_DEMAND])
        if hard > HARD_SHIFT_CEILING:
            overloaded.append((name, hard))
        run = _longest_run(sorted({a.get("date") for a in assignments if a.get("date")}))
        if run > CONSECUTIVE_DAY_CEILING:
            long_runs.append((name, run))
        # Hours, not only days: a 13-hour double and a 46-hour week are
        # both fatigue the day count never sees.
        if ctx.max_shift_hours:
            longest = max((a.get("hours") or 0) for a in assignments) if assignments else 0
            if longest > float(ctx.max_shift_hours) + 0.01:
                long_shifts.append((name, longest))
        total = sum((a.get("hours") or 0) for a in assignments)
        ceiling = None
        lim = (ctx.hours_limits or {}).get(name)
        if lim and lim[1]:
            ceiling = float(lim[1])
        elif ctx.weekly_ceiling:
            ceiling = float(ctx.weekly_ceiling)
        if ceiling and total > ceiling + 0.05:
            heavy_weeks.append((name, round(total, 1), ceiling))

    strained = len({n for n, _ in overloaded} | {n for n, _ in long_runs}
                   | {n for n, _ in long_shifts} | {n for n, _, _ in heavy_weeks})
    score = _pct(len(tracked) - strained, len(tracked))
    res = DimensionResult(
        key="fatigue", label="Fatigue", score=score,
        weight=DEFAULT_WEIGHTS["fatigue"],
        facts={"overloaded": [{"name": n, "hard_shifts": h} for n, h in overloaded],
               "long_runs": [{"name": n, "days": d} for n, d in long_runs],
               "long_shifts": [{"name": n, "hours": h} for n, h in long_shifts],
               "heavy_weeks": [{"name": n, "hours": h, "ceiling": c} for n, h, c in heavy_weeks],
               "tracked": len(tracked)},
    )
    for name, hard in overloaded[:2]:
        res.weaknesses.append(
            f"{name} is on {hard} of the week's busiest shifts — watch for burnout.")
    for name, days in long_runs[:2]:
        res.weaknesses.append(f"{name} works {days} days in a row this week.")
    for name, hours in long_shifts[:2]:
        res.weaknesses.append(f"{name} has a {hours:g}-hour shift this week.")
    for name, hours, ceiling in heavy_weeks[:2]:
        res.weaknesses.append(f"{name} is at {hours:g}h against a {ceiling:g}h ceiling.")
    if not res.weaknesses:
        res.strengths.append("Nobody is carrying an unreasonable share of the hard shifts.")
    return res


def _longest_run(dates: list) -> int:
    """Longest streak of consecutive calendar days in a sorted date list."""
    best = run = 0
    previous = None
    for raw in dates:
        try:
            current = datetime.strptime(raw, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        run = run + 1 if previous and (current - previous) == timedelta(days=1) else 1
        best = max(best, run)
        previous = current
    return best


def dim_fairness(ctx: ShiftContext) -> DimensionResult | None:
    """Are the shifts worth working spread around?

    Premium shifts are the ones that pay. Handing every Friday and Saturday
    to the same three people is a real grievance in a real restaurant, and
    it is invisible to every other dimension here.
    """
    if not ctx.week_assignments:
        return None
    working_names = [n for n, a in ctx.week_assignments.items() if a]
    if len(working_names) < 3:
        return None

    def _count(pred):
        return {n: sum(1 for a in (ctx.week_assignments.get(n) or []) if pred(a)) for n in working_names}

    kinds = {
        "busiest": _count(lambda a: DEMAND_RANK.get(a.get("demand", "normal"), 1) >= DEMAND_RANK[HARD_DEMAND]),
        "weekend": _count(lambda a: a.get("weekend")),
        "closing": _count(lambda a: a.get("closing")),
    }
    kinds = {k: v for k, v in kinds.items() if any(v.values())}
    if not kinds:
        return None

    # Two shifts of difference across a roster is ordinary. Beyond that it
    # starts to look like a pattern rather than a rota. The worst of the
    # three spreads sets the score; each is reported on its own.
    worst_key, worst_spread, facts = None, -1, {}
    for key, counts in kinds.items():
        top, bottom = max(counts.values()), min(counts.values())
        spread = top - bottom
        facts[key] = {"spread": spread,
                      "most": {"names": sorted(n for n, c in counts.items() if c == top), "shifts": top},
                      "least": {"names": sorted(n for n, c in counts.items() if c == bottom), "shifts": bottom}}
        if spread > worst_spread:
            worst_key, worst_spread = key, spread
    score = SCORE_MAX if worst_spread <= 2 else max(0, SCORE_MAX - (worst_spread - 2) * 22)
    res = DimensionResult(key="fairness", label="Fairness", score=score,
                          weight=DEFAULT_WEIGHTS["fairness"], facts=facts)
    labels = {"busiest": "the week's busiest shifts", "weekend": "the weekend shifts", "closing": "the closes"}
    uneven = [(k, f) for k, f in facts.items() if f["spread"] > 2]
    if uneven:
        # One sentence, because the week summary keeps one line per
        # dimension: the person carrying the most of the worst-spread kind,
        # then each kind's figure.
        key, f = max(uneven, key=lambda kf: kf[1]["spread"])
        who = _names(f["most"]["names"][:2])
        parts = [f"{facts[k]['most']['shifts']} of {labels[k]}" for k, _ in uneven]
        least = _names(f["least"]["names"][:2])
        res.weaknesses.append(
            f"{who} " + _plural(len(f["most"]["names"][:2]), "works", "work") + " " + " and ".join(parts)
            + f" while {least} " + _plural(len(f["least"]["names"][:2]), "works", "work")
            + f" {f['least']['shifts']}.")
    else:
        res.strengths.append("Busy shifts, weekends and closes are spread evenly across the roster.")
    return res


def dim_stability(ctx: ShiftContext) -> DimensionResult | None:
    """Are people getting roughly the schedule they had last week?

    Predictability is a real benefit to staff and costs the restaurant
    nothing when demand has not moved. Weighted lightly, because a genuine
    demand change should always win over the comfort of the same rota.
    """
    if not ctx.prior_pattern:
        return None
    people = [n for n in ctx.people if n in ctx.prior_pattern]
    if not people:
        return None
    familiar = 0
    changed = []
    for name in people:
        pattern = ctx.prior_pattern.get(name) or {}
        days = {d.strip().lower() for d in (pattern.get("days") or [])}
        parts = {p.strip().lower() for p in (pattern.get("dayparts") or [])}
        day_ok = not days or (ctx.day or "").strip().lower() in days
        part_ok = not parts or (ctx.daypart or "").lower() in parts
        if day_ok and part_ok:
            familiar += 1
        else:
            changed.append(name)
    score = _pct(familiar, len(people))
    res = DimensionResult(
        key="stability", label="Schedule stability", score=score,
        weight=DEFAULT_WEIGHTS["stability"],
        facts={"familiar": familiar, "changed": changed, "checked": len(people)},
    )
    if changed:
        res.weaknesses.append(
            f"{_names(changed[:3])} " + _plural(len(changed), "is", "are") +
            " on a shift they do not usually work.")
    else:
        res.strengths.append("Everybody is on a shift they normally work.")
    return res


def dim_cross_training(ctx: ShiftContext) -> DimensionResult | None:
    """How much flex is on the floor if something goes wrong?

    A shift where every person can only do their own job has no answer to
    a no-show. One where half the floor can cover a second station does.
    """
    if not ctx.cross_trained:
        return None
    people = ctx.people
    if not people:
        return None
    flexible = [n for n in people if len(ctx.cross_trained.get(n) or []) > 1]
    # A third of the shift able to flex is a healthy floor; past that there
    # is no extra credit to give.
    score = min(SCORE_MAX, int(round(len(flexible) / float(len(people)) / 0.34 * 100)))
    res = DimensionResult(
        key="cross_training", label="Cross-training", score=score,
        weight=DEFAULT_WEIGHTS["cross_training"],
        facts={"flexible": flexible, "on_shift": len(people)},
    )
    if flexible:
        res.strengths.append(
            f"{_names(flexible[:3])} can cover more than one station.")
    else:
        res.weaknesses.append("Nobody on this shift can cover a second station.")
    return res


# ── Coverage by the half hour ──────────────────────────────────────────────

SLOT_MINUTES = 30
DAYPART_CUTOVER = 15 * 60


def _slot_minutes(value: str):
    raw = (value or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(raw, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    return None


def dim_coverage_curve(ctx: ShiftContext) -> DimensionResult | None:
    """Is every required position filled at every half hour, not just on
    average across the daypart?

    Requirements are the per-daypart floors the owner set (role_floors),
    else their whole-day role minimums. Without either, the question is not
    being asked. The window is this daypart's slice of the opening hours;
    with no hours on file it is the span of the day's own shifts.
    """
    required = {r: int(n) for r, n in (ctx.role_floors or {}).items() if n}
    source = "your staffing floors"
    if not required and ctx.role_minimums and ctx.open_minutes is not None:
        # Whole-day minimums are only an hourly requirement when the hours
        # they apply across are on file; without them the day's own shifts
        # would define the window and a thin day could never be short.
        required = {r: int(n) for r, n in ctx.role_minimums.items() if n}
        source = "your role minimums"
    if not required:
        return None
    rows = ctx.day_rows or ctx.rows
    spans = []
    for r in rows:
        s, e = _slot_minutes(r.get("shift_start")), _slot_minutes(r.get("shift_end"))
        if s is None or e is None or ctx._is_flagged(r):
            continue
        if e <= s:
            e += 24 * 60
        spans.append(((r.get("role") or "").strip().lower(), s, e))
    if not spans:
        return None
    day_open = ctx.open_minutes if ctx.open_minutes is not None else min(s for _, s, _ in spans)
    day_close = ctx.close_minutes if ctx.close_minutes is not None else max(e for _, _, e in spans)
    if day_close <= day_open:
        day_close += 24 * 60
    if ctx.daypart == "morning":
        lo, hi = day_open, min(day_close, DAYPART_CUTOVER)
    elif ctx.daypart == "night":
        lo, hi = max(day_open, DAYPART_CUTOVER), day_close
    else:
        lo, hi = day_open, day_close
    if hi - lo < SLOT_MINUTES:
        return None

    slots = list(range(lo, hi, SLOT_MINUTES))
    total = covered = 0
    gaps = {}
    for role, need in required.items():
        key = role.strip().lower()
        for t in slots:
            on = sum(1 for rl, s, e in spans if rl == key and s <= t < e)
            total += 1
            if on >= need:
                covered += 1
            else:
                g = gaps.setdefault(role, {"short_slots": 0, "worst": None, "worst_on": None})
                g["short_slots"] += 1
                if g["worst"] is None or on < g["worst_on"]:
                    g["worst"], g["worst_on"] = t, on
    if not total:
        return None
    score = _pct(covered, total)
    res = DimensionResult(
        key="coverage_curve", label="Coverage by the hour", score=score,
        weight=DEFAULT_WEIGHTS["coverage_curve"], floor=60,
        facts={"required": required, "window": [_fmt_minutes(lo), _fmt_minutes(hi)],
               "slots": len(slots), "gaps": {r: {"minutes_short": g["short_slots"] * SLOT_MINUTES,
                                                  "worst_at": _fmt_minutes(g["worst"]), "on_at_worst": g["worst_on"]}
                                             for r, g in gaps.items()},
               "requirement_source": source},
    )
    for role, g in sorted(gaps.items(), key=lambda kv: -kv[1]["short_slots"])[:3]:
        need = required[role]
        res.weaknesses.append(
            f"{role} is under {need} for {g['short_slots'] * SLOT_MINUTES // 60}h{(g['short_slots'] * SLOT_MINUTES % 60) and ' 30m' or ''} "
            f"— down to {g['worst_on']} at {_fmt_minutes(g['worst'])}.")
    if not gaps:
        res.strengths.append(f"Every floor is held from {_fmt_minutes(lo)} to {_fmt_minutes(hi)}.")
    if ctx.demand_curve:
        # The busiest hour of the day by sales share, and whether it is the
        # best-staffed. A mismatch is a fact worth stating, not yet a score.
        peak_hour = max(ctx.demand_curve.items(), key=lambda kv: kv[1])[0]
        on_peak = sum(1 for _, s, e in spans if s <= int(peak_hour) * 60 < e)
        on_max = max((sum(1 for _, s, e in spans if s <= t < e) for t in slots), default=0)
        res.facts["peak_hour"] = int(peak_hour)
        res.facts["on_at_peak"] = on_peak
        if on_max and on_peak < on_max:
            res.weaknesses.append(
                f"Sales peak around {_fmt_minutes(int(peak_hour) * 60)} with {on_peak} on, while the day tops out at {on_max} on.")
    return res


def _fmt_minutes(m):
    if m is None:
        return ""
    m %= 24 * 60
    h, mm = divmod(m, 60)
    return f"{h % 12 or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


# ── Reliability ────────────────────────────────────────────────────────────

UNRELIABLE_RATE = 0.2


def dim_reliability(ctx: ShiftContext) -> DimensionResult | None:
    """Is a station resting on somebody who does not reliably turn up?

    From the clocked history: the share of scheduled shifts with no clock-in.
    Somebody at or above UNRELIABLE_RATE alone in their role, or on a busy
    shift at all, is the risk this names. Never a judgement of the person —
    the number is theirs and the fix is a second body, not a benching.
    """
    if not ctx.reliability:
        return None
    on_role = ctx.by_role
    known = [n for n in ctx.people if n in ctx.reliability]
    if not known:
        return None
    busy = DEMAND_RANK.get(ctx.profile.demand or "normal", 1) >= DEMAND_RANK[HARD_DEMAND]
    at_risk, exposed = [], []
    for role, names in on_role.items():
        for n in names:
            rate = float((ctx.reliability.get(n) or {}).get("no_show_rate") or 0)
            if rate < UNRELIABLE_RATE:
                continue
            at_risk.append(n)
            if len(names) == 1 or busy:
                exposed.append((n, role, rate, len(names)))
    score = _pct(len(known) - len(exposed), len(known))
    res = DimensionResult(key="reliability", label="Reliability", score=score,
                          weight=DEFAULT_WEIGHTS["reliability"],
                          facts={"at_risk": at_risk, "exposed": [{"name": n, "role": r, "no_show_rate": rt}
                                                                  for n, r, rt, _ in exposed],
                                 "checked": len(known)})
    for n, role, rate, count in exposed[:2]:
        why = "alone on " + role.lower() if count == 1 else "on a busy shift"
        res.weaknesses.append(f"{n} has missed {int(round(rate * 100))}% of scheduled shifts and is {why}.")
    if not exposed:
        res.strengths.append("No station rests on somebody with an attendance problem.")
    unknown = [n for n in ctx.people if n not in ctx.reliability]
    if unknown:
        res.blind_spots.append(f"No clock-in history for {_names(unknown[:3])}, so attendance is unknown.")
    return res


# ── Pairings ───────────────────────────────────────────────────────────────

def dim_pairings(ctx: ShiftContext) -> DimensionResult | None:
    """Two people the owner keeps apart are not on together; two the owner
    likes together are. Real in every kitchen and invisible to a rating."""
    avoid = (ctx.pairs or {}).get("avoid") or set()
    prefer = (ctx.pairs or {}).get("prefer") or set()
    if not avoid and not prefer:
        return None
    on = {n.lower(): n for n in ctx.people}
    clashes = [p for p in avoid if all(x in on for x in p)]
    matches = [p for p in prefer if all(x in on for x in p)]
    if not clashes and not matches and not any(any(x in on for x in p) for p in avoid | prefer):
        return None
    score = max(0, SCORE_MAX - 45 * len(clashes))
    res = DimensionResult(key="pairings", label="Pairings", score=score,
                          weight=DEFAULT_WEIGHTS["pairings"],
                          facts={"clashes": [sorted(on[x] for x in p) for p in clashes],
                                 "matches": [sorted(on[x] for x in p) for p in matches]})
    for p in clashes[:2]:
        a, b = sorted(on[x] for x in p)
        res.weaknesses.append(f"{a} and {b} are on together, and you asked to keep them apart.")
    for p in matches[:2]:
        a, b = sorted(on[x] for x in p)
        res.strengths.append(f"{a} and {b} are on together, as you prefer.")
    return res


DIMENSIONS = {
    "coverage": dim_coverage,
    "coverage_curve": dim_coverage_curve,
    "operational_strength": dim_operational_strength,
    "leadership": dim_leadership,
    "demand_match": dim_demand_match,
    "labor_efficiency": dim_labor_efficiency,
    "experience_balance": dim_experience_balance,
    "training_balance": dim_training_balance,
    "reliability": dim_reliability,
    "pairings": dim_pairings,
    "fatigue": dim_fatigue,
    "fairness": dim_fairness,
    "stability": dim_stability,
    "cross_training": dim_cross_training,
}

# What a customer sees today. The rest is computed, stored and available to
# the explanation layer, but not put on screen yet — a manager reading
# fourteen numbers is reading none of them.
CUSTOMER_DIMENSIONS = ("coverage", "coverage_curve", "operational_strength", "leadership",
                       "training_balance", "labor_efficiency", "demand_match", "reliability")

# At least one of these has to have data before a shift claims a score. The
# rest are real dimensions and genuinely count, but none of them alone says
# anything about whether the shift will actually run.
SUBSTANTIVE_DIMENSIONS = ("coverage", "coverage_curve", "operational_strength", "leadership",
                          "demand_match", "labor_efficiency", "experience_balance",
                          "training_balance")


# ── Scoring one shift ──────────────────────────────────────────────────────

BANDS = ((90, "excellent"), (78, "good"), (65, "fair"), (0, "weak"))


def band_for(score: int) -> str:
    for floor, name in BANDS:
        if score >= floor:
            return name
    return "weak"


def evaluate_shift(ctx: ShiftContext, weights: dict = None) -> dict:
    """Score one shift across every dimension that has something to say.

    Weights renormalise over the dimensions that actually applied. A
    restaurant with no tenure history and no demand data is judged on what
    it does have, at full scale, rather than being quietly marked down for
    fields nobody has filled in.
    """
    merged = dict(DEFAULT_WEIGHTS)
    merged.update(ctx.profile.weights or {})
    merged.update(weights or {})

    ctx.notes = []
    applied, skipped, failed = [], [], []
    for key, fn in DIMENSIONS.items():
        try:
            result = fn(ctx)
            reason = "no data"
        except Exception as exc:      # one bad dimension must not lose the shift
            result, reason = None, f"failed: {exc}"
            # Silently withdrawing here made a crash indistinguishable from
            # an unconfigured dimension, and coverage crashing turned a
            # capped 50 into a clean 100 with nothing on screen to say so.
            failed.append({"key": key, "error": str(exc)[:200]})
        if result is None:
            skipped.append({"key": key, "reason": reason})
            continue
        result.weight = float(merged.get(key, result.weight) or 0)
        if result.weight <= 0:
            # An admin who weighted a dimension to zero said it does not
            # count here. It must then not cap the shift either, or the
            # setting only half works and in the more surprising direction.
            skipped.append({"key": key, "reason": "weighted to zero"})
            continue
        applied.append(result)

    # Fatigue and fairness alone are not an evaluation. Both can return a
    # cheerful 100 for a restaurant that has configured nothing at all,
    # and "Shift Quality 100/100" off the back of "nobody is overworked"
    # is a number this engine has no business printing.
    if not any(d.key in SUBSTANTIVE_DIMENSIONS for d in applied):
        applied = []

    if not applied:
        return {"date": ctx.date, "day": ctx.day, "daypart": ctx.daypart,
                "scored": False, "score": None, "failed": failed,
                "profile": _profile_facts(ctx.profile),
                "reason": ("Every dimension failed to compute for this shift."
                           if failed else
                           "Nothing configured yet to judge this shift against.")}

    total_weight = sum(d.weight for d in applied) or 1.0
    raw = sum(d.score * d.weight for d in applied) / total_weight
    score = int(round(raw))

    # A critical dimension under its floor caps the shift AT ITS OWN SCORE.
    # Averaging a 50% coverage away behind eight 95s produces a number no
    # operator would recognise as describing their Saturday, and any
    # allowance on top of the cap re-opens the same hole in miniature: a
    # kitchen missing both cooks read 82 while the allowance was 15 points.
    # The shift is not better than its worst critical part, full stop.
    capped_by = None
    for d in applied:
        if d.floor is not None and d.score < d.floor and d.score < score:
            score = d.score
            capped_by = d.key
    score = max(0, min(SCORE_MAX, score))

    # Weaknesses are ordered by how much each one actually costs the shift —
    # how far under it is, times how much it counts — rather than by score
    # alone. Training balance at 0 on a weight of 7 is a smaller problem
    # than coverage at 50 on a weight of 20, and score-first ordering put
    # the lighter one on top and pushed the coverage gap off the end of the
    # summary entirely.
    strengths = [s for d in sorted(applied, key=lambda x: -x.weight) for s in d.strengths]
    weaknesses = [w for d in sorted(applied,
                                    key=lambda x: -((SCORE_MAX - x.score) * x.weight))
                  for w in d.weaknesses]
    blind = [b for d in applied for b in d.blind_spots] + list(ctx.notes)
    for f in failed:
        blind.append(f"{f['key'].replace('_', ' ').capitalize()} could not be worked out "
                     "for this shift, so it was left out of the score.")

    return {
        "date": ctx.date, "day": ctx.day, "daypart": ctx.daypart,
        "scored": True,
        "score": score,
        "band": band_for(score),
        "profile": _profile_facts(ctx.profile),
        "meets_profile": score >= int(ctx.profile.min_quality or 0),
        "capped_by": capped_by,
        "people": ctx.people,
        "headline": _headline(ctx, score),
        "dimensions": [
            {"key": d.key, "label": d.label, "score": d.score, "weight": d.weight,
             "strengths": d.strengths, "weaknesses": d.weaknesses, "facts": d.facts,
             "customer_facing": d.key in CUSTOMER_DIMENSIONS}
            for d in sorted(applied, key=lambda x: -x.weight)
        ],
        "not_applicable": sorted({s["key"] for s in skipped if s["key"] not in
                                  {f["key"] for f in failed}}),
        # Kept apart from not_applicable on purpose: "you have not set this
        # up" and "we could not compute this" are different facts and only
        # one of them is the owner's to act on.
        "failed": failed,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "blind_spots": blind,
        # One sentence per person on this shift saying why they are here,
        # composed from facts the dimensions already loaded. Deterministic,
        # so it can never claim something the engine did not see.
        "assignments": [explain_assignment(r, ctx, applied) for r in ctx.rows
                        if (r.get("employee") or "").strip()],
        # Which dimension wrote each line. Not rendered — it lets the week
        # summary group findings by dimension rather than by exact wording,
        # so "81h under target" and "120h under target" are recognised as
        # one recurring problem instead of two unrelated ones.
        "line_keys": {line: d.key for d in applied
                      for line in d.strengths + d.weaknesses + d.blind_spots},
    }


def explain_assignment(row: dict, ctx: ShiftContext, applied: list = None) -> dict:
    """Why this person is on this shift, in one sentence an owner can argue
    with: "Level 5 bartender; keeps the bar at 9 against a target of 8; the
    one authorised to close; usually works Saturday nights; 32h this week."

    Built only from what the context holds. A fact that is not on file
    (no rating, no history) is simply not claimed.
    """
    name = (row.get("employee") or "").strip()
    role = (row.get("role") or "").strip()
    bits, facts = [], {}
    score = ctx.scores.get(name)
    if score is not None:
        label = {1: "very weak", 2: "below average", 3: "average", 4: "strong", 5: "excellent"}.get(int(score), "")
        bits.append(f"Level {int(score)} {role.lower() or 'team member'}" + (f" ({label})" if label else ""))
        facts["score"] = score
    elif role:
        bits.append(f"{role}, not yet rated")
    # strength: what this person contributes to the role's target
    for d in (applied or []):
        if d.key == "operational_strength":
            for m in (d.facts.get("met") or []) + (d.facts.get("shortfalls") or []):
                if m["role"].strip().lower() == role.lower() and score is not None:
                    verb = "keeps" if m["strength"] >= m["target"] else "leaves"
                    bits.append(f"{verb} {role.lower()} at {m['strength']:g} against a target of {m['target']:g}")
                    facts["role_strength"] = m
                    break
    # leadership
    if ctx.leader_flags.get(name):
        bits.append("authorised to close")
        facts["can_close"] = True
    elif ctx.profile.requires_leader and score is not None and score >= ctx.profile.leader_min_score:
        bits.append(f"clears the {ctx.profile.leader_min_score:g}+ leader bar this shift wants")
    # pattern
    pattern = ctx.prior_pattern.get(name) or {}
    days = {d.strip().lower() for d in (pattern.get("days") or [])}
    parts = {p.strip().lower() for p in (pattern.get("dayparts") or [])}
    if days and (ctx.day or "").lower() in days and (not parts or (ctx.daypart or "").lower() in parts):
        part = {"morning": "days", "night": "nights"}.get(ctx.daypart, "shifts")
        bits.append(f"usually works {ctx.day} {part}")
        facts["usual"] = True
    elif days and (ctx.day or "").lower() not in days:
        bits.append(f"not a day they usually work")
        facts["usual"] = False
    # tenure
    t = ctx.tenure.get(name)
    if t is not None:
        if t >= EXPERIENCE_SHIFTS:
            bits.append(f"{t} shifts here")
        elif t < DEVELOPING_SHIFTS:
            bits.append(f"still new ({t} shifts here)")
    # hours this week and headroom
    assignments = ctx.week_assignments.get(name) or []
    total = round(sum((a.get("hours") or 0) for a in assignments), 1)
    if total:
        ceiling = None
        lim = (ctx.hours_limits or {}).get(name)
        if lim and lim[1]:
            ceiling = float(lim[1])
        elif ctx.weekly_ceiling:
            ceiling = float(ctx.weekly_ceiling)
        if ceiling:
            room = round(ceiling - total, 1)
            bits.append(f"{total:g}h this week" + (f", {room:g}h under the {ceiling:g}h ceiling" if room >= 0 else f", {abs(room):g}h OVER the {ceiling:g}h ceiling"))
        else:
            bits.append(f"{total:g}h this week")
        facts["week_hours"] = total
    # reliability
    rel = (ctx.reliability or {}).get(name)
    if rel and float(rel.get("no_show_rate") or 0) >= UNRELIABLE_RATE:
        bits.append(f"has missed {int(round(float(rel['no_show_rate']) * 100))}% of scheduled shifts")
    # constraints
    note = (ctx.constraints or {}).get(name)
    if note:
        bits.append(f"note on file: {str(note)[:60]}")
    # pairings
    on = {n.lower() for n in ctx.people}
    for p in (ctx.pairs or {}).get("prefer") or set():
        if name.lower() in p and p <= on:
            other = next(x for x in p if x != name.lower())
            bits.append(f"paired with {other.title()} as you prefer")
            break
    text = "; ".join(bits) if bits else "on the shift"
    return {"employee": name, "role": role, "date": ctx.date, "day": ctx.day, "daypart": ctx.daypart,
            "why": text[0].upper() + text[1:] + ".", "facts": facts}


def _profile_facts(profile: ShiftProfile) -> dict:
    return {"key": profile.key, "label": profile.label, "demand": profile.demand,
            "min_quality": profile.min_quality, "training_allowed": profile.training_allowed,
            "source": profile.source}


def _headline(ctx: ShiftContext, score: int) -> str:
    part = {"morning": "lunch", "night": "dinner"}.get(ctx.daypart, ctx.daypart or "shift")
    where = f"{ctx.day} {part}".strip() if ctx.day else ctx.date
    return f"{where} scored {score}/100 on Shift Quality."


# ── Scoring a whole schedule ───────────────────────────────────────────────

def evaluate_schedule(contexts: list, weights: dict = None,
                      signals: dict = None) -> dict:
    """Roll every shift up into one number, with the reasons intact.

    Shifts are weighted by demand: a weak Saturday dinner is a worse week
    than a weak Monday lunch, and a flat mean says the opposite.
    """
    shifts = [evaluate_shift(c, weights) for c in contexts]
    scored = [s for s in shifts if s.get("scored")]
    if not scored:
        return {"checked": False, "score": None, "shifts": shifts,
                "reason": "No shift had enough configured to judge it.",
                "confidence": confidence(shifts, signals or {})}

    weighted = sum(s["score"] * DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0) for s in scored)
    divisor = sum(DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0) for s in scored) or 1.0
    overall = int(round(weighted / divisor))

    # Strip what is true of the week out of the individual shifts FIRST, so
    # the week-level lists below are built from what is left. Two overlapping
    # summaries is one too many, and the shift sections are what get read.
    hoisted = _hoist_common_lines(scored)

    return {
        "checked": True,
        "score": overall,
        "band": band_for(overall),
        "shifts": shifts,
        "dimensions": _rollup(scored),
        "below_profile": [
            {"date": s["date"], "day": s["day"], "daypart": s["daypart"],
             "score": s["score"], "min_quality": s["profile"]["min_quality"],
             "label": s["profile"]["label"]}
            for s in scored if not s["meets_profile"]
        ],
        "worst": min(scored, key=lambda s: s["score"])["headline"] if scored else None,
        "best": max(scored, key=lambda s: s["score"])["headline"] if scored else None,
        "strengths": _week_reasons(hoisted, scored, "strengths"),
        "weaknesses": _week_reasons(hoisted, scored, "weaknesses"),
        "recommendations": recommendations(scored),
        "confidence": confidence(shifts, signals or {}),
    }


# A line true of most of the week is a fact about the WEEK, and printing it
# inside every shift's own explanation is how seven shifts end up reading
# like the same paragraph seven times. Both figures are deliberately blunt:
# more than half the week, and never fewer than three shifts, so a two-shift
# coincidence stays where it belongs.
COMMON_SHARE = 0.6
COMMON_MIN_SHIFTS = 3


def _hoist_common_lines(scored: list) -> dict:
    """Move what is true across the week out of the individual shifts.

    Returns the hoisted lines and strips them from each shift in place, so a
    shift's own section is left saying only what is different about it. The
    three most repeated offenders in practice are a fully staffed roster, a
    role sitting the same distance under target every night, and a fatigue
    warning about somebody who works every day — all of them genuinely
    week-level, and all of them previously printed seven times.

    Grouped by DIMENSION, not by exact wording. A finding that carries a
    per-day number writes a different sentence every day, so string matching
    left "81h under target" on two shifts while the summary reported "120h
    under target" for five — the same problem, presented as two.
    """
    if len(scored) < COMMON_MIN_SHIFTS:
        return {"strengths": [], "weaknesses": [], "blind_spots": []}

    bar = max(COMMON_MIN_SHIFTS, int(round(len(scored) * COMMON_SHARE)))
    out = {}
    for field_name in ("strengths", "weaknesses", "blind_spots"):
        # {dimension: {"shifts": n, "lines": {text: count}, "order": first seen}}
        by_dim = {}
        for shift in scored:
            keys = shift.get("line_keys") or {}
            here = set()
            for line in shift.get(field_name) or []:
                dim = keys.get(line, line)   # unattributed lines group by themselves
                entry = by_dim.setdefault(dim, {"shifts": 0, "lines": {},
                                                "order": len(by_dim)})
                entry["lines"][line] = entry["lines"].get(line, 0) + 1
                here.add(dim)
            for dim in here:
                by_dim[dim]["shifts"] += 1

        common = {dim: e for dim, e in by_dim.items() if e["shifts"] >= bar}
        lines = []
        # Ties break on first appearance, which is stable and meaningful:
        # each shift emits its costliest dimension first. A set's iteration
        # order is neither, and reshuffled the summary on every page load.
        for dim, entry in sorted(common.items(),
                                 key=lambda kv: (-kv[1]["shifts"], kv[1]["order"])):
            # The wording that came up most often speaks for the group.
            text = max(entry["lines"].items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
            varied = len(entry["lines"]) > 1
            if entry["shifts"] == len(scored) and not varied:
                lines.append(text)
            elif entry["shifts"] == len(scored):
                lines.append(f"{text} Similar on every shift this week.")
            else:
                lines.append(f"{text} — {entry['shifts']} of {len(scored)} shifts")
        out[field_name] = lines

        for shift in scored:
            keys = shift.get("line_keys") or {}
            shift[field_name] = [x for x in (shift.get(field_name) or [])
                                 if keys.get(x, x) not in common]

    for shift in scored:
        shift.pop("line_keys", None)
        if not shift["strengths"] and not shift["weaknesses"]:
            shift["nothing_specific"] = True
    return out


def _rollup(scored: list) -> list:
    """Each dimension's mean across the week, weighted the same way."""
    buckets = {}
    for shift in scored:
        w = DEMAND_WEIGHT.get(shift["profile"]["demand"], 1.0)
        for d in shift["dimensions"]:
            b = buckets.setdefault(d["key"], {"key": d["key"], "label": d["label"],
                                              "total": 0.0, "weight": 0.0, "shifts": 0,
                                              "worst": None,
                                              "customer_facing": d["customer_facing"]})
            b["total"] += d["score"] * w
            b["weight"] += w
            b["shifts"] += 1
            if b["worst"] is None or d["score"] < b["worst"]["score"]:
                b["worst"] = {"score": d["score"], "date": shift["date"],
                              "day": shift["day"], "daypart": shift["daypart"]}
    out = []
    for b in buckets.values():
        out.append({"key": b["key"], "label": b["label"], "shifts": b["shifts"],
                    "score": int(round(b["total"] / (b["weight"] or 1.0))),
                    "worst": b["worst"], "customer_facing": b["customer_facing"]})
    order = list(DIMENSIONS)
    out.sort(key=lambda d: order.index(d["key"]) if d["key"] in order else 99)
    return out


def _week_reasons(hoisted: dict, scored: list, field_name: str, limit: int = 5) -> list:
    """The week's own lines: what held across it, then what stood out.

    The hoisted lines come first because they are the pattern — the thing no
    single shift row can tell a manager. Whatever room is left goes to the
    most notable remaining line, so a week with no pattern still says
    something specific rather than nothing at all.
    """
    lines = list(hoisted.get(field_name) or [])[:limit]
    if len(lines) < limit:
        lines += [x for x in _top_reasons(scored, field_name, limit - len(lines))
                  if x not in lines]
    return lines


def _top_reasons(scored: list, field_name: str, limit: int = 4) -> list:
    """The reasons that recur ACROSS shifts, most common first.

    Deliberately not a digest of everything: each shift already carries its
    own lines, and repeating them at week level put the same sentence on
    screen twice for a manager to read twice. What belongs here is the
    pattern — one problem showing up on five different nights — because
    that is the thing no single shift row can tell them.

    A week where nothing recurs falls back to naming the worst shift's own
    reasons, because an empty section says less than a specific one.
    """
    counts, first_seen = {}, {}
    for shift in scored:
        for line in shift.get(field_name) or []:
            counts[line] = counts.get(line, 0) + 1
            first_seen.setdefault(line, f"{shift['day']} {shift['daypart']}".strip())
    # Insertion order is the tiebreak, for the same reason as above.
    order = {line: i for i, line in enumerate(counts)}
    recurring = sorted([kv for kv in counts.items() if kv[1] > 1],
                       key=lambda kv: (-kv[1], order[kv[0]]))
    if recurring:
        return [f"{line} — {n} shifts" for line, n in recurring[:limit]]
    worst = min(scored, key=lambda s: s["score"]) if scored else None
    if not worst:
        return []
    where = f"{worst['day']} {worst['daypart']}".strip() or worst["date"]
    return [f"{where}: {line}" for line in (worst.get(field_name) or [])[:limit]]


# ── Confidence ─────────────────────────────────────────────────────────────
#
# Deliberately separate from quality. Quality says how good the schedule
# is; confidence says how much the engine actually knew when it said so.
# A 94 built on a fully rated roster with a year of sales history is a
# different claim from a 94 built on three ratings and no demand data, and
# collapsing them into one number would be the single most misleading thing
# this engine could do.

CONFIDENCE_LEVELS = ((80, "high"), (55, "moderate"), (0, "low"))


def confidence(shifts: list, signals: dict) -> dict:
    """How much to trust the scores above, and exactly why."""
    score = 100
    reasons = []

    rated = signals.get("rated_people")
    total = signals.get("scheduled_people")
    if total:
        unrated_share = 1.0 - (float(rated or 0) / float(total))
        if unrated_share > 0:
            penalty = int(round(unrated_share * 45))
            score -= penalty
            missing = int(round(unrated_share * total))
            reasons.append(f"{missing} of {total} scheduled staff have no Operational Score.")

    if not signals.get("has_tenure"):
        score -= 12
        reasons.append("No shift history yet, so experience could not be judged.")
    if not signals.get("has_demand"):
        score -= 10
        reasons.append("Not enough sales history to know how busy these days really are.")
    if not signals.get("has_availability"):
        score -= 8
        reasons.append("No availability on file, so nobody could be ruled out.")
    if not signals.get("has_profiles"):
        score -= 5
        reasons.append("Using default shift profiles rather than this restaurant's own.")

    flagged = int(signals.get("rows_needing_review") or 0)
    if flagged:
        score -= min(15, flagged * 3)
        reasons.append(f"{flagged} generated {_plural(flagged, 'row')} needed a human check.")
    dropped = int(signals.get("dropped_rows") or 0)
    if dropped:
        score -= min(20, dropped * 5)
        reasons.append(f"{dropped} {_plural(dropped, 'row')} could not be read at all.")

    broken = sum(len(s.get("failed") or []) for s in shifts)
    if broken:
        # A computation failure is the one confidence penalty that is our
        # fault rather than the owner's, and the one most worth seeing.
        score -= min(35, broken * 6)
        reasons.append(f"{broken} dimension {_plural(broken, 'reading')} could not be "
                       "worked out, so the score is built on less than usual.")

    unfixable = int(signals.get("unsatisfiable") or 0)
    if unfixable:
        score -= min(20, unfixable * 7)
        reasons.append(
            f"{unfixable} {_plural(unfixable, 'requirement')} could not be met with "
            "who was available.")

    hard_constraints = int(signals.get("constrained_people") or 0)
    if hard_constraints and total and hard_constraints / float(total) > 0.4:
        score -= 8
        reasons.append("Availability is tight across most of the roster.")

    score = max(0, min(100, score))
    level = next(name for floor, name in CONFIDENCE_LEVELS if score >= floor)
    return {"score": score, "level": level, "reasons": reasons,
            "summary": {"high": "The engine had what it needed to judge this schedule.",
                        "moderate": "Usable, but some of this is estimated.",
                        "low": "Treat these scores as provisional."}[level]}


# ── Recommendations ────────────────────────────────────────────────────────

def recommendations(scored: list) -> list:
    """What the manager could actually do about it, most valuable first.

    Built from the dimensions' own facts rather than written by a model, so
    a recommendation can never reference a shift or a person that is not in
    the schedule.
    """
    out = []
    unrated = set()
    coverage_gaps, leader_gaps, isolated, over_hours = [], [], [], []
    overloaded, long_runs = set(), set()

    for shift in scored:
        where = f"{shift['day']} {shift['daypart']}".strip() or shift["date"]
        for d in shift["dimensions"]:
            facts = d.get("facts") or {}
            if d["key"] == "coverage" and facts.get("gaps"):
                coverage_gaps.append((where, facts["gaps"][0]))
            if d["key"] == "leadership":
                for miss in facts.get("misses") or []:
                    leader_gaps.append((where, miss["rule"]))
            if d["key"] == "training_balance":
                isolated.extend((where, n) for n in facts.get("isolated_names") or [])
            if d["key"] == "labor_efficiency" and facts.get("ratio", 1) > 1.02:
                over_hours.append((where, facts["scheduled_hours"] - facts["target_hours"]))
            if d["key"] == "fatigue":
                overloaded.update(o["name"] for o in facts.get("overloaded") or [])
                long_runs.update((r["name"], r["days"]) for r in facts.get("long_runs") or [])
        for spot in shift.get("blind_spots") or []:
            if "Operational Score" in spot:
                unrated.add(spot)

    for where, gap in coverage_gaps[:2]:
        out.append(f"Fill the gap on {where}: {gap}.")
    for where, rule in leader_gaps[:2]:
        out.append(f"Move somebody who clears \"{rule}\" onto {where}.")
    for where, name in isolated[:2]:
        out.append(f"Pair {name} on {where} with a stronger hand, or move one there.")
    if over_hours:
        where, over = max(over_hours, key=lambda x: x[1])
        out.append(f"Trim about {over:.0f}h from {where} to get back under target.")
    if overloaded:
        out.append(
            f"Spread the busy shifts — {_names(sorted(overloaded)[:2])} " +
            _plural(len(overloaded), "is", "are") + " carrying too many.")
    for name, days in sorted(long_runs)[:1]:
        out.append(f"Give {name} a day off — {days} in a row this week.")
    if unrated:
        out.append("Rate the unscored staff; several shift figures are reading low "
                   "only because nothing was counted for them.")
    return out[:6]


# ── Turning a finished schedule into contexts ──────────────────────────────

def daypart_of(shift_start: str) -> str:
    """Morning or night, on a 3pm cutoff.

    Both clock formats parse. Every CSV a client uploads is 24-hour and
    every schedule this product generates is 12-hour, and a parser that
    only read one of them classified the other as night — which made the
    entire morning crew invisible. An unreadable value says so rather than
    guessing, because a wrong daypart silently reassigns people between
    shifts that are judged against different profiles.
    """
    raw = (shift_start or "").strip().lower().replace(" ", "")
    if not raw:
        return "unknown"
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            return "night" if datetime.strptime(raw, fmt).hour >= 15 else "morning"
        except ValueError:
            continue
    return "unknown"


def _day_name(date_str: str, fallback: str = "") -> str:
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
    except (ValueError, TypeError):
        return fallback or ""


def _end_minutes(value: str) -> int:
    """Minutes past midnight for a shift end, or -1 when it cannot be read.

    A shift ending after midnight is deliberately NOT wrapped: for the one
    question this answers — which bucket closes the day — a 1:00am end is
    the last one out, and treating it as 60 would make the lunch crew the
    closers.
    """
    raw = (value or "").strip().lower().replace(" ", "")
    if not raw:
        return -1
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(raw, fmt)
            minutes = t.hour * 60 + t.minute
            return minutes + 1440 if t.hour < 5 else minutes
        except ValueError:
            continue
    return -1


def _row_hours(row: dict) -> float:
    try:
        return float(row.get("scheduled_hours") or 0)
    except (ValueError, TypeError):
        return 0.0


def build_contexts(rows: list, profiles: list = None, **signals) -> list:
    """Bucket a finished schedule into the shifts the engine scores.

    One context per (date, daypart) — the whole shift across every role,
    not per role. Coverage and strength look at roles from the inside;
    leadership, training and fatigue are properties of the team as a whole
    and cannot be seen one role at a time.
    """
    profiles = profiles if profiles is not None else BUILTIN_PROFILES
    buckets, day_hours = {}, {}
    for row in rows or []:
        name = (row.get("employee") or "").strip()
        date = (row.get("date") or "").strip()
        if not (name and date):
            continue
        part = daypart_of(row.get("shift_start", ""))
        buckets.setdefault((date, part), []).append(row)
        day_hours[date] = day_hours.get(date, 0.0) + _row_hours(row)

    # Which bucket actually closes each date, so a closing requirement binds
    # the shift it means rather than every shift of the day.
    closes_on = {}
    for (date, part), shift_rows in buckets.items():
        latest = max((_end_minutes(r.get("shift_end")) for r in shift_rows), default=-1)
        if latest > closes_on.get(date, (-1, None))[0]:
            closes_on[date] = (latest, part)

    # Who works what across the whole week, so fatigue and fairness can see
    # past the one shift they are scoring. Seeded with the tail of the
    # PREVIOUS schedule, because a run of nine days looks like five when the
    # engine can only see inside its own seven-day box — and the week
    # boundary is exactly where that matters.
    week_assignments = {}
    for name, entries in (signals.get("prior_week_assignments") or {}).items():
        week_assignments.setdefault(name, []).extend(entries)
    demand_by_date = signals.get("demand_by_date") or {}
    for (date, part), shift_rows in buckets.items():
        day = _day_name(date)
        demand = resolve_profile(day, part, profiles).demand
        override = demand_from_pct((demand_by_date.get(date) or {}).get("lift_pct"))
        if override and DEMAND_RANK[override] > DEMAND_RANK.get(demand, 1):
            demand = override
        closing_part = closes_on.get(date, (None, None))[1]
        for row in shift_rows:
            name = (row.get("employee") or "").strip()
            if not name:
                continue
            entries = week_assignments.setdefault(name, [])
            if not any(e["date"] == date and e["daypart"] == part for e in entries):
                entries.append({"date": date, "daypart": part, "day": day, "demand": demand,
                                "weekend": day in ("Friday", "Saturday", "Sunday"),
                                "closing": part == closing_part, "hours": _row_hours(row)})
            else:
                for e in entries:
                    if e["date"] == date and e["daypart"] == part:
                        e["hours"] = (e.get("hours") or 0) + _row_hours(row)

    targets = signals.get("daily_target_hours") or {}
    demand_by_day = signals.get("demand_by_day") or {}
    open_times = signals.get("open_times") or {}
    close_times = signals.get("close_times") or {}
    role_floors_all = signals.get("role_floors") or {}
    demand_curves = signals.get("demand_curve") or {}
    day_rows_by_date = {}
    for row in rows or []:
        if (row.get("employee") or "").strip() and (row.get("date") or "").strip():
            day_rows_by_date.setdefault(row["date"].strip(), []).append(row)

    contexts = []
    for (date, part), shift_rows in sorted(buckets.items()):
        day = _day_name(date, shift_rows[0].get("day", ""))
        profile = resolve_profile(day, part, profiles)
        lift = (demand_by_date.get(date) or {}).get("lift_pct")
        if lift is not None:
            bumped = demand_from_pct(lift)
            if bumped and DEMAND_RANK[bumped] > DEMAND_RANK.get(profile.demand, 1):
                profile = _clone(profile)
                profile.demand = bumped
                profile.source = "what you told us about this date"
        floors_here = {}
        for role, spec in role_floors_all.items():
            dspec = (spec.get("days") or {}).get(day) or {}
            n = dspec.get(part) if part in dspec else spec.get(part)
            if n:
                floors_here[role] = int(n)
        contexts.append(ShiftContext(
            date=date, day=day, daypart=part, rows=shift_rows,
            profile=profile,
            is_closing=(closes_on.get(date, (None, None))[1] == part),
            open_minutes=_slot_minutes(open_times.get(day)) if open_times.get(day) else None,
            close_minutes=_slot_minutes(close_times.get(day)) if close_times.get(day) else None,
            day_rows=day_rows_by_date.get(date, []),
            role_floors=floors_here,
            demand_curve=demand_curves.get(day) or {},
            reliability=signals.get("reliability") or {},
            pairs=signals.get("pairs") or {},
            max_shift_hours=signals.get("max_shift_hours"),
            weekly_ceiling=signals.get("weekly_ceiling"),
            hours_limits=signals.get("hours_limits") or {},
            elsewhere=signals.get("elsewhere") or {},
            constraints=signals.get("constraints") or {},
            flagged=signals.get("flagged") or set(),
            scores=signals.get("scores") or {},
            tenure=signals.get("tenure") or {},
            leader_flags=signals.get("leader_flags") or {},
            leader_rules=signals.get("leader_rules") or [],
            cross_trained=signals.get("cross_trained") or {},
            typical_headcount=(signals.get("typical_headcount") or {}).get((day, part), {}),
            role_minimums=signals.get("role_minimums") or {},
            demand_pct=demand_by_day.get(day),
            target_hours=targets.get(date),
            day_hours=day_hours.get(date, 0.0),
            week_assignments=week_assignments,
            prior_pattern=signals.get("prior_pattern") or {},
            availability=signals.get("availability") or {},
        ))
    return contexts


def score_rows(rows: list, profiles: list = None, weights: dict = None,
               **signals) -> dict:
    """One call from a finished schedule to a full evaluation.

    This is what every caller outside this module uses: generation, a
    manager's live edit, and the what-if loop all go through here, so they
    can never drift apart on how a schedule is judged.
    """
    contexts = build_contexts(rows, profiles=profiles, **signals)
    people = {(r.get("employee") or "").strip() for r in rows or []}
    people.discard("")
    scores = signals.get("scores") or {}
    conf_signals = {
        "scheduled_people": len(people),
        "rated_people": len([p for p in people if scores.get(p) is not None]),
        "has_tenure": bool(signals.get("tenure")),
        "has_demand": bool(signals.get("demand_by_day")),
        "has_availability": bool(signals.get("availability")),
        "has_constraints": bool(signals.get("constraints")),
        "has_profiles": bool(profiles) and profiles is not BUILTIN_PROFILES,
        "rows_needing_review": signals.get("rows_needing_review"),
        "dropped_rows": signals.get("dropped_rows"),
        "constrained_people": len(signals.get("availability") or {}),
        "unsatisfiable": signals.get("unsatisfiable"),
    }
    result = evaluate_schedule(contexts, weights=weights, signals=conf_signals)
    result["people"] = sorted(people)
    return result


# ── What-if: comparing candidate schedules ─────────────────────────────────
#
# One Claude call produces one schedule. Asking for three costs three times
# the money and the owner's patience, so the comparison here is built the
# cheap way instead: take the schedule that came back and try swapping who
# works which shift, keeping headcount, hours and every role identical.
#
# That restriction is what makes it safe. A swap of two people in the same
# role changes nobody's coverage, nobody's role mix and nobody's cost — so
# labor efficiency and coverage cannot move, and the only thing under test
# is whether a better TEAM could have been built from the same bodies. It
# is also why the tradeoffs are explainable: exactly two names changed.

WEEKLY_HOURS_CEILING = 40.0
MAX_CANDIDATE_EVALUATIONS = 60
MIN_IMPROVEMENT = 1


def _weekly_hours(rows: list) -> dict:
    out = {}
    for row in rows:
        name = (row.get("employee") or "").strip().lower()
        if name:
            out[name] = out.get(name, 0.0) + _row_hours(row)
    return out


def _unavailable(availability: dict, name: str, day: str) -> bool:
    blocked = availability.get(name) or availability.get((name or "").strip()) or set()
    return (day or "").strip().lower() in {str(d).strip().lower() for d in blocked}


def _span(row):
    """(start, end) minutes from the row's date midnight; end may pass 1440."""
    try:
        base = datetime.strptime(row.get("date", ""), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None, None
    s, e = _slot_minutes(row.get("shift_start")), _slot_minutes(row.get("shift_end"))
    if s is None or e is None:
        return None, None
    start = base + timedelta(minutes=s)
    end = base + timedelta(minutes=e if e > s else e + 24 * 60)
    return start, end


class _SwapIndex:
    """Everything a legality check needs, computed once per pass.

    The first version of this rebuilt the whole weekly-hours map and
    rescanned every row inside the check itself, which made the check O(n)
    and the pass O(n^3): a 300-person roster spent nineteen seconds here
    and evaluated nothing. Each lookup below is now O(1) and the pass only
    ever pairs rows within the same role.

    The extra rules — approved time off, daypart windows, per-person hours
    limits, hours already published this payroll week, rest between shifts,
    a deactivated name — arrive as plain dicts in `rules` so this module
    stays free of database code.
    """

    def __init__(self, rows: list, availability: dict, constraints: dict, rules: dict = None):
        self.rows = rows
        self.availability = availability or {}
        self.constrained = {n.strip().lower() for n, note in (constraints or {}).items()
                            if n and str(note or "").strip()}
        rules = rules or {}
        self.blocked = {k.lower(): v for k, v in (rules.get("blocked_dates") or {}).items()}
        self.daypart_avail = {k.lower(): v for k, v in (rules.get("daypart_avail") or {}).items()}
        self.limits = {k.lower(): v for k, v in (rules.get("hours_limits") or {}).items()}
        self.base_hours = {k.lower(): float(v or 0) for k, v in (rules.get("base_hours") or {}).items()}
        self.ceiling = float(rules.get("weekly_ceiling") or WEEKLY_HOURS_CEILING)
        self.min_rest = float(rules.get("min_rest_hours") or 0)
        self.inactive = {str(n).lower() for n in (rules.get("inactive") or [])}
        self.hours = _weekly_hours(rows)
        self.working = set()
        self.by_role = {}
        self.spans_by_person = {}
        for i, row in enumerate(rows):
            name = (row.get("employee") or "").strip().lower()
            date = row.get("date") or ""
            if name and date:
                self.working.add((name, date))
                s, e = _span(row)
                if s:
                    self.spans_by_person.setdefault(name, []).append((i, s, e))
            role = (row.get("role") or "").strip().lower()
            if name and role:
                self.by_role.setdefault(role, []).append(i)
        for name, entries in (rules.get("base_rows") or {}).items():
            for r in entries:
                s, e = _span(r)
                if s:
                    self.spans_by_person.setdefault(name.lower(), []).append((None, s, e))

    def cap(self, name_low: str) -> float:
        lim = self.limits.get(name_low)
        if lim and lim[1]:
            return min(float(lim[1]), self.ceiling)
        return self.ceiling

    def total_hours(self, name_low: str) -> float:
        return self.hours.get(name_low, 0.0) + self.base_hours.get(name_low, 0.0)

    def person_fits(self, name: str, row: dict, ignore_index=None) -> bool:
        """Could `name` work `row` (date, daypart, rest), leaving hours aside?"""
        low = (name or "").strip().lower()
        if not low or low in self.inactive or low in self.constrained:
            return False
        date = row.get("date") or ""
        day = _day_name(date, row.get("day", ""))
        if _unavailable(self.availability, name, day):
            return False
        if date in (self.blocked.get(low) or {}):
            return False
        choice = (self.daypart_avail.get(low) or {}).get(day)
        part = daypart_of(row.get("shift_start", ""))
        if choice == "off" or (choice in ("morning", "night") and part not in ("unknown", choice)):
            return False
        if self.min_rest:
            s, e = _span(row)
            if s:
                for idx, ps, pe in self.spans_by_person.get(low, []):
                    if idx is not None and idx == ignore_index:
                        continue
                    if ps >= e:
                        gap = (ps - e).total_seconds() / 3600
                    elif pe <= s:
                        gap = (s - pe).total_seconds() / 3600
                    else:
                        return False
                    # Same calendar date is a double shift, not a rest
                    # breach — the rule is the overnight turnaround.
                    if ps.date() == s.date():
                        continue
                    if gap < self.min_rest:
                        return False
        return True

    def pairs(self):
        """Only same-role pairs are ever candidates, so never enumerate the rest."""
        for indices in self.by_role.values():
            for a in range(len(indices)):
                for b in range(a + 1, len(indices)):
                    yield indices[a], indices[b]

    def legal(self, i: int, j: int, scores: dict) -> bool:
        a, b = self.rows[i], self.rows[j]
        name_a = (a.get("employee") or "").strip()
        name_b = (b.get("employee") or "").strip()
        if not name_a or not name_b:
            return False
        low_a, low_b = name_a.lower(), name_b.lower()
        if low_a == low_b:
            return False
        if (a.get("date") or "") == (b.get("date") or ""):
            return False

        # A staff constraint is the one rule the generator's prompt calls
        # absolute, and it is free text this engine cannot read. It can still
        # refuse to move the person it applies to, which is the honest
        # answer: better no suggestion than a persuasive illegal one.
        if low_a in self.constrained or low_b in self.constrained:
            return False

        if (low_b, a.get("date")) in self.working or (low_a, b.get("date")) in self.working:
            return False
        # b takes a's shift and a takes b's: date, daypart window, time off,
        # rest — every rule the constraint set knows, for both directions.
        if not self.person_fits(name_b, a, ignore_index=j) or not self.person_fits(name_a, b, ignore_index=i):
            return False

        if scores is not None:
            rated_a = scores.get(name_a) is not None
            rated_b = scores.get(name_b) is not None
            if rated_a != rated_b:
                return False

        delta = _row_hours(b) - _row_hours(a)
        if self.total_hours(low_a) + delta > self.cap(low_a):
            return False
        if self.total_hours(low_b) - delta > self.cap(low_b):
            return False
        return True

    def replacement_legal(self, i: int, name: str) -> bool:
        """Could `name` take row i outright (nobody else moves)?"""
        row = self.rows[i]
        low = (name or "").strip().lower()
        current = (row.get("employee") or "").strip().lower()
        if not low or low == current:
            return False
        if (low, row.get("date")) in self.working:
            return False
        if not self.person_fits(name, row):
            return False
        if self.total_hours(low) + _row_hours(row) > self.cap(low):
            return False
        return True


def _swap_is_legal(rows: list, i: int, j: int, availability: dict,
                   scores: dict = None, constraints: dict = None, rules: dict = None) -> bool:
    """Can these two rows trade employees without breaking anything?

    Six ways a swap goes wrong: an unavailable day, a staff constraint, a
    person already working that shift, a week pushed over forty hours, a
    double booking, and — the subtle one — trading a rated employee against
    an unrated one, which always "improves" the score because an unrated
    person counts as nothing and would have the engine advising an owner
    not to schedule the people they have not got round to rating.
    """
    return _SwapIndex(rows, availability, constraints, rules).legal(i, j, scores)


def compare_candidates(rows: list, profiles: list = None, weights: dict = None,
                       max_evaluations: int = MAX_CANDIDATE_EVALUATIONS,
                       **signals) -> dict:
    """Hill-climb same-role swaps and report what the alternatives cost.

    Returns the winning rows, the baseline and final scores, and one line
    per accepted swap saying which dimensions moved. A run that finds
    nothing is a real result and says so — but only ever about the
    candidates it actually tried, never about the whole space.
    """
    baseline = score_rows(rows, profiles=profiles, weights=weights, **signals)
    if not baseline.get("checked"):
        return {"ran": False, "reason": baseline.get("reason"), "baseline": baseline,
                "best": baseline, "rows": rows, "swaps": [], "evaluated": 0,
                "legal_swaps": 0}

    availability = signals.get("availability") or {}
    constraints = signals.get("constraints") or {}
    rules = signals.get("rules") or {}
    roster = [n for n in (signals.get("roster") or []) if n]
    scores = signals.get("scores") or {}
    current_rows = [dict(r) for r in rows]
    current = baseline
    swaps, evaluated = [], 0
    legal_total = 0

    improved = True
    while improved and evaluated < max_evaluations:
        improved = False
        index = _SwapIndex(current_rows, availability, constraints, rules)
        # Keep only the most promising candidates rather than sorting the
        # whole space: the widest score gap is where an improvement lives,
        # and sorting a million pairs to use sixty was the other half of
        # the cost.
        best_pairs = []
        legal_here = 0
        for i, j in index.pairs():
            if not index.legal(i, j, scores):
                continue
            legal_here += 1
            gap = abs((scores.get((current_rows[i].get("employee") or "").strip()) or 0)
                      - (scores.get((current_rows[j].get("employee") or "").strip()) or 0))
            if len(best_pairs) < max_evaluations:
                best_pairs.append((gap, i, j))
                if len(best_pairs) == max_evaluations:
                    best_pairs.sort(reverse=True)
            elif gap > best_pairs[-1][0]:
                best_pairs[-1] = (gap, i, j)
                best_pairs.sort(reverse=True)
        # A second kind of move: somebody on the roster who is not working
        # that date takes the row outright. Only people seen in that role
        # this week (or cross-trained into it) are candidates, so a cook is
        # never proposed for the bar.
        role_people = {}
        for r in current_rows:
            role_people.setdefault((r.get("role") or "").strip().lower(), set()).add((r.get("employee") or "").strip())
        cross = signals.get("cross_trained") or {}
        roster_low = {n.strip().lower() for n in roster}
        for i, row in enumerate(current_rows) if roster else []:
            role = (row.get("role") or "").strip().lower()
            cur = (row.get("employee") or "").strip()
            pool = {n for n in role_people.get(role, set()) if n.strip().lower() in roster_low}
            pool |= {n for n in roster if any(x.strip().lower() == role for x in (cross.get(n) or []))}
            for name in pool:
                if name == cur or not index.replacement_legal(i, name):
                    continue
                if scores and (scores.get(cur) is None) != (scores.get(name) is None):
                    continue
                legal_here += 1
                gap = abs((scores.get(cur) or 0) - (scores.get(name) or 0))
                entry = (gap, i, ("replace", name))
                if len(best_pairs) < max_evaluations:
                    best_pairs.append(entry)
                    if len(best_pairs) == max_evaluations:
                        best_pairs.sort(reverse=True, key=lambda t: t[0])
                elif gap > best_pairs[-1][0]:
                    best_pairs[-1] = entry
                    best_pairs.sort(reverse=True, key=lambda t: t[0])
        legal_total = max(legal_total, legal_here)
        best_pairs.sort(reverse=True, key=lambda t: t[0])

        for _gap, i, j in best_pairs:
            if evaluated >= max_evaluations:
                break
            trial = [dict(r) for r in current_rows]
            if isinstance(j, tuple):
                trial[i]["employee"] = j[1]
            else:
                trial[i]["employee"], trial[j]["employee"] = \
                    current_rows[j]["employee"], current_rows[i]["employee"]
            candidate = score_rows(trial, profiles=profiles, weights=weights, **signals)
            evaluated += 1
            if not candidate.get("checked"):
                continue
            gain = candidate["score"] - current["score"]
            if gain >= MIN_IMPROVEMENT:
                if isinstance(j, tuple):
                    swaps.append(_describe_replacement(current_rows[i], j[1], current, candidate, gain))
                else:
                    swaps.append(_describe_swap(current_rows[i], current_rows[j],
                                                current, candidate, gain))
                current_rows, current = trial, candidate
                improved = True
                break

    return {
        "ran": True,
        "evaluated": evaluated,
        "legal_swaps": legal_total,
        "baseline_score": baseline["score"],
        "best_score": current["score"],
        "improvement": current["score"] - baseline["score"],
        "swaps": swaps,
        "rows": current_rows,
        "baseline": baseline,
        "best": current,
        "verdict": _candidate_verdict(baseline, current, swaps, evaluated, legal_total),
    }


def _describe_swap(row_a: dict, row_b: dict, before: dict, after: dict, gain: int) -> dict:
    """Which two people traded shifts, and which dimensions it moved."""
    moved = []
    before_dims = {d["key"]: d["score"] for d in before.get("dimensions") or []}
    for d in after.get("dimensions") or []:
        delta = d["score"] - before_dims.get(d["key"], d["score"])
        if delta:
            moved.append(f"{d['label']} {delta:+d}")
    return {
        "from": {"employee": row_a.get("employee"), "date": row_a.get("date"),
                 "day": row_a.get("day"), "role": row_a.get("role")},
        "to": {"employee": row_b.get("employee"), "date": row_b.get("date"),
               "day": row_b.get("day"), "role": row_b.get("role")},
        "gain": gain,
        "moved": moved,
        "reason": (f"Swapped {row_a.get('employee')} and {row_b.get('employee')} between "
                   f"{row_a.get('day') or row_a.get('date')} and "
                   f"{row_b.get('day') or row_b.get('date')} "
                   f"({row_a.get('role')}) — overall {gain:+d}"
                   + (", " + ", ".join(moved) if moved else "") + "."),
    }


def _describe_replacement(row: dict, name: str, before: dict, after: dict, gain: int) -> dict:
    moved = []
    before_dims = {d["key"]: d["score"] for d in before.get("dimensions") or []}
    for d in after.get("dimensions") or []:
        delta = d["score"] - before_dims.get(d["key"], d["score"])
        if delta:
            moved.append(f"{d['label']} {delta:+d}")
    return {
        "from": {"employee": row.get("employee"), "date": row.get("date"), "day": row.get("day"), "role": row.get("role")},
        "to": {"employee": name, "date": row.get("date"), "day": row.get("day"), "role": row.get("role")},
        "gain": gain, "moved": moved, "kind": "replace",
        "reason": (f"Put {name} on {row.get('day') or row.get('date')} {row.get('role')} instead of "
                   f"{row.get('employee')} — overall {gain:+d}" + (", " + ", ".join(moved) if moved else "") + "."),
    }


def apply_fixes(rows: list, violations: list, profiles: list = None, weights: dict = None,
                max_evaluations: int = MAX_CANDIDATE_EVALUATIONS, **signals) -> dict:
    """Repair the rows that break a hard rule by putting somebody legal on
    them, choosing the replacement that scores best. Rows nobody legal can
    take are left as they are and named, never dropped: a coverage gap the
    owner can see beats a silently thinner week.

    Returns {rows, fixes: [{index, from, to, kind, reason}], unfixed: [...]}.
    """
    rows = [dict(r) for r in rows]
    availability = signals.get("availability") or {}
    constraints = signals.get("constraints") or {}
    rules = signals.get("rules") or {}
    roster = [n for n in (signals.get("roster") or []) if n]
    cross = signals.get("cross_trained") or {}
    fixes, unfixed, evaluated = [], [], 0
    hard = [v for v in (violations or []) if v.get("hard")]
    # over_max_hours lands on EVERY row of the person's payroll week. Fixing
    # each one would strip them of the whole week (48h → 0h); only the
    # excess should move. Keep the latest rows until the week fits, and
    # drop the rest of that person's over-hours entries from the work list.
    over = {}
    for v in hard:
        if v.get("kind") == "over_max_hours":
            over.setdefault((v.get("employee") or "").strip().lower(), []).append(v)
    if over:
        probe = _SwapIndex(rows, availability, constraints, rules)
        keep = set()
        for low, vs in over.items():
            cap = probe.cap(low)
            excess = probe.total_hours(low) - cap
            vs_sorted = sorted(vs, key=lambda x: (rows[x["index"]].get("date") or "", _slot_minutes(rows[x["index"]].get("shift_start") or "") or 0),
                               reverse=True)
            removed = 0.0
            for v in vs_sorted:
                if removed >= excess - 0.05:
                    break
                keep.add(id(v))
                removed += _row_hours(rows[v["index"]])
        hard = [v for v in hard if v.get("kind") != "over_max_hours" or id(v) in keep]
    seen_idx = set()
    for v in sorted(hard, key=lambda x: (0 if x.get("no_show") else 1, x.get("index", 0))):
        i = v.get("index")
        if i is None or i in seen_idx or i >= len(rows):
            continue
        seen_idx.add(i)
        row = rows[i]
        index = _SwapIndex(rows, availability, constraints, rules)
        role = (row.get("role") or "").strip().lower()
        cur = (row.get("employee") or "").strip()
        pool = {(r.get("employee") or "").strip() for r in rows if (r.get("role") or "").strip().lower() == role}
        pool |= {n for n in roster if any(x.strip().lower() == role for x in (cross.get(n) or []))}
        pool |= {n for n in roster if not cross.get(n) and not any(r.get("employee") == n for r in rows)} if not pool else set()
        candidates = [n for n in sorted(pool) if n != cur and index.replacement_legal(i, n)]
        best, best_score = None, None
        baseline = score_rows(rows, profiles=profiles, weights=weights, **signals)
        for name in candidates:
            if evaluated >= max_evaluations:
                break
            trial = [dict(r) for r in rows]
            trial[i]["employee"] = name
            cand = score_rows(trial, profiles=profiles, weights=weights, **signals)
            evaluated += 1
            sc = cand.get("score") if cand.get("checked") else (baseline.get("score") or 0)
            if best is None or (sc or 0) > (best_score or -1):
                best, best_score = name, sc
        if best:
            rows[i]["employee"] = best
            note = (rows[i].get("notes") or "").strip()
            rows[i]["notes"] = (note + f" (was {cur} — {v.get('label') or v.get('kind')})").strip()
            fixes.append({"index": i, "from": cur, "to": best, "kind": v.get("kind"),
                          "reason": f"{cur} — {v.get('detail') or v.get('label')}; {best} can take it."})
        else:
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"),
                            "reason": f"Nobody on the roster can legally take {cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift."})
    return {"rows": rows, "fixes": fixes, "unfixed": unfixed, "evaluated": evaluated}


def _candidate_verdict(baseline: dict, best: dict, swaps: list,
                       evaluated: int, legal: int = 0) -> str:
    """What the comparison actually established, and nothing more.

    The first version said "This is the strongest team available from this
    roster" after sixty evaluations. On a two-hundred-person week that is
    sixty of roughly sixteen thousand legal swaps, and the sentence was the
    most confident thing on the panel and the least supported. It now only
    makes that claim when the run genuinely exhausted the candidates.
    """
    if not evaluated:
        return ("No alternative arrangement was possible — every swap would have "
                "double-booked somebody, broken an availability or a staff note, "
                "or pushed a week past forty hours.")
    if not swaps:
        if legal and evaluated >= legal:
            return (f"Tried every one of the {legal} possible swaps and none scored "
                    "better. This is the strongest team available from this roster.")
        scope = (f" out of {legal:,} possible" if legal > evaluated else "")
        return (f"Tried the {evaluated} most promising swaps{scope} and none scored "
                "better. A wider search might still find something.")
    names = ", ".join(f"{s['from']['employee']}/{s['to']['employee']}" for s in swaps[:3])
    tail = (f", out of {legal:,} possible" if legal > evaluated else "")
    return (f"{len(swaps)} " + _plural(len(swaps), "swap") +
            f" raised overall quality from {baseline['score']} to {best['score']} "
            f"({names}), from {evaluated} tried{tail}.")


# ── Assembling the profile set for one restaurant ──────────────────────────

def demand_from_pct(vs_average_pct) -> str | None:
    """A demand level from this restaurant's own sales, not from a guess.

    The built-in profiles assume a busy Friday because most restaurants
    have one. Plenty do not — a lunch-counter's Friday night is dead — and
    asserting it anyway is exactly the kind of invented fact that has had
    to be removed from this codebase repeatedly. Where real sales history
    exists it overrides the assumption.
    """
    if vs_average_pct is None:
        return None
    pct = float(vs_average_pct)
    if pct >= 25:
        return "peak"
    if pct >= 8:
        return "high"
    if pct <= -15:
        return "low"
    return "normal"


def profiles_from_config(stored: list = None, default_strength: dict = None,
                         default_leader_rules: list = None,
                         demand_by_day: dict = None) -> list:
    """The profile set the engine should judge this restaurant against.

    Three layers, narrowest last:

      the built-in profiles, or the restaurant's own if it has configured any
      its own sales history, which corrects the built-ins' demand guesses
      the restaurant-wide strength targets and leader rules, filling any
        role a profile does not name for itself

    That last layer is what keeps the flat targets editor meaningful: an
    owner who sets "Bartender 10" once has it apply everywhere, and only
    the shifts they deliberately profile differ from it.
    """
    profiles = [_clone(p) for p in (stored or BUILTIN_PROFILES)]
    using_builtins = not stored

    for profile in profiles:
        # Correct a built-in's assumed demand from real sales. A profile an
        # owner configured is left exactly as they set it.
        if using_builtins and demand_by_day:
            days = profile.days or list(demand_by_day)
            observed = [demand_from_pct(demand_by_day.get(d)) for d in days]
            observed = [o for o in observed if o]
            if observed:
                profile.demand = max(observed, key=lambda level: DEMAND_RANK[level])
                profile.source = "your sales history"

        merged = dict(default_strength or {})
        merged.update(profile.min_strength or {})
        profile.min_strength = merged

        if default_leader_rules and not profile.leader_roles and profile.requires_leader:
            profile.leader_roles = sorted(
                {(r.get("role") or "").strip() for r in default_leader_rules
                 if (r.get("role") or "").strip()})

    # A catch-all always has to exist, carrying the restaurant-wide targets.
    #
    # Without it, an owner who added a single "Monday lunch" profile lost
    # their flat per-role targets on every OTHER shift of the week: nothing
    # matched, resolve_profile handed back a bare default, and the strength
    # dimension quietly stopped applying. The targets editor is the
    # baseline and profiles override it — that contract only holds if the
    # baseline is reachable from every shift.
    if not any(not p.days and not p.daypart for p in profiles):
        profiles.append(ShiftProfile(
            key="default", label="Standard shift",
            min_strength=dict(default_strength or {}),
            source="your overall targets"))
    return profiles


def _clone(profile: ShiftProfile) -> ShiftProfile:
    return ShiftProfile(
        key=profile.key, label=profile.label, days=list(profile.days or []),
        daypart=profile.daypart, demand=profile.demand, min_quality=profile.min_quality,
        min_strength=dict(profile.min_strength or {}),
        critical_positions=dict(profile.critical_positions or {}),
        requires_leader=profile.requires_leader, leader_roles=list(profile.leader_roles or []),
        leader_min_score=profile.leader_min_score, experience_mix=profile.experience_mix,
        training_allowed=profile.training_allowed, weights=dict(profile.weights or {}),
        priority=profile.priority, source=profile.source,
    )


# The Operational Score scale, duplicated here rather than imported so this
# module stays free of database code. models.SCORE_MIN/SCORE_MAX are the
# same numbers and a test holds them together.
SCORE_SCALE_MIN, SCORE_SCALE_MAX = 1, 5
MAX_WEIGHT = 100


def _num(value, low, high, default):
    """A number inside its own range, or the default if it is not one.

    Every profile field goes through this. Without it a negative strength
    target saved happily and was then always met, so an owner believed a
    bar was enforced when it was inert — the worst kind of setting, one
    that looks configured and does nothing.
    """
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n or n in (float("inf"), float("-inf")):
        return default
    return max(low, min(high, n))


def profile_from_dict(data: dict) -> ShiftProfile:
    """One stored profile row into the dataclass, tolerant of missing keys
    and refusing to carry a value the engine could never act on."""
    return ShiftProfile(
        key=str(data.get("key") or "custom"),
        label=str(data.get("label") or "Custom shift"),
        days=[str(d) for d in (data.get("days") or []) if d],
        daypart=(data.get("daypart") or None),
        demand=(data.get("demand") if data.get("demand") in DEMAND_LEVELS else "normal"),
        min_quality=int(_num(data.get("min_quality"), 0, 100, 70)),
        min_strength={str(r): _num(v, 0, 200, 0)
                      for r, v in (data.get("min_strength") or {}).items()
                      if _num(v, 0, 200, 0) > 0},
        critical_positions={str(r): int(_num(v, 0, 99, 0))
                            for r, v in (data.get("critical_positions") or {}).items()
                            if int(_num(v, 0, 99, 0)) > 0},
        requires_leader=bool(data.get("requires_leader")),
        leader_roles=[str(r) for r in (data.get("leader_roles") or []) if r],
        leader_min_score=_num(data.get("leader_min_score"),
                              SCORE_SCALE_MIN, SCORE_SCALE_MAX, 4.0),
        experience_mix=_num(data.get("experience_mix"), 0.0, 1.0, 0.4),
        training_allowed=bool(data.get("training_allowed")),
        weights={str(k): _num(v, 0, MAX_WEIGHT, 0)
                 for k, v in (data.get("weights") or {}).items() if k in DIMENSIONS},
        priority=int(_num(data.get("priority"), 0, 99, 0)),
        source=str(data.get("source") or "restaurant"),
    )


def profile_to_dict(profile: ShiftProfile) -> dict:
    return {"key": profile.key, "label": profile.label, "days": list(profile.days or []),
            "daypart": profile.daypart, "demand": profile.demand,
            "min_quality": profile.min_quality,
            "min_strength": dict(profile.min_strength or {}),
            "critical_positions": dict(profile.critical_positions or {}),
            "requires_leader": profile.requires_leader,
            "leader_roles": list(profile.leader_roles or []),
            "leader_min_score": profile.leader_min_score,
            "experience_mix": profile.experience_mix,
            "training_allowed": profile.training_allowed,
            "weights": dict(profile.weights or {}), "priority": profile.priority,
            "source": profile.source}
