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

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache

from time_utils import BUSINESS_DAY_START_HOUR
from types import SimpleNamespace

# The scale every dimension reports on. Percentages, because "Coverage 100%"
# is a sentence an owner can act on and "Coverage 1.0" is not.
SCORE_MAX = 100

def _role_word_kept(w: str) -> bool:
    return w.lower() in ("am", "pm") or (len(w) > 1 and w.isupper())


def role_words(role, count: int = 1, default: str = "") -> str:
    """A role inside a sentence: ordinary words in lower case, AM/PM and
    any word the owner wrote in capitals kept that way — "Bartender AM" →
    "bartender AM", never "bartender am" (Will, 10/2/26). A count other
    than one pluralises the last ordinary word: "2 bartenders AM"."""
    words = str(role or "").split()
    if not words:
        return default
    out = [w.upper() if w.lower() in ("am", "pm") else (w if _role_word_kept(w) else w.lower()) for w in words]
    if count != 1:
        idx = [i for i, w in enumerate(out) if not _role_word_kept(w)]
        i = idx[-1] if idx else len(out) - 1
        w = out[i]
        out[i] = w + ("es" if w.endswith(("s", "sh", "ch", "x")) else "s")
    return " ".join(out)


# A role's family: the role with its daypart words taken off, so the job
# codes a POS splits by shift ("Server AM", "Server PM", "PM Bartender",
# "Host - Lunch") are one role to every rule that counts people in a role —
# requirements, floors, the section cap, leader rules, cross-training, the
# owner's "two servers Saturday" (schedule audit 10/3/26 D-13, D-14, SQ-10).
# The restaurant's own map (restaurants.role_families_json, {role: family})
# wins; a role the words would empty keeps its name.
_DAYPART_WORDS = frozenset({"am", "pm", "a.m.", "p.m.", "lunch", "dinner", "brunch", "breakfast", "day", "night",
                            "morning", "evening", "late", "overnight", "weekend", "weekday", "wknd", "open",
                            "opening", "opener", "close", "closing", "closer"})


def role_family(role, families=None) -> str:
    """'Server AM' → 'server'; 'Bartender - PM' → 'bartender'; the
    restaurant's map ({role lower: family}) first. Lower case."""
    low = " ".join(str(role or "").strip().lower().split())
    if not low:
        return ""
    if families:
        mapped = families.get(low)
        if mapped:
            return " ".join(str(mapped).strip().lower().split())
    return _family_words(low)


@lru_cache(maxsize=4096)
def _family_words(low: str) -> str:
    """The family a role name's own words give (role_family without the
    restaurant's map) — pure, so remembered: the scorer asks it for every
    person on every shift it scores."""
    cleaned = []
    for w in low.replace("(", " ").replace(")", " ").replace("/", " ").replace("-", " ").replace("_", " ").split():
        if w.strip(".,:;") in _DAYPART_WORDS:
            continue
        cleaned.append(w.strip(".,:;"))
    return " ".join(w for w in cleaned if w) or low


def name_key(name) -> str:
    """One spelling of a person's name for every comparison: case and
    spacing ignored — the key Constraints.salaried and models.
    salaried_name_key use ("erik  baylis" is "Erik Baylis")."""
    return " ".join(str(name or "").lower().split())


def job_code_daypart(role):
    """'morning' for a job code named "... AM", 'night' for "... PM", else
    None: a rule or a target on an AM or PM job keeps to its own half of
    the day (owner, 10/2/26 — "Host AM" at dinner is nobody's). The same
    reading as models.leader_rule_daypart, here for the pure layer."""
    words = str(role or "").strip().lower().split()
    if words and words[-1] == "am":
        return "morning"
    if words and words[-1] == "pm":
        return "night"
    return None


# Demand levels, weakest to strongest. Profiles name one of these; the
# engine also derives one from the restaurant's own sales history when a
# profile does not pin it.
DEMAND_LEVELS = ("low", "normal", "high", "peak")
DEMAND_RANK = {level: i for i, level in enumerate(DEMAND_LEVELS)}

# How much a shift at each demand level counts toward the week's overall
# quality. A weak Saturday dinner is a worse week than a weak Monday lunch,
# and averaging them flat says otherwise.
DEMAND_WEIGHT = {"low": 0.6, "normal": 1.0, "high": 1.5, "peak": 2.0}

# The critical floors: a dimension under its floor caps the whole shift at
# its own score (evaluate_shift). They were constants written into each
# dimension (schedule audit 10/3/26 SQ-23); a profile now carries its own
# (ShiftProfile.floors) over these, so a quiet lunch and a peak Saturday can
# draw the line in different places and calibration or the owner can move
# one without a code change. A floor of 0 means the dimension never caps
# that profile's shifts. Leadership's floor binds only where an owner wrote
# a rule (dim_leadership).
CRITICAL_FLOORS = {"coverage": 70, "operational_strength": 55, "leadership": 60, "coverage_curve": 60}

# A shift carrying a hard breach the rule sweep found (signals
# "hard_breaches") is held at this, under the bar of every built-in profile
# (schedule audit 10/3/26 SQ-14). Not a dimension: it has nothing to weigh,
# only a ceiling, so a clean shift scores exactly what it did before.
HARD_BREACH_CAP = 50
HARD_RULES_KEY = "hard_rules"
HARD_RULES_LABEL = "Hard rules"
# A hard breach is a fact about the WEEK as well as its shift. Capping only
# the shift left a week with a Saturday-dinner manager gap at 92
# "excellent" (the demand-weighted mean barely moved), and the optimiser
# stopped there at "target reached" (schedule re-audit 10/4/26 SQ-2). A week
# with any hard breach on it is scaled into [0, WEEK_HARD_BREACH_CEILING] —
# under the "fair" band, so it reads "weak" — rather than clipped flat, so
# every pass that chooses by the score still sees a move that lifts a
# capped shift, or fixes one breach of three, as a gain.
WEEK_HARD_BREACH_CEILING = 64


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
    # True when this profile's demand should come from the restaurant's own
    # sales for the actual weekday being scored, not one level for every day
    # the profile covers. The built-in catch-all covers all seven days, and
    # taking the busiest of them made a quiet Sunday dinner a "peak" shift
    # whenever Saturday was one (profiles_from_config).
    per_day_demand: bool = False
    # {dimension: floor} over CRITICAL_FLOORS for the shifts this profile
    # governs (SQ-23): the owner's own, or what an applied calibration set.
    # A floor on any dimension caps the shift at its score; 0 = never caps.
    floors: dict = field(default_factory=dict)
    # What an applied calibration set on a profile no owner configured
    # ({"min_quality", "floors"}, restaurants.quality_tuning_json): applied
    # over the built-in's own bars, and kept when its demand level moves.
    tuning: dict = field(default_factory=dict)
    # One of the engine's own (BUILTIN_PROFILES, or the catch-all
    # profiles_from_config adds), never one the owner saved — whatever its
    # source text says (the editor round-trips a built-in's). Only these
    # follow a demand level and take tuning (SQ-15, SQ-22).
    builtin: bool = False

    def floor(self, key: str):
        """The critical floor of `key` on this profile's shifts: its own,
        else CRITICAL_FLOORS. None when the dimension never caps here."""
        if key in (self.floors or {}):
            try:
                value = int(self.floors[key])
            except (TypeError, ValueError):
                value = CRITICAL_FLOORS.get(key)
            return value if value else None
        return CRITICAL_FLOORS.get(key)

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
    ShiftProfile(key="default", label="Standard shift", demand="normal", min_quality=70, builtin=True),
    ShiftProfile(key="weekday_lunch", label="Weekday lunch", daypart="morning",
                 days=["Monday", "Tuesday", "Wednesday", "Thursday"],
                 demand="low", min_quality=65, training_allowed=True,
                 experience_mix=0.3, priority=1, builtin=True),
    ShiftProfile(key="weekday_dinner", label="Weekday dinner", daypart="night",
                 days=["Monday", "Tuesday", "Wednesday", "Thursday"],
                 demand="normal", min_quality=70, experience_mix=0.4, priority=1, builtin=True),
    ShiftProfile(key="friday_dinner", label="Friday dinner", daypart="night",
                 days=["Friday"], demand="high", min_quality=78,
                 requires_leader=True, experience_mix=0.5, priority=2, builtin=True),
    ShiftProfile(key="saturday_dinner", label="Saturday dinner", daypart="night",
                 days=["Saturday"], demand="peak", min_quality=82,
                 requires_leader=True, experience_mix=0.6, priority=2, builtin=True),
    ShiftProfile(key="brunch", label="Weekend brunch", daypart="morning",
                 days=["Saturday", "Sunday"], demand="high", min_quality=75,
                 experience_mix=0.45, priority=2, builtin=True),
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
    leader_flags: dict = field(default_factory=dict)   # {name: authorized to close}
    leader_rules: list = field(default_factory=list)
    cross_trained: dict = field(default_factory=dict)  # {name: [roles]}
    typical_headcount: dict = field(default_factory=dict)   # {role: people}
    role_minimums: dict = field(default_factory=dict)       # {role: floor}
    demand_pct: float | None = None                    # vs an average day
    target_hours: float | None = None                  # this DAY's hour target
    day_hours: float = 0.0                             # hours scheduled this day
    week_assignments: dict = field(default_factory=dict)   # {name: [assignment]}
    prior_pattern: dict = field(default_factory=dict)  # {name: {"days": [...], "dayparts": [...]}}
    history_weeks: int = 0                             # how many weeks the shift history spans
    ledger: dict = field(default_factory=dict)         # {name: {weekend, closing, holiday, shifts, weeks}} over ~8 published weeks
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
    # Names the owner marked as experienced (staff_settings.experienced),
    # whatever the shift history window shows.
    experienced: set = field(default_factory=set)
    # Share of a shift able to cover a second station that counts as a
    # healthy floor (cross_training).
    cross_training_target: float = 0.34
    # What staff said they want (staff_settings.stated_preferences).
    preferences: dict = field(default_factory=dict)
    # The owner's cross-training target per role ({role lower: share}),
    # over the defaults in CROSS_TRAINING_DEFAULTS.
    cross_training_targets: dict = field(default_factory=dict)
    # The section count and the roles it counts (restaurants.section_count,
    # foh_roles_json): no requirement asks for more of them at once.
    section_cap: int = 0
    cap_roles: set = field(default_factory=set)
    # The multi-week rotation (schedule_intel.rotation_plan): per role, who
    # is due a weekend off, next to close, resting from closing.
    rotation: dict = field(default_factory=dict)
    # Sales per labor hour (schedule_economics.splh_objective): this
    # weekday's daypart target, the sales that daypart usually does, and
    # the hours this draft puts on it (rows counted once, under their start).
    splh_target: float | None = None
    expected_sales: float | None = None
    daypart_hours: float = 0.0
    # ── people the hourly measures and the defaults treat differently ──
    # (schedule audit 10/3/26 D-3, D-6) Salaried people (name_key): paid the
    # same whatever the hours, so their rows are not spent from the hourly
    # day and daypart targets (day_hours, daypart_hours leave them out) and
    # the 40h ceiling is not theirs.
    salaried: set = field(default_factory=set)
    # Each person's weekly hours ceiling as the rules hold it (Constraints.
    # max_hours: their own limit, a salaried person's cap, else the
    # restaurant's ceiling) — {name_key: hours}. Fatigue reads it first.
    hours_ceilings: dict = field(default_factory=dict)
    # The roster's managers ({lower: role}) and who stands in as one on
    # given dates ({lower: set(iso)}) — Constraints.managers / acting_managers.
    managers: dict = field(default_factory=dict)
    acting_managers: dict = field(default_factory=dict)
    # Experienced by default whatever the shift history shows: managers
    # (acting ones too) and salaried people — lowercase (D-6).
    experienced_default: set = field(default_factory=set)
    # The restaurant's role families ({role lower: family}), the roles each
    # person holds beyond their roster role ({lower: set(role lower)}) and
    # each roster person's role ({name: role}) — cross-training and the
    # training mentor read people by family, not by job code (SQ-9, SQ-10).
    role_families: dict = field(default_factory=dict)
    held_roles: dict = field(default_factory=dict)
    roster_roles: dict = field(default_factory=dict)
    # Kitchen stations (kitchen_stations.normalise), {} when none (SQ-26).
    stations: dict = field(default_factory=dict)
    # What staff showed they want by what they drop and claim (schedule_
    # intel.behaviour_preferences): {name: {"avoid": [(day, daypart)],
    # "prefer": [...], "weight"}} — counted below a stated preference (L-19).
    learned_preferences: dict = field(default_factory=dict)
    # The last published weeks, per person, oldest first: {name: [{"week",
    # "hours", "slots": [(day, daypart)], "role"}]} — fatigue and fairness
    # across weeks (SQ-27). busy_slots: the (weekday, daypart) shifts this
    # restaurant's profiles call busy, to read those weeks with.
    load_ledger: dict = field(default_factory=dict)
    busy_slots: set = field(default_factory=set)
    # The first date of the week being scored: only the published weeks
    # right before it count as "week after week" (SQ-27).
    week_start: str = ""
    # What overtime costs (SQ-25): {"line", "bucket_of": {date: payroll
    # week}, "published": {lower: {week: hours}}, "rates", "default_rate",
    # "rules"} — the overtime forecast's own inputs; {} when not supplied.
    overtime: dict = field(default_factory=dict)
    # The scheduling memory's active facts (schedule_memory.enforced_signals:
    # [{kind, key, person, day, daypart, role, value, confidence, enforcement,
    # source}]) — what week_learned holds the week to (L-3).
    learned: list = field(default_factory=list)
    # ── what the rule sweep found, and what the restaurant's own record says ──
    # The hard breaches the rule sweep pinned to this shift ([{kind,
    # label}]); any one holds the shift at HARD_BREACH_CAP (SQ-14).
    hard_breaches: list = field(default_factory=list)
    # A daypart this restaurant runs that the draft wrote nobody onto (SQ-8).
    unwritten: bool = False
    # {(family, target): (crew, source)}: the full crew each strength target
    # was set for — the most of that role any shift the target governs needs
    # (build_contexts). Strength is judged per person against target ÷ crew
    # (SQ-4), so a quiet lunch is not held to the busiest night's total.
    strength_crews: dict = field(default_factory=dict)
    # {family: [ratings]} of the rated people on the roster, and "*" for
    # every rating on file: what demand match reads this restaurant's own
    # level from (SQ-12).
    role_ratings: dict = field(default_factory=dict)
    # {family: {(weekday, daypart)}} — where each role works by the
    # restaurant's requirements (role_runs): a leader rule naming no
    # daypart binds only those (SQ-2).
    role_runs: dict = field(default_factory=dict)
    # Why this shift's required numbers moved off its usual crew (the
    # date's measured demand, the sales-per-labor-hour hold) — the
    # requirements table's reasons (schedule audit 10/3/26 P-19).
    requirement_reasons: list = field(default_factory=list)
    # ── the late segment (D-32): on a night closing past 11pm, the window
    # from 10pm to close, the people it usually runs, its usual sales, its
    # sales-per-labor-hour target and the hours this draft puts in it.
    late_window: tuple | None = None
    late_required: dict = field(default_factory=dict)
    late_expected_sales: float | None = None
    late_splh_target: float | None = None
    late_hours: float = 0.0

    # ── Derived views every dimension wants ────────────────────────────
    def family(self, role) -> str:
        """The role family of `role` ("Server AM" → "server") by the
        restaurant's own map, else role_family's word rule."""
        return role_family(role, self.role_families)

    def manages(self, name) -> bool:
        """Whether `name` is a manager, or an acting manager on this date —
        Constraints.manages, read from the signals the engine passes."""
        key = (name or "").strip().lower()
        if not key:
            return False
        if key in (self.managers or {}):
            return True
        return bool(self.date) and self.date in ((self.acting_managers or {}).get(key) or ())

    @property
    def by_family(self) -> dict:
        """{family: [names]} — by_role with a role's AM/PM job codes as one
        role, each person still counted once across the shift."""
        out = {}
        for role, names in self.by_role.items():
            bucket = out.setdefault(self.family(role), [])
            bucket.extend(n for n in names if n not in bucket)
        return out

    @property
    def people(self) -> list:
        """Everybody really on this shift. A row the rule sweep says will not
        stand (time off, double-booked) is not somebody on the floor, for
        leadership, experience, fatigue or anything else."""
        seen, out = set(), []
        for r in self.rows:
            n = (r.get("employee") or "").strip()
            if self._is_flagged(r):
                continue
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

    def manages(self, name) -> bool:
        """A manager, or standing in as one on this shift's date — the
        reading Constraints.manages gives."""
        key = (name or "").strip().lower()
        return key in self.managers or bool(self.date) and self.date in (self.acting_managers.get(key) or ())

    def is_salaried(self, name) -> bool:
        return bool(self.salaried) and name_key(name) in self.salaried

    def ceiling_for(self, name):
        """The weekly hours ceiling fatigue holds `name` to: the rules' own
        (hours_ceilings), else their own limit, else — for an hourly person
        — the restaurant's ceiling. A salaried person with neither has none
        here: they owe no overtime and the 40h line is not theirs (D-3)."""
        if self.hours_ceilings:
            c = self.hours_ceilings.get(name_key(name))
            if c:
                return float(c)
        lim = (self.hours_limits or {}).get(name)
        if lim and lim[1]:
            return float(lim[1])
        if self.is_salaried(name):
            return None
        return float(self.weekly_ceiling) if self.weekly_ceiling else None


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


def _floor(ctx, key: str):
    """The critical floor of `key` on this shift's profile (SQ-23)."""
    profile = getattr(ctx, "profile", None)
    if profile is not None and hasattr(profile, "floor"):
        return profile.floor(key)
    return CRITICAL_FLOORS.get(key)


# The name each dimension carries on screen - the same words its result
# uses (DimensionResult.label). The weighting list used to build them from
# the key, so "splh" read "Splh" (owner, 9/30/26).
DIMENSION_LABELS = {
    "coverage": "Coverage", "coverage_curve": "Coverage by the hour",
    "operational_strength": "Operational strength", "leadership": "Leadership",
    "demand_match": "Demand match", "labor_efficiency": "Labor efficiency",
    "splh": "Sales per labor hour", "experience_balance": "Experience",
    "training_balance": "Training balance", "reliability": "Reliability",
    "pairings": "Pairings", "fatigue": "Fatigue", "fairness": "Fairness",
    "preferences": "Staff preferences", "stability": "Schedule stability",
    "cross_training": "Cross-training", "min_hours": "Minimum hours", "overtime": "Overtime",
    "stations": "Kitchen stations", "learned": "Learned patterns",
}


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
    "preferences": 3,
    # Sales per labor hour against the daypart's target (#48). Beside labor
    # efficiency (10), which asks whether the DAY fits its hours target:
    # this asks whether the hours sit where the sales are, so moving an
    # hour from a slow lunch to a busy dinner is seen. Withdraws without
    # sales.
    "splh": 5,
    # The hours the owner set as people's minimums, given (week_min_hours,
    # P-4). Judged once for the week; withdraws when nobody has a minimum.
    "min_hours": 4,
    # The overtime premium the week runs up (schedule audit 10/3/26 SQ-25):
    # judged once for the week, as fatigue is — overtime is a payroll-week
    # fact. It was priced only in the review, so no pass that chose between
    # options could see it.
    "overtime": 8,
    # Every kitchen station a daypart needs held by a cook trained on it
    # (SQ-26). Withdraws for a restaurant that has set no stations.
    "stations": 10,
    # What the restaurant's scheduling has learned and the week breaks
    # (week_learned, schedule audit 10/3/26 L-3): judged once for the week;
    # withdraws until the scheduling memory has an active fact. The
    # managers' own repeated edits — "an owner's edits never vanish".
    "learned": 10,
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


def shift_role_requirements(typical: dict = None, floors: dict = None, minimums: dict = None,
                            critical: dict = None) -> dict:
    """{role: (people, source)} one shift needs — the ONE definition the
    generation prompt's requirements table and the coverage score share.

    Each role takes the largest of: what this restaurant usually runs on
    this weekday and daypart (typical), the owner's staffing floor for this
    daypart, the owner's whole-day role minimum (only for roles that work
    this daypart — a bar that opens at 4pm is not short at lunch), and the
    shift profile's critical positions. They used to be read one OR the
    other, in a fixed order, so a restaurant with role minimums set had its
    usual headcount ignored by the score while the prompt asked for it."""
    out, display = {}, {}

    def put(role, n, source, owner=False):
        key = (role or "").strip().lower()
        try:
            n = int(n or 0)
        except (TypeError, ValueError):
            n = 0
        if not key or n <= 0:
            return
        # The owner's spelling of a role wins over the history's.
        if owner or key not in display:
            display[key] = (role or "").strip()
        if n > out.get(key, (0, None))[0]:
            out[key] = (n, source)
    typical = typical or {}
    runs_here = {r.strip().lower() for r, n in typical.items() if n}
    for role, n in typical.items():
        put(role, n, "your usual staffing")
    for role, n in (minimums or {}).items():
        if not runs_here or role.strip().lower() in runs_here:
            put(role, n, "your role minimums", owner=True)
    for role, n in (floors or {}).items():
        put(role, n, "your staffing floors", owner=True)
    for role, n in (critical or {}).items():
        put(role, n, "the shift profile", owner=True)
    return {display[k]: v for k, v in out.items()}


def _capped(ctx: ShiftContext, required: dict) -> tuple:
    """(required, trimmed) under the section cap (staffing_curve.cap_requirement)."""
    if not ctx.section_cap:
        return dict(required), {}
    from staffing_curve import cap_requirement
    return cap_requirement(required, ctx.section_cap, ctx.cap_roles)


def dim_coverage(ctx: ShiftContext) -> DimensionResult | None:
    """Is every required position filled?

    Requirements come from shift_role_requirements: the largest of the
    restaurant's usual headcount, its floors, its role minimums and the
    profile's critical positions, per role. A restaurant that has
    configured nothing is judged against its own history rather than
    against a number this file invented.
    """
    reqs = shift_role_requirements(ctx.typical_headcount, ctx.role_floors, ctx.role_minimums,
                                   ctx.profile.critical_positions)
    if not reqs:
        return None
    required = {r: n for r, (n, _src) in reqs.items()}
    # Never more of the section-counted roles than there are sections: the
    # generator and the backstop hold the draft to the cap, so a requirement
    # over it marked every such shift short however it was built.
    required, over_cap = _capped(ctx, required)
    sources = sorted({src for _n, src in reqs.values()})
    source = sources[0] if len(sources) == 1 else ", ".join(sources)

    on = ctx.by_role
    filled = missing = 0
    gaps = []
    short = {}
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
            short[role] = need - have
            gaps.append(f"{role} short {need - have} of {need}")

    total_required = sum(int(c or 0) for c in required.values() if int(c or 0) > 0)
    if total_required <= 0:
        return None

    score = _pct(filled, total_required)
    res = DimensionResult(
        key="coverage", label="Coverage", score=score,
        weight=DEFAULT_WEIGHTS["coverage"], floor=_floor(ctx, "coverage"),
        facts={"required": total_required, "filled": filled, "missing": missing,
               "gaps": gaps, "short": short, "requirement_source": source},
    )
    if over_cap:
        res.facts["held_to_section_cap"] = {"cap": int(ctx.section_cap), "trimmed": over_cap}
    if ctx.unwritten:
        # A daypart this restaurant runs and the draft wrote nobody onto. It
        # is still 0 — nobody is on it — but "Tuesday dinner 0 / 70" read the
        # same as a missed leader or a phantom rule, and the owner could not
        # tell which (schedule audit 10/3/26 SQ-8).
        part = {"morning": "lunch", "night": "dinner"}.get(ctx.daypart, ctx.daypart or "shift")
        crew = ", ".join(f"{r} {int(n)}" for r, n in required.items() if int(n or 0) > 0)
        res.facts["no_shift_written"] = True
        res.weaknesses.append(f"No shift written for {ctx.day} {part} — it needs {crew} ({source}).")
    elif missing:
        res.weaknesses.append(
            f"{missing} {_plural(missing, 'position')} unfilled — " + "; ".join(gaps) + ".")
    else:
        res.strengths.append("Every required position is filled.")
    # Somebody already working another site in this group at the same time
    # is not coverage here, whatever the row says. A shift there that ends
    # before this one starts (lunch there, dinner here) is not a clash
    # (schedule audit 10/3/26 D-40); one without times is read as the date.
    for name in ctx.people:
        mine = [r for r in ctx.rows if (r.get("employee") or "").strip() == name]
        for entry in (ctx.elsewhere.get(name) or []):
            if entry.get("date") != ctx.date:
                continue
            es, ee = _end_minutes(entry.get("shift_start")), _end_minutes(entry.get("shift_end"))
            if es >= 0 and ee >= 0 and mine:
                if ee <= es:
                    ee += 24 * 60
                clash = False
                for r in mine:
                    rs, re_ = _end_minutes(r.get("shift_start")), _end_minutes(r.get("shift_end"))
                    if rs < 0 or re_ < 0:
                        clash = True
                        break
                    if re_ <= rs:
                        re_ += 24 * 60
                    if rs < ee and es < re_:
                        clash = True
                        break
                if not clash:
                    continue
            res.weaknesses.append(
                f"{name} is also on the schedule at {entry['location']} at the same time.")
            res.score = min(res.score, 60)
    for clash in ctx.role_conflicts:
        res.weaknesses.append(
            f"{clash['name']} is down for {' and '.join(role_words(r) for r in clash['roles'])} "
            "at the same time — only one of them is counted.")
    res.facts["role_conflicts"] = ctx.role_conflicts
    return res


def _strength_crew(ctx: ShiftContext, family: str, target: float) -> tuple:
    """(crew, source): how many people of `family` the strength target was
    set for — the most any shift the target governs needs (build_contexts'
    strength_crews). With nothing on file about how many the role works, the
    fewest people who could reach the target on the 1-5 scale."""
    hit = (ctx.strength_crews or {}).get((family, float(target)))
    if hit and int(hit[0] or 0) > 0:
        return int(hit[0]), hit[1]
    return max(1, int(math.ceil(float(target) / SCORE_SCALE_MAX - 1e-9))), "the fewest people who could reach it"


def dim_operational_strength(ctx: ShiftContext) -> DimensionResult | None:
    """Each role's Operational Score on this shift, per person, against the
    bar its strength target sets.

    The owner's target is a combined score for a full crew ("Bartender 8":
    two bartenders who together make 8). It is judged per person: the
    average rating of the role's rated people here against the target ÷ the
    crew it was set for (the most of that role any shift the target governs
    needs; strength_crews). Three findings of the 10/3/26 schedule audit,
    one design:
      SQ-3  an unrated person counted 0 as soon as anybody in the role was
            rated, while the prompt and the solver treat unrated as
            unknown — the bar is judged over the rated share now, and the
            unrated are named as a blind spot;
      SQ-4  the full crew's total was asked of every shift, so a quiet
            lunch that needs one bartender read 4 of 8 and was capped;
      SQ-5  a sum rewarded bodies — a third average bartender cleared a
            target two never could. An average does not.
    A role's AM/PM job codes are one role here (role_family). Nobody rated
    in a role on the shift: its strength is unknown, not zero.
    """
    targets = {r: v for r, v in (ctx.profile.min_strength or {}).items() if v}
    if not targets or not ctx.scores:
        return None

    on = ctx.by_family
    roles_on = {}
    for role, names in ctx.by_role.items():
        roles_on.setdefault(ctx.family(role), set()).add(role)
    ratios, shorts, mets = [], [], []
    unrated_by_role = {}
    for role, target in targets.items():
        # A target on an AM or PM job is that half of the day's, judged on
        # the role's people whichever code they punch under.
        if job_code_daypart(role) not in (None, ctx.daypart):
            continue
        fam = ctx.family(role)
        people = on.get(fam) or []
        if not people:
            continue  # coverage owns an empty role, not strength
        unrated = ctx.unrated(people)
        if unrated:
            unrated_by_role[role] = unrated
        rated = ctx.rated(people)
        if not rated:
            continue
        target = float(target)
        crew, crew_source = _strength_crew(ctx, fam, target)
        bar = round(max(float(SCORE_SCALE_MIN), min(float(SCORE_SCALE_MAX), target / crew)), 2)
        avg = round(sum(float(ctx.scores[n]) for n in rated) / len(rated), 2)
        ratios.append(min(1.0, avg / bar))
        entry = {"role": role, "strength": avg, "average": avg, "bar": bar, "target": target,
                 "crew": crew, "crew_source": crew_source, "rated": len(rated), "on": len(people),
                 "roles": sorted(roles_on.get(fam) or ())}
        (mets if avg >= bar else shorts).append((entry, rated))

    if not ratios:
        return None

    # The weakest role sets the tone rather than the mean. A kitchen at
    # half strength is not rescued by an over-strong bar, and an operator
    # reading "82%" would never guess one station was in trouble.
    score = int(round(min(ratios) * 100))
    res = DimensionResult(
        key="operational_strength", label="Operational strength", score=score,
        weight=DEFAULT_WEIGHTS["operational_strength"], floor=_floor(ctx, "operational_strength"),
        facts={"targets": {r: float(v) for r, v in targets.items()},
               "shortfalls": [e for e, _p in shorts], "met": [e for e, _p in mets],
               "unrated": sorted({n for names in unrated_by_role.values() for n in names}),
               "judged": "the average of the rated people in each role against target ÷ crew"},
    )
    for e, rated in shorts:
        res.weaknesses.append(
            f"{e['role']} strength averages {e['average']:g} against a bar of {e['bar']:g} "
            f"({e['target']:g} for a crew of {e['crew']}): {_names(rated, ctx.scores)}.")
    for e, _rated in mets[:2]:
        res.strengths.append(f"{e['role']} averages {e['average']:g}, clear of its {e['bar']:g} bar "
                             f"({e['target']:g} for a crew of {e['crew']}).")
    for role, names in unrated_by_role.items():
        res.blind_spots.append(
            f"{_names(sorted(set(names)))} " + _plural(len(set(names)), "has", "have")
            + f" no Operational Score, so {role_words(role)} strength was judged on the rated people only.")
    return res


# A leader check with nobody qualified on scores this much; each qualified
# person found earns their share of the rest (dim_leadership, SQ-1).
LEADER_MISS_SCORE = 50


def dim_leadership(ctx: ShiftContext) -> DimensionResult | None:
    """Is somebody on this shift able to run it?

    Two sources, both optional. The profile can require a leader generally;
    a shift leader rule can name one precisely ("Saturday dinner needs a
    bartender at 5"). Neither configured means the question is not being
    asked of this restaurant, which is different from failing it.

    Each check earns credit for what it found — a rule its qualified people
    ÷ the people it asks for, the profile's "somebody able to run it" all or
    nothing — and the dimension is the weakest check, on a scale where a
    check with nobody qualified scores LEADER_MISS_SCORE and a met one 100.
    One missing leader therefore holds an owner-ruled shift at the same
    level whether one rule applies or four. It used to be rules met ÷ rules
    applying: one miss capped a shift at 0, the same miss among four at 75
    or 25, and among three at nothing (schedule audit 10/3/26 SQ-1 — Erik's
    "Monday dinner 25 / 70" and "Tuesday dinner 0 / 70"). A miss says
    whether nobody in the role is on the shift or somebody is and does not
    qualify: the first is a person to add, the second a person to swap.
    A manager, or an acting manager on their dates, runs the shift the
    profile asks about (SQ-13).
    """
    rules = [r for r in (ctx.leader_rules or []) if _rule_applies(r, ctx)]
    if not rules and not ctx.profile.requires_leader:
        return None

    people = ctx.people
    on_family = ctx.by_family
    codes = {}
    for r, names in ctx.by_role.items():
        codes.setdefault(ctx.family(r), set()).add(r)
    satisfied, missed, unanswerable = [], [], []

    # A requirement phrased in scores cannot be judged by a restaurant that
    # has rated nobody. Answering it "not met" would be a miss, and
    # leadership carries a floor, so one unconfigured built-in profile
    # capped a perfectly good Saturday on the strength of a fact the owner
    # never supplied. Unanswerable requirements are set aside and named;
    # the dimension withdraws if none are left. A rule that needs no score —
    # authorized to close, or a plain headcount — is always answered (SQ-16).
    answerable = []
    for rule in rules:
        if not ctx.scores and rule.get("min_score") is not None and not rule.get("attribute"):
            unanswerable.append(f"{role_words(rule.get('role'), default='somebody')} scoring "
                                f"{float(rule['min_score']):g} or above")
        else:
            answerable.append(rule)
    rules = answerable
    can_identify = bool(ctx.scores or ctx.leader_flags or ctx.managers or ctx.acting_managers)
    check_profile = ctx.profile.requires_leader and can_identify
    if ctx.profile.requires_leader and not can_identify:
        unanswerable.append("somebody able to run the shift")
    if not rules and not check_profile:
        if unanswerable:
            ctx.notes.append(
                "Leadership was not checked — nobody is rated yet, so "
                + ", ".join(sorted(set(unanswerable))) + " could not be identified.")
        return None

    credits = []
    for rule in rules:
        role = (rule.get("role") or "").strip()
        try:
            need = max(1, int(rule.get("count") or 1))
        except (TypeError, ValueError):
            need = 1
        min_score = rule.get("min_score")
        attribute = (rule.get("attribute") or "").strip()
        # The role's people on the shift, its AM/PM job codes as one role.
        pool = on_family.get(ctx.family(role)) or []
        # Three ways a rule can be answered, in the order an operator would
        # think of them: a named capability, a minimum score, or simply
        # being on the shift. The capability branch is what makes "every
        # closing shift needs somebody authorized to close" real rather
        # than documented.
        if attribute:
            qualified = [n for n in pool if ctx.leader_flags.get(n)]
        elif min_score is None:
            qualified = list(pool)
        else:
            qualified = [n for n in pool if (ctx.scores.get(n) or 0) >= float(min_score)]
        # A bar on the role's people never asks for more of them than the
        # role has on the shift: "4 Server PM scoring 5" with 3 Server PM on
        # means all 3 (owner, 10/2/26). A plain headcount rule is not capped.
        if (attribute or min_score is not None) and pool:
            need = min(need, len(pool))
        credit = min(len(qualified), need) / float(need)
        credits.append(credit)
        label = (f"{need} {role_words(role, need)}" +
                 (" authorized to close" if attribute
                  else (f" scoring {float(min_score):g} or above" if min_score is not None else "")))
        if rule.get("closing"):
            label += " on the closing shift"
        if credit >= 1:
            satisfied.append(label)
        else:
            missed.append({"rule": label, "found": len(qualified),
                           "scheduled": _names(pool, ctx.scores),
                           "role": role, "count": need, "min_score": min_score,
                           "attribute": attribute or None, "on": len(pool),
                           "why": "nobody_on" if not pool else ("not_qualified" if not qualified else "short"),
                           "credit": round(credit, 2),
                           # The job codes the role's people here work under.
                           "roles": sorted(codes.get(ctx.family(role)) or ())})

    # The profile's own softer requirement: a manager on the floor (an
    # acting one on their dates), or anybody authorized to close or
    # clearing the leader score in one of the leader roles. Managers were
    # left out, so an unrated manager read as nobody able to run the
    # heaviest-weighted shifts of the week (SQ-13).
    profile_ok, managers_on = True, []
    if check_profile:
        managers_on = [n for n in people if ctx.manages(n)]
        pool = people
        if ctx.profile.leader_roles:
            wanted = {ctx.family(r) for r in ctx.profile.leader_roles}
            pool = [n for fam, names in on_family.items() if fam in wanted for n in names]
        leaders = managers_on + [n for n in pool if n not in managers_on and (
            ctx.leader_flags.get(n) or (ctx.scores.get(n) or 0) >= ctx.profile.leader_min_score)]
        profile_ok = bool(leaders)
        credits.append(1.0 if profile_ok else 0.0)

    worst = min(credits) if credits else 1.0
    score = int(round(LEADER_MISS_SCORE + (SCORE_MAX - LEADER_MISS_SCORE) * worst))
    total = len(rules) + (1 if check_profile else 0)
    met = len(satisfied) + (1 if (check_profile and profile_ok) else 0)

    # The floor (which caps the whole shift) is earned only by a rule the
    # owner wrote. A built-in profile's "somebody able to run it" is a
    # preference: it costs the dimension its points, never the shift its
    # score — a fully staffed Saturday is not held down because nobody on
    # it has been rated 4 yet.
    res = DimensionResult(
        key="leadership", label="Leadership", score=score,
        weight=DEFAULT_WEIGHTS["leadership"], floor=_floor(ctx, "leadership") if rules else None,
        facts={"rules_checked": total, "rules_met": met, "misses": missed,
               "profile_leader_missing": bool(check_profile and not profile_ok),
               "leader_roles": list(ctx.profile.leader_roles or []),
               "leader_min_score": ctx.profile.leader_min_score,
               "managers_on": managers_on, "miss_score": LEADER_MISS_SCORE},
    )
    for miss in missed:
        if miss["why"] == "nobody_on":
            res.weaknesses.append(
                f"No {role_words(miss['role'], default='one')} on this shift — needs {miss['rule']}.")
        else:
            res.weaknesses.append(
                f"Needs {miss['rule']}, found {miss['found']}. On this shift: {miss['scheduled']}.")
    if check_profile and not profile_ok:
        res.weaknesses.append(
            f"Nobody on this shift is a manager, authorized to close or rated "
            f"{ctx.profile.leader_min_score:g} or above.")
    if met == total and total:
        res.strengths.append(
            "Leadership requirements met" + (f" — {_names(managers_on[:2])} on as manager." if managers_on else "."))
    if unanswerable:
        res.blind_spots.append(
            "Nobody is rated yet, so " + ", ".join(sorted(set(unanswerable)))
            + " could not be checked on this shift.")
    return res


def leader_rule_applies(rule: dict, day: str, daypart: str, is_closing=None,
                        runs: dict = None, families: dict = None) -> bool:
    """Whether a shift leader rule binds one shift — the ONE test the
    scorer (_rule_applies) and the prompt's requirements table
    (schedule_requirements.shift_requirements) both ask.

    Its day and daypart, and the closing shift for a closing rule
    (is_closing None: not known yet, so not ruled out). A rule naming no
    daypart binds only where its role works: `runs` is role_runs — every
    weekday and daypart the restaurant's requirements (usual staffing, the
    owner's floors, role minimums for roles working that daypart, profile
    critical positions) put the role on, its AM/PM job codes counted as the
    role. "Saturday: 1 bartender scoring 5" was applied to a Saturday lunch
    with no bar, scored it 0 and capped it, and the table told the model to
    add a bartender at lunch (schedule audit 10/3/26 SQ-2). A role nothing
    on file puts anywhere is unknown, not absent: the rule binds every
    daypart it names, as before. A rule the owner scoped to a daypart binds
    that daypart whatever the history says."""
    role = (rule.get("role") or "").strip()
    if not role:
        return False
    # "Every closing shift" means the last shift to end that day, not every
    # shift. Applied to all of them it demanded a closer at breakfast.
    if rule.get("closing") and is_closing is not None and not is_closing:
        return False
    days = {d.strip().lower() for d in (rule.get("days") or []) if d}
    if days and (day or "").strip().lower() not in days:
        return False
    part = (rule.get("daypart") or "").strip().lower() or job_code_daypart(role)
    if part:
        return part == (daypart or "").strip().lower()
    where = (runs or {}).get(role_family(role, families))
    if not where:
        return True
    return ((day or "").strip().capitalize(), (daypart or "").strip().lower()) in where


def _rule_applies(rule: dict, ctx) -> bool:
    """leader_rule_applies for one scored shift (a caller that passes only
    day, daypart and is_closing gets the day and daypart test alone)."""
    return leader_rule_applies(rule, getattr(ctx, "day", ""), getattr(ctx, "daypart", ""),
                               bool(getattr(ctx, "is_closing", False)), getattr(ctx, "role_runs", None),
                               getattr(ctx, "role_families", None))


def role_runs(typical_headcount: dict = None, role_floors: dict = None, role_minimums: dict = None,
              profiles: list = None, families: dict = None) -> dict:
    """{family: {(weekday, daypart)}} — where each role works: every
    weekday and daypart whose requirement (shift_role_requirements, the one
    coverage reads) includes it. Shared by the scorer's contexts and the
    prompt's requirements table so a leader rule binds the same shifts in
    both (leader_rule_applies, SQ-2)."""
    profiles = profiles if profiles is not None else BUILTIN_PROFILES
    out = {}
    for day in _WEEKDAYS:
        for part in CORE_WINDOWS:          # every daypart the scorer buckets shifts into
            profile = resolve_profile(day, part, profiles)
            reqs = shift_role_requirements((typical_headcount or {}).get((day, part)) or {},
                                           _floors_for(role_floors, day, part), role_minimums,
                                           profile.critical_positions)
            for role in reqs:
                out.setdefault(role_family(role, families), set()).add((day, part))
    return out


# Somebody is "experienced" once they have worked this many shifts in the
# history the restaurant has uploaded. Deliberately a count of real shifts
# rather than a hire date, because shift data is what this product actually
# has — a hire date would be a field nobody fills in.
EXPERIENCE_SHIFTS = 20
# With too little history for tenure to tell, experience is judged from the
# owner's marks only once this many people are marked.
EXPERIENCE_MIN_MARKED = 3
# Below this, somebody is still learning and should not be the only person
# holding a station on a busy shift.
DEVELOPING_SHIFTS = 6


def experience_judged(tenure: dict, marked=None) -> bool:
    """Whether experience can be judged at all: somebody on file has
    EXPERIENCE_SHIFTS, or the owner has marked EXPERIENCE_MIN_MARKED people.
    People experienced only by default (managers, salaried) never count
    toward it — they say nothing about the rest of the team."""
    marks = {str(n).strip().lower() for n in (marked or ()) if n}
    return (max((int(v or 0) for v in (tenure or {}).values()), default=0) >= EXPERIENCE_SHIFTS
            or len(marks) >= EXPERIENCE_MIN_MARKED)


def experienced_by_default(managers=None, acting_managers=None, salaried=None) -> set:
    """Lowercase names experienced whatever the shift history shows: the
    managers (and anyone standing in as one) and the salaried people
    (schedule audit 10/3/26 D-6). Tenure is the punch count; somebody who
    never clocks in — an owner, a salaried manager — had 1 or 2 shifts on
    file and read as still developing."""
    out = {name_key(n) for n in (managers or {})}
    out |= {name_key(n) for n in (acting_managers or {})}
    out |= {name_key(n) for n in (salaried or ())}
    out.discard("")
    return out


def dim_experience_balance(ctx: ShiftContext) -> DimensionResult | None:
    """Enough hands who have done this before.

    Separate from Operational Score on purpose: a strong new hire and a
    steady veteran are different kinds of useful, and a shift made entirely
    of the first kind goes wrong in ways no rating predicts.

    "Experienced" is EXPERIENCE_SHIFTS on file, or the owner's word for it
    (staff_settings.experienced). When the history on file is too short for
    anybody at all to reach the bar and nobody has been marked, the question
    cannot be answered: it withdraws, as a dimension with no data must.
    Scoring it 0 marked every shift of every week down for the length of the
    upload window — at Gia Mia, 14 days of history cost 11.8 points on every
    shift while nobody's staffing was at fault.
    """
    flagged = {n.strip().lower() for n in (ctx.experienced or set()) if n}
    if not ctx.tenure and not flagged:
        return None
    # With a short history, being under EXPERIENCE_SHIFTS says nothing about
    # a person; only the owner's marks do. One mark is not a classification
    # of the team — it turned everybody unmarked into a known beginner and
    # dropped the week the moment the first person was marked — so short-
    # history scoring waits until the owner has marked a few. (The managers
    # and salaried people experienced by default below say nothing about
    # anybody else, so they never turn it on.)
    if not experience_judged(ctx.tenure, flagged):
        ctx.notes.append(
            f"Experience was not judged — the shift history on file is too short for anybody to have "
            f"{EXPERIENCE_SHIFTS} shifts yet. Mark at least {EXPERIENCE_MIN_MARKED} experienced staff to turn it on sooner.")
        return None
    # Managers (acting ones too) and salaried people are experienced by
    # default (schedule audit 10/3/26 D-6): tenure is the punch count, and
    # an owner who never clocks in had 1 shift on file — Erik read as
    # "still developing" and the owner's shifts as inexperienced.
    by_default = {name_key(n) for n in (ctx.experienced_default or set()) if n}
    people = ctx.people
    known = [n for n in people if n in ctx.tenure or n.lower() in flagged or name_key(n) in by_default]
    if not known:
        return None

    def _veteran(n):
        return (n.lower() in flagged or name_key(n) in by_default
                or int(ctx.tenure.get(n) or 0) >= EXPERIENCE_SHIFTS)

    veterans = [n for n in known if _veteran(n)]
    rookies = [n for n in known if not _veteran(n) and int(ctx.tenure.get(n) or 0) < DEVELOPING_SHIFTS]
    want = float(ctx.profile.experience_mix or 0)
    have = len(veterans) / float(len(known))
    score = SCORE_MAX if want <= 0 else _pct(have, want)

    res = DimensionResult(
        key="experience_balance", label="Experience", score=score,
        weight=DEFAULT_WEIGHTS["experience_balance"],
        facts={"veterans": veterans, "rookies": rookies,
               "experienced_share": round(have, 2), "target_share": want,
               "by_default": [n for n in veterans if name_key(n) in by_default
                              and n.lower() not in flagged and int(ctx.tenure.get(n) or 0) < EXPERIENCE_SHIFTS],
               "unknown_tenure": [n for n in people if n not in known]},
    )
    if have >= want:
        res.strengths.append(
            f"{len(veterans)} experienced {_plural(len(veterans), 'hand')} on, "
            f"{int(round(have * 100))}% of the shift.")
    else:
        res.weaknesses.append(
            f"Only {len(veterans)} of {len(known)} are experienced; "
            f"this shift usually wants about {int(round(want * 100))}%.")
    if len(rookies) > 1 and len(known) - len(rookies) <= 1:
        res.weaknesses.append(
            f"{_names(rookies)} are all still new, with little experienced cover.")
    unknown = res.facts["unknown_tenure"]
    if unknown:
        res.blind_spots.append(
            f"No shift history for {_names(unknown)}, so their experience is unknown.")
    return res


def _families_of(ctx: ShiftContext, name: str) -> set:
    """The role families `name` can work: the roles their history shows
    (cross_trained), the roles they hold beyond their roster role
    (held_roles — trained, promoted) and their roster role. "Server AM" and
    "Server PM" are one family, so working both is not cross-training
    (schedule audit 10/3/26 SQ-10)."""
    roles = list(ctx.cross_trained.get(name) or [])
    roles += list(ctx.held_roles.get((name or "").strip().lower()) or ())
    if ctx.roster_roles.get(name):
        roles.append(ctx.roster_roles[name])
    return {f for f in (role_family(r, ctx.role_families) for r in roles) if f}


def _family_requirements(ctx: ShiftContext) -> dict:
    """{family: people this shift needs} — coverage's own requirement
    (shift_role_requirements), per role family."""
    try:
        reqs = shift_role_requirements(ctx.typical_headcount, ctx.role_floors, ctx.role_minimums,
                                       ctx.profile.critical_positions) or {}
    except Exception:
        return {}
    out = {}
    for role, (n, _src) in reqs.items():
        fam = role_family(role, ctx.role_families)
        out[fam] = max(out.get(fam, 0), int(n or 0))
    return out


def _mentor_for(ctx: ShiftContext, weak: str, fam: str, names: list, bar: float, one_person: bool) -> tuple:
    """(mentor, how) for a developing person on this shift, or (None, None):
    somebody in the same role family `bar` or above; else somebody else on
    the floor who also works that station and clears the bar (cross-trained
    or holding the role); else, at a one-person station only, the manager on
    the floor — the one backstop a station built for one person can have."""
    def _best(pool):
        pool = [n for n in pool if n != weak and (ctx.scores.get(n) or 0) >= bar]
        return max(pool, key=lambda n: (ctx.scores.get(n) or 0, n)) if pool else None
    same = _best(ctx.rated(names))
    if same:
        return same, "same_role"
    others = [n for n in ctx.rated(ctx.people) if n not in names and fam in _families_of(ctx, n)]
    cross = _best(others)
    if cross:
        return cross, "cross_role"
    if one_person:
        managers = sorted(n for n in ctx.people if n != weak and ctx.manages(n))
        if managers:
            return managers[0], "manager"
    return None, None


def dim_training_balance(ctx: ShiftContext) -> DimensionResult | None:
    """Is anybody weak left holding a station alone?

    The point is not to keep developing people off the floor. It is the
    opposite: pair them with someone stronger so the shift works AND they
    get better. A schedule that maximises rating benches the weaker half
    permanently, which is how a team stops improving and how people leave.
    """
    if not ctx.scores:
        return None
    # One station per role family: "Server AM" and "Server PM" on the same
    # shift are one station, and a mentor in either counts for both.
    by_family, shown = {}, {}
    for role, names in ctx.by_role.items():
        fam = role_family(role, ctx.role_families)
        by_family.setdefault(fam, []).extend(names)
        shown.setdefault(fam, role)
    need = _family_requirements(ctx)
    checked = isolated = mentored = 0
    isolated_names, mentored_pairs, mentors, solo = [], [], [], []

    for fam, names in by_family.items():
        role = shown[fam]
        rated = ctx.rated(names)
        if not rated:
            continue
        weak = [n for n in rated if (ctx.scores.get(n) or 0) <= 2]
        if not weak:
            continue
        # A one-person station — the shift needs one of the role (or, with no
        # requirement on file, runs one) — can never have a second person in
        # the role beside the weak one, so a lone dishwasher rated 2 scored
        # training 0 on every shift they worked, however the week was built
        # (schedule audit 10/3/26 SQ-9).
        one_person = int(need.get(fam, len(names)) or 0) <= 1
        if one_person:
            solo.append(role)
        checked += len(weak)
        for w in weak:
            mentor, how = _mentor_for(ctx, w, fam, names, (ctx.scores.get(w) or 0) + 2, one_person)
            if mentor:
                mentored += 1
                mentors.append({"name": w, "mentor": mentor, "how": how, "role": role})
                if how == "same_role":
                    mentored_pairs.append(f"{w} with {mentor} on {role_words(role)}")
                elif how == "cross_role":
                    mentored_pairs.append(f"{w} with {mentor}, who also works {role_words(role)}")
                else:
                    mentored_pairs.append(f"{w} on {role_words(role)}, a one-person station, with {mentor} "
                                          "managing the floor")
            else:
                isolated += 1
                isolated_names.append(f"{w} on {role_words(role)}")

    if not checked:
        return None
    score = _pct(mentored, checked)
    res = DimensionResult(
        key="training_balance", label="Training balance", score=score,
        weight=DEFAULT_WEIGHTS["training_balance"],
        facts={"developing": checked, "mentored": mentored, "isolated": isolated,
               "pairs": mentored_pairs, "isolated_names": isolated_names,
               "mentors": mentors, "one_person_stations": solo},
    )
    if mentored:
        res.strengths.append("Paired " + "; ".join(mentored_pairs[:2]) + ".")
    for name in isolated_names[:2]:
        res.weaknesses.append(f"{name} has nobody stronger alongside them.")
    if ctx.profile.training_allowed and isolated:
        res.weaknesses.append(
            "This shift allows training, but a mentor still has to be on it.")
    return res


# What a team should average on a shift, against this restaurant's own
# rated level, at each demand level: a busy night wants the stronger part of
# the team, a quiet one may run under its average (SQ-12). Never above what
# the role's strongest people could make on that many places.
DEMAND_BAR_OFFSET = {"low": -0.4, "normal": 0.0, "high": 0.6, "peak": 1.0}
# Points demand match gives up per rating point the team is under its bar.
DEMAND_POINT_STEP = 25
# Ratings on file before the restaurant's own level is known — per role for
# a role's own level, else across everybody rated.
DEMAND_MIN_RATED = 3


def dim_demand_match(ctx: ShiftContext) -> DimensionResult | None:
    """Does the quality of the team track how busy the shift will be?

    Only penalises under-quality on a busy shift. Putting a strong team on
    a quiet Tuesday is not a fault this dimension should punish — that is
    labor efficiency's question, and double-counting it would push the
    scheduler toward deliberately weak quiet shifts.

    Judged against the restaurant's OWN ratings, role by role: the rated
    people on the shift against their role's average here, raised for a
    busy shift (DEMAND_BAR_OFFSET) but never past what the role's strongest
    people could make in those places. The bars were fixed — 4.0 for a peak
    shift on a scale where 3 means average — so an average team could never
    score 100 on a busy night and rating everybody a point higher cleared
    them (schedule audit 10/3/26 SQ-12). Scored on the distance under the
    bar, so a scale shifted up or down by the owner moves nothing.
    """
    rated = ctx.rated(ctx.people)
    if not rated:
        return None
    every = list((ctx.role_ratings or {}).get("*") or [])
    if not every:
        every = [float(v) for v in (ctx.scores or {}).values() if v is not None]
    if len(every) < DEMAND_MIN_RATED:
        ctx.notes.append(f"Demand match was not judged — it takes {DEMAND_MIN_RATED} rated people to know "
                         "this team's own level.")
        return None
    demand = ctx.profile.demand or "normal"
    offset = DEMAND_BAR_OFFSET.get(demand, 0.0)
    have_sum = want_sum = mean_sum = 0.0
    count = 0
    by_role = {}
    for fam, names in ctx.by_family.items():
        here = ctx.rated(names)
        if not here:
            continue
        own = sorted((float(v) for v in ((ctx.role_ratings or {}).get(fam) or [])), reverse=True)
        # The role's own level once it has enough ratings, else everybody's;
        # the best it could field is always its own people — a 5-rated
        # bartender is no bar for the cooks.
        pool = own if len(own) >= DEMAND_MIN_RATED else sorted((float(v) for v in every), reverse=True)
        mean = sum(pool) / len(pool)
        best = (own or pool)[:len(here)]
        best_possible = sum(best) / len(best)
        want = max(float(SCORE_SCALE_MIN), min(mean + offset, best_possible))
        have = sum(float(ctx.scores[n]) for n in here) / len(here)
        by_role[fam] = {"average": round(have, 2), "wanted": round(want, 2), "team_average": round(mean, 2),
                        "best_possible": round(best_possible, 2), "rated": len(here)}
        have_sum += have * len(here)
        want_sum += want * len(here)
        mean_sum += mean * len(here)
        count += len(here)
    avg, wanted, mean = have_sum / count, want_sum / count, mean_sum / count
    score = SCORE_MAX if avg >= wanted - 1e-9 else max(0, int(round(SCORE_MAX - (wanted - avg) * DEMAND_POINT_STEP)))

    res = DimensionResult(
        key="demand_match", label="Demand match", score=score,
        weight=DEFAULT_WEIGHTS["demand_match"],
        facts={"demand": demand, "average_score": round(avg, 2), "wanted": round(wanted, 2),
               "team_average": round(mean, 2), "offset": offset, "by_role": by_role,
               "vs_average_day_pct": ctx.demand_pct},
    )
    if score >= SCORE_MAX:
        res.strengths.append(
            f"Team averages {avg:.1f} against the {wanted:.1f} a {demand}-demand shift here wants "
            f"(your rated team averages {mean:.1f}).")
    else:
        busy = "your busiest" if demand == "peak" else f"a {demand}-demand"
        res.weaknesses.append(
            f"Team averages {avg:.1f} on {busy} shift — about {wanted:.1f} would match it; "
            f"your rated team averages {mean:.1f}.")
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


# Sales per labor hour within this share under the daypart's target is on
# target; each point further under costs SPLH_STEP. Over the target is not
# marked down here — a thin floor is coverage's question, not this one's.
SPLH_TOLERANCE = 0.05
SPLH_STEP = 200
SPLH_THIN = 1.4


def dim_splh(ctx: ShiftContext) -> DimensionResult | None:
    """Sales per labor hour on this shift against the daypart's target.

    The sales are what this weekday's daypart usually does (raised by a
    lift recorded for the date), the hours what the draft puts on it. No
    sales record, no target or no hours: the dimension withdraws."""
    if not ctx.splh_target or ctx.splh_target <= 0 or not ctx.expected_sales or ctx.expected_sales <= 0:
        return None
    if ctx.daypart_hours <= 0:
        return None
    splh = ctx.expected_sales / ctx.daypart_hours
    ratio = splh / float(ctx.splh_target)
    score = SCORE_MAX if ratio >= 1 - SPLH_TOLERANCE else max(0, int(round(SCORE_MAX - (1 - SPLH_TOLERANCE - ratio) * SPLH_STEP)))
    need_hours = ctx.expected_sales / float(ctx.splh_target)
    res = DimensionResult(
        key="splh", label="Sales per labor hour", score=score, weight=DEFAULT_WEIGHTS["splh"],
        facts={"splh": round(splh, 0), "target": round(float(ctx.splh_target), 0),
               "expected_sales": round(ctx.expected_sales, 0), "hours": round(ctx.daypart_hours, 1),
               "hours_at_target": round(need_hours, 1), "ratio": round(ratio, 2)})
    part = "lunch" if ctx.daypart == "morning" else "dinner"
    if ratio >= 1 - SPLH_TOLERANCE:
        res.strengths.append(f"${splh:,.0f} of sales per labor hour against a ${float(ctx.splh_target):,.0f} {part} target.")
        if ratio >= SPLH_THIN:
            res.blind_spots.append(f"${splh:,.0f} per labor hour is well past the ${float(ctx.splh_target):,.0f} target — "
                                   "check the floor can carry the volume.")
    else:
        res.weaknesses.append(f"${splh:,.0f} of sales per labor hour against a ${float(ctx.splh_target):,.0f} {part} target — "
                              f"about {ctx.daypart_hours - need_hours:.0f}h more than the usual sales here carry.")
    _late_splh(ctx, res)
    return res


def _late_splh(ctx: ShiftContext, res: DimensionResult) -> None:
    """The late segment's own sales per labor hour on a night closing past
    11pm (schedule audit 10/3/26 D-32), in place: a strength or weakness
    line, `facts["late"]`, and the night's score blended with the late
    window's by its share of the night's hours (at most half). Nothing
    without a late target, late sales or late hours."""
    if ctx.daypart != "night" or not ctx.late_window:
        return
    t, s, h = ctx.late_splh_target, ctx.late_expected_sales, float(ctx.late_hours or 0)
    if not t or t <= 0 or not s or s <= 0 or h <= 0:
        return
    splh = s / h
    ratio = splh / float(t)
    late_score = SCORE_MAX if ratio >= 1 - SPLH_TOLERANCE else max(0, int(round(SCORE_MAX - (1 - SPLH_TOLERANCE - ratio) * SPLH_STEP)))
    share = min(0.5, h / max(float(ctx.daypart_hours or 0), h))
    res.score = int(round(res.score * (1 - share) + late_score * share))
    res.facts["late"] = {"splh": round(splh, 0), "target": round(float(t), 0), "expected_sales": round(s, 0),
                         "hours": round(h, 1), "hours_at_target": round(s / float(t), 1), "ratio": round(ratio, 2),
                         "window": [_fmt_minutes(ctx.late_window[0]), _fmt_minutes(ctx.late_window[1])]}
    when = f"from {_fmt_minutes(ctx.late_window[0])} to close"
    if ratio >= 1 - SPLH_TOLERANCE:
        res.strengths.append(f"Late night ({when}): ${splh:,.0f} of sales per labor hour against a ${float(t):,.0f} target.")
    else:
        res.weaknesses.append(f"Late night ({when}): ${splh:,.0f} of sales per labor hour against a ${float(t):,.0f} "
                              f"target — about {h - s / float(t):.0f}h more than late sales usually carry.")


# A shift at this demand level or above is a hard one to work, and is what
# fatigue and fairness both count.
HARD_DEMAND = "high"
# Hard shifts in one week before somebody is carrying too many of them.
HARD_SHIFT_CEILING = 4
# Consecutive days worked before the run itself is the problem.
CONSECUTIVE_DAY_CEILING = 6


# Fatigue across weeks (schedule audit 10/3/26 SQ-27): one week was all it
# judged, so somebody carrying four of the busiest shifts — one under the
# weekly ceiling — every week for two months, or averaging past their hours
# ceiling week after week, never read as strained. The last published weeks
# (load_ledger) are read with this one: over the latest SUSTAINED_WINDOW
# weeks, this one included and with at least SUSTAINED_MIN_WEEKS on record,
# an average at the busy-shift ceiling or past the hours ceiling is strain —
# unless this week is the relief (under the ceiling, or under RELIEF_SHARE
# of the hours one), which is never what the schedule is blamed for.
SUSTAINED_WINDOW = 4
SUSTAINED_MIN_WEEKS = 3
RELIEF_SHARE = 0.9


def _is_busy(entry: dict) -> bool:
    return DEMAND_RANK.get(entry.get("demand") or "normal", 1) >= DEMAND_RANK[HARD_DEMAND]


def _past_load(ctx: ShiftContext, name: str) -> tuple:
    """([busy shifts], [hours]) per published week in the SUSTAINED_WINDOW
    before this one, oldest first, from the load ledger — busy read with
    this restaurant's own profiles (busy_slots), as this week's shifts are.
    A week further back than the window is not "week after week"."""
    cutoff = ""
    if ctx.week_start:
        try:
            cutoff = (_iso_datetime(ctx.week_start)
                      - timedelta(weeks=SUSTAINED_WINDOW - 1)).strftime("%Y-%m-%d")
        except ValueError:
            cutoff = ""
    busy, hours = [], []
    for w in (ctx.load_ledger or {}).get(name) or []:
        if cutoff and str(w.get("week") or "") < cutoff:
            continue
        busy.append(sum(1 for s in (w.get("slots") or []) if tuple(s) in (ctx.busy_slots or set())))
        try:
            hours.append(float(w.get("hours") or 0))
        except (TypeError, ValueError):
            hours.append(0.0)
    return busy, hours


def _fatigue_findings(names, ctx: ShiftContext) -> dict:
    """{overloaded, long_runs, long_shifts, heavy_weeks, sustained_busy,
    sustained_hours} for `names`. The week's counts are this week's shifts
    only; the run of days counts the previous week's tail (so a run over the
    boundary is seen), and the sustained measures read the published weeks
    before it (SQ-27). Each person's hours ceiling is their own (ShiftContext.
    ceiling_for): a salaried manager at 55h is not over a 40h line that was
    never theirs (D-3)."""
    out = {k: [] for k in ("overloaded", "long_runs", "long_shifts", "heavy_weeks",
                           "sustained_busy", "sustained_hours")}
    for name in names:
        assignments = ctx.week_assignments.get(name) or []
        mine = [a for a in assignments if not a.get("prior")]
        hard = sum(1 for a in mine if _is_busy(a))
        if hard > HARD_SHIFT_CEILING:
            out["overloaded"].append((name, hard))
        run = _longest_run(sorted({a.get("date") for a in assignments if a.get("date")}))
        if run > CONSECUTIVE_DAY_CEILING:
            out["long_runs"].append((name, run))
        # Hours, not only days: a 13-hour double and a 46-hour week are
        # both fatigue the day count never sees.
        if ctx.max_shift_hours:
            longest = max((a.get("hours") or 0) for a in mine) if mine else 0
            if longest > float(ctx.max_shift_hours) + 0.01:
                out["long_shifts"].append((name, longest))
        total = sum((a.get("hours") or 0) for a in mine)
        ceiling = ctx.ceiling_for(name)
        if ceiling and total > ceiling + 0.05:
            out["heavy_weeks"].append((name, round(total, 1), ceiling))
        past_busy, past_hours = _past_load(ctx, name)
        if past_busy:
            window = (past_busy + [hard])[-SUSTAINED_WINDOW:]
            avg = sum(window) / float(len(window))
            if len(window) >= SUSTAINED_MIN_WEEKS and hard >= HARD_SHIFT_CEILING and avg >= HARD_SHIFT_CEILING:
                out["sustained_busy"].append((name, round(avg, 1), len(window)))
        if past_hours and ceiling:
            window = (past_hours + [total])[-SUSTAINED_WINDOW:]
            avg = sum(window) / float(len(window))
            if len(window) >= SUSTAINED_MIN_WEEKS and total >= ceiling * RELIEF_SHARE and avg > ceiling + 0.05:
                out["sustained_hours"].append((name, round(avg, 1), len(window), ceiling))
    return out


def _fatigue_result(tracked: list, findings: dict, scope: str = "shift") -> DimensionResult:
    overloaded, long_runs = findings["overloaded"], findings["long_runs"]
    long_shifts, heavy_weeks = findings["long_shifts"], findings["heavy_weeks"]
    sustained_busy, sustained_hours = findings["sustained_busy"], findings["sustained_hours"]
    strained = sorted({n for n, _ in overloaded} | {n for n, _ in long_runs}
                      | {n for n, _ in long_shifts} | {n for n, _, _ in heavy_weeks}
                      | {n for n, _, _ in sustained_busy} | {n for n, _, _, _ in sustained_hours})
    score = _pct(len(tracked) - len(strained), len(tracked))
    res = DimensionResult(
        key="fatigue", label="Fatigue", score=score,
        weight=DEFAULT_WEIGHTS["fatigue"],
        facts={"overloaded": [{"name": n, "hard_shifts": h} for n, h in overloaded],
               "long_runs": [{"name": n, "days": d} for n, d in long_runs],
               "long_shifts": [{"name": n, "hours": h} for n, h in long_shifts],
               "heavy_weeks": [{"name": n, "hours": h, "ceiling": c} for n, h, c in heavy_weeks],
               "sustained_busy": [{"name": n, "average": a, "weeks": w} for n, a, w in sustained_busy],
               "sustained_hours": [{"name": n, "average": a, "weeks": w, "ceiling": c}
                                   for n, a, w, c in sustained_hours],
               "tracked": len(tracked), "strained": strained, "scope": scope},
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
    for name, avg, weeks in sustained_busy[:2]:
        res.weaknesses.append(f"{name} has averaged {avg:g} of the week's busiest shifts over the last "
                              f"{weeks} weeks, this one included — give them a lighter week.")
    for name, avg, weeks, ceiling in sustained_hours[:2]:
        res.weaknesses.append(f"{name} has averaged {avg:g}h a week over the last {weeks} weeks, this one "
                              f"included, against a {ceiling:g}h ceiling.")
    if not res.weaknesses:
        res.strengths.append("Nobody is carrying an unreasonable share of the hard shifts.")
    return res


def dim_fatigue(ctx: ShiftContext) -> DimensionResult | None:
    """Are the same people carrying every hard shift? — asked of the people
    on one shift.

    This is the dimension that stops the engine degenerating into "put the
    best people on everything". A rating system can only ever push in that
    direction; burning out the strong half is the predictable result, and
    it shows up as turnover months later rather than as a bad schedule
    anybody could point at.

    It is a property of the WEEK, so the week score judges it once per
    person (week_fatigue, WEEK_LEVEL_DIMENSIONS) and evaluate_shift no
    longer runs it: a tired person used to be marked down on every shift
    they worked, so the same strain counted five or six times, more on a
    peak night, and more on a thin shift than a busy one. Kept for callers
    that ask about one shift's people directly.
    """
    if not ctx.week_assignments:
        return None
    tracked = [n for n in ctx.people if n in ctx.week_assignments]
    if not tracked:
        return None
    return _fatigue_result(tracked, _fatigue_findings(tracked, ctx))


def week_fatigue(contexts: list) -> DimensionResult | None:
    """Fatigue judged once per person per week: of everybody working this
    week, how many are carrying too many busy shifts, too many days in a
    row, an over-long shift or an over-ceiling week — this week, or week
    after week across the published weeks before it (SQ-27). None when the
    week has nobody on it (and so nothing to judge)."""
    if not contexts:
        return None
    ctx = contexts[0]
    if not ctx.week_assignments:
        return None
    working = set()
    for c in contexts:
        working.update(c.people)
    tracked = sorted(n for n in working
                     if any(not e.get("prior") for e in (ctx.week_assignments.get(n) or [])))
    if not tracked:
        return None
    return _fatigue_result(tracked, _fatigue_findings(tracked, ctx), scope="week")


def week_min_hours(contexts: list) -> DimensionResult | None:
    """Minimum hours, judged once for the week: of the hours the owner set as
    each person's minimum (hours_limits), the share the week gives them —
    counted in hours, so a cook set to 40h on 7h is 7 of 40 and every shift
    moved to them scores, not only the one that reaches it. Nothing in the
    score read a minimum, so the optimizer, the solver's judge and the fix
    pass were blind to Erik's full-time cook on 7h of 40-45h (schedule audit
    10/3/26 P-4). Somebody with a minimum and no shift counts too. None when
    nobody has a minimum."""
    if not contexts:
        return None
    ctx = contexts[0]
    limits = {n: lim for n, lim in (ctx.hours_limits or {}).items() if n and lim and lim[0]}
    if not limits:
        return None
    by_low = {}
    for name, entries in (ctx.week_assignments or {}).items():
        by_low.setdefault((name or "").strip().lower(), []).extend(e for e in entries or () if not e.get("prior"))
    want = got = 0.0
    short = []
    for name, lim in sorted(limits.items()):
        try:
            need = float(lim[0])
        except (TypeError, ValueError):
            continue
        have = sum((e.get("hours") or 0) for e in by_low.get(name.strip().lower(), []))
        want += need
        got += min(have, need)
        if have + 0.05 < need:
            short.append((name, round(have, 1), need))
    if want <= 0:
        return None
    res = DimensionResult(key="min_hours", label=DIMENSION_LABELS["min_hours"], score=_pct(got, want),
                          weight=DEFAULT_WEIGHTS["min_hours"],
                          facts={"people": len(limits), "hours_owed": round(want, 1), "hours_given": round(got, 1),
                                 "short": [{"name": n, "hours": h, "min": m} for n, h, m in short]})
    for name, have, need in short[:2]:
        res.weaknesses.append(f"{name} has {have:g}h of the {need:g}h minimum you set.")
    if not short:
        res.strengths.append("Everybody with a minimum is at it.")
    return res


def dim_min_hours(ctx: ShiftContext) -> DimensionResult | None:
    """The minimum-hours measure for one shift's context — a property of the
    WEEK, judged once in evaluate_schedule (WEEK_LEVEL_DIMENSIONS); kept for
    callers that ask about one shift's people directly."""
    return week_min_hours([ctx])


def _longest_run(dates: list) -> int:
    """Longest streak of consecutive calendar days in a sorted date list."""
    best = run = 0
    previous = None
    for raw in dates:
        try:
            current = _iso_datetime(raw).date()
        except (ValueError, TypeError):
            continue
        run = run + 1 if previous and (current - previous) == timedelta(days=1) else 1
        best = max(best, run)
        previous = current
    return best


# How far over their share of a kind of shift someone may be before it is a
# pattern rather than a rota: one and a half shifts of that kind this week.
FAIRNESS_ALLOWANCE = 1.5
# Points off per shift of excess past the allowance.
FAIRNESS_STEP = 30
# A comparison needs at least this many comparable people in the role.
FAIRNESS_MIN_GROUP = 3
_WEEKEND = ("friday", "saturday", "sunday")


def _primary_role(entries: list, families: dict = None) -> str:
    """The role family somebody works most of `entries` in — "Server AM"
    and "Server PM" are one role to compare a share within."""
    counts = {}
    for e in entries:
        r = role_family(e.get("role") or "", families)
        if r:
            counts[r] = counts.get(r, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0] if counts else ""


# The busiest shifts across the published weeks (schedule audit 10/3/26
# SQ-27): somebody on a busy shift who has had this many more of them than
# their share among their role over those weeks is carrying the load week
# after week — a pattern no single week's comparison can see. Costs what the
# weekend and close ledger does.
LEDGER_BUSY_EXCESS = 3
LEDGER_MIN_SHIFTS = 3
LEDGER_PENALTY = 15


def _busy_ledger(ctx: ShiftContext):
    """The busiest-shift share across the published weeks for the people on
    this shift, against their role family's rate over the same weeks: None
    when nobody here could be compared (fewer than FAIRNESS_MIN_GROUP people
    in their role with LEDGER_MIN_SHIFTS on record), else {"overloaded":
    [...]} — empty when nobody is past their share."""
    per = {}
    for name, weeks in (ctx.load_ledger or {}).items():
        slots = [tuple(s) for w in weeks for s in (w.get("slots") or [])]
        if len(slots) < LEDGER_MIN_SHIFTS:
            continue
        roles = {}
        for w in weeks:
            fam = role_family(w.get("role") or "", ctx.role_families)
            if fam:
                roles[fam] = roles.get(fam, 0) + len(w.get("slots") or [])
        fam = max(roles.items(), key=lambda kv: (kv[1], kv[0]))[0] if roles else ""
        per[name] = (fam, len(slots), sum(1 for s in slots if s in (ctx.busy_slots or set())), len(weeks))
    here = [n for n in ctx.people if n in per]
    if not here:
        return None
    out, judged = [], False
    for fam in sorted({per[n][0] for n in here}):
        group = [n for n, v in per.items() if v[0] == fam]
        if len(group) < FAIRNESS_MIN_GROUP:
            continue
        shifts = sum(per[n][1] for n in group)
        busy = sum(per[n][2] for n in group)
        if not shifts:
            continue
        judged = True
        rate = busy / float(shifts)
        for n in here:
            if per[n][0] != fam:
                continue
            expected = per[n][1] * rate
            excess = per[n][2] - expected
            if excess > LEDGER_BUSY_EXCESS:
                out.append({"name": n, "role": fam, "have": per[n][2], "share": round(expected, 1),
                            "excess": round(excess, 1), "weeks": per[n][3]})
    return {"overloaded": out} if judged else None


def dim_fairness(ctx: ShiftContext) -> DimensionResult | None:
    """Is somebody on THIS shift carrying more than their share of this kind
    of shift — the closes, the weekends, the week's busiest services?

    Asked of the shift, about the people on it, against the others in the
    same role who work a comparable week. It used to be the spread between
    the most and the fewest across the whole roster, every role and every
    part-timer at once, repeated on all fourteen shifts: a lunch-only host
    with no closes against a closer with six read as unfair on every shift
    of a sixty-person week (Gia Mia scored 12 however the week was built),
    and no single move could change it. Now each person's count is set
    against their share — their shifts this week times the role's rate — so
    a two-shift part-timer is not expected to close as often as a five-shift
    closer, and only people who can work that kind of shift are compared.
    """
    if not ctx.week_assignments:
        return None
    kinds = []
    if ctx.is_closing:
        kinds.append(("closing", "closes"))
    if (ctx.day or "").strip().lower() in _WEEKEND:
        kinds.append(("weekend", "weekend shifts"))
    if DEMAND_RANK.get(ctx.profile.demand or "normal", 1) >= DEMAND_RANK[HARD_DEMAND]:
        kinds.append(("busiest", "busiest shifts"))
    if not kinds:
        return None

    def _is(kind, e):
        if kind == "closing":
            return bool(e.get("closing"))
        if kind == "weekend":
            return bool(e.get("weekend"))
        return DEMAND_RANK.get(e.get("demand", "normal"), 1) >= DEMAND_RANK[HARD_DEMAND]

    def _eligible(kind, name, entries):
        if kind == "weekend":
            off = {d.strip().lower() for d in (ctx.availability.get(name) or set())}
            return not all(d in off for d in _WEEKEND)
        if kind == "closing":
            pattern = ctx.prior_pattern.get(name) or {}
            return (any(e.get("daypart") == "night" for e in entries)
                    or "night" in {p.strip().lower() for p in (pattern.get("dayparts") or [])})
        return True

    by_role = {}
    for name, entries in ctx.week_assignments.items():
        # This week's shifts only: prior_week_assignments seeds the tail of
        # the last schedule for fatigue, and is not this week's share.
        mine = [e for e in entries if e.get("date") and not e.get("prior")]
        if len(mine) >= 2:
            by_role.setdefault(_primary_role(mine, ctx.role_families), []).append((name, mine))

    worst, facts, lines = SCORE_MAX, {}, []
    on_here = {n.lower() for n in ctx.people}
    # Whether anybody on this shift could be compared with anybody at all
    # (schedule audit 10/3/26 SQ-11). A kind was written into the facts even
    # when no role reached FAIRNESS_MIN_GROUP, and the dimension withdrew
    # only when no kind was — so a two- or three-person week scored a
    # phantom 100 that diluted the real problems. A kind now counts only when
    # somebody on this shift sat in a group large enough to share it, and
    # with no comparison anywhere the dimension withdraws.
    compared = False
    for kind, label in kinds:
        overloaded, judged = [], False
        for role, members in by_role.items():
            group = [(n, es) for n, es in members if _eligible(kind, n, es)]
            if len(group) < FAIRNESS_MIN_GROUP:
                continue
            total_shifts = sum(len(es) for _n, es in group)
            total_kind = sum(sum(1 for e in es if _is(kind, e)) for _n, es in group)
            if not total_kind:
                continue
            rate = total_kind / float(total_shifts)
            for n, es in group:
                if n.lower() not in on_here:
                    continue
                judged = True
                have = sum(1 for e in es if _is(kind, e))
                expected = len(es) * rate
                excess = have - expected
                if excess > FAIRNESS_ALLOWANCE:
                    overloaded.append({"name": n, "role": role, "have": have,
                                       "share": round(expected, 1), "excess": round(excess, 1)})
        if not judged:
            continue
        compared = True
        if overloaded:
            top = max(overloaded, key=lambda o: o["excess"])
            kind_score = max(0, int(round(SCORE_MAX - (top["excess"] - FAIRNESS_ALLOWANCE) * FAIRNESS_STEP)))
            worst = min(worst, kind_score)
            lines.append(f"{top['name']} has {top['have']} of the week's {label} — about "
                         f"{top['share']:g} would be their share among {top['role'] or 'their'} staff.")
        facts[kind] = {"overloaded": overloaded}

    # The multi-week rotation: the week judged against who was due a
    # weekend off or next to close across the last published weeks, not
    # only against itself. Replaces the ledger check below when a plan
    # exists — the plan is built from the same weeks, per role.
    rot_lines, rot_good, rot_facts = _rotation_findings(ctx, {k for k, _l in kinds})
    if rot_facts:
        facts["rotation"] = rot_facts
    if rot_facts or _rotation_covers(ctx, {k for k, _l in kinds}):
        compared = True
    if rot_lines:
        lines.extend(rot_lines)
        worst = max(0, worst - ROTATION_PENALTY)

    # The rotation ledger: over the last published weeks, somebody on this
    # shift already has far more of this kind than their colleagues.
    if ctx.ledger and not ctx.rotation:
        present = {k for k, _l in kinds}
        for kind, label in (("weekend", "weekend shifts"), ("closing", "closes")):
            if kind not in present:
                continue
            on = [n for n in ctx.people if n in ctx.ledger and ctx.ledger[n].get("shifts", 0) >= 3]
            everyone = [n for n in ctx.ledger if ctx.ledger[n].get("shifts", 0) >= 3]
            if len(everyone) < 3 or not on:
                continue
            compared = True
            counts = {n: int(ctx.ledger[n].get(kind) or 0) for n in everyone}
            top, bottom = max(counts.values()), min(counts.values())
            most_here = [n for n in on if counts.get(n) == top]
            if top - bottom > 4 and most_here:
                wk = ctx.ledger[most_here[0]].get("weeks", 8)
                lines.append(f"{_names(most_here[:2])} already {'has' if len(most_here) == 1 else 'have'} the most "
                             f"{label} of the last {wk} published weeks ({top} against {bottom}) and "
                             f"{'is' if len(most_here) == 1 else 'are'} on another here.")
                facts["ledger"] = {"kind": kind, "most": most_here, "top": top, "bottom": bottom, "weeks": wk}
                worst = max(0, worst - LEDGER_PENALTY)
                break

    # The busiest shifts across the published weeks (SQ-27): the rotation
    # and the ledger above cover weekends and closes only, so the same
    # people carrying every Friday and Saturday night for two months read
    # as fair whenever one week's share looked even.
    if any(k == "busiest" for k, _l in kinds) and ctx.load_ledger and ctx.busy_slots:
        across = _busy_ledger(ctx)
        if across is not None:
            compared = True
            facts["busy_ledger"] = across
            if across["overloaded"]:
                top = max(across["overloaded"], key=lambda o: o["excess"])
                lines.append(f"{top['name']} has had {top['have']} of the busiest shifts over the last "
                             f"{top['weeks']} published weeks — about {top['share']:g} would be their share "
                             f"among {role_words(top['role'], 2, default='their role')} — and is on another here.")
                worst = max(0, worst - LEDGER_PENALTY)

    if not compared:
        return None
    res = DimensionResult(key="fairness", label="Fairness", score=worst,
                          weight=DEFAULT_WEIGHTS["fairness"], facts=facts)
    res.weaknesses.extend(lines)
    if not lines:
        res.strengths.append("Nobody on this shift is carrying more than their share of "
                             + " or ".join(label for _k, label in kinds) + ".")
    res.strengths.extend(rot_good)
    return res


# Points a shift loses when it puts somebody due a weekend off, or resting
# from closes, on it while the rotation's next person in that role is free.
ROTATION_PENALTY = 15


def _rotation_covers(ctx: ShiftContext, kinds: set) -> bool:
    """Whether the rotation plan speaks for a role on this shift at all — a
    weekend or a close judged against it, with or without a finding."""
    plan = (ctx.rotation or {}).get("roles") or {}
    if not plan or not kinds & {"weekend", "closing"}:
        return False
    keys = {str(r).strip().lower() for r in plan}
    return any(role.strip().lower() in keys for role in ctx.by_role)


def _rotation_findings(ctx: ShiftContext, kinds: set):
    """(weaknesses, strengths, facts) for this shift against the rotation
    plan. A person due a weekend off who is on a weekend shift counts only
    when somebody else in their role, not due, works this week with the
    weekend free — the rotation had somebody to give it to. Likewise a
    person resting from closes on a close, while the next closer in the
    role has none this week."""
    plan = (ctx.rotation or {}).get("roles") or {}
    if not plan or not kinds & {"weekend", "closing"}:
        return [], [], {}
    by_low = {str(r).strip().lower(): v for r, v in plan.items()}
    this_week = {}
    for n, entries in (ctx.week_assignments or {}).items():
        mine = [e for e in entries if e.get("date") and not e.get("prior")]
        if mine:
            this_week[n.strip().lower()] = mine
    weak, good, facts = [], [], {}
    part = "lunch" if ctx.daypart == "morning" else "dinner"
    for role, names in ctx.by_role.items():
        rp = by_low.get(role.strip().lower())
        if not rp:
            continue
        on = {n.strip().lower(): n for n in names}
        if "weekend" in kinds:
            due = {n.strip().lower() for n in rp.get("weekend_due") or []}
            for low, n in on.items():
                if low not in due:
                    continue
                free = [p for p in rp.get("people") or [] if p.strip().lower() not in due
                        and p.strip().lower() in this_week
                        and not any(e.get("weekend") for e in this_week[p.strip().lower()])]
                if free:
                    streak = (rp.get("weekend_streak") or {}).get(n)
                    weak.append(f"{n} is due a weekend off" + (f" ({streak} weekends in a row)" if streak else "")
                                + f" and is on {ctx.day} {part}; {free[0]} has this weekend free.")
                    facts.setdefault("weekend_due_working", []).append(n)
            rested = [p for p in rp.get("weekend_due") or [] if p.strip().lower() in this_week
                      and not any(e.get("weekend") for e in this_week[p.strip().lower()])]
            if rested:
                good.append(f"{', '.join(rested)} {'gets' if len(rested) == 1 else 'get'} the weekend off the rotation "
                            f"said {'was' if len(rested) == 1 else 'were'} due.")
                facts["weekend_due_off"] = rested
        if "closing" in kinds and ctx.is_closing:
            resting = {n.strip().lower() for n in rp.get("rest_from_close") or []}
            nexts = [p for p in (rp.get("next_close") or [])[:3] if p.strip().lower() in this_week
                     and not any(e.get("closing") for e in this_week[p.strip().lower()])]
            for low, n in on.items():
                if low in resting and nexts:
                    c = (rp.get("closes") or {}).get(n) or {}
                    had = (f"has closed {c['closes']} of their last {c['shifts']} shifts" if c.get("shifts")
                           else "has carried more than their share of closes")
                    weak.append(f"{n} {had} and closes again here; {nexts[0]} is next to close and has none this week.")
                    facts.setdefault("resting_closer_closing", []).append(n)
    return weak, good, facts



# A regular is someone whose usual week is at least this many hours; one
# scheduled under this share of it is not getting the week they usually get
# (models.usual_pattern avg_hours, D-37).
USUAL_HOURS_FLOOR = 16.0
USUAL_HOURS_CUT = 0.6


def dim_stability(ctx: ShiftContext) -> DimensionResult | None:
    """Are people getting roughly the schedule they had last week?

    Predictability is a real benefit to staff and costs the restaurant
    nothing when demand has not moved. Weighted lightly, because a genuine
    demand change should always win over the comfort of the same rota.
    """
    if not ctx.prior_pattern:
        return None
    # A date the owner flagged (an event, a large party) is exactly when the
    # usual rota should change; holding it to last week's pattern would mark
    # the right response down.
    if ctx.profile.source == "what you told us about this date":
        return None
    people = [n for n in ctx.people if n in ctx.prior_pattern]
    if not people:
        return None
    familiar = 0
    changed, cut = [], []
    for name in people:
        pattern = ctx.prior_pattern.get(name) or {}
        days = {d.strip().lower() for d in (pattern.get("days") or [])}
        parts = {p.strip().lower() for p in (pattern.get("dayparts") or [])}
        day_ok = not days or (ctx.day or "").strip().lower() in days
        part_ok = not parts or (ctx.daypart or "").lower() in parts
        # Their usual hours too (schedule audit 10/3/26 D-37): a regular cut
        # well below the week they usually work is not getting the schedule
        # they had, whatever day this shift is on.
        usual = pattern.get("avg_hours")
        hours_ok = True
        if usual and float(usual) >= USUAL_HOURS_FLOOR:
            week = sum(float(e.get("hours") or 0) for e in (ctx.week_assignments.get(name) or [])
                       if not e.get("prior"))
            if week + 0.05 < USUAL_HOURS_CUT * float(usual):
                hours_ok = False
                cut.append((name, float(usual), round(week, 1)))
        if day_ok and part_ok and hours_ok:
            familiar += 1
        elif not (day_ok and part_ok):
            changed.append(name)
    score = _pct(familiar, len(people))
    res = DimensionResult(
        key="stability", label="Schedule stability", score=score,
        weight=DEFAULT_WEIGHTS["stability"],
        facts={"familiar": familiar, "changed": changed, "checked": len(people),
               "hours_cut": [{"name": n, "usual": u, "week": w} for n, u, w in cut]},
    )
    if changed:
        res.weaknesses.append(
            f"{_names(changed[:3])} " + _plural(len(changed), "is", "are") +
            " on a shift they do not usually work.")
    for n, u, w in cut[:2]:
        res.weaknesses.append(f"{n} usually works about {u:g}h a week; {w:g}h this week.")
    if not changed and not cut:
        res.strengths.append("Everybody is on a shift they normally work.")
    return res


def dim_cross_training(ctx: ShiftContext) -> DimensionResult | None:
    """How much flex is on the floor if something goes wrong?

    A shift where every person can only do their own job has no answer to
    a no-show. One where half the floor can cover a second station does.

    Stations are role FAMILIES (schedule audit 10/3/26 SQ-10): "flexible"
    used to mean two role names in the history, so a server who worked
    "Server AM" and "Server PM" read as cross-trained; now it means two
    families among the roles they have worked, the roles they hold (trained,
    promoted — people.held_roles) and their roster role. A role nobody who
    works it can flex — no dishwasher on the roster can cover a second
    station — is not judged at all: scoring it 0 on every shift was a fact
    about training, not about this week's schedule.
    """
    if not ctx.cross_trained and not ctx.held_roles:
        return None
    people = ctx.people
    if not people:
        return None
    on_family, shown, raw_roles = {}, {}, {}
    for role, names in ctx.by_role.items():
        fam = role_family(role, ctx.role_families)
        on_family.setdefault(fam, []).extend(names)
        raw_roles.setdefault(fam, set()).add(role.strip())
    for fam, raws in raw_roles.items():
        shown[fam] = next(iter(raws)) if len(raws) == 1 else fam.title()
    working_as = {n: role_family(r, ctx.role_families) for r, names in ctx.by_role.items() for n in names}

    def _fams(n):
        f = _families_of(ctx, n)
        if n in working_as:
            f.add(working_as[n])
        return f

    flexible = [n for n in people if len(_fams(n)) > 1]
    # The families somebody who can flex works in — the roster, the history
    # and the people here. A family no such person works is not judged.
    everyone = set(ctx.cross_trained) | set(ctx.roster_roles) | set(people)
    can_flex = set()
    for n in everyone:
        f = _fams(n)
        if len(f) > 1:
            can_flex |= f
    for low, roles in (ctx.held_roles or {}).items():
        f = {role_family(r, ctx.role_families) for r in roles or ()}
        if not any((n or "").strip().lower() == low for n in everyone) and len(f) > 1:
            can_flex |= f
    owner_targets = {}
    for r, v in (ctx.cross_training_targets or {}).items():
        fam = role_family(r, ctx.role_families)
        try:
            owner_targets[fam] = max(float(owner_targets.get(fam, v)), float(v))
        except (TypeError, ValueError):
            continue
    # Judged role by role against that role's own target (the owner's, else
    # CROSS_TRAINING_DEFAULTS): a dish crew is not expected to flex the way
    # a bar is. A role whose target is 0 is not asked. Each role counts by
    # its headcount on the shift; past its target there is no extra credit.
    per_role, total_w, total, unjudged = {}, 0, 0.0, []
    for fam, names in on_family.items():
        target = cross_training_target_for(fam, owner_targets, ctx.cross_training_target)
        if target <= 0 or not names:
            continue
        if fam not in can_flex:
            unjudged.append(shown[fam])
            continue
        flex = [n for n in names if len(_fams(n)) > 1]
        role_score = min(SCORE_MAX, int(round(len(flex) / float(len(names)) / target * 100)))
        per_role[shown[fam]] = {"flexible": len(flex), "on_shift": len(names), "target": round(target, 2),
                                "score": role_score}
        total += role_score * len(names)
        total_w += len(names)
    if not total_w:
        if unjudged:
            ctx.notes.append("Cross-training was not judged for " + ", ".join(role_words(r, 2) for r in sorted(unjudged))
                             + " — nobody who works " + ("that role" if len(unjudged) == 1 else "those roles")
                             + " can cover a second station yet.")
        return None
    score = int(round(total / total_w))
    res = DimensionResult(
        key="cross_training", label="Cross-training", score=score,
        weight=DEFAULT_WEIGHTS["cross_training"],
        facts={"flexible": flexible, "on_shift": len(people), "by_role": per_role,
               "not_judged": sorted(unjudged)},
    )
    if flexible:
        res.strengths.append(
            f"{_names(flexible[:3])} can cover more than one station.")
    short = sorted((r for r, f in per_role.items() if f["score"] < SCORE_MAX),
                   key=lambda r: per_role[r]["score"])
    if not flexible:
        res.weaknesses.append("Nobody on this shift can cover a second station.")
    elif short:
        r = short[0]
        f = per_role[r]
        res.weaknesses.append(f"{f['flexible']} of {f['on_shift']} {role_words(r, f['on_shift'])} on this shift can cover another "
                              f"station — the target is {int(round(f['target'] * 100))}%.")
    return res


# What share of each role on a shift should be able to cover a second
# station when the owner has not said. A third is the long-standing default;
# a bar and a manager are expected to flex more (the bartender who runs food,
# the manager who jumps on the line), a dish crew and hosts less. Matched on
# the role's lowercase name, then on a word in it ("Line Cook" → "cook").
CROSS_TRAINING_DEFAULT = 0.34
CROSS_TRAINING_DEFAULTS = {
    "manager": 0.5, "bartender": 0.5, "barback": 0.34, "server": 0.34, "cook": 0.34,
    "prep": 0.34, "host": 0.25, "busser": 0.25, "runner": 0.25, "dishwasher": 0.2, "dish": 0.2,
}


def cross_training_target_for(role: str, owner: dict = None, fallback: float = None) -> float:
    """The share of `role` on a shift that should be able to cover another
    station: the owner's own number for the role (restaurants
    .role_cross_training_json), else the role's default, else the flat
    target. An owner's 0 means "not expected" and the role is not judged."""
    key = (role or "").strip().lower()
    owned = {str(k).strip().lower(): v for k, v in (owner or {}).items()}
    if key in owned and owned[key] is not None:
        try:
            return max(0.0, min(1.0, float(owned[key])))
        except (TypeError, ValueError):
            pass
    if key in CROSS_TRAINING_DEFAULTS:
        return CROSS_TRAINING_DEFAULTS[key]
    for word in key.replace("-", " ").split():
        if word in CROSS_TRAINING_DEFAULTS:
            return CROSS_TRAINING_DEFAULTS[word]
    try:
        return float(fallback) if fallback else CROSS_TRAINING_DEFAULT
    except (TypeError, ValueError):
        return CROSS_TRAINING_DEFAULT


# ── Coverage by the half hour ──────────────────────────────────────────────

SLOT_MINUTES = 30
DAYPART_CUTOVER = 15 * 60


@lru_cache(maxsize=4096)
def _clock(raw: str):
    """(hour, minute) of a normalised clock string ("4:30pm", "16:30"), or
    None. Remembered: the scorer reads the same few dozen times a thousand
    times a week score, and strptime was most of a score's cost (schedule
    audit 10/3/26 P-38)."""
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(raw, fmt)
            return t.hour, t.minute
        except ValueError:
            continue
    return None


def _slot_minutes(value: str):
    raw = (value or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    hm = _clock(raw)
    return hm[0] * 60 + hm[1] if hm else None


def _row_span(row: dict):
    """(start, end) minutes of a row on its own date; an end at or before
    its start runs past midnight. None when either time is unreadable."""
    s, e = _slot_minutes(row.get("shift_start")), _slot_minutes(row.get("shift_end"))
    if s is None or e is None:
        return None
    return (s, e + 24 * 60) if e <= s else (s, e)


def floor_window(daypart: str, open_minutes=None, close_minutes=None):
    """(lo, hi) minutes past midnight an owner's staffing floor for a
    daypart holds over: the daypart's core service window (CORE_WINDOWS),
    clipped to the hours the restaurant is open that day. None when the
    restaurant is not open through any of it.

    The ONE window the scorer's coverage by the hour and schedule_rules'
    coverage_floor breach hold a floor over (schedule audit 10/3/26 SQ-6).
    Held flat from the changeover to close, a 4-11pm and a 5-9pm server
    read as a floor of 2 broken from 9pm on — a critical failure capping
    the shift at 57 — while the hard rule counted both as on for dinner and
    passed: one screen, two verdicts. The floor is the owner's statement
    about service; the shoulders before and after it are the measured sales
    curve's to judge, where there is one."""
    win = CORE_WINDOWS.get(daypart)
    if not win:
        return None
    lo, hi = win
    if open_minutes is not None:
        lo = max(lo, int(open_minutes))
    if close_minutes is not None:
        close = int(close_minutes)
        # A close before 5am, or at or before opening, is after midnight.
        if close < 5 * 60 or (open_minutes is not None and close <= int(open_minutes)):
            close += 24 * 60
        hi = min(hi, close)
    return (lo, hi) if hi - lo >= SLOT_MINUTES else None


def floor_slots(daypart: str, open_minutes=None, close_minutes=None) -> list:
    """The half hours (on the clock's own half-hour grid) inside the floor
    window — the moments a floor is checked at."""
    win = floor_window(daypart, open_minutes, close_minutes)
    if not win:
        return []
    lo, hi = win
    return list(range(lo + (-lo) % SLOT_MINUTES, hi, SLOT_MINUTES))


def _on_at(spans, t: int, family: str) -> int:
    """Distinct people of `family` on at minute `t` — spans are
    (family, person, start, end). A person on two overlapping rows is one."""
    return len({p for f, p, s, e in spans if f == family and s <= t < e})


def floor_shortfall(rows: list, role: str, need: int, daypart: str, open_minutes=None,
                    close_minutes=None, families: dict = None, skip=None) -> dict:
    """Whether `need` people of `role` — its AM/PM job codes as one role
    (role_family, by `families`) — are on at every half hour of the
    daypart's floor window (floor_window), across one date's `rows`.

    The ONE floor test (SQ-6): the scorer's coverage by the hour runs it on
    every floor, and schedule_rules._coverage_violations is to call it with
    the same arguments for its coverage_floor breach, so the hard rule and
    the score cannot disagree about the same rows. `skip(row)` leaves out a
    row that does not count (the scorer's flagged rows).

    {"held", "window": (lo, hi) | None, "slots", "short_slots",
    "short_minutes", "worst_at": minute | None, "on_at_worst", "need"}.
    No window (no service in the core) is held."""
    slots = floor_slots(daypart, open_minutes, close_minutes)
    out = {"held": True, "window": floor_window(daypart, open_minutes, close_minutes), "slots": len(slots),
           "short_slots": 0, "short_minutes": 0, "worst_at": None, "on_at_worst": None, "need": int(need or 0)}
    if out["need"] <= 0 or not slots:
        return out
    fam = role_family(role, families)
    spans = []
    for r in rows or []:
        person = (r.get("employee") or "").strip().lower()
        if not person or role_family(r.get("role"), families) != fam or (skip is not None and skip(r)):
            continue
        sp = _row_span(r)
        if sp:
            spans.append((fam, person, sp[0], sp[1]))
    for t in slots:
        on = _on_at(spans, t, fam)
        if on < out["need"]:
            out["short_slots"] += 1
            if out["on_at_worst"] is None or on < out["on_at_worst"]:
                out["worst_at"], out["on_at_worst"] = t, on
    out["short_minutes"] = out["short_slots"] * SLOT_MINUTES
    out["held"] = not out["short_slots"]
    return out


def dim_coverage_curve(ctx: ShiftContext) -> DimensionResult | None:
    """Is every required position filled at every half hour, not just on
    average across the daypart?

    Two kinds of requirement. The owner's per-daypart floors (role_floors),
    else their whole-day role minimums, hold over the daypart's core
    service window (floor_window — the same test as the hard rule's
    coverage_floor, floor_shortfall). The measured sales curve's half-hour
    needs hold across the daypart's whole slice of the opening hours. Each
    role's AM/PM job codes are one role: a lunch server staying into
    dinner is a server at dinner. Without floors or a curve the question is
    not being asked.
    """
    required = {r: int(n) for r, n in (ctx.role_floors or {}).items() if n}
    source = "your staffing floors"
    if not required and ctx.role_minimums and ctx.open_minutes is not None:
        # Whole-day minimums are only an hourly requirement when the hours
        # they apply across are on file; without them the day's own shifts
        # would define the window and a thin day could never be short.
        # A whole-day minimum binds only the dayparts the role works, as in
        # dim_coverage: a bar that opens at 4pm is not "under 2" all lunch.
        runs_here = {r.strip().lower() for r in (ctx.typical_headcount or {})}
        required = {r: int(n) for r, n in ctx.role_minimums.items()
                    if n and (not runs_here or r.strip().lower() in runs_here)}
        source = "your role minimums"
    has_curve = bool(ctx.demand_curve and ctx.typical_headcount)
    has_late = bool(ctx.daypart == "night" and ctx.late_window and ctx.late_required)
    if not required and not has_curve and not has_late:
        return None
    rows = ctx.day_rows or ctx.rows
    spans = []
    for r in rows:
        sp = _row_span(r)
        if sp is None or ctx._is_flagged(r):
            continue
        spans.append((ctx.family(r.get("role")), (r.get("employee") or "").strip().lower(), sp[0], sp[1]))
    if not spans:
        return None
    day_open = ctx.open_minutes if ctx.open_minutes is not None else min(s for _f, _p, s, _e in spans)
    day_close = ctx.close_minutes if ctx.close_minutes is not None else max(e for _f, _p, _s, e in spans)
    if day_close <= day_open:
        day_close += 24 * 60
    # The daypart's window runs from open to the changeover (lunch) or the
    # changeover to close (dinner) — but the changeover is where the day's
    # own shifts hand over, not a fixed 3pm: lunch ending at 2:30 and dinner
    # starting at 4 is a handover, not an hour and a half "short".
    own = [sp for r in (ctx.rows or []) for sp in [_row_span(r)] if sp is not None and not ctx._is_flagged(r)]
    if ctx.daypart == "morning":
        latest = max((e for _s, e in own), default=DAYPART_CUTOVER)
        lo, hi = day_open, min(day_close, max(CORE_WINDOWS["morning"][1], min(latest, DAYPART_CUTOVER)))
    elif ctx.daypart == "night":
        earliest = min((s for s, _e in own), default=DAYPART_CUTOVER)
        lo, hi = max(day_open, min(CORE_WINDOWS["night"][0], max(earliest, DAYPART_CUTOVER))), day_close
    else:
        lo, hi = day_open, day_close
    if hi - lo < SLOT_MINUTES:
        return None

    # The floors hold through the core of service, where the hard rule
    # holds them; the sales curve judges every half hour of the daypart.
    fslots = set(floor_slots(ctx.daypart, ctx.open_minutes, ctx.close_minutes)) if required else set()
    fwin = floor_window(ctx.daypart, ctx.open_minutes, ctx.close_minutes) if required else None
    # Half-hour needs from this restaurant's own measured sales curve (at
    # least three same-weekday readings), across every half hour of service:
    # the busiest half hour needs most of the usual crew and each other one
    # that crew scaled by its share of the sales. Only the peak hour used to
    # carry a requirement, so the climb into the rush was never judged. The
    # section cap holds over both. Without a curve this is the floors alone.
    curve_needs = _half_hour_requirement(ctx, lo, hi)
    all_slots = list(range(lo + (-lo) % SLOT_MINUTES, hi, SLOT_MINUTES))
    # The late segment's own usual crew over its window, 10pm to close
    # (schedule audit 10/3/26 D-32): a 2am-close bar was judged on its dinner.
    late_needs = _late_requirement(ctx, all_slots) if has_late else {}
    peak_slot = None
    if curve_needs:
        from staffing_curve import half_hour_shares as _hhs
        _shares = _hhs(ctx.demand_curve or {})
        peak_slot = max(curve_needs, key=lambda m: (_shares.get(m, 0), -m))
    slots = sorted(fslots | (set(all_slots) if curve_needs else set()) | {t for t in late_needs if lo <= t < hi})
    total = covered = 0
    gaps = {}
    peak_gaps = []
    for t in slots:
        floor_here = dict(required) if t in fslots else {}
        here = dict(floor_here)
        for role, n in (curve_needs.get(t) or {}).items():
            match = next((r for r in here if r.strip().lower() == role.strip().lower()), role)
            here[match] = max(int(here.get(match) or 0), int(n))
        for role, n in (late_needs.get(t) or {}).items():
            match = next((r for r in here if r.strip().lower() == role.strip().lower()), role)
            here[match] = max(int(here.get(match) or 0), int(n))
        here, _over = _capped(ctx, here)
        # One requirement per role family: its job codes' needs added up,
        # named by the owner's floor where there is one.
        fam_need, fam_floor, fam_name, fam_curve, fam_late = {}, {}, {}, {}, {}
        for role, need in here.items():
            need = int(need or 0)
            if need <= 0:
                continue
            fam = ctx.family(role)
            fam_need[fam] = fam_need.get(fam, 0) + need
            fam_floor[fam] = fam_floor.get(fam, 0) + int(floor_here.get(role) or 0)
            fam_curve[fam] = fam_curve.get(fam, 0) + int((curve_needs.get(t) or {}).get(role) or 0)
            fam_late[fam] = fam_late.get(fam, 0) + int((late_needs.get(t) or {}).get(role) or 0)
            if fam not in fam_name or role in floor_here:
                fam_name[fam] = role
        for fam, need in fam_need.items():
            role = fam_name[fam]
            on = _on_at(spans, t, fam)
            total += 1
            if on >= need:
                covered += 1
                continue
            # What set this half hour's need above the owner's floor: the
            # late segment's usual crew (D-32), else the sales curve.
            from_late = (need > fam_floor.get(fam, 0) and fam_late.get(fam, 0) >= need
                         and need > fam_curve.get(fam, 0))
            from_curve = need > fam_floor.get(fam, 0) and not from_late
            if t == peak_slot and from_curve:
                peak_gaps.append({"role": role, "need": need, "on": on, "at": _fmt_minutes(t),
                                  "worst_minute": t})
            g = gaps.setdefault(role, {"short_slots": 0, "worst": None, "worst_on": None, "need": need,
                                       "from_curve": False, "from_late": False})
            g["short_slots"] += 1
            g["from_curve"] = g["from_curve"] or from_curve
            g["from_late"] = g.get("from_late") or from_late
            if g["worst"] is None or on < g["worst_on"] or (on == g["worst_on"] and need > g["need"]):
                g["worst"], g["worst_on"], g["need"] = t, on, need
    if not total:
        return None
    score = _pct(covered, total)
    for role, g in gaps.items():
        required.setdefault(role, g["need"])
    res = DimensionResult(
        key="coverage_curve", label="Coverage by the hour", score=score,
        weight=DEFAULT_WEIGHTS["coverage_curve"], floor=_floor(ctx, "coverage_curve"),
        facts={"required": required, "window": [_fmt_minutes(lo), _fmt_minutes(hi)],
               "floor_window": [_fmt_minutes(fwin[0]), _fmt_minutes(fwin[1])] if fwin else None,
               "slots": len(slots), "gaps": {r: {"minutes_short": g["short_slots"] * SLOT_MINUTES,
                                                  "worst_at": _fmt_minutes(g["worst"]), "on_at_worst": g["worst_on"],
                                                  "worst_minute": g["worst"], "need": g["need"],
                                                  "from_curve": g["from_curve"]}
                                             for r, g in gaps.items()},
               "requirement_source": source},
    )
    res.facts["peak_gaps"] = peak_gaps
    if curve_needs:
        from staffing_curve import ramp_runs as _rr, INTERPOLATED_NOTE as _inote
        res.facts["half_hour_needs"] = {role: [[_fmt_minutes(m), n] for m, n in pts]
                                        for role, pts in _rr(curve_needs).items()}
        res.facts["curve_basis"] = _inote
    for g in peak_gaps[:2]:
        res.weaknesses.append(f"{g['role']} has {g['on']} on at {g['at']}, the busiest hour by your sales — "
                              f"usually {g['need']}+ for the rush.")
    peak_roles = {g["role"] for g in peak_gaps[:2]}
    for role, g in sorted(gaps.items(), key=lambda kv: -kv[1]["short_slots"])[:3]:
        if not g["short_slots"] or (role in peak_roles and g["short_slots"] == 1):
            continue
        need = g["need"]
        if g.get("from_late") and not g["from_curve"]:
            res.weaknesses.append(
                f"{role} is under the {need} usually on late at night ({_fmt_minutes(ctx.late_window[0])} to "
                f"{_fmt_minutes(ctx.late_window[1])}) for {g['short_slots'] * SLOT_MINUTES // 60}h"
                f"{(g['short_slots'] * SLOT_MINUTES % 60) and ' 30m' or ''} — {g['worst_on']} on at "
                f"{_fmt_minutes(g['worst'])}.")
            continue
        if g["from_curve"]:
            res.weaknesses.append(
                f"{role} is under what your sales curve needs for {g['short_slots'] * SLOT_MINUTES // 60}h"
                f"{(g['short_slots'] * SLOT_MINUTES % 60) and ' 30m' or ''} — {g['worst_on']} on at "
                f"{_fmt_minutes(g['worst'])} against {need} (hourly sales, read to the half hour).")
            continue
        res.weaknesses.append(
            f"{role} is under {need} for {g['short_slots'] * SLOT_MINUTES // 60}h{(g['short_slots'] * SLOT_MINUTES % 60) and ' 30m' or ''} "
            f"of service — down to {g['worst_on']} at {_fmt_minutes(g['worst'])}.")
    if not gaps:
        held_lo, held_hi = fwin if (fwin and not curve_needs) else (lo, hi)
        res.strengths.append(f"Every floor is held from {_fmt_minutes(held_lo)} to {_fmt_minutes(held_hi)}.")
    in_window = {h: v for h, v in (ctx.demand_curve or {}).items() if lo <= int(h) * 60 < hi}
    if in_window:
        # The busiest hour of THIS daypart by sales share, and whether it is
        # the best-staffed — lunch was being told about the dinner peak.
        peak_hour = max(in_window.items(), key=lambda kv: kv[1])[0]
        on_peak = len({p for _f, p, s, e in spans if s <= int(peak_hour) * 60 < e})
        on_max = max((len({p for _f, p, s, e in spans if s <= t < e}) for t in slots), default=0)
        res.facts["peak_hour"] = int(peak_hour)
        res.facts["on_at_peak"] = on_peak
        if on_max and on_peak < on_max:
            res.weaknesses.append(
                f"Sales peak around {_fmt_minutes(int(peak_hour) * 60)} with {on_peak} on, while the day tops out at {on_max} on.")
    return res


def _late_requirement(ctx: ShiftContext, slots) -> dict:
    """{minute: {role: people}} for the half-hour slots that fall in the
    late segment: the late row's people (the requirements table's,
    demand-scaled like every other row) at every half hour from 10pm to
    close (D-32). {} without a late window or crew."""
    if not ctx.late_window or not ctx.late_required:
        return {}
    a, b = ctx.late_window
    need = {r: int(n) for r, n in ctx.late_required.items() if int(n or 0) > 0}
    return {t: dict(need) for t in slots or () if a <= t < b} if need else {}


# Share of a role's usual daypart headcount that should overlap the
# daypart's busiest measured hour.
PEAK_SHARE = 0.75


def _half_hour_requirement(ctx: ShiftContext, lo: int, hi: int) -> dict:
    """{minute: {role: people}} for this daypart's half hours of service,
    from the measured sales curve and the usual crew (staffing_curve
    .half_hour_needs) — {} without both. The same function the requirements
    table the model reads is built from."""
    if not ctx.demand_curve or not ctx.typical_headcount:
        return {}
    from staffing_curve import half_hour_needs
    # Normalised over the daypart's whole half of the day, as the
    # requirements table is (schedule_requirements._service_window), so the
    # two agree on every half hour; then only this shift's window is judged.
    half = (0, DAYPART_CUTOVER) if ctx.daypart == "morning" else (DAYPART_CUTOVER, 48 * 60)
    needs = half_hour_needs(ctx.demand_curve, ctx.typical_headcount, *half)
    return {m: n for m, n in needs.items() if lo <= m < hi}


def _peak_requirement(ctx: ShiftContext):
    """(hour, {role: people}) for this daypart's busiest hour, from the
    measured sales curve and the usual crew — or None without both."""
    if not ctx.demand_curve or not ctx.typical_headcount:
        return None
    lo, hi = (0, DAYPART_CUTOVER // 60) if ctx.daypart == "morning" else (DAYPART_CUTOVER // 60, 24)
    hours = {int(h): v for h, v in ctx.demand_curve.items() if lo <= int(h) < hi and v}
    if not hours:
        return None
    hour = max(hours.items(), key=lambda kv: kv[1])[0]
    import math
    needs = {role: max(1, int(math.ceil(PEAK_SHARE * int(n)))) for role, n in ctx.typical_headcount.items() if n}
    return (hour, needs) if needs else None


def _fmt_minutes(m):
    if m is None:
        return ""
    m %= 24 * 60
    h, mm = divmod(m, 60)
    return f"{h % 12 or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


# ── Reliability ────────────────────────────────────────────────────────────

UNRELIABLE_RATE = 0.2


def _late_at_the_edges(ctx: ShiftContext) -> list:
    """[(name, role, "opens" | "closes", late_rate)] — people on this shift
    with a lateness risk (their reliability's `late_risk`: late to at least
    staff_settings.LATE_RISK_RATE of their clocked shifts, said only once
    staff_settings.LATE_MIN_SHIFTS of them exist) who are the only one of
    their role at its first start, or its last finish, that date (schedule
    audit 10/3/26 D-44)."""
    risky = {str(n).strip().lower(): r for n, r in (ctx.reliability or {}).items() if (r or {}).get("late_risk")}
    if not risky:
        return []
    mine = {(r.get("employee") or "").strip().lower() for r in ctx.rows if not ctx._is_flagged(r)}
    by_role = {}
    for r in (ctx.day_rows or ctx.rows):
        if not (r.get("employee") or "").strip() or ctx._is_flagged(r):
            continue
        s, e = _span(r)
        if s is not None:
            by_role.setdefault((r.get("role") or "").strip().lower(), []).append((s, e, r))
    out = []
    for role, spans in by_role.items():
        first, last = min(s for s, _e, _r in spans), max(e for _s, e, _r in spans)
        for edge, who in (("opens", [r for s, _e, r in spans if s == first]),
                          ("closes", [r for _s, e, r in spans if e == last])):
            if len(who) != 1:
                continue                    # somebody else of the role is there at that minute
            n = (who[0].get("employee") or "").strip()
            if n.lower() in mine and n.lower() in risky:
                out.append((n, who[0].get("role") or role, edge, float(risky[n.lower()].get("late_rate") or 0)))
    return out


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
    # Lateness at the edges of a role's day (schedule audit 10/3/26 D-44):
    # reliability read no-shows only, so chronic lateness never weighed on
    # who opens or closes. Somebody with a lateness risk alone at their
    # role's first start or last finish counts against the shift too.
    late = _late_at_the_edges(ctx)
    late_only = {n.lower() for n, *_ in late} - {n.lower() for n, *_ in exposed}
    score = _pct(len(known) - len(exposed) - len(late_only), len(known))
    res = DimensionResult(key="reliability", label="Reliability", score=score,
                          weight=DEFAULT_WEIGHTS["reliability"],
                          facts={"at_risk": at_risk, "exposed": [{"name": n, "role": r, "no_show_rate": rt}
                                                                  for n, r, rt, _ in exposed],
                                 "late_exposed": [{"name": n, "role": r, "edge": edge, "late_rate": rt}
                                                  for n, r, edge, rt in late],
                                 "checked": len(known)})
    for n, role, rate, count in exposed[:2]:
        why = "alone on " + role.lower() if count == 1 else "on a busy shift"
        res.weaknesses.append(f"{n} has missed {int(round(rate * 100))}% of scheduled shifts and is {why}.")
    for n, role, edge, rate in late[:2]:
        res.weaknesses.append(f"{n} is late to {int(round(rate * 100))}% of their clocked shifts and is the only "
                              f"{role_words(role)} {'opening' if edge == 'opens' else 'closing'}.")
    if not exposed and not late:
        res.strengths.append("No station rests on somebody with an attendance problem.")
    unknown = [n for n in ctx.people if n not in ctx.reliability]
    if unknown:
        res.blind_spots.append(f"No clock-in history for {_names(unknown[:3])}, so attendance is unknown.")
    return res


# ── Pairings ───────────────────────────────────────────────────────────────

# What one preferred pair split across a shift costs the Pairings score
# (a kept-apart pair on together costs 45).
PAIR_SPLIT_COST = 20


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
    # A pair the owner likes together with one of them on and the other not
    # (owner, 9/30/26: Erik's "prefer" pairs moved nothing - only a clash
    # ever cost a point, so a preferred pair was text in the prompt and no
    # pass could see it). A split costs less than a clash: preferring is a
    # wish, keeping apart is a rule.
    splits = [p for p in prefer if sum(1 for x in p if x in on) == 1]
    if not clashes and not matches and not splits:
        return None
    score = max(0, SCORE_MAX - 45 * len(clashes) - PAIR_SPLIT_COST * len(splits))
    # A pairing from an owner-only rule counts and is never named: the
    # review is shared with the team (schedule audit 10/3/26 D-38).
    private = (ctx.pairs or {}).get("private") or set()
    res = DimensionResult(key="pairings", label="Pairings", score=score,
                          weight=DEFAULT_WEIGHTS["pairings"],
                          facts={"clashes": [sorted(on[x] for x in p) for p in clashes if p not in private],
                                 "matches": [sorted(on[x] for x in p) for p in matches if p not in private],
                                 "splits": [sorted(x for x in p) for p in splits if p not in private],
                                 "private": sum(1 for p in clashes + matches + splits if p in private)})
    for p in clashes[:2]:
        if p in private:
            res.weaknesses.append("Two people one of your owner-only rules keeps apart are on together.")
            continue
        a, b = sorted(on[x] for x in p)
        res.weaknesses.append(f"{a} and {b} are on together, and you asked to keep them apart.")
    matches = [p for p in matches if p not in private]
    splits = [p for p in splits if p not in private]
    for p in matches[:2]:
        a, b = sorted(on[x] for x in p)
        res.strengths.append(f"{a} and {b} are on together, as you prefer.")
    for p in splits[:2]:
        here = next(on[x] for x in p if x in on)
        other = next(x for x in p if x not in on)
        res.weaknesses.append(f"{here} is on without {other.title()}, and you prefer them together.")
    return res


PREFERENCE_HOURS_BAND = 0.2
# What a preference staff showed by what they drop and claim (schedule_intel.
# behaviour_preferences: two or more of the same slot) counts against one
# they stated themselves (schedule audit 10/3/26 L-19): it reached only the
# prompt, so somebody who dropped Sunday nights every time kept being handed
# them by every pass that chose by the score.
LEARNED_PREFERENCE_WEIGHT = 0.5


def _parts_words(parts) -> str:
    return " or ".join("days" if x == "morning" else "nights" for x in parts)


def _slot_words(slot) -> str:
    day, part = slot
    return f"{day} {'day' if part == 'morning' else 'night'}"


def _learned(lp: dict) -> tuple:
    """(avoid slots, prefer slots, weight) of one person's learned preferences."""
    def _slots(key):
        return {tuple(s) for s in (lp.get(key) or []) if isinstance(s, (list, tuple)) and len(s) == 2}
    try:
        w = float(lp.get("weight") if lp.get("weight") is not None else LEARNED_PREFERENCE_WEIGHT)
    except (TypeError, ValueError):
        w = LEARNED_PREFERENCE_WEIGHT
    return _slots("avoid"), _slots("prefer"), max(0.0, min(1.0, w))


def dim_preferences(ctx: ShiftContext) -> DimensionResult | None:
    """What people want, for the people on ONE shift: the daypart they said
    they prefer, and a slot they keep dropping or claiming (learned, at
    LEARNED_PREFERENCE_WEIGHT of a stated preference). The week score judges
    preferences once for the week (week_preferences, WEEK_LEVEL_DIMENSIONS);
    this is kept for callers asking about one shift's people. The hours a
    person asked for are a fact about their week and are judged only there."""
    prefs = ctx.preferences or {}
    learned = ctx.learned_preferences or {}
    if not prefs and not learned:
        return None
    checked = met = 0.0
    misses = []
    for name in ctx.people:
        parts = [x for x in ((prefs.get(name) or {}).get("preferred_dayparts") or []) if x in ("morning", "night")]
        if parts:
            checked += 1
            if ctx.daypart in parts:
                met += 1
            else:
                misses.append(f"{name} prefers {_parts_words(parts)}")
        if learned.get(name):
            avoid, prefer, w = _learned(learned[name])
            slot = (ctx.day, ctx.daypart)
            if slot in avoid:
                checked += w
                misses.append(f"{name} keeps asking to drop {_slot_words(slot)}")
            elif slot in prefer:
                checked += w
                met += w
    if not checked:
        return None
    res = DimensionResult(key="preferences", label="Staff preferences", score=_pct(met, checked),
                          weight=DEFAULT_WEIGHTS["preferences"],
                          facts={"checked": round(checked, 2), "met": round(met, 2), "misses": misses})
    if misses:
        res.weaknesses.append("; ".join(misses[:2]) + ".")
    else:
        res.strengths.append("Everybody here is on the shift they asked for.")
    return res


def week_preferences(contexts: list) -> DimensionResult | None:
    """Staff preferences judged once for the week (schedule audit 10/3/26
    SQ-24). For each person working it: each of their shifts on a daypart
    they said they prefer, or not; their week against the hours they asked
    for, within PREFERENCE_HOURS_BAND — ONCE, where it used to be charged
    again on every shift they worked (a 30h-wanting server on five shifts
    was five misses for one fact, more on a peak night, the same mistake
    fatigue was moved off the shifts for); and each shift on a slot they keep
    dropping, or claiming, at LEARNED_PREFERENCE_WEIGHT of a stated one
    (L-19). Never a rule, weighted lightly, silent for anybody who has
    neither stated nor shown a preference."""
    if not contexts:
        return None
    ctx = contexts[0]
    prefs = ctx.preferences or {}
    learned = ctx.learned_preferences or {}
    if not prefs and not learned:
        return None
    working = set()
    for c in contexts:
        working.update(c.people)
    checked = met = 0.0
    misses, learned_misses, strained = [], [], set()
    for name in sorted(working):
        mine = [e for e in (ctx.week_assignments.get(name) or []) if e.get("date") and not e.get("prior")]
        if not mine:
            continue
        p = prefs.get(name) or {}
        parts = [x for x in (p.get("preferred_dayparts") or []) if x in ("morning", "night")]
        if parts:
            judged = [e for e in mine if e.get("daypart") in ("morning", "night")]
            off = [e for e in judged if e.get("daypart") not in parts]
            checked += len(judged)
            met += len(judged) - len(off)
            if off:
                misses.append(f"{name} prefers {_parts_words(parts)} and has {len(off)} of {len(judged)} "
                              f"{_plural(len(judged), 'shift')} on {_parts_words([x for x in ('morning', 'night') if x not in parts])}")
                strained.add(name)
        try:
            want = float(p.get("desired_hours")) if p.get("desired_hours") else None
        except (TypeError, ValueError):
            want = None
        if want:
            have = round(sum(float(e.get("hours") or 0) for e in mine), 1)
            checked += 1
            if abs(have - want) <= want * PREFERENCE_HOURS_BAND:
                met += 1
            else:
                misses.append(f"{name} asked for about {want:g}h and has {have:g}h")
                strained.add(name)
        if learned.get(name):
            avoid, prefer, w = _learned(learned[name])
            for e in mine:
                slot = (e.get("day"), e.get("daypart"))
                if slot in avoid:
                    checked += w
                    learned_misses.append(f"{name} keeps asking to drop {_slot_words(slot)} and is on it")
                    strained.add(name)
                elif slot in prefer:
                    checked += w
                    met += w
    if not checked:
        return None
    res = DimensionResult(key="preferences", label="Staff preferences", score=_pct(met, checked),
                          weight=DEFAULT_WEIGHTS["preferences"],
                          facts={"checked": round(checked, 2), "met": round(met, 2), "misses": misses,
                                 "learned_misses": learned_misses, "learned_weight": LEARNED_PREFERENCE_WEIGHT,
                                 "strained": sorted(strained), "scope": "week"})
    lines = misses + learned_misses
    if lines:
        res.weaknesses.append("; ".join(lines[:2]) + ".")
    else:
        res.strengths.append("Everybody working this week is on the shifts and hours they asked for.")
    return res


# ── Overtime ───────────────────────────────────────────────────────────────

# The overtime premium the week runs up, against the week's hourly pay
# (schedule audit 10/3/26 SQ-25). Overtime pay starts past `line`
# (labor.OVERTIME_THRESHOLD_HOURS, 40h) in the payroll week, the hours
# already published in that payroll week included, and costs the half of
# time-and-a-half — the overtime forecast's own premium. Each 1% of the
# week's hourly pay spent on premium a same-role teammate with room could
# have avoided costs OVERTIME_POINTS; overtime nobody else could take costs
# UNAVOIDABLE_OVERTIME_SHARE of that — a real cost, but not a choice this
# schedule made. Salaried people owe none.
OVERTIME_PREMIUM = 0.5
OVERTIME_POINTS = 25
UNAVOIDABLE_OVERTIME_SHARE = 0.5


def week_overtime(contexts: list) -> DimensionResult | None:
    """The overtime premium the week runs up — judged once for the week, a
    payroll-week fact. Withdraws without the overtime inputs (the engine
    supplies them: the line, each date's payroll week, the hours already
    published, the rates, a daily line where daily overtime applies) or
    without any hourly hours to judge. Counted the way the week is priced
    (schedule_economics.priced_cost): the hours past `daily_line` in a day
    are overtime, and do not also count toward the weekly line."""
    if not contexts:
        return None
    ctx = contexts[0]
    ot = ctx.overtime or {}
    try:
        line = float(ot.get("line") or 0)
    except (TypeError, ValueError):
        line = 0.0
    if line <= 0:
        return None
    bucket_of = ot.get("bucket_of") or {}
    published = ot.get("published") or {}
    raw_rates = ot.get("rates") or {}
    rates = {}
    for k, v in raw_rates.items():
        try:
            if k != "_default" and v:
                rates[str(k).strip().lower()] = float(v)
        except (TypeError, ValueError):
            continue
    try:
        default_rate = float(ot.get("default_rate") or raw_rates.get("_default") or 0) or None
    except (TypeError, ValueError):
        default_rate = None
    priced = bool(rates or default_rate)

    def _rate(role):
        r = rates.get((role or "").strip().lower())
        return r if r else (default_rate or (None if priced else 1.0))

    rows_by_date = {}
    for c in contexts:
        if c.date not in rows_by_date:
            rows_by_date[c.date] = [r for r in (c.day_rows or c.rows) if not c._is_flagged(r)]
    rows = [r for rs in rows_by_date.values() for r in rs if (r.get("employee") or "").strip()]
    hours, role_hours, display, working = {}, {}, {}, set()
    cost = 0.0
    for r in rows:
        n = r["employee"].strip()
        working.add((name_key(n), r.get("date")))
        if ctx.is_salaried(n):
            continue
        h = _row_hours(r)
        key = (name_key(n), bucket_of.get(r.get("date"), ""))
        hours[key] = hours.get(key, 0.0) + h
        role = (r.get("role") or "").strip()
        role_hours.setdefault(key, {})[role] = role_hours.get(key, {}).get(role, 0.0) + h
        display[name_key(n)] = n
        cost += h * (_rate(role) or 0.0)
    if cost <= 0:
        return None

    def _published(k, b):
        v = published.get(k) or published.get(display.get(k, k), 0) or 0
        if isinstance(v, dict):
            return float(v.get(b, 0) or 0)
        return float(v)

    def _total(k, b):
        return hours.get((k, b), 0.0) + _published(k, b)

    try:
        daily_line = float(ot.get("daily_line") or 0)
    except (TypeError, ValueError):
        daily_line = 0.0
    # Overtime hours and premium per (person, payroll week), shift by shift
    # in order, as priced_cost prices them.
    ot_hours_of, premium_of = {}, {}
    by_person = {}
    for r in rows:
        n = r["employee"].strip()
        if ctx.is_salaried(n):
            continue
        by_person.setdefault(name_key(n), []).append(
            (r.get("date") or "", _slot_minutes(r.get("shift_start")) or 0, _row_hours(r), (r.get("role") or "").strip()))
    for k, items in by_person.items():
        so_far, day_used = {}, {}
        for d, _s, h, role in sorted(items):
            b = bucket_of.get(d, "")
            if b not in so_far:
                so_far[b] = _published(k, b)
            daily_ot = 0.0
            if daily_line > 0:
                used = day_used.get(d, 0.0)
                daily_ot = max(0.0, h - max(0.0, daily_line - used))
                day_used[d] = used + h
            weekly_part = h - daily_ot
            reg = min(weekly_part, max(0.0, line - so_far[b]))
            extra = daily_ot + (weekly_part - reg)
            so_far[b] += weekly_part
            if extra > 0:
                ot_hours_of[(k, b)] = ot_hours_of.get((k, b), 0.0) + extra
                premium_of[(k, b)] = premium_of.get((k, b), 0.0) + extra * (_rate(role) or 0.0) * OVERTIME_PREMIUM

    # The cheap half of the swap index's legality (_SwapIndex.person_fits):
    # approved time off, a pending request, the day or daypart they can't
    # work, a deactivated name. Enough to tell avoidable overtime from the
    # rest on every option a pass scores; the move itself is re-checked in
    # full by whatever makes it (the rebalance, the optimizer).
    rules = ot.get("rules") or {}
    blocked = {str(k).lower(): v for k, v in (rules.get("blocked_dates") or {}).items()}
    pending = {str(k).lower(): set(v or ()) for k, v in (rules.get("pending_off") or {}).items()}
    by_daypart = {str(k).lower(): v for k, v in (rules.get("daypart_avail") or {}).items()}
    inactive = {str(n).lower() for n in (rules.get("inactive") or [])}

    def _free(other, r):
        low = (other or "").strip().lower()
        day = _day_name(r.get("date"), r.get("day", ""))
        if low in inactive or r.get("date") in (blocked.get(low) or {}) or r.get("date") in pending.get(low, ()):
            return False
        if _unavailable(ctx.availability or {}, other, day):
            return False
        choice = (by_daypart.get(low) or {}).get(day)
        return choice != "off" and works_daypart_ok(r, choice)

    people_by_family = {}
    for n, role in (ctx.roster_roles or {}).items():
        people_by_family.setdefault(role_family(role, ctx.role_families), set()).add(n)
    for r in rows:
        people_by_family.setdefault(role_family(r.get("role"), ctx.role_families), set()).add(r["employee"].strip())

    def _teammate(k, b, role):
        """Somebody hourly in the same role family with room under the line
        for one of this person's shifts in that payroll week."""
        mine = sorted((r for r in rows if name_key(r["employee"]) == k and bucket_of.get(r.get("date"), "") == b),
                      key=lambda r: _row_hours(r))
        fam = role_family(role, ctx.role_families)
        for r in mine:
            for other in sorted(people_by_family.get(fam) or ()):
                ok = name_key(other)
                if ok == k or ctx.is_salaried(other) or (ok, r.get("date")) in working:
                    continue
                cap = ctx.ceiling_for(other)
                room = min(line, cap) if cap else line
                if _total(ok, b) + _row_hours(r) > room + 0.05 or not _free(other, r):
                    continue
                return other
        return None

    over = []
    for (k, b), h in sorted(hours.items()):
        ot_hours = ot_hours_of.get((k, b), 0.0)
        if ot_hours <= 0.05:
            continue
        role = max(role_hours[(k, b)].items(), key=lambda kv: (kv[1], kv[0]))[0]
        teammate = _teammate(k, b, role)
        over.append({"name": display.get(k, k), "bucket": b, "hours": round(_total(k, b), 1),
                     "published_hours": round(_published(k, b), 1), "overtime_hours": round(ot_hours, 1),
                     "premium": round(premium_of.get((k, b), 0.0), 2), "role": role,
                     "avoidable": bool(teammate), "teammate": teammate})
    weighted = sum(o["premium"] * (1.0 if o["avoidable"] else UNAVOIDABLE_OVERTIME_SHARE) for o in over)
    share = weighted / cost * 100.0
    score = max(0, int(round(SCORE_MAX - OVERTIME_POINTS * share)))
    res = DimensionResult(
        key="overtime", label="Overtime", score=score, weight=DEFAULT_WEIGHTS["overtime"],
        facts={"people": over, "line": line, "priced": priced,
               "premium": round(sum(o["premium"] for o in over), 2) if priced else None,
               "premium_hours": round(sum(o["overtime_hours"] for o in over) * OVERTIME_PREMIUM, 2),
               "week_pay": round(cost, 2) if priced else None, "share_pct": round(share, 2),
               "strained": sorted(o["name"] for o in over), "scope": "week"})
    if not over:
        res.strengths.append(f"Nobody is scheduled past {line:g}h in a payroll week.")
        return res
    from time_utils import mdy
    for o in sorted(over, key=lambda o: (-o["premium"], -o["overtime_hours"], o["name"]))[:2]:
        when = f" in the payroll week of {mdy(o['bucket'])}" if o["bucket"] else ""
        cost_words = (f" — about ${o['premium']:,.0f} of overtime premium" if priced and o["premium"]
                      else f" — {o['overtime_hours'] * OVERTIME_PREMIUM:g}h of pay at the overtime premium")
        tail = (f"; {o['teammate']} has room in the same role for one of their shifts." if o["avoidable"]
                else "; nobody else in the role has room to take a shift.")
        res.weaknesses.append(f"{o['name']} is {o['overtime_hours']:g}h into overtime{when}{cost_words}{tail}")
    return res


# ── What the restaurant's scheduling has learned (L-3, D-35) ───────────────

# Points of the measure one broken memory costs at full confidence, from a
# week that breaks none: a slot memory at 0.8 is about 20 of them — some 1.7
# points of a fourteen-shift week at the default weight, several times what a
# leader on one weekday dinner earns it, so a move or a fill that puts "Bob
# back on Tuesday dinner" for the generic reasons no longer wins; only a real
# fix (a shift short, a station nobody can work) still outweighs what the
# managers keep doing. Each further unit of broken weight costs the same
# share of what is left (geometric, never a flat floor): a role's start time
# missed on six rows still shows each row put right.
LEARNED_MISS_POINTS = 25
_MEALS = {"morning": "lunch", "night": "dinner"}


def _learned_line(m: dict, miss: dict) -> str:
    """One owner-facing sentence for a memory the week breaks."""
    kind = m.get("kind")
    v = m.get("value") if isinstance(m.get("value"), dict) else {}
    who = (m.get("person") or "").strip()
    day, meal = m.get("day") or "", _MEALS.get(m.get("daypart"), m.get("daypart") or "")
    slot = " ".join(x for x in (day, meal) if x)
    role = role_words(v.get("role") or m.get("role") or "") if (v.get("role") or m.get("role")) else "staff"
    if kind == "moved_off":
        return f"{who} is on {slot}; your managers keep taking them off it."
    if kind == "moved_on":
        return f"{who} is not on {slot}; your managers keep putting them on it."
    if kind in ("retime_start", "retime_end"):
        edge = "start" if kind == "retime_start" else "end"
        return f"{role.capitalize()} shifts on {slot} do not {edge} at {v.get('time')}, where your managers keep setting them."
    if kind == "role_change":
        return f"{who} is on {slot} in another role; your managers keep making them {v.get('role')}."
    if kind == "leader_swap":
        return f"None of {_names(list(v.get('names') or []))} is on {slot}; your managers keep putting one of them there."
    if kind == "opener":
        return f"{who} is on {day} but not opening {role}, which they usually do."
    if kind == "closer":
        return f"{who} is on {day} but not closing {role}, which they usually do."
    if kind == "pair" and v.get("kind") == "avoid":
        # The owner's own keep-apart: counted, never named — the review is
        # shared with the team (schedule re-audit 10/4/26 LEARN-1, LEARN-6).
        return "Two people you chose to keep apart are on the same shift."
    if kind == "pair":
        return f"{who} and {_names(list(v.get('with') or []))} are on different shifts; their shifts together run well."
    if kind == "end_overrun":
        return (f"{role.capitalize()} closes on {slot} end before {v.get('padded_end')}; they usually run about "
                f"{v.get('minutes')} minutes past the scheduled end.")
    return str(miss.get("text") or "")


def learned_score(lost: float) -> int:
    """The learned-patterns measure for `lost` units of broken memory weight
    (schedule_memory.misses' weights): SCORE_MAX less LEARNED_MISS_POINTS of
    what is left per unit."""
    return int(round(SCORE_MAX * (1 - LEARNED_MISS_POINTS / 100.0) ** max(0.0, float(lost or 0))))


def week_learned(contexts: list) -> DimensionResult | None:
    """What this restaurant's scheduling has learned and the week breaks
    (schedule audit 10/3/26 L-3, D-35): the active memories the passes are
    held to (signals["learned"], schedule_memory.enforced_signals — somebody
    the managers keep taking off a slot or putting on one, the role's usual
    opener or closer, a team whose shifts together run well, somebody who
    habitually runs past their shift, closes that run late), each broken one
    costing LEARNED_MISS_POINTS of what is left at its weight (learned_score;
    schedule_memory.misses — the meaning of every kind lives there, once).
    Judged once for the week. The
    patterns reached only the prompt, so a fill, the trim, the solver or the
    optimizer put back the edit the manager kept making, and the score said
    nothing. None without an active memory."""
    if not contexts:
        return None
    ctx = contexts[0]
    # A memory enforced only in the prompt stays there (enforced_signals
    # hands over none; a caller's list may).
    learned = [m for m in (ctx.learned or []) if isinstance(m, dict) and m.get("kind")
               and str(m.get("enforcement") or "soft").lower() != "prompt"]
    if not learned:
        return None
    rows_by_date = {}
    for c in contexts:
        if c.date not in rows_by_date:
            rows_by_date[c.date] = [r for r in (c.day_rows or c.rows) if not c._is_flagged(r)]
    rows = [r for d in sorted(rows_by_date) for r in rows_by_date[d] if (r.get("employee") or "").strip()]
    ot = ctx.overtime or {}
    bucket_of = ot.get("bucket_of") or {}
    try:
        line = float(ot.get("line") or 0) or None
    except (TypeError, ValueError):
        line = None
    import schedule_memory as _smem          # pure: the one meaning of each memory
    found = _smem.misses(rows, learned, families=ctx.role_families or None, line=line,
                         bucket=(lambda d: bucket_of.get(d, "")) if bucket_of else None)
    by_key = {m.get("key"): m for m in learned}
    misses = []
    for x in found:
        m = by_key.get(x.get("key")) or {}
        rs = [rows[i] for i in (x.get("indexes") or []) if 0 <= i < len(rows)]
        if x.get("kind") == "pair" and (m.get("value") or {}).get("kind") == "avoid":
            # The owner's own keep-apart (schedule re-audit 10/4/26 LEARN-1,
            # LEARN-6): its cost counts and its shifts are named (as an
            # owner-only pairing's are), never its people — the optimizer
            # reads the memory itself by its key.
            misses.append({"key": x.get("key"), "kind": "pair", "weight": float(x.get("weight") or 0),
                           "person": None, "day": m.get("day"), "daypart": m.get("daypart"), "role": None,
                           "value": {"kind": "avoid", "private": True}, "rows": [],
                           "slots": sorted({(r.get("date") or "", present_dayparts(r)[0]) for r in rs}),
                           "text": _learned_line(m, x)})
            continue
        misses.append({"key": x.get("key"), "kind": x.get("kind"), "weight": float(x.get("weight") or 0),
                       "person": m.get("person"), "day": m.get("day"), "daypart": m.get("daypart"),
                       "role": m.get("role"), "value": dict(m["value"]) if isinstance(m.get("value"), dict) else {},
                       "rows": [{"employee": (r.get("employee") or "").strip(), "date": r.get("date") or "",
                                 "shift_start": r.get("shift_start") or "", "shift_end": r.get("shift_end") or "",
                                 "role": r.get("role") or "", "daypart": present_dayparts(r)[0]} for r in rs],
                       "text": _learned_line(m, x) if x.get("kind") != "ot_risk" else str(x.get("text") or "")})
    lost = sum(x["weight"] for x in misses)
    score = learned_score(lost)
    res = DimensionResult(key="learned", label=DIMENSION_LABELS["learned"], score=score,
                          weight=DEFAULT_WEIGHTS["learned"],
                          facts={"memories": len(learned), "broken": len(misses), "misses": misses,
                                 "strained": sorted({x["person"] for x in misses if x.get("person")}),
                                 "scope": "week"})
    for x in sorted(misses, key=lambda x: -x["weight"])[:3]:
        res.weaknesses.append(x["text"])
    if not misses:
        res.strengths.append(f"Keeps all {len(learned)} thing{'s' if len(learned) != 1 else ''} your managers "
                             "keep doing by hand.")
    return res


# ── Kitchen stations ───────────────────────────────────────────────────────

# Under this share of a daypart's required stations held by a trained cook,
# the shift is capped — a grill nobody on the line can work is a kitchen
# that cannot run its menu, whatever else the shift has (SQ-26).
STATIONS_FLOOR = 70


def dim_stations(ctx: ShiftContext) -> DimensionResult | None:
    """Is every kitchen station this daypart needs held by a cook trained on
    it? (schedule audit 10/3/26 SQ-26.) The engine enforced stations
    outside the score (schedule_engine._ensure_station_coverage), so a draft
    the station pass could not finish scored as if its kitchen were whole.
    Read exactly as that pass and the station report read it
    (kitchen_stations: the needs for this weekday and daypart, the kitchen
    rows on the floor for it, each cook matched to at most one station they
    are trained on). Withdraws when the owner has set no stations, or none
    is needed on this daypart."""
    cfg = ctx.stations or {}
    if not cfg or ctx.daypart not in ("morning", "night"):
        return None
    import kitchen_stations as _ks          # pure; the one matching of cooks to stations
    need = _ks.required(cfg, ctx.day, ctx.daypart)
    if not need:
        return None
    cooks = []
    for r in (ctx.day_rows or ctx.rows):
        n = (r.get("employee") or "").strip()
        if (not n or ctx._is_flagged(r) or not _ks.is_kitchen(cfg, r.get("role"))
                or ctx.daypart not in _ks.parts_of(r)):
            continue
        if n not in cooks:
            cooks.append(n)
    placed, unmet = _ks.assign(cooks, need, cfg)
    filled = len(need) - len(unmet)
    part = "lunch" if ctx.daypart == "morning" else "dinner"
    res = DimensionResult(
        key="stations", label="Kitchen stations", score=_pct(filled, len(need)),
        weight=DEFAULT_WEIGHTS["stations"], floor=STATIONS_FLOOR,
        facts={"required": list(need), "assigned": dict(placed), "gaps": list(unmet), "cooks": cooks})
    for st in sorted(set(unmet), key=need.index):
        trained = sorted(p for p, have in (cfg.get("skills") or {}).items() if st in (have or []))
        off = [p for p in trained if p not in cooks]
        res.weaknesses.append(
            f"{st} has no trained cook on {ctx.day} {part}"
            + (f" — {_names(off[:3])} {'is' if len(off) == 1 else 'are'} trained on it." if off
               else " — nobody is marked trained on it yet."))
    if not unmet:
        res.strengths.append("Every station this shift needs has a trained cook: "
                             + ", ".join(f"{st} ({c})" for c, st in sorted(placed.items(), key=lambda kv: need.index(kv[1])))
                             + ".")
    return res


DIMENSIONS = {
    "coverage": dim_coverage,
    "coverage_curve": dim_coverage_curve,
    "operational_strength": dim_operational_strength,
    "leadership": dim_leadership,
    "demand_match": dim_demand_match,
    "labor_efficiency": dim_labor_efficiency,
    "splh": dim_splh,
    "experience_balance": dim_experience_balance,
    "training_balance": dim_training_balance,
    "reliability": dim_reliability,
    "pairings": dim_pairings,
    "fatigue": dim_fatigue,
    "fairness": dim_fairness,
    "preferences": dim_preferences,
    "stability": dim_stability,
    "cross_training": dim_cross_training,
    "min_hours": dim_min_hours,
    "overtime": week_overtime,          # a payroll-week fact: judged only for the week
    "stations": dim_stations,
    "learned": week_learned,            # the scheduling memory: judged only for the week
}

# Dimensions that are properties of the whole week, judged once per week in
# evaluate_schedule and weighted once into the week score (week_score_raw),
# never repeated on every shift. Still in DIMENSIONS so their weight is
# editable and their key is known everywhere a dimension is listed.
# Preferences (SQ-24) and overtime (SQ-25) are week facts the same way
# fatigue is: a person's hours, their payroll week.
WEEK_LEVEL_DIMENSIONS = {
    "fatigue": week_fatigue,
    "min_hours": week_min_hours,
    "preferences": week_preferences,
    "overtime": week_overtime,
    "learned": week_learned,
}

# What the owner sees as a bar: every shift dimension that counts toward the
# number. Six of them (experience, fairness, pairings, stability,
# cross-training, preferences — 25 of 130 weight points) used to be scored
# and hidden, so part of every score had no bar to explain it (schedule
# audit 10/3/26 SQ-28). The week-level measures show under "Across the
# week" instead (evaluate_schedule's week_dimensions).
CUSTOMER_DIMENSIONS = tuple(k for k in DIMENSIONS if k not in WEEK_LEVEL_DIMENSIONS)

# At least one of these has to have data before a shift claims a score. The
# rest are real dimensions and genuinely count, but none of them alone says
# anything about whether the shift will actually run.
SUBSTANTIVE_DIMENSIONS = ("coverage", "coverage_curve", "operational_strength", "leadership",
                          "demand_match", "labor_efficiency", "splh", "experience_balance",
                          "training_balance", "stations")


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
        if key in WEEK_LEVEL_DIMENSIONS:
            continue            # judged once for the week, in evaluate_schedule
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
        # A floor this shift's profile sets for the dimension — the owner's,
        # or what an applied calibration tuned — over its default; 0 is no
        # floor (SQ-23). The critical dimensions read it themselves too.
        if key in (getattr(ctx.profile, "floors", None) or {}):
            result.floor = ctx.profile.floor(key)
        applied.append(result)

    # Under the day's hour target is only a problem if the floor is thin.
    # The generator is told the hours budget is a ceiling and landing under
    # it is fine; scoring every under-target day as a loss contradicted that
    # and marked down a week that staffed every position (restaurant 64:
    # "28h under target" on every shift). While coverage — and the half-hour
    # sweep too, when it applies — holds its critical floor, under target is
    # efficiency: a thin spot above the floor is coverage's to charge, and
    # charging it again here as "under target" was a double penalty. It used
    # to need both at exactly 100, so one thin half hour turned an
    # under-target day into two losses (schedule audit 10/3/26 SQ-31).
    _by_key = {d.key: d for d in applied}
    _lab = _by_key.get("labor_efficiency")
    if _lab is not None and (_lab.facts.get("ratio") or 1) < 0.9:
        _cov, _curve = _by_key.get("coverage"), _by_key.get("coverage_curve")

        def _holds(d):
            return d.score >= (d.floor if d.floor is not None else SCORE_MAX)
        if _cov is not None and _holds(_cov) and (_curve is None or _holds(_curve)):
            _lab.score = SCORE_MAX
            _lab.weaknesses = []
            _lab.strengths = [f"{_lab.facts['scheduled_hours']:g}h against a {_lab.facts['target_hours']:g}h "
                              "target — under it is fine while coverage holds its floor."]
            _lab.facts["under_with_coverage"] = True

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
        # A critical dimension under its floor that the mean already sits
        # at (an empty daypart scored on coverage alone) is still the reason
        # for the number, and is named as one (SQ-8).
        if d.floor is not None and d.score < d.floor and (d.score < score or (d.score == score and not capped_by)):
            score = d.score
            capped_by = d.key
    score = max(0, min(SCORE_MAX, score))
    # A hard rule broken on this shift — no manager on the floor, a floor
    # short, a person who may not legally work it — holds it at
    # HARD_BREACH_CAP. None of them used to reach the score, so a week read
    # 92 "excellent" with a manager gap in it (schedule audit 10/3/26
    # SQ-14). A tie goes to the rule: it is the reason that matters more.
    breach_lines = [b["label"] for b in (ctx.hard_breaches or []) if b.get("label")]
    if breach_lines and HARD_BREACH_CAP <= score:
        score = HARD_BREACH_CAP
        capped_by = HARD_RULES_KEY

    # Weaknesses are ordered by how much each one actually costs the shift —
    # how far under it is, times how much it counts — rather than by score
    # alone. Training balance at 0 on a weight of 7 is a smaller problem
    # than coverage at 50 on a weight of 20, and score-first ordering put
    # the lighter one on top and pushed the coverage gap off the end of the
    # summary entirely.
    strengths = [s for d in sorted(applied, key=lambda x: -x.weight) for s in d.strengths]
    # The dimension holding the shift at its score speaks first: it is the
    # reason for the number, whatever the others cost. A broken hard rule
    # comes before everything.
    breach_text = [f"Hard rule broken: {line}." if not line.endswith(".") else f"Hard rule broken: {line}"
                   for line in breach_lines]
    weaknesses = breach_text + [w for d in sorted(applied,
                                                  key=lambda x: (0 if x.key == capped_by else 1,
                                                                 -((SCORE_MAX - x.score) * x.weight)))
                                for w in d.weaknesses]
    held_by = None
    if capped_by == HARD_RULES_KEY:
        more = len(breach_lines) - 1
        held_by = {"key": HARD_RULES_KEY, "label": HARD_RULES_LABEL, "score": score, "floor": None,
                   "text": f"A hard rule is broken on this shift, holding it at {score}: {breach_lines[0]}"
                           + (f" (and {more} more)" if more > 0 else "") + "."}
    elif capped_by:
        _cap = next(d for d in applied if d.key == capped_by)
        if capped_by == "coverage" and _cap.facts.get("no_shift_written"):
            # Nobody written onto a daypart the restaurant runs: say that,
            # not "positions unfilled" (SQ-8).
            text = _cap.weaknesses[0]
        else:
            text = (f"{_cap.label} is holding this shift at {score}"
                    + (f": {_cap.weaknesses[0]}" if _cap.weaknesses else "."))
        held_by = {"key": _cap.key, "label": _cap.label, "score": _cap.score, "floor": _cap.floor, "text": text}
    blind = [b for d in applied for b in d.blind_spots] + list(ctx.notes)
    for f in failed:
        blind.append(f"{f['key'].replace('_', ' ').capitalize()} could not be worked out "
                     "for this shift, so it was left out of the score.")
    line_keys = {line: d.key for d in applied for line in d.strengths + d.weaknesses + d.blind_spots}
    line_keys.update({line: HARD_RULES_KEY for line in breach_text})

    return {
        "date": ctx.date, "day": ctx.day, "daypart": ctx.daypart,
        "scored": True,
        "score": score,
        "band": band_for(score),
        "profile": _profile_facts(ctx.profile),
        "meets_profile": score >= int(ctx.profile.min_quality or 0),
        "capped_by": capped_by,
        "held_by": held_by,
        # A daypart the restaurant runs with nobody written onto it (SQ-8).
        "no_shift_written": bool(ctx.unwritten),
        # The hard rules broken on this shift, in the sweep's own words.
        "hard_breaches": breach_lines,
        "people": ctx.people,
        "headline": _headline(ctx, score),
        # Every dimension that counted, with the floor that caps it: the
        # owner sees all of them, not the nine the panel used to pick (SQ-28).
        "dimensions": [
            {"key": d.key, "label": d.label, "score": d.score, "weight": d.weight,
             "floor": d.floor,
             "strengths": d.strengths, "weaknesses": d.weaknesses, "facts": d.facts,
             "customer_facing": d.key in CUSTOMER_DIMENSIONS,
             # The floor this dimension was held to on this shift, kept with
             # the stored score so calibration reads what applied
             # (schedule_learning.calibrate_weights, SQ-22).
             "floor": d.floor}
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
        "line_keys": line_keys,
    }


def explain_assignment(row: dict, ctx: ShiftContext, applied: list = None) -> dict:
    """Why this person is on this shift, in one sentence an owner can argue
    with: "Level 5 bartender; keeps the bar at 9 against a target of 8; the
    one authorized to close; usually works Saturday nights; 32h this week."

    Built only from what the context holds. A fact that is not on file
    (no rating, no history) is simply not claimed.
    """
    name = (row.get("employee") or "").strip()
    role = (row.get("role") or "").strip()
    bits, facts = [], {}
    score = ctx.scores.get(name)
    if score is not None:
        label = {1: "very weak", 2: "below average", 3: "average", 4: "strong", 5: "excellent"}.get(int(score), "")
        bits.append(f"Level {int(score)} {role_words(role, default='team member')}" + (f" ({label})" if label else ""))
        facts["score"] = score
    elif role:
        bits.append(f"{role}, not yet rated")
    # strength: where this person leaves the role against its per-person bar
    for d in (applied or []):
        if d.key == "operational_strength":
            for m in (d.facts.get("met") or []) + (d.facts.get("shortfalls") or []):
                if ctx.family(m["role"]) == ctx.family(role) and score is not None:
                    verb = "keeps" if m["strength"] >= m.get("bar", m["target"]) else "leaves"
                    bits.append(f"{verb} {role_words(role, 2)} at an average of {m['strength']:g} against a bar of "
                                f"{m.get('bar', m['target']):g}")
                    facts["role_strength"] = m
                    break
    # leadership
    if ctx.manages(name):
        bits.append("manager on duty — able to run the shift")
        facts["manages"] = True
    if ctx.leader_flags.get(name):
        bits.append("authorized to close")
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
    elif days and (ctx.day or "").lower() not in days and ctx.history_weeks >= 4:
        # Only with enough history to know what "usually" means.
        bits.append(f"not a day they usually work")
        facts["usual"] = False
    # tenure — a manager or salaried person is experienced by default (D-6):
    # their punch count is not how long they have run the place.
    t = ctx.tenure.get(name)
    if t is not None:
        if t >= EXPERIENCE_SHIFTS:
            bits.append(f"{t} shifts here")
        elif t < DEVELOPING_SHIFTS and name_key(name) not in {name_key(n) for n in ctx.experienced_default or ()}:
            bits.append(f"still new ({t} shifts here)")
    # hours this week and headroom, against the person's own ceiling (D-3)
    assignments = [a for a in (ctx.week_assignments.get(name) or []) if not a.get("prior")]
    total = round(sum((a.get("hours") or 0) for a in assignments), 1)
    if total:
        ceiling = ctx.ceiling_for(name)
        if ceiling:
            room = round(ceiling - total, 1)
            bits.append(f"{total:g}h this week" + (f", {room:g}h under the {ceiling:g}h ceiling" if room >= 0 else f", {abs(room):g}h OVER the {ceiling:g}h ceiling"))
        else:
            bits.append(f"{total:g}h this week")
        facts["week_hours"] = total
    # reliability
    rel = (ctx.reliability or {}).get(name)
    if rel and float(rel.get("no_show_rate") or 0) >= UNRELIABLE_RATE:
        # The rate is smoothed toward the restaurant's base rate
        # (staff_settings.reliability); the words use what happened.
        bits.append(f"has missed {rel['no_shows']} of {rel['shifts']} scheduled shifts"
                    if rel.get("no_shows") is not None and rel.get("shifts") else
                    f"has missed {int(round(float(rel['no_show_rate']) * 100))}% of scheduled shifts")
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
            "source": profile.source,
            # The floors this shift's dimensions cap it at (SQ-23).
            "floors": {k: (profile.floor(k) if hasattr(profile, "floor") else CRITICAL_FLOORS.get(k))
                       for k in sorted(set(CRITICAL_FLOORS) | set(getattr(profile, "floors", None) or {}))}}


def _headline(ctx: ShiftContext, score: int) -> str:
    part = {"morning": "lunch", "night": "dinner"}.get(ctx.daypart, ctx.daypart or "shift")
    where = f"{ctx.day} {part}".strip() if ctx.day else ctx.date
    return f"{where} scored {score}/100 on Shift Quality."


# ── Scoring a whole schedule ───────────────────────────────────────────────

def evaluate_schedule(contexts: list, weights: dict = None,
                      signals: dict = None, shifts: list = None) -> dict:
    """Roll every shift up into one number, with the reasons intact.

    Shifts are weighted by demand: a weak Saturday dinner is a worse week
    than a weak Monday lunch, and a flat mean says the opposite. `shifts`,
    each context's evaluate_shift already worked out (LocalScorer.evaluate),
    in the contexts' order, are rolled up as they are."""
    if shifts is None:
        shifts = [evaluate_shift(c, weights) for c in contexts]
    scored = [s for s in shifts if s.get("scored")]
    if not scored:
        return {"checked": False, "score": None, "shifts": shifts,
                "reason": "No shift had enough configured to judge it.",
                "confidence": confidence(shifts, signals or {})}

    week_dims = evaluate_week_dimensions(contexts, weights)
    raw = week_score_raw(scored, week_dims)
    overall = int(round(raw))

    # Strip what is true of the week out of the individual shifts FIRST, so
    # the week-level lists below are built from what is left. Two overlapping
    # summaries is one too many, and the shift sections are what get read.
    hoisted = _hoist_common_lines(scored)
    # A week-level measure speaks at week level, once.
    for d in week_dims:
        hoisted["weaknesses"] = list(d.weaknesses) + list(hoisted.get("weaknesses") or [])
    share = _week_share(scored, week_dims)
    details = recommendation_details(scored, week_dims)
    # The week-level hold (SQ-2), said once at the top of the week: which
    # rules are broken and on what, so "weak" never reads unexplained.
    held_by = None
    if week_held(scored):
        broken = [s for s in scored if s.get("hard_breaches")]
        first = broken[0]
        where = f"{first['day']} {'lunch' if first['daypart'] == 'morning' else 'dinner'}"
        lines = sum(len(s["hard_breaches"]) for s in broken)
        held_by = {"key": HARD_RULES_KEY, "label": HARD_RULES_LABEL, "score": overall,
                   "ceiling": WEEK_HARD_BREACH_CEILING, "shifts": len(broken), "breaches": lines,
                   "text": (f"A hard rule is broken on {where}: {first['hard_breaches'][0]}"
                            + (f" (and {lines - 1} more this week)" if lines > 1 else "")
                            + f". The week cannot score above {WEEK_HARD_BREACH_CEILING} until it is fixed.")}
        hoisted["weaknesses"] = [held_by["text"]] + [w for w in (hoisted.get("weaknesses") or [])
                                                      if w != held_by["text"]]

    return {
        "checked": True,
        "score": overall,
        # The week's hard-rule hold, or None (SQ-2).
        "held_by": held_by,
        # Before rounding, for a search that has to see a shift move three
        # points (schedule_optimizer.objective).
        "raw_score": round(raw, 4),
        "band": band_for(overall),
        "shifts": shifts,
        "dimensions": _rollup(scored) + [
            {"key": d.key, "label": d.label, "shifts": 0, "score": d.score, "worst": None,
             "customer_facing": False, "week_level": True} for d in week_dims],
        # Measures of the whole week, judged once and weighted once:
        # `share` is the part of the week score they make up.
        "week_dimensions": [
            {"key": d.key, "label": d.label, "score": d.score, "weight": d.weight,
             "share": round(share, 3), "strengths": d.strengths, "weaknesses": d.weaknesses,
             "facts": d.facts, "week_level": True} for d in week_dims],
        # Each shift under its profile's bar, with why: the cap holding it
        # (held_by) or its costliest weakness. The line printed only score
        # against bar, so "Tuesday dinner 0 / 70" could be an empty daypart,
        # a missed leader or a rule on the wrong daypart (SQ-8, SQ-28).
        "below_profile": [_below_line(s) for s in scored if not s["meets_profile"]],
        "worst": min(scored, key=lambda s: s["score"])["headline"] if scored else None,
        "best": max(scored, key=lambda s: s["score"])["headline"] if scored else None,
        "strengths": _week_reasons(hoisted, scored, "strengths"),
        "weaknesses": _week_reasons(hoisted, scored, "weaknesses"),
        "recommendations": [d["text"] for d in details],
        # The same suggestions with their kind, the dimension they lift and
        # the points the week gains when each is fully done (SQ-28).
        "recommendation_details": details,
        "recommendation_points": {d["text"]: d["points"] for d in details},
        "confidence": confidence(shifts, signals or {}),
    }


def _below_line(shift: dict) -> dict:
    """One shift under its profile's bar, with the reason it is there."""
    held = shift.get("held_by")
    reason = held.get("text") if held else ""
    if not reason:
        # Uncapped: the costliest dimension speaks — what it is under, times
        # what it counts — from the dimension's own lines, which the week's
        # pattern-hoisting never strips.
        for d in sorted(shift.get("dimensions") or [],
                        key=lambda x: -((SCORE_MAX - x["score"]) * float(x.get("weight") or 0))):
            if d.get("weaknesses") and d["score"] < SCORE_MAX:
                reason = d["weaknesses"][0]
                break
    return {"date": shift["date"], "day": shift["day"], "daypart": shift["daypart"],
            "score": shift["score"], "min_quality": shift["profile"]["min_quality"],
            "label": shift["profile"]["label"], "capped_by": shift.get("capped_by"),
            "held_by": held, "no_shift_written": bool(shift.get("no_shift_written")),
            "reason": reason or f"{shift['score']} against a bar of {shift['profile']['min_quality']}."}


def evaluate_week_dimensions(contexts: list, weights: dict = None) -> list:
    """The WEEK_LEVEL_DIMENSIONS for one week, each judged once, weighted by
    the owner's weights over the defaults. A measure with nothing to judge,
    or weighted to zero, is left out."""
    merged = dict(DEFAULT_WEIGHTS)
    merged.update(weights or {})
    out = []
    for key, fn in WEEK_LEVEL_DIMENSIONS.items():
        try:
            res = fn(contexts)
        except Exception:
            res = None
        if res is None:
            continue
        res.weight = float(merged.get(key, res.weight) or 0)
        if res.weight > 0:
            out.append(res)
    return out


def _shift_share(shift: dict, week_w: float) -> float:
    """The part of one shift's number the week-level measures stand in for:
    their weight against the shift's own dimension weights — exactly the
    share they had when they sat on the shift at that weight. A shift
    capped by a critical dimension takes none: it is no better than its
    worst critical part, and a rested week does not lift it."""
    if week_w <= 0 or shift.get("capped_by"):
        return 0.0
    own = sum(float(d.get("weight") or 0) for d in shift.get("dimensions") or [])
    return week_w / (week_w + own) if (week_w + own) > 0 else 0.0


def _week_share(scored: list, week_dims: list) -> float:
    """The share of the week score the week-level measures carry, demand
    weighted over the shifts (capped shifts carry none). Bounded to [0, 1)."""
    week_w = sum(float(d.weight or 0) for d in week_dims or [])
    if week_w <= 0 or not scored:
        return 0.0
    num = den = 0.0
    for s in scored:
        w = DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0)
        num += _shift_share(s, week_w) * w
        den += w
    return num / (den or 1.0)


def week_held(scored: list) -> bool:
    """True when a scored shift of the week carries a hard breach the rule
    sweep found: the week is held under WEEK_HARD_BREACH_CEILING (SQ-2)."""
    return any(s.get("hard_breaches") for s in scored or [])


def week_score_raw(scored: list, week_dims: list = None) -> float:
    """The week's score before rounding.

    The week-level measures (fatigue) are judged ONCE, as one number for the
    week, and that one number stands in for the part of each shift they
    always carried (_shift_share) — so with nobody tired the week scores
    what it did when fatigue sat on every shift, and a tired person costs
    the week once, for being tired, not once per shift they work. Each
    shift's blend is convex on 0-100, so the week stays on 0-100; with no
    week-level measure it is the demand-weighted shift mean exactly."""
    if not scored:
        return 0.0
    week_w = sum(float(d.weight or 0) for d in week_dims or [])
    week_val = (sum(d.score * float(d.weight or 0) for d in week_dims) / week_w) if week_w > 0 else 0.0
    num = den = 0.0
    for s in scored:
        w = DEMAND_WEIGHT.get(s["profile"]["demand"], 1.0)
        share = _shift_share(s, week_w)
        num += (s["score"] * (1 - share) + week_val * share) * w
        den += w
    raw = max(0.0, min(float(SCORE_MAX), num / (den or 1.0)))
    # A broken hard rule holds the whole week, not only its shift (SQ-2).
    if week_held(scored):
        raw = raw * WEEK_HARD_BREACH_CEILING / float(SCORE_MAX)
    return raw


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
            # On every shift, the line stands alone (owner, 9/26/26): "Similar
            # on every shift this week." added words, not a finding.
            if entry["shifts"] == len(scored):
                lines.append(text)
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
    """How much to trust the scores above, and exactly why.

    Every point taken off is a line in `breakdown` with the points it cost,
    biggest first, and `top_reason` is the one to show beside the
    percentage: Erik's "70%" was mostly the share of his staff nobody had
    rated, which a bare number never said (schedule audit 10/3/26 SQ-32)."""
    score = 100
    reasons, breakdown = [], []

    def charge(points, reason):
        nonlocal score
        points = int(points)
        score -= points
        reasons.append(reason)
        if points > 0:
            breakdown.append({"reason": reason, "points": points})

    rated = signals.get("rated_people")
    total = signals.get("scheduled_people")
    if total:
        unrated_share = 1.0 - (float(rated or 0) / float(total))
        if unrated_share > 0:
            missing = int(round(unrated_share * total))
            charge(int(round(unrated_share * 45)), f"{missing} of {total} scheduled staff have no Operational Score.")

    if not signals.get("has_tenure"):
        charge(12, "No shift history yet, so experience could not be judged.")
    if not signals.get("has_demand"):
        charge(10, "Not enough sales history to know how busy these days really are.")
    if not signals.get("has_availability"):
        charge(8, "No availability on file, so nobody could be ruled out.")
    # Only when nothing of this restaurant's own judged the shifts: built-in
    # profiles re-levelled by its own sales history are its own demand
    # (score_rows' has_profiles), and were charged as defaults all the same.
    if not signals.get("has_profiles"):
        charge(5, "Using default shift profiles rather than this restaurant's own.")

    flagged = int(signals.get("rows_needing_review") or 0)
    if flagged:
        charge(min(15, flagged * 3), f"{flagged} generated {_plural(flagged, 'row')} needed a human check.")
    dropped = int(signals.get("dropped_rows") or 0)
    if dropped:
        charge(min(20, dropped * 5), f"{dropped} {_plural(dropped, 'row')} could not be read at all.")

    broken = sum(len(s.get("failed") or []) for s in shifts)
    if broken:
        # A computation failure is the one confidence penalty that is our
        # fault rather than the owner's, and the one most worth seeing.
        charge(min(35, broken * 6), f"{broken} dimension {_plural(broken, 'reading')} could not be "
                                    "worked out, so the score is built on less than usual.")

    for label in signals.get("unmeetable_rules") or []:
        reasons.append(f"Nobody on the roster can meet the rule \u201c{label}\u201d, so it was set aside "
                       "rather than failing every shift it covers — rate someone to it, or change the rule.")
    # A rule fewer people can meet than it asks for is kept and held to the
    # people able (schedule audit 10/3/26 SQ-17), and says so.
    for c in signals.get("capped_rules") or []:
        reasons.append(f"Only {c['able']} on the roster can meet the rule “{c['label']}”, so each "
                       f"shift it covers is held to {c['able']} — rate or mark more people for it, or change the rule.")

    unfixable = int(signals.get("unsatisfiable") or 0)
    if unfixable:
        charge(min(20, unfixable * 7), f"{unfixable} {_plural(unfixable, 'requirement')} could not be met with "
                                       "who was available.")

    hard_constraints = int(signals.get("constrained_people") or 0)
    if hard_constraints and total and hard_constraints / float(total) > 0.4:
        charge(8, "Availability is tight across most of the roster.")

    score = max(0, min(100, score))
    level = next(name for floor, name in CONFIDENCE_LEVELS if score >= floor)
    breakdown.sort(key=lambda b: -b["points"])
    return {"score": score, "level": level, "reasons": reasons,
            # Each deduction with what it cost, biggest first; the first is
            # what to name beside the percentage.
            "breakdown": breakdown,
            "top_reason": breakdown[0]["reason"] if breakdown else None,
            "summary": {"high": "The engine had what it needed to judge this schedule.",
                        "moderate": "Usable, but some of this is estimated.",
                        "low": "Treat these scores as provisional."}[level]}


# ── Recommendations ────────────────────────────────────────────────────────

_REC_KINDS = (("Fill the gap", "coverage"), ("Cover the gap", "coverage"), ("Move somebody", "leadership"),
              ("Put a stronger", "strength"), ("Pair ", "strength"), ("Fix the rule", "rules"),
              ("Trim about", "hours"), ("Give ", "fatigue"), ("Spread the busy", "fatigue"), ("Rate the", "ratings"))
# Kinds about whether the shift is safe to run: never switched off by being
# ignored — a coverage gap the owner has stopped reading is still a gap, and
# a broken hard rule is still broken.
PROTECTED_REC_KINDS = frozenset({"coverage", "leadership", "fatigue", "rules"})
# The most suggestions, and the most of one kind, a week carries.
MAX_RECOMMENDATIONS = 6
MAX_PER_KIND = 2


def recommendation_kind(text: str) -> str:
    """The kind of a recommendation sentence, for the accept/dismiss ledger."""
    for prefix, kind in _REC_KINDS:
        if (text or "").startswith(prefix):
            return kind
    return "other"


def _rescore(shift: dict, fixed: str) -> tuple:
    """(score, capped_by) of a scored shift with `fixed` — a dimension key,
    or HARD_RULES_KEY — fully met: the shift's own aggregation over its
    stored dimensions (weights and floors), the other caps re-applied."""
    dims = shift.get("dimensions") or []
    total = sum(float(d.get("weight") or 0) for d in dims) or 1.0
    raw = sum((SCORE_MAX if d["key"] == fixed else d["score"]) * float(d.get("weight") or 0) for d in dims) / total
    score, capped = int(round(raw)), None
    for d in dims:
        fl = d.get("floor")
        if d["key"] != fixed and fl is not None and d["score"] < fl and (
                d["score"] < score or (d["score"] == score and not capped)):
            score, capped = d["score"], d["key"]
    if fixed != HARD_RULES_KEY and shift.get("hard_breaches") and HARD_BREACH_CAP <= score:
        score, capped = HARD_BREACH_CAP, HARD_RULES_KEY
    return max(0, min(SCORE_MAX, score)), capped


def _week_measures(week_dims) -> list:
    """Week-level measures as week_score_raw reads them, from either the
    DimensionResults or the dicts evaluate_schedule returns."""
    return [SimpleNamespace(key=d.get("key"), score=float(d.get("score") or 0), weight=float(d.get("weight") or 0))
            if isinstance(d, dict) else d for d in (week_dims or [])]


def points_if_fixed(shifts: list, shift: dict = None, key: str = None, week_dims: list = None) -> float:
    """How many points the WEEK's score rises if `key` were fully met on
    `shift` — the week's own aggregation (week_score_raw) with that one
    shift re-scored, its other caps standing. With no shift, `key` is a
    week-level measure (fatigue) judged at 100. The most the suggestion
    naming it can be worth: each recommendation carries it (SQ-28), and a
    change the passes make is scored in full by the LocalScorer."""
    scored = [s for s in (shifts or []) if s.get("scored")]
    if not scored:
        return 0.0
    measures = _week_measures(week_dims)
    before = week_score_raw(scored, measures)
    if shift is None:
        lifted = [SimpleNamespace(key=d.key, score=SCORE_MAX if d.key == key else d.score, weight=d.weight)
                  for d in measures]
        return round(max(0.0, week_score_raw(scored, lifted) - before), 2)
    score, capped = _rescore(shift, key)
    # Fixing the shift's hard breach also lifts the week's hold once no
    # other shift carries one (SQ-2).
    lifted_rules = {"hard_breaches": []} if key == HARD_RULES_KEY else {}
    trial = [dict(s, score=score, capped_by=capped, **lifted_rules) if s is shift else s for s in scored]
    return round(max(0.0, week_score_raw(trial, measures) - before), 2)


def recommendation_details(scored: list, week_dims: list = None) -> list:
    """What the manager could actually do about it, most valuable first:
    [{text, kind, points, dimension, date, daypart}].

    Built from the dimensions' own facts rather than written by a model, so
    a recommendation can never reference a shift or a person that is not in
    the schedule. Each carries how many points the week gains if what it
    names is fully fixed (points_if_fixed); the most valuable come first,
    at most MAX_PER_KIND of a kind and MAX_RECOMMENDATIONS in all. They used
    to come in a fixed order with no figure, so the owner could not tell a
    two-point suggestion from a twenty-point one (schedule audit 10/3/26
    SQ-28). The sentences keep their shapes: the ledger keys on them and
    "Fill it" reads "<role> short N of M".
    """
    found, unrated = [], False
    over_hours = []
    overloaded, long_runs = set(), set()

    def add(shift, kind, text, key, order):
        found.append({"text": text, "kind": kind, "dimension": key,
                      "date": shift["date"] if shift else None, "daypart": shift["daypart"] if shift else None,
                      "points": points_if_fixed(scored, shift, key, week_dims), "_order": order})

    for shift in scored:
        where = f"{shift['day']} {shift['daypart']}".strip() or shift["date"]
        dims = {d["key"]: d for d in shift["dimensions"]}
        if shift.get("hard_breaches"):
            add(shift, "rules", f"Fix the rule breach on {where}: {shift['hard_breaches'][0]}.", HARD_RULES_KEY, 0)
        facts = (dims.get("coverage") or {}).get("facts") or {}
        if facts.get("gaps"):
            add(shift, "coverage", f"Fill the gap on {where}: {facts['gaps'][0]}.", "coverage", 1)
        facts = (dims.get("coverage_curve") or {}).get("facts") or {}
        if facts.get("gaps"):
            role, g = max(facts["gaps"].items(), key=lambda kv: (kv[1]["minutes_short"], kv[0]))
            add(shift, "coverage", f"Cover the gap in service on {where}: {role} is down to {g['on_at_worst']} "
                                   f"at {g['worst_at']}, under {g['need']}.", "coverage_curve", 2)
        for miss in ((dims.get("leadership") or {}).get("facts") or {}).get("misses") or []:
            add(shift, "leadership", f"Move somebody who clears \"{miss['rule']}\" onto {where}.", "leadership", 3)
        facts = (dims.get("operational_strength") or {}).get("facts") or {}
        for sf in facts.get("shortfalls") or []:
            add(shift, "strength", f"Put a stronger {role_words(sf['role'])} on {where}: "
                                   f"{role_words(sf['role'], 2)} average {sf['strength']:g} against a bar of "
                                   f"{sf.get('bar', sf['target']):g}.", "operational_strength", 4)
        unrated = unrated or bool(facts.get("unrated"))
        for n in ((dims.get("training_balance") or {}).get("facts") or {}).get("isolated_names") or []:
            add(shift, "strength", f"Pair {n} on {where} with a stronger hand, or move one there.",
                "training_balance", 5)
        facts = (dims.get("labor_efficiency") or {}).get("facts") or {}
        if facts.get("ratio", 1) > 1.02:
            over_hours.append((facts["scheduled_hours"] - facts["target_hours"], where, shift))
        facts = (dims.get("fatigue") or {}).get("facts") or {}
        overloaded.update(o["name"] for o in facts.get("overloaded") or [])
        long_runs.update((r["name"], r["days"]) for r in facts.get("long_runs") or [])
        if any("Operational Score" in spot for spot in shift.get("blind_spots") or []):
            unrated = True
    for d in week_dims or []:
        if (d.get("key") if isinstance(d, dict) else d.key) == "fatigue":
            facts = (d.get("facts") if isinstance(d, dict) else d.facts) or {}
            overloaded.update(o["name"] for o in facts.get("overloaded") or [])
            long_runs.update((r["name"], r["days"]) for r in facts.get("long_runs") or [])

    if over_hours:
        over, where, shift = max(over_hours, key=lambda x: x[0])
        add(shift, "hours", f"Trim about {over:.0f}h from {where} to get back under target.", "labor_efficiency", 6)
    if overloaded:
        add(None, "fatigue", f"Spread the busy shifts — {_names(sorted(overloaded)[:2])} " +
            _plural(len(overloaded), "is", "are") + " carrying too many.", "fatigue", 7)
    for name, days in sorted(long_runs)[:1]:
        add(None, "fatigue", f"Give {name} a day off — {days} in a row this week.", "fatigue", 8)

    found.sort(key=lambda x: (-x["points"], x["_order"]))
    out, per_kind, seen = [], {}, set()
    for item in found:
        if item["text"] in seen or per_kind.get(item["kind"], 0) >= MAX_PER_KIND:
            continue
        seen.add(item["text"])
        per_kind[item["kind"]] = per_kind.get(item["kind"], 0) + 1
        out.append({k: v for k, v in item.items() if k != "_order"})
    if unrated:
        # Not a score change: a rating turns a blind spot into a judgement.
        out.append({"text": "Rate the unscored staff — strength and demand on their shifts are judged on the "
                            "rated people only, so part of each figure is unknown.",
                    "kind": "ratings", "dimension": None, "date": None, "daypart": None, "points": 0.0})
    return out[:MAX_RECOMMENDATIONS]


def recommendations(scored: list, week_dims: list = None) -> list:
    """The recommendation sentences, most valuable first
    (recommendation_details carries the points and kind of each)."""
    return [d["text"] for d in recommendation_details(scored, week_dims)]


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
    hm = _clock(raw)
    if hm is None:
        return "unknown"
    hour = hm[0]
    # A start in the small hours is the night it belongs to — the row's
    # date is its business date (time_utils.BUSINESS_DAY_START_HOUR;
    # schedule audit 10/3/26 E-32), never the next morning; from 4am it
    # is early prep for the morning (schedule_rules._night_offset).
    return "night" if hour >= 15 or _small_hours_night(hour * 60) else "morning"


@lru_cache(maxsize=1024)
def _weekday_name(date_str: str) -> str:
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")


@lru_cache(maxsize=2048)
def _iso_datetime(date_str: str) -> datetime:
    """An ISO date as a datetime, remembered: the week measures read the
    same seven dates thousands of times a pass (P-38). Raises as strptime
    does."""
    return datetime.strptime(date_str, "%Y-%m-%d")


def _day_name(date_str: str, fallback: str = "") -> str:
    try:
        return _weekday_name(date_str)
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
    hm = _clock(raw)
    if hm is None:
        return -1
    minutes = hm[0] * 60 + hm[1]
    return minutes + 1440 if hm[0] < 5 else minutes


def _row_hours(row: dict) -> float:
    try:
        return float(row.get("scheduled_hours") or 0)
    except (ValueError, TypeError):
        return 0.0


# The core service window of each daypart. A shift is counted as present in
# the OTHER daypart when it covers at least PRESENCE_MIN_OVERLAP minutes of
# that daypart's core: an 11:30am-7pm server is on the floor for dinner, and
# bucketing people by start time alone read that dinner as a server short
# while the generator was told (correctly) that she counts toward it.
CORE_WINDOWS = {"morning": (11 * 60, 14 * 60 + 30), "night": (17 * 60 + 30, 20 * 60 + 30)}
PRESENCE_MIN_OVERLAP = 60

# The late segment (schedule audit 10/3/26 D-32): late-night service, from
# 10pm to close, on a day that closes past 11pm. Not a third daypart — a
# shift on the floor then is still the night's (availability, floors,
# fairness and the week's assignments read morning and night) — but a
# window of the night with its own usual crew, demand and sales per labor
# hour: Simple EJ's 2am-close Friday bar was judged on its dinner alone.
LATE_WINDOW_START = 22 * 60
LATE_CLOSE_AFTER = 23 * 60


def late_window(close_minutes):
    """(start, close) minutes of the late segment for a day closing at
    `close_minutes` (a small-hours close may be given as minutes past that
    midnight or past 24h), or None when the close is not past 11pm."""
    if close_minutes is None:
        return None
    try:
        c = int(close_minutes)
    except (TypeError, ValueError):
        return None
    if c < 5 * 60:
        c += 24 * 60
    return (LATE_WINDOW_START, c) if c > LATE_CLOSE_AFTER else None


def late_minutes(row: dict, window) -> int:
    """Minutes a row is on the floor inside the late `window` (start, end) —
    0 outside it or when its times cannot be read."""
    if not window:
        return 0
    s, e = _slot_minutes(row.get("shift_start")), _slot_minutes(row.get("shift_end"))
    if s is None or e is None:
        return 0
    if e <= s:
        e += 24 * 60
    return max(0, min(e, window[1]) - max(s, window[0]))


def present_dayparts(row: dict) -> list:
    """The dayparts a row is on the floor for: each whose core service
    window it covers for at least PRESENCE_MIN_OVERLAP minutes, the one it
    covers most first (that is the row's own daypart, where its hours and
    week assignment count). A shift covering neither core window (an early
    prep, a 3-5pm changeover) belongs to its start's daypart.

    Judged by what the shift covers, not when it starts: a 2pm-11:30pm cook
    started "at lunch" by the 3pm cutover and was counted as full lunch
    coverage for half an hour of it."""
    primary = daypart_of(row.get("shift_start", ""))
    s, e = _slot_minutes(row.get("shift_start")), _slot_minutes(row.get("shift_end"))
    if s is None or e is None or primary == "unknown":
        return [primary]
    if e <= s:
        e += 24 * 60
    covered = []
    for part, (lo, hi) in CORE_WINDOWS.items():
        overlap = min(e, hi) - max(s, lo)
        if overlap >= min(PRESENCE_MIN_OVERLAP, hi - lo):
            covered.append((overlap, part == primary, part))
    if not covered:
        return [primary]
    covered.sort(key=lambda t: (-t[0], not t[1]))
    return [p for _o, _pr, p in covered]


def works_daypart_ok(row: dict, choice: str) -> bool:
    """Whether a row fits somebody's morning-only / night-only availability:
    every daypart the shift is on the floor for must be the one they chose."""
    if choice not in ("morning", "night"):
        return True
    parts = [p for p in present_dayparts(row) if p != "unknown"]
    return all(p == choice for p in parts)


def profile_for_shift(day: str, part: str, profiles: list = None, demand_by_day: dict = None,
                      lift_pct=None) -> ShiftProfile:
    """The profile one shift is judged against, with its demand settled.

    Most specific matching profile, then: a per-day-demand profile takes the
    level this restaurant's own sales give THAT weekday, and a date the
    owner flagged (an event) can only raise it. Shared by the scorer and
    the generator's requirements table so the two never disagree.

    With no profile set (None) it is the built-in set re-levelled per
    weekday from the restaurant's own sales (profile_set). A built-in
    settled at another level takes that level's bars with it (_follow_level),
    and then any tuning the owner applied to it (SQ-22)."""
    profile = resolve_profile(day, part, profile_set(profiles, demand_by_day))
    if profile.per_day_demand and demand_by_day and demand_by_day.get(day) is not None:
        level = demand_from_pct(demand_by_day.get(day))
        if level and level != profile.demand:
            profile = _clone(profile)
            profile.demand = level
            profile.source = "your sales history"
            _follow_level(profile)
    if lift_pct is not None:
        bumped = demand_from_pct(lift_pct)
        if bumped and DEMAND_RANK[bumped] > DEMAND_RANK.get(profile.demand, 1):
            profile = _clone(profile)
            profile.demand = bumped
            _follow_level(profile)
            profile.source = "what you told us about this date"
    return profile

# The built-in profile whose bars each demand level carries: a quiet weekday
# lunch, a weekday dinner, Friday and Saturday dinner.
_LEVEL_PROFILE = {"low": "weekday_lunch", "normal": "weekday_dinner", "high": "friday_dinner",
                  "peak": "saturday_dinner"}


def _follow_level(profile: ShiftProfile) -> None:
    """A built-in profile settled at another demand level takes that
    level's bars — the quality bar, the leader requirement, the experience
    mix, whether it may be a training shift — from the built-in that level
    names (schedule audit 10/3/26 SQ-15). The demand was re-levelled from
    the restaurant's own sales while the bars stayed with the guess, so a
    Saturday 30% under an average day read "low" and was still held to the
    peak night's 82 and its leader; and a Thursday busier than any Friday
    was held to a quiet dinner's 70. Tuning applied to the profile
    (calibration, SQ-22) stays on top. In place; a profile an owner
    configured is left exactly as they set it."""
    if not profile.builtin:
        return
    src = next((b for b in BUILTIN_PROFILES if b.key == _LEVEL_PROFILE.get(profile.demand)), None)
    if src is None:
        return
    profile.min_quality = src.min_quality
    profile.requires_leader = src.requires_leader
    profile.experience_mix = src.experience_mix
    profile.training_allowed = src.training_allowed
    _apply_tuning(profile)


def _apply_tuning(profile: ShiftProfile) -> None:
    """The bar and floors calibration (or the owner) set for this profile,
    over whatever it was settled to (SQ-22). In place."""
    t = profile.tuning or {}
    if t.get("min_quality") is not None:
        profile.min_quality = int(_num(t.get("min_quality"), 0, 100, profile.min_quality))
    if t.get("floors"):
        profile.floors = dict(profile.floors or {})
        profile.floors.update({k: int(_num(v, 0, 100, 0)) for k, v in t["floors"].items() if k in DIMENSIONS})


def profile_set(profiles: list = None, demand_by_day: dict = None) -> list:
    """The profiles a shift is judged against: the ones given, else the
    built-in set — re-levelled per weekday from this restaurant's own sales
    whenever they are known, exactly as profiles_from_config builds it
    (schedule audit 10/3/26 SQ-15). A restaurant that had rated nobody and
    saved no profile was judged on the built-ins' guesses with the sales
    re-levelling off: a Saturday 30% under an average day was still "peak"
    with an 82 bar, contradicting the built-ins' own promise."""
    if profiles is not None:
        return profiles
    return profiles_from_config(None, demand_by_day=demand_by_day) if demand_by_day else BUILTIN_PROFILES


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _floors_for(role_floors_all: dict, day: str, part: str) -> dict:
    """{role: people} the owner's floors ask of one weekday and daypart."""
    out = {}
    for role, spec in (role_floors_all or {}).items():
        dspec = (spec.get("days") or {}).get(day) or {}
        n = dspec.get(part) if part in dspec else spec.get(part)
        if n:
            out[role] = int(n)
    return out


def strength_crews(profiles: list = None, typical_headcount: dict = None, role_floors: dict = None,
                   role_minimums: dict = None, section_cap: int = 0, cap_roles=None,
                   families: dict = None) -> dict:
    """{(family, target): (crew, source)} — the full crew each strength
    target was set for: the most of the target's role that any weekday and
    daypart the target governs needs (each resolved to its profile, the
    requirement read as coverage reads it — shift_role_requirements under
    the section cap — and a role's AM/PM job codes added up). A target
    written for Saturday's two bartenders is judged as 4 a person, so a
    lunch that needs one bartender asks for a 4, not for 8 (SQ-4). A target
    the profile set apart keeps its own crew. Pure; the scorer and, through
    INTERFACES, the solver read the same numbers."""
    profiles = profiles if profiles is not None else BUILTIN_PROFILES
    out = {}
    for day in _WEEKDAYS:
        for part in CORE_WINDOWS:          # every daypart the scorer buckets shifts into
            profile = resolve_profile(day, part, profiles)
            targets = {r: float(v) for r, v in (profile.min_strength or {}).items() if v}
            if not targets:
                continue
            reqs = shift_role_requirements((typical_headcount or {}).get((day, part)) or {},
                                           _floors_for(role_floors, day, part), role_minimums,
                                           profile.critical_positions)
            if not reqs:
                continue
            need = {r: n for r, (n, _s) in reqs.items()}
            if section_cap:
                from staffing_curve import cap_requirement
                need, _trimmed = cap_requirement(need, section_cap, cap_roles)
            by_family = {}
            for r, n in need.items():
                entry = by_family.setdefault(role_family(r, families), [0, set()])
                entry[0] += int(n or 0)
                entry[1].add(reqs[r][1])
            for role, target in targets.items():
                if job_code_daypart(role) not in (None, part):
                    continue
                n, sources = by_family.get(role_family(role, families), (0, set()))
                key = (role_family(role, families), target)
                if n > (out.get(key) or (0, ""))[0]:
                    out[key] = (n, ", ".join(sorted(s for s in sources if s)))
    return out


def role_ratings(scores: dict = None, roster_roles: dict = None, rows: list = None,
                 families: dict = None) -> dict:
    """{family: [ratings], "*": [every rating]} of the rated people: each
    person under their roster role (else a role they work this week) — the
    restaurant's own level demand match judges a shift against (SQ-12)."""
    role_of = {}
    for name, role in (roster_roles or {}).items():
        if name and (role or "").strip():
            role_of[str(name).strip().lower()] = role
    for r in rows or []:
        name = (r.get("employee") or "").strip().lower()
        if name and (r.get("role") or "").strip():
            role_of.setdefault(name, r.get("role"))
    out = {"*": []}
    for name, score in (scores or {}).items():
        try:
            value = float(score)
        except (TypeError, ValueError):
            continue
        out["*"].append(value)
        role = role_of.get(str(name).strip().lower())
        if role:
            out.setdefault(role_family(role, families), []).append(value)
    return out


# Breaches about the close of a day: the closing shift carries them.
_CLOSE_BREACHES = frozenset({"nobody_at_close", "keyholder_until_close"})
# Breaches named on every row of a person's payroll week or run: the shift
# that tips them over (their latest) carries them, not the whole week.
_PERSON_WEEK_BREACHES = frozenset({"over_max_hours", "minor_week_hours", "long_run"})


# Breaches about a day — its floors, its manager, its close — not about one
# person's row, whoever the sweep pinned them to.
_DAY_BREACHES = frozenset({"no_manager", "coverage_floor", "keyholder_until_close", "nobody_at_close",
                           "owner_rule", "no_manager_on_duty", "ends_before_role_close"})


def place_breaches(breaches, rows: list, closes_on: dict = None) -> dict:
    """{(date, daypart): [{kind, label}]} — each hard breach the rule sweep
    found (signals["hard_breaches"]), on the shift it is about (SQ-14).
    "*" as the daypart is every shift of that date.

    `breaches` is the engine's map (schedule_engine.hard_breach_map:
    {"by_date": {date: [breach]}, "week": [breach]}), or a list of
    violation dicts as schedule_rules.violations returns them, or of
    schedule_rules.breach_id tuples. A day's breach (a floor short, no
    manager, nobody at close) lands on its daypart — the dayparts its
    unmanaged minutes cross, the closing shift for a close — else every
    shift of its date. A person's own breach lands on the shifts they work
    that date (the row it names when it is still there); a week-long one
    (hours over, a run too long) on their latest. A soft flag never lands.

    Scoring rows other than the ones swept (a pass's trial, the what-if's
    swaps): a person's breach whose person no longer works that date is gone
    with them and does not land; a day's breach stays. Dropping a whole
    changed date instead credited ANY change on a capped day with lifting
    the cap — a what-if "better arrangement" the owner would read that fixed
    nothing; the score of the finished rows is always swept fresh."""
    rows_by_date = {}
    for r in rows or []:
        d = (r.get("date") or "").strip()
        if d and (r.get("employee") or "").strip():
            rows_by_date.setdefault(d, []).append(r)
    closes_on = closes_on or {}
    out, latest = {}, {}

    def put(date, parts, kind, label):
        for part in parts or ("*",):
            items = out.setdefault((date, part), [])
            if not any(x["label"] == label for x in items):
                items.append({"kind": kind, "label": label})

    for b in _breach_items(breaches, rows_by_date):
        date, kind, label, emp = b["date"], b["kind"], b["label"], b["employee"]
        if emp:
            mine = [r for r in rows_by_date.get(date, []) if (r.get("employee") or "").strip().lower() == emp]
            if not mine:
                continue
            exact = [r for r in mine if b["shift_start"] and (r.get("shift_start") or "") == b["shift_start"]]
            parts = sorted({p for r in (exact or mine) for p in present_dayparts(r) if p != "unknown"}) or None
            if kind in _PERSON_WEEK_BREACHES:
                when = (date, _slot_minutes(b["shift_start"]) or 0)
                if b["week_key"] not in latest or when > latest[b["week_key"]][0]:
                    latest[b["week_key"]] = (when, date, parts, kind, label)
                continue
            put(date, parts, kind, label)
            continue
        parts = b["parts"]
        if not parts and kind in _CLOSE_BREACHES:
            closing = (closes_on.get(date) or (None, None))[1]
            parts = [closing] if closing else None
        put(date, parts, kind, label)
    for _when, date, parts, kind, label in latest.values():
        put(date, parts, kind, label)
    return out


def _breach_items(breaches, rows_by_date: dict):
    """Each hard breach as {date, kind, label, employee (lower; None for a
    day's), shift_start, parts (None: not known), week_key} — from any of
    the shapes place_breaches takes. A breach about no date (the engine
    map's "week") stands on every date with rows."""
    if isinstance(breaches, dict):
        raw = [dict(b, date=b.get("date") or d) for d, items in (breaches.get("by_date") or {}).items()
               for b in (items or []) if isinstance(b, dict)]
        raw += [dict(b, date=d) for b in (breaches.get("week") or []) if isinstance(b, dict) for d in rows_by_date]
    else:
        raw = list(breaches or [])
    for v in raw:
        if isinstance(v, (tuple, list)):
            v = _breach_from_id(v)
        if not isinstance(v, dict) or v.get("hard") is False:
            continue
        date = str(v.get("date") or "").strip()
        if not date:
            continue
        kind = v.get("kind") or ""
        emp = (v.get("employee") or "").strip().lower()
        if v.get("day_level") or kind in _DAY_BREACHES:
            emp = ""
        parts = [p for p in (v.get("dayparts") or []) if p] or None
        if not parts:
            part = (v.get("daypart") or "").strip().lower()
            if part in CORE_WINDOWS:
                parts = [part]
            elif v.get("gap_start") is not None and v.get("gap_end") is not None:
                try:
                    gs, ge = int(v["gap_start"]), int(v["gap_end"])
                    parts = (["morning"] if gs < DAYPART_CUTOVER else []) + (["night"] if ge > DAYPART_CUTOVER else [])
                except (TypeError, ValueError):
                    parts = None
            elif kind == "no_manager_on_duty" and v.get("shift_start"):
                parts = [daypart_of(v.get("shift_start"))]
        yield {"date": date, "kind": kind,
               "label": str(v.get("detail") or v.get("label") or kind.replace("_", " ") or "a hard rule").strip(),
               "employee": emp or None, "shift_start": v.get("shift_start") or "", "parts": parts or None,
               "week_key": tuple(v.get("id") or ()) or (kind, emp, v.get("bucket") or v.get("week") or "")}


def _breach_from_id(bid) -> dict:
    """A schedule_rules.breach_id tuple as the fields place_breaches reads:
    (kind, date, ...) for the day-level kinds, (kind, date, employee) for a
    person's own. An id with no date cannot be placed and is skipped."""
    bid = list(bid)
    kind = str(bid[0]) if bid else ""
    rest = bid[1:]
    if kind == "coverage_floor" and len(rest) >= 3:
        return {"kind": kind, "date": rest[0], "daypart": rest[2], "hard": True}
    if kind in _CLOSE_BREACHES or kind in ("no_manager", "owner_rule", "ends_before_role_close"):
        return {"kind": kind, "date": rest[0] if rest else "", "hard": True}
    if kind == "no_manager_on_duty" and len(rest) >= 2:
        return {"kind": kind, "date": rest[0], "daypart": rest[1], "hard": True}
    if len(rest) >= 2 and isinstance(rest[0], str) and rest[0][:2] == "20":
        return {"kind": kind, "date": rest[0], "employee": rest[1], "hard": True}
    return {}


def build_contexts(rows: list, profiles: list = None, only_dates=None, frame: dict = None, **signals) -> list:
    """Bucket a finished schedule into the shifts the engine scores.

    `only_dates` builds the contexts of those dates only — the week-level
    facts every context shares (who works what across the week, the hard
    breaches' placement) are still read from every row; `frame`, a dict,
    is filled with those shared facts ({"week_assignments", "flagged",
    "breaches_at", "week_start", "ratings"}) — LocalScorer's way of building
    only the dates a move touched (schedule audit 10/3/26 P-38).

    One context per (date, daypart) — the whole shift across every role,
    not per role. Coverage and strength look at roles from the inside;
    leadership, training and fatigue are properties of the team as a whole
    and cannot be seen one role at a time.

    A row belongs to every daypart it is on the floor for (present_dayparts),
    so a straight-through counts at lunch and at dinner; its hours and its
    place in the week's assignments are counted once, under its start.

    A salaried person's hours are not the day's or the daypart's hours
    (schedule audit 10/3/26 D-3): the day targets (labor.week_hours_plan)
    and the sales-per-labor-hour history are hourly only, so every manager
    row the manager-every-minute rule added read as "Xh over this day's
    target" and as thin SPLH, and the passes trimmed exactly the coverage
    the hard rule needs. They still work the shift — coverage, fatigue
    (against their own ceiling) and everything else count them.
    """
    demand_by_day = signals.get("demand_by_day") or {}
    profiles = profile_set(profiles, demand_by_day)
    demand_by_date = signals.get("demand_by_date") or {}
    salaried = {name_key(n) for n in (signals.get("salaried") or ()) if str(n or "").strip()}
    # A role's AM/PM job codes are one role (the restaurant's own map over
    # role_family's word rule), managers run a shift (SQ-13).
    families = {str(k).strip().lower(): v for k, v in (signals.get("role_families") or {}).items() if k}
    managers = {str(k).strip().lower(): v for k, v in (signals.get("managers") or {}).items() if k}
    acting = {str(k).strip().lower(): set(v or ()) for k, v in (signals.get("acting_managers") or {}).items() if k}
    buckets, primary, day_hours, part_hours = {}, {}, {}, {}
    for row in rows or []:
        name = (row.get("employee") or "").strip()
        date = (row.get("date") or "").strip()
        if not (name and date):
            continue
        parts = present_dayparts(row)
        for part in parts:
            buckets.setdefault((date, part), []).append(row)
        primary.setdefault((date, parts[0]), []).append(row)
        if name_key(name) in salaried:
            continue
        day_hours[date] = day_hours.get(date, 0.0) + _row_hours(row)
        part_hours[(date, parts[0])] = part_hours.get((date, parts[0]), 0.0) + _row_hours(row)

    # Which bucket actually closes each date, so a closing requirement binds
    # the shift it means rather than every shift of the day. Judged on each
    # row's own daypart, so a straight-through does not make lunch a close.
    closes_on, latest_end = {}, {}
    for (date, part), shift_rows in primary.items():
        latest = max((_end_minutes(r.get("shift_end")) for r in shift_rows), default=-1)
        latest_end[date] = max(latest_end.get(date, -1), latest)
        if latest > closes_on.get(date, (-1, None))[0]:
            closes_on[date] = (latest, part)

    def _profile(date, day, part):
        return profile_for_shift(day, part, profiles, demand_by_day,
                                 (demand_by_date.get(date) or {}).get("lift_pct"))

    # The hard breaches the sweep found, each on the shift it is about.
    breaches_at = place_breaches(signals.get("hard_breaches"), rows, closes_on)

    # Who works what across the whole week, so fatigue and fairness can see
    # past the one shift they are scoring. Seeded with the tail of the
    # PREVIOUS schedule, because a run of nine days looks like five when the
    # engine can only see inside its own seven-day box — and the week
    # boundary is exactly where that matters. Those seeded entries carry
    # "prior" so fairness does not count them as this week's share. Their
    # demand is read from the same profiles as this week's shifts (SQ-27:
    # every tail day was written "normal", so a busy Saturday before the
    # week began read as quiet to anything that asked).
    week_assignments = {}
    for name, entries in (signals.get("prior_week_assignments") or {}).items():
        for e in entries:
            e = dict(e, prior=True)
            if not e.get("demand") and e.get("day") and e.get("daypart"):
                e["demand"] = profile_for_shift(e["day"], e["daypart"], profiles, demand_by_day).demand
            week_assignments.setdefault(name, []).append(e)
    for (date, part), shift_rows in primary.items():
        day = _day_name(date)
        demand = _profile(date, day, part).demand
        closing_part = closes_on.get(date, (None, None))[1]
        for row in shift_rows:
            name = (row.get("employee") or "").strip()
            if not name:
                continue
            entries = week_assignments.setdefault(name, [])
            closing = part == closing_part or (
                _end_minutes(row.get("shift_end")) >= 0
                and _end_minutes(row.get("shift_end")) == latest_end.get(date))
            existing = [e for e in entries if e["date"] == date and e["daypart"] == part and not e.get("prior")]
            if not existing:
                entries.append({"date": date, "daypart": part, "day": day, "demand": demand,
                                "weekend": day in ("Friday", "Saturday", "Sunday"),
                                "closing": closing, "hours": _row_hours(row),
                                "role": (row.get("role") or "").strip()})
            else:
                existing[0]["hours"] = (existing[0].get("hours") or 0) + _row_hours(row)
                existing[0]["closing"] = existing[0]["closing"] or closing

    targets = signals.get("daily_target_hours") or {}
    open_times = signals.get("open_times") or {}
    close_times = signals.get("close_times") or {}
    role_floors_all = signals.get("role_floors") or {}
    demand_curves = signals.get("demand_curve") or {}
    day_rows_by_date = {}
    for row in rows or []:
        if (row.get("employee") or "").strip() and (row.get("date") or "").strip():
            day_rows_by_date.setdefault(row["date"].strip(), []).append(row)

    # A shift this restaurant runs that the draft left out entirely is still
    # a shift: scored with nobody on it rather than not scored at all. Only
    # scoring the buckets that had rows let a week with no Saturday dinner
    # read "good". Expected means its usual crew or a staffing floor says it
    # runs, on a date the draft covers (a closed date has no rows at all).
    typical_all = signals.get("typical_headcount") or {}
    expected = set()
    for date in {d for d, _p in buckets}:
        day = _day_name(date)
        for part in ("morning", "night"):
            if (date, part) in buckets:
                continue
            runs = any(n for n in ((signals.get("requirements_by_date") or {}).get((date, part))
                                   or typical_all.get((day, part)) or {}).values())
            if not runs:
                for spec in role_floors_all.values():
                    dspec = (spec.get("days") or {}).get(day) or {}
                    if (dspec.get(part) if part in dspec else spec.get(part)):
                        runs = True
                        break
            if runs:
                expected.add((date, part))
    for key in expected:
        buckets[key] = []

    splh_targets = signals.get("splh_targets") or {}
    daypart_sales = signals.get("daypart_sales") or {}
    base_scores = signals.get("scores") or {}
    role_scores = signals.get("role_scores") or {}

    def _scores_for(shift_rows):
        """The scores this shift is judged on: each person's score for the
        role their row is in when the owner rated them in it, else their
        overall score (schedule audit 10/3/26 D-12 — a 5 as Server counted
        the same on Bar)."""
        if not role_scores:
            return base_scores
        out = dict(base_scores)
        for r in shift_rows:
            name = (r.get("employee") or "").strip()
            mine = role_scores.get(name)
            if not mine:
                continue
            role = (r.get("role") or "").strip()
            for key in (role_family(role, families), role_family(role), role.lower()):
                if key in mine and mine[key] is not None:
                    out[name] = mine[key]
                    break
        return out
    # The people facts every context shares (schedule audit 10/3/26, D1b).
    by_default = signals.get("experienced_default")
    if by_default is None:
        by_default = experienced_by_default(managers, acting, salaried)
    by_default = set(by_default or ())
    learned = signals.get("learned_preferences")
    if learned is None:
        # Merged into the preferences as each person's "learned" entry.
        learned = {n: p["learned"] for n, p in (signals.get("preferences") or {}).items()
                   if isinstance(p, dict) and isinstance(p.get("learned"), dict)}
    ceilings = {name_key(k): float(v) for k, v in (signals.get("hours_ceilings") or {}).items() if v}
    load_ledger = signals.get("load_ledger") or {}
    week_start = min(day_rows_by_date) if day_rows_by_date else ""
    busy_slots = set()
    if load_ledger:
        for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
            for p in ("morning", "night"):
                if _is_busy({"demand": profile_for_shift(d, p, profiles, demand_by_day).demand}):
                    busy_slots.add((d, p))
    # Week-level facts every shift reads the same: the crew each strength
    # target was set for (SQ-4), and the restaurant's own ratings by role
    # (SQ-12).
    crews = strength_crews(profiles, typical_all, role_floors_all, signals.get("role_minimums") or {},
                           int(signals.get("section_cap") or 0), set(signals.get("cap_roles") or ()), families)
    runs = role_runs(typical_all, role_floors_all, signals.get("role_minimums") or {}, profiles, families)
    ratings = role_ratings(signals.get("scores") or {}, signals.get("roster_roles") or {}, rows, families)
    # The week's demand-scaled requirements ({(date, daypart): {role:
    # people}}, schedule_requirements.requirements_map) — what the model was
    # told each shift needs, judged here instead of the weekday's usual crew
    # wherever the draft carries them (schedule audit 10/3/26 P-19).
    req_by_date = signals.get("requirements_by_date") or {}
    req_reasons = signals.get("requirement_reasons") or {}
    if frame is not None:
        frame.update(week_assignments=week_assignments, flagged=signals.get("flagged") or set(),
                     breaches_at=breaches_at, week_start=week_start, ratings=ratings)
    contexts = []
    for (date, part), shift_rows in sorted(buckets.items()):
        if only_dates is not None and date not in only_dates:
            continue
        day = _day_name(date, shift_rows[0].get("day", "") if shift_rows else "")
        profile = _profile(date, day, part)
        # The sales this weekday's daypart usually does, raised by a lift
        # the owner recorded for the date — what the hours are set against.
        expected = ((daypart_sales.get(day) or {}).get(part)) if daypart_sales else None
        if expected:
            lift = (demand_by_date.get(date) or {}).get("lift_pct")
            try:
                expected = float(expected) * (1 + float(lift) / 100.0) if lift else float(expected)
            except (TypeError, ValueError):
                expected = float(expected)
        floors_here = {}
        for role, spec in role_floors_all.items():
            dspec = (spec.get("days") or {}).get(day) or {}
            n = dspec.get(part) if part in dspec else spec.get(part)
            if n:
                floors_here[role] = int(n)
        # The late segment rides on the night of a date closing past 11pm.
        late = _late_facts(date, day, part, close_times, req_by_date, day_rows_by_date.get(date, []),
                           splh_targets, daypart_sales, demand_by_date, salaried=salaried)
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
            scores=_scores_for(shift_rows),
            tenure=signals.get("tenure") or {},
            leader_flags=signals.get("leader_flags") or {},
            leader_rules=signals.get("leader_rules") or [],
            cross_trained=signals.get("cross_trained") or {},
            typical_headcount=(req_by_date.get((date, part))
                               or (signals.get("typical_headcount") or {}).get((day, part), {})),
            requirement_reasons=list(req_reasons.get((date, part)) or []),
            role_minimums=signals.get("role_minimums") or {},
            demand_pct=demand_by_day.get(day),
            target_hours=targets.get(date),
            day_hours=day_hours.get(date, 0.0),
            week_assignments=week_assignments,
            prior_pattern=signals.get("prior_pattern") or {},
            history_weeks=int(signals.get("history_weeks") or 0),
            ledger=signals.get("ledger") or {},
            availability=signals.get("availability") or {},
            experienced=set(signals.get("experienced") or ()),
            cross_training_target=float(signals.get("cross_training_target") or 0.34),
            preferences=signals.get("preferences") or {},
            cross_training_targets=signals.get("cross_training_targets") or {},
            section_cap=int(signals.get("section_cap") or 0),
            cap_roles=set(signals.get("cap_roles") or ()),
            rotation=signals.get("rotation") or {},
            splh_target=(splh_targets.get(day) or {}).get(part),
            expected_sales=expected or None,
            daypart_hours=part_hours.get((date, part), 0.0),
            salaried=salaried,
            hours_ceilings=ceilings,
            experienced_default=by_default,
            held_roles=signals.get("held_roles") or {},
            roster_roles=signals.get("roster_roles") or {},
            stations=signals.get("stations") or {},
            learned_preferences=learned or {},
            load_ledger=load_ledger,
            busy_slots=busy_slots,
            week_start=week_start,
            overtime=signals.get("overtime") or {},
            learned=list(signals.get("learned") or []),
            managers=managers,
            acting_managers=acting,
            role_families=families,
            hard_breaches=list(breaches_at.get((date, part)) or []) + list(breaches_at.get((date, "*")) or []),
            unwritten=not shift_rows,
            strength_crews=crews,
            role_ratings=ratings,
            role_runs=runs,
            **late,
        ))
    return contexts


def _late_facts(date, day, part, close_times, req_by_date, day_rows, splh_targets, daypart_sales,
                demand_by_date, salaried=frozenset()) -> dict:
    """The late segment's ShiftContext fields for one night (D-32): its
    window, the people it needs (the requirements table's late row), its
    usual sales raised by the date's lift, its target and the hours the
    draft puts in it. {} for a morning, or a night not closing past 11pm.

    `salaried` (name keys): their late hours are not the late segment's, on
    the basis its target is measured on (splh_by_daypart is hourly only) and
    the night's own hours already are (schedule re-audit 10/4/26 SQ-9): a
    salaried owner closing beside one bartender read half the late SPLH and
    told the owner to cut 4 late hours."""
    if part != "night":
        return {}
    window = late_window(_slot_minutes((close_times or {}).get(day)) if (close_times or {}).get(day) else None)
    if window is None:
        return {}
    sales = ((daypart_sales or {}).get(day) or {}).get("late")
    if sales:
        lift = ((demand_by_date or {}).get(date) or {}).get("lift_pct")
        try:
            sales = float(sales) * (1 + float(lift) / 100.0) if lift else float(sales)
        except (TypeError, ValueError):
            sales = float(sales)
    hours = sum(late_minutes(r, window) for r in day_rows or []
                if (r.get("employee") or "").strip() and name_key(r.get("employee")) not in salaried) / 60.0
    return {"late_window": window, "late_required": dict(req_by_date.get((date, "late")) or {}),
            "late_expected_sales": sales or None, "late_splh_target": ((splh_targets or {}).get(day) or {}).get("late"),
            "late_hours": round(hours, 2)}


def _rule_label(rule: dict) -> str:
    need = int(rule.get("count") or 1)
    role = (rule.get("role") or "").strip().lower()
    return (f"{need} {role}{'' if need == 1 else 's'}"
            + (" authorized to close" if (rule.get("attribute") or "").strip()
               else f" scoring {float(rule['min_score']):g} or above"))


def roster_leader_rules(rules: list, scores: dict = None, flags: dict = None, people_roles: dict = None,
                        families: dict = None) -> tuple:
    """(kept, set_aside, capped): the leader rules this roster can be held
    to. A rule asking for a capability — a minimum score, or authorized to
    close — is held to the people in its role family who have it: nobody
    able, it is set aside (named, never failing every shift it covers); fewer
    able than it asks for, it is KEPT with its count capped at the people
    able (`count` the capped number, `asked_count` the owner's) and named in
    `capped` ({label, asked, able}). The unmeetable check used to set aside
    the whole of "2 bartenders scoring 5" when one such bartender existed,
    so the one who could be scheduled was never asked for at all (schedule
    audit 10/3/26 SQ-17). A plain headcount rule is untouched.

    people_roles: {name: roles they can work} (roster role, the roles their
    history shows, the roles they hold); None counts everybody rated or
    flagged for every role. Roles compare by family ("Bartender AM" and
    "Bartender PM" are one), names by name_key."""
    lscores = {name_key(k): v for k, v in (scores or {}).items() if v is not None}
    lflags = {name_key(k) for k, v in (flags or {}).items() if v}
    pools = None
    if people_roles is not None:
        pools = {}
        for n, roles in people_roles.items():
            for r in roles or ():
                fam = role_family(r, families)
                if fam:
                    pools.setdefault(fam, set()).add(name_key(n))
    kept, aside, capped = [], [], []
    for rule in rules or []:
        role = (rule.get("role") or "").strip()
        attribute = (rule.get("attribute") or "").strip()
        min_score = rule.get("min_score")
        if not role or (not attribute and min_score is None):
            kept.append(rule)
            continue
        pool = pools.get(role_family(role, families), set()) if pools is not None else set(lscores) | lflags
        if attribute:
            able = sorted(n for n in pool if n in lflags)
        else:
            try:
                bar = float(min_score)
            except (TypeError, ValueError):
                kept.append(rule)
                continue
            able = sorted(n for n in pool if float(lscores.get(n) or 0) >= bar)
        try:
            count = max(1, int(rule.get("count") or 1))
        except (TypeError, ValueError):
            count = 1
        if not able:
            aside.append(rule)
        elif len(able) < count:
            kept.append(dict(rule, count=len(able), asked_count=count))
            capped.append({"label": _rule_label(rule), "asked": count, "able": len(able)})
        else:
            kept.append(rule)
    return kept, aside, capped


def _rules_for_roster(rows: list, signals: dict) -> tuple:
    """(signals, set-aside rules, capped rules) — the leader rules the week
    is judged on (roster_leader_rules).

    The engine knows the roster: it passes the rules already capped, the
    ones nobody can meet (`unmeetable_leader_rules`, set aside here) and the
    capped ones (`capped_leader_rules`). A caller that only says HOW MANY
    were unmeetable (`unsatisfiable`) has the rules judged against the
    people in each role this week, or cross-trained into it — capped the same
    way. With neither, nothing is set aside: a qualified person merely off
    this shift is a real miss."""
    rules = list(signals.get("leader_rules") or [])
    capped = list(signals.get("capped_leader_rules") or [])
    explicit = signals.get("unmeetable_leader_rules")
    if explicit:
        aside = list(explicit)
        kept = [r for r in rules if r not in explicit]
    elif int(signals.get("unsatisfiable") or 0) and (signals.get("scores") or signals.get("leader_flags")):
        people_roles = {}
        for r in rows or []:
            n = (r.get("employee") or "").strip()
            if n and (r.get("role") or "").strip():
                people_roles.setdefault(n, set()).add(r["role"].strip())
        for n, roles in (signals.get("cross_trained") or {}).items():
            people_roles.setdefault(n, set()).update(str(x) for x in roles or [] if str(x).strip())
        kept, aside, more = roster_leader_rules(rules, signals.get("scores"), signals.get("leader_flags"),
                                                people_roles, signals.get("role_families"))
        capped += more
    else:
        return signals, [], capped
    if kept != rules:
        signals = dict(signals)
        signals["leader_rules"] = kept
    return signals, aside, capped


def _unmeetable_rules(rows: list, signals: dict) -> dict:
    """{id(rule): label} for the rules in signals["leader_rules"] nobody on
    the roster can meet — the set-aside half of _rules_for_roster, for a
    caller that only removes rules (schedule_solver._build_groups). The
    capped half is in _rules_for_roster."""
    _sig, aside, _capped = _rules_for_roster(rows, signals)
    return {id(r): _rule_label(r) for r in (signals.get("leader_rules") or []) if r in aside}


def score_rows(rows: list, profiles: list = None, weights: dict = None,
               **signals) -> dict:
    """One call from a finished schedule to a full evaluation.

    This is what every caller outside this module uses: generation, a
    manager's live edit, and the what-if loop all go through here, so they
    can never drift apart on how a schedule is judged.
    """
    # A leader rule nobody on the roster can ever meet (the best-rated
    # bartender is a 4, the rule asks for a 5) is not something this week can
    # fix: failing it capped every matching shift and made the week "weak",
    # a publish blocker no schedule could clear (SCHED-30). It is set aside
    # and named once, in the confidence reasons; a rule only some of the
    # roster can meet is held to the people able, and named too (SQ-17).
    signals, aside, capped = _rules_for_roster(rows, signals)
    contexts = build_contexts(rows, profiles=profiles, **signals)
    people, conf_signals = _confidence_inputs(rows, profiles, signals, aside, capped)
    result = evaluate_schedule(contexts, weights=weights, signals=conf_signals)
    result["people"] = sorted(people)
    return result


def _confidence_inputs(rows: list, profiles, signals: dict, aside: list, capped: list) -> tuple:
    """(people on the rows, the confidence signals) — what score_rows hands
    evaluate_schedule beside the contexts."""
    unmeetable = sorted({_rule_label(r) for r in aside})
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
        # The restaurant's own profiles, or the built-ins re-levelled by its
        # own sales history (per_day_demand with demand on file): either way
        # the shifts were judged on this restaurant's demand, and the
        # "default profiles" charge is for neither (SQ-32).
        "has_profiles": bool(profiles) and profiles is not BUILTIN_PROFILES
                        and (any((getattr(p, "source", "") or "") not in ("default", "your sales history",
                                                                       "your overall targets") for p in profiles)
                             or (bool(signals.get("demand_by_day"))
                                 and any(getattr(p, "per_day_demand", False) for p in profiles))),
        "rows_needing_review": signals.get("rows_needing_review"),
        "dropped_rows": signals.get("dropped_rows"),
        "constrained_people": len(signals.get("availability") or {}),
        "unsatisfiable": signals.get("unsatisfiable"),
        "unmeetable_rules": unmeetable,
        "capped_rules": capped,
    }
    return people, conf_signals


# ── Scoring a change without re-scoring the week ───────────────────────────
#
# The fill-in and trim passes choose between a handful of legal options —
# which of four people takes a floor shift, which of three rows gives way at
# the section cap — and each choice should cost the week the least score.
# A move touches one date; every shift-level dimension reads that date's
# rows (coverage, the half-hour sweep, the day's hours, who closes), so the
# shifts on every OTHER date score exactly as they did and are reused — and
# only the touched dates' contexts are BUILT: it used to build every
# context of the week on every call, so the "local" scorer re-did most of a
# whole-week score each time (schedule audit 10/3/26 P-38). A date is
# re-scored when its rows, the rows on it that will not stand, or the hard
# breaches placed on it differ from a date already scored. The week-level
# measures (fatigue, preferences, overtime, minimum hours) are recomputed
# every time — they are the part a move on one date can change anywhere.
# The one approximation: fairness on an untouched date can drift slightly
# because a move changes a role's overall rate; the passes only compare
# options against each other, and the finished week is always scored in
# full.

LOCAL_CACHE_LIMIT = 4000


class LocalScorer:
    """score(rows) -> the week score before rounding, re-evaluating only
    the dates whose rows differ from a date already evaluated.

        scorer = LocalScorer(rows, profiles=..., weights=..., **signals)
        best = max(options, key=lambda rows_after: scorer.score(rows_after))

    Pure, like the rest of this module. Build one per pass: the signals are
    taken as they are when it is built; a pass that sweeps its options can
    hand each one's own unstanding rows and hard breaches to score().

    `exact` (the Studio's re-score as a manager edits, schedule audit
    10/3/26 P-25): a date is reused only when everything its shifts read
    from the rest of the week is unchanged too — the week's hours of the
    people on it (each person's explanation), the fairness groups of the
    people on it, the rotation — so evaluate(rows) is score_rows(rows)
    exactly, re-scoring only the dates an edit could have moved."""

    def __init__(self, rows: list, profiles: list = None, weights: dict = None, exact: bool = False,
                 cache_limit: int = LOCAL_CACHE_LIMIT, **signals):
        self.cache_limit = int(cache_limit or LOCAL_CACHE_LIMIT)
        self._raw_signals = dict(signals)
        signals, _aside, _capped = _rules_for_roster(rows, signals)
        import time as _time
        self._profiles_arg = profiles
        self.profiles = profile_set(profiles, signals.get("demand_by_day"))
        self.weights = weights
        self.exact = bool(exact)
        self.signals = signals
        self._cache = {}
        self.evaluations = 0
        self.dates_built = 0
        self.started = _time.monotonic()      # a pass may stop consulting it past its own time budget
        self.baseline = self.score(rows)

    def _cross_keys(self, frame: dict, rows_by_date: dict, sig: dict) -> dict:
        """{date: what its shifts read from the rest of the week} — the
        exact mode's part of each date's cache key."""
        wa = frame.get("week_assignments") or {}
        families = sig.get("role_families") or {}
        summary, group_of = {}, {}
        for name, entries in wa.items():
            mine = [e for e in entries if e.get("date") and not e.get("prior")]
            hours = round(sum(float(e.get("hours") or 0) for e in mine), 3)
            summary[name] = hours
            if len(mine) >= 2:
                fam = _primary_role(mine, families)
                group_of[name] = fam
        groups = {}
        for name, fam in group_of.items():
            mine = [e for e in wa.get(name) or [] if e.get("date") and not e.get("prior")]
            groups.setdefault(fam, []).append((name, len(mine), sum(1 for e in mine if e.get("closing")),
                                               sum(1 for e in mine if e.get("weekend")),
                                               sum(1 for e in mine if _is_busy(e)),
                                               any(e.get("daypart") == "night" for e in mine)))
        groups = {f: tuple(sorted(v)) for f, v in groups.items()}
        rotation = ()
        plan = ((sig.get("rotation") or {}).get("roles")) or {}
        if plan:
            rot = []
            for role, rp in sorted(plan.items()):
                names = set(rp.get("people") or []) | set(rp.get("next_close") or []) | set(rp.get("weekend_due") or [])
                for n in sorted(names):
                    es = [e for e in wa.get(n) or [] if e.get("date") and not e.get("prior")]
                    rot.append((role, n, bool(es), any(e.get("weekend") for e in es), any(e.get("closing") for e in es)))
            rotation = tuple(rot)
        out = {}
        for date, rs in rows_by_date.items():
            people = sorted({(r.get("employee") or "").strip() for r in rs})
            out[date] = (tuple((n, summary.get(n, 0.0)) for n in people),
                         tuple(sorted((f, groups.get(f, ())) for f in {group_of[n] for n in people if n in group_of})),
                         rotation)
        return out

    @staticmethod
    def _signature(rows: list) -> tuple:
        return tuple(sorted(((r.get("employee") or "").strip().lower(), (r.get("role") or "").strip().lower(),
                             r.get("shift_start") or "", r.get("shift_end") or "",
                             str(r.get("scheduled_hours") or "")) for r in rows))

    def score(self, rows: list, flagged=None, hard_breaches=None) -> float:
        """The week score before rounding. `flagged` and `hard_breaches` are
        these rows' own (the sweep of THIS option, P-28) when given, else the
        signals'."""
        contexts, shifts, _sig = self._shifts(rows, flagged, hard_breaches)
        scored = [x for x in shifts if x.get("scored")]
        if not scored:
            return 0.0
        return week_score_raw(scored, evaluate_week_dimensions(contexts, self.weights))

    def evaluate(self, rows: list, flagged=None, hard_breaches=None) -> dict:
        """score_rows(rows) as a whole evaluation — exactly, when built with
        `exact` — re-scoring only the dates the rows moved."""
        sig = self._raw_signals
        if flagged is not None or hard_breaches is not None:
            sig = dict(sig)
            if flagged is not None:
                sig["flagged"] = flagged
            if hard_breaches is not None:
                sig["hard_breaches"] = hard_breaches
        sig, aside, capped = _rules_for_roster(rows, sig)
        contexts, shifts, _s = self._shifts(rows, flagged, hard_breaches, signals=sig)
        people, conf = _confidence_inputs(rows, self._profiles_arg, sig, aside, capped)
        # The roll-up reassigns the lines it hoists out of each shift (and
        # drops its line_keys): a copy of each, so the kept evaluations stay
        # as they were scored.
        result = evaluate_schedule(contexts, weights=self.weights, signals=conf,
                                   shifts=[dict(x) for x in shifts])
        result["people"] = sorted(people)
        return result

    def _shifts(self, rows: list, flagged=None, hard_breaches=None, signals: dict = None) -> tuple:
        """(contexts, shift evaluations, signals) of `rows`, building and
        evaluating only the dates not already kept."""
        sig = self.signals if signals is None else signals
        if signals is None and (flagged is not None or hard_breaches is not None):
            sig = dict(sig)
            if flagged is not None:
                sig["flagged"] = flagged
            if hard_breaches is not None:
                sig["hard_breaches"] = hard_breaches
        rows_by_date = {}
        for r in rows or []:
            if (r.get("employee") or "").strip() and (r.get("date") or "").strip():
                rows_by_date.setdefault(r["date"].strip(), []).append(r)
        # The week-level facts every context shares, with no context built.
        frame = {}
        build_contexts(rows, profiles=self.profiles, only_dates=(), frame=frame, **sig)
        flags = {}
        for f in frame.get("flagged") or ():
            if isinstance(f, (tuple, list)) and len(f) >= 2:
                flags.setdefault(f[1], []).append(tuple(f))
        placed = {}
        for (d, part), items in (frame.get("breaches_at") or {}).items():
            placed.setdefault(d, []).append((part, tuple((b.get("kind"), b.get("label")) for b in items)))
        ratings = tuple(sorted((k, tuple(v)) for k, v in (frame.get("ratings") or {}).items()))
        cross = self._cross_keys(frame, rows_by_date, sig) if self.exact else {}
        rules = tuple(sorted(repr(sorted((k, repr(v)) for k, v in r.items())) for r in sig.get("leader_rules") or []
                             if isinstance(r, dict)))
        keys, misses = {}, set()
        for date in rows_by_date:
            key = (date, self._signature(rows_by_date[date]), tuple(sorted(flags.get(date, ()))),
                   tuple(sorted(placed.get(date, ()))), frame.get("week_start"), ratings, rules, cross.get(date))
            keys[date] = key
            if key not in self._cache:
                misses.add(date)
        built = {}
        if misses:
            for c in build_contexts(rows, profiles=self.profiles, only_dates=misses, **sig):
                built.setdefault(c.date, []).append(c)
            self.dates_built += len(misses)
        contexts, shifts = [], []
        for date in sorted(rows_by_date):
            key = keys[date]
            hit = self._cache.get(key)
            if hit is None:
                ctxs = built.get(date, [])
                hit = (ctxs, [evaluate_shift(c, self.weights) for c in ctxs])
                self.evaluations += 1
                if len(self._cache) >= self.cache_limit:
                    self._cache.clear()
                self._cache[key] = hit
            for c in hit[0]:
                # The week's own facts, not the ones it was cached with.
                c.week_assignments = frame.get("week_assignments") or {}
                c.flagged = frame.get("flagged") or set()
                c.week_start = frame.get("week_start") or ""
            contexts.extend(hit[0])
            shifts.extend(hit[1])
        return contexts, shifts, sig

    def cost(self, rows: list) -> float:
        """Points the week gives up against the scorer's baseline (negative
        is a gain)."""
        return self.baseline - self.score(rows)

    def best(self, options: list, seconds: float = None):
        """The option whose rows score highest: options are (key, rows).
        Ties keep the earlier option, so a pass's own order breaks them.
        Returns (key, score) or (None, None) when nothing could be scored."""
        import time as _time
        t0 = _time.monotonic()
        best = None
        for key, rows in options:
            if seconds is not None and best is not None and _time.monotonic() - t0 > seconds:
                break
            try:
                val = self.score(rows)
            except Exception:
                continue
            if best is None or val > best[1] + 1e-9:
                best = (key, val)
        return best if best is not None else (None, None)


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


def _small_hours_night(start_m, end_m=None) -> int:
    """1440 when a shift belongs to the small hours of its row's night —
    schedule_rules._night_offset's rule, here because this module is pure:
    it starts before the business day's first hour and is over by 6am (a
    12:30am porter), or, with no end to judge by, starts before 4am. A
    4:30am–12:30pm baker is the morning's (schedule audit 10/3/26 E-32)."""
    if start_m is None or start_m >= BUSINESS_DAY_START_HOUR * 60:
        return 0
    if end_m is None:
        return 24 * 60 if start_m < 4 * 60 else 0
    end = end_m if end_m > start_m else end_m + 24 * 60
    return 24 * 60 if end <= 6 * 60 else 0


def _span(row):
    """(start, end) datetimes on the row's date; an end before the start
    crosses midnight, and a start in the small hours is that night's
    (schedule_rules.shift_span, E-32), so a swap's rest and overlap checks
    read a 12:30am porter the way the rule sweep does."""
    try:
        base = _iso_datetime(row.get("date", ""))
    except (ValueError, TypeError):
        return None, None
    s, e = _slot_minutes(row.get("shift_start")), _slot_minutes(row.get("shift_end"))
    if s is None or e is None:
        return None, None
    night = _small_hours_night(s, e)
    start = base + timedelta(minutes=s + night)
    end = base + timedelta(minutes=(e if e > s else e + 24 * 60) + night)
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
        self.caps = {}
        for k, v in (rules.get("caps") or {}).items():
            try:
                self.caps[str(k).strip().lower()] = float(v)
            except (TypeError, ValueError):
                continue
        self.base_hours = {k.lower(): float(v or 0) for k, v in (rules.get("base_hours") or {}).items()}
        self.ceiling = float(rules.get("weekly_ceiling") or WEEKLY_HOURS_CEILING)
        self.min_rest = float(rules.get("min_rest_hours") or 0)
        self.inactive = {str(n).lower() for n in (rules.get("inactive") or [])}
        # The rules the violation sweep holds a finished week to, so a swap,
        # a fix or a claim can never create a breach the sweep would flag:
        # a minor past the latest end, a role's certification, a person's
        # own hours window (SCHED-6).
        self.minors = {str(n).lower() for n in (rules.get("minors") or [])}
        self.minor_latest = rules.get("minor_latest_end")
        self.minor_max = float(rules.get("minor_max_daily_hours") or 0)
        self.certs = {k.lower(): {str(x).lower() for x in (v or [])} for k, v in (rules.get("certifications") or {}).items()}
        self.role_certs = {k.lower(): {str(x).lower() for x in (v or [])} for k, v in (rules.get("role_requirements") or {}).items() if v}
        self.windows = {k.lower(): v for k, v in (rules.get("time_windows") or {}).items() if v}
        # Days somebody has asked off and the owner has not decided yet: a
        # suggestion must not put them on one.
        self.pending = {k.lower(): set(v or ()) for k, v in (rules.get("pending_off") or {}).items()}
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
        """The person's weekly maximum — the one schedule_rules computes
        (rules["caps"], Constraints.max_hours) when the caller passed it; else
        their own maximum when set, even above the ceiling (the owner allowing
        them the hours, P-12), else the ceiling."""
        if name_low in self.caps:
            return self.caps[name_low]
        lim = self.limits.get(name_low)
        if lim and lim[1]:
            return float(lim[1])
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
        if date in self.pending.get(low, ()):
            return False
        choice = (self.daypart_avail.get(low) or {}).get(day)
        if choice == "off" or not works_daypart_ok(row, choice):
            return False
        start_m, end_m = _slot_minutes(row.get("shift_start")), _slot_minutes(row.get("shift_end"))
        if low in self.minors:
            if self.minor_latest is not None and start_m is not None and end_m is not None \
                    and (end_m > self.minor_latest or end_m < start_m):
                return False
            if self.minor_max and _row_hours(row) > self.minor_max + 0.01:
                return False
        need = self.role_certs.get((row.get("role") or "").strip().lower())
        if need and not need <= (self.certs.get(low) or set()):
            return False
        win = (self.windows.get(low) or {}).get(day)
        if win:
            from schedule_rules import window_allows
            if not window_allows(win[0], win[1], start_m, end_m)[0]:
                return False
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
                    return False          # an overlap is never legal, rest rule or not
                # Same calendar date is a double shift, not a rest
                # breach — the rule is the overnight turnaround.
                if not self.min_rest or ps.date() == s.date():
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

        # Rated and unrated people may trade (schedule audit 10/3/26 SQ-19):
        # the guard refused it while the score counted an unrated person as
        # nothing, so any such trade "improved" it; strength and demand match
        # now judge the rated people and treat the unrated as unknown, so the
        # score decides. `scores` is kept for callers that still pass it.

        # The cap binds only the side whose hours go UP. Somebody already
        # over it (a week written at 47.5h against 40) can still trade a
        # shift for one no longer than it — refusing that left the only
        # authorized closer unable to move to the night that needed him.
        delta = _row_hours(b) - _row_hours(a)
        if delta > 0 and self.total_hours(low_a) + delta > self.cap(low_a):
            return False
        if delta < 0 and self.total_hours(low_b) - delta > self.cap(low_b):
            return False
        return True

    def replacement_legal(self, i: int, name: str, allow_double: bool = False) -> bool:
        """Could `name` take row i outright (nobody else moves)?

        allow_double: a person already working that date may still take a
        leg that does not overlap theirs (a lunch server picking up the
        dinner shift — SCHED-34). The automatic passes keep refusing it so
        they never build doubles on their own; a person or a manager
        choosing one is different."""
        row = self.rows[i]
        low = (name or "").strip().lower()
        current = (row.get("employee") or "").strip().lower()
        if not low or low == current:
            return False
        if not allow_double and (low, row.get("date")) in self.working:
            return False
        if not self.person_fits(name, row):
            return False
        if self.total_hours(low) + _row_hours(row) > self.cap(low):
            return False
        return True


def _swap_is_legal(rows: list, i: int, j: int, availability: dict,
                   scores: dict = None, constraints: dict = None, rules: dict = None) -> bool:
    """Can these two rows trade employees without breaking anything?

    Five ways a swap goes wrong: an unavailable day, a staff constraint, a
    person already working that shift, a week pushed over forty hours, a
    double booking. Trading a rated employee against an unrated one was a
    sixth while an unrated person counted as nothing toward strength; the
    score now treats them as unknown and decides (schedule audit 10/3/26
    SQ-19). The day's rules (a manager, a closer) are the whole sweep's:
    compare_candidates asks it when given the week's Constraints.
    """
    return _SwapIndex(rows, availability, constraints, rules).legal(i, j, scores)


def _people_by_family(rows: list, roster: list, signals: dict, families: dict = None) -> dict:
    """{family: names} who may take a shift of the family: anybody working
    it this week, the roster by role, held roles and the roles their
    history shows (cross_trained) — "Server AM" and "Server PM" are one
    family (D-13). Without a roster, only the people working the family."""
    out = {}
    roster_low = {n.strip().lower() for n in roster}
    for r in rows:
        n = (r.get("employee") or "").strip()
        if n and (not roster or n.lower() in roster_low):
            out.setdefault(role_family(r.get("role"), families), set()).add(n)
    roles = signals.get("roster_roles") or {}
    cross = signals.get("cross_trained") or {}
    held = {str(k).strip().lower(): v for k, v in (signals.get("held_roles") or {}).items()}
    for n in roster:
        for role in [roles.get(n)] + list(cross.get(n) or []) + list(held.get(n.strip().lower()) or ()):
            if str(role or "").strip():
                out.setdefault(role_family(role, families), set()).add(n)
    return out


def compare_candidates(rows: list, profiles: list = None, weights: dict = None,
                       max_evaluations: int = MAX_CANDIDATE_EVALUATIONS, rule_constraints=None,
                       max_seconds: float = None, **signals) -> dict:
    """Hill-climb same-role swaps and replacements, and report what the
    alternatives cost.

    Returns the winning rows, the baseline and final scores, and one line
    per accepted swap saying which dimensions moved. A run that finds
    nothing is a real result and says so — but only ever about the
    candidates it actually tried, never about the whole space.

    With `rule_constraints` (schedule_rules.Constraints) every candidate is
    held to every rule the week is: the person-level rules for whoever
    gains a shift (Constraints.can_add, the overtime line for the side whose
    hours go up, fillable), then the whole week swept and compared by
    breach identity (schedule_rules.regressions to the budget tier — days
    in a row, the manager every minute, the closer, overtime, minimum
    hours). The swap index alone knew none of the day's rules, so the panel
    could offer an arrangement that broke one (schedule audit 10/3/26 P-33).
    A row the manager plan or the owner pinned is never offered. Rated and
    unrated people may trade: the score judges an unrated person as unknown,
    not as nothing, so it decides (SQ-19); rated pairs, where a gap can be
    seen, are tried first. Candidates are ranked with the local scorer and
    the one taken is confirmed by the full score (P-25: sixty whole-week
    scores per drag). `max_seconds` bounds the search in time as well as in
    evaluations (a generation's deadline); the verdict speaks only of what
    was tried."""
    import time as _time
    t0 = _time.monotonic()

    def out_of_time() -> bool:
        return max_seconds is not None and _time.monotonic() - t0 > max_seconds
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
    families = signals.get("role_families") or getattr(rule_constraints, "role_families", None) or None
    c = rule_constraints
    current_rows = [dict(r) for r in rows]
    current = baseline
    swaps, evaluated = [], 0
    legal_total = 0
    sweep = prof = None
    if c is not None:
        import schedule_rules as _rules
        sweep = _rules.IncrementalSweep(c)
        prof = _rules.breach_profile(current_rows, c, viols=sweep.violations(current_rows))
    scorer = LocalScorer(current_rows, profiles=profiles, weights=weights, **signals)
    pool_of = _people_by_family(current_rows, roster, signals, families)

    def _sc(n):
        return scores.get((n or "").strip())

    def legal(trial, gainers):
        """(flagged, hard breaches, profile) of a trial every rule allows;
        False when a rule refuses it; None with no rule set to ask."""
        if c is None:
            return None
        for name, i, up in gainers:
            row = trial[i]
            if not c.fillable(name, row.get("date") or "")[0] or not c.holds(name, row.get("role") or "", row.get("date")):
                return False
            if not c.can_add(row, trial, overtime=up)[0]:
                return False
        viols = sweep.violations(trial)
        after = _rules.breach_profile(trial, c, viols=viols)
        if _rules.regressions(prof, after, upto=_rules.TIER_BUDGET, hard_only=False):
            return False
        return ({((v.get("employee") or "").strip().lower(), v.get("date") or "", v.get("shift_start") or "")
                 for v in viols if v.get("no_show")}, [v for v in viols if v.get("hard")], after)

    improved = True
    while improved and evaluated < max_evaluations and not out_of_time():
        improved = False
        index = _SwapIndex(current_rows, availability, constraints, rules)
        # Rated pairs first, the widest rating gap first: that is where an
        # improvement can be seen. Unrated pairs after, in order.
        pinned = {i for i, r in enumerate(current_rows) if r.get("_pinned")}
        by_family = {}
        for i, r in enumerate(current_rows):
            if (r.get("employee") or "").strip() and i not in pinned:
                by_family.setdefault(role_family(r.get("role"), families), []).append(i)
        ranked = []
        legal_here = 0
        for idxs in by_family.values():
            for a in range(len(idxs)):
                for b in range(a + 1, len(idxs)):
                    i, j = idxs[a], idxs[b]
                    if not index.legal(i, j, None):
                        continue
                    legal_here += 1
                    si, sj = _sc(current_rows[i].get("employee")), _sc(current_rows[j].get("employee"))
                    ranked.append(((0, -abs(float(si) - float(sj))) if si is not None and sj is not None else (1, 0.0),
                                   i, j))
        # A second kind of move: somebody who works the role's family and is
        # not on that date takes the row outright.
        for i, row in enumerate(current_rows) if roster else []:
            if i in pinned:
                continue
            cur = (row.get("employee") or "").strip()
            for name in sorted(pool_of.get(role_family(row.get("role"), families)) or ()):
                if name == cur or not index.replacement_legal(i, name):
                    continue
                legal_here += 1
                sc, sn = _sc(cur), _sc(name)
                ranked.append(((0, -abs(float(sc) - float(sn))) if sc is not None and sn is not None else (1, 0.0),
                               i, ("replace", name)))
        legal_total = max(legal_total, legal_here)
        ranked.sort(key=lambda t: t[0])

        for _prio, i, j in ranked:
            if evaluated >= max_evaluations or out_of_time():
                break
            trial = list(current_rows)
            if isinstance(j, tuple):
                trial[i] = dict(current_rows[i], employee=j[1])
                gainers = [(j[1], i, True)]
            else:
                a, b = current_rows[i], current_rows[j]
                trial[i], trial[j] = dict(a, employee=b["employee"]), dict(b, employee=a["employee"])
                up_b = _row_hours(a) > _row_hours(b)
                gainers = [((b["employee"] or "").strip(), i, up_b), ((a["employee"] or "").strip(), j, not up_b)]
            judged = legal(trial, gainers)
            if judged is False:
                continue
            fl, hb = (judged[0], judged[1]) if judged is not None else (None, None)
            evaluated += 1
            try:
                quick = scorer.score(trial, flagged=fl, hard_breaches=hb)
            except Exception:
                continue
            if quick - float(current.get("raw_score") or current["score"]) < MIN_IMPROVEMENT - 0.5:
                continue
            sig = dict(signals, flagged=fl, hard_breaches=hb) if judged is not None else signals
            candidate = score_rows(trial, profiles=profiles, weights=weights, **sig)
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
                if judged is not None:
                    prof = judged[2]
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
        # Whether each swap was held to every rule the week is, or only to the
        # swap index's person checks (no rule set given).
        "checked_with": "every rule" if c is not None else "availability and hours",
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


# Breaches another person on the same shift can clear. A shift that is too
# long, or a shift with no manager on it, is about the SHIFT, not who works
# it: swapping the person "fixed" nothing and cost them the shift. A minor
# over their age band's weekly cap or starting before its earliest start is
# cleared by an adult on the shift that crosses it (schedule audit 10/3/26
# P-5, E-27: both stayed hard flags every week, never tried). A role written
# for somebody who does not hold it (D-15) is cleared by somebody who does,
# and a day a person's own scheduling note holds them off by somebody free.
PERSON_FIXABLE = frozenset({"off_roster", "inactive", "outside_week", "double_booked", "overlap",
                            "approved_time_off", "unavailable_day", "unavailable_daypart", "elsewhere",
                            "outside_window", "missing_cert", "over_max_hours", "rest_gap",
                            "minor_late", "minor_hours", "long_run", "minor_week_hours", "minor_early",
                            "role_not_held", "note_unavailable"})


def apply_fixes(rows: list, violations: list, profiles: list = None, weights: dict = None,
                max_evaluations: int = MAX_CANDIDATE_EVALUATIONS, rule_constraints=None,
                only_dates=None, max_seconds: float = None, **signals) -> dict:
    """Repair the rows that break a hard rule by putting somebody legal on
    them, choosing the replacement that scores best. Rows nobody legal can
    take are left as they are and named, never dropped: a coverage gap the
    owner can see beats a silently thinner week.

    Only breaches a different person can clear are attempted (PERSON_FIXABLE).
    A breach about a DAY (no manager on, a floor short — `day_level`) is the
    day's, shown on the day by the review, never an "unfixed" line on whoever
    the sweep pinned it to (E-13). A row the manager plan or the owner pinned
    ("_pinned") is never handed to somebody else.

    With `rule_constraints` (schedule_rules.Constraints) each candidate is
    somebody code may choose that day (Constraints.fillable) for whom the row
    is legal with their week swept (Constraints.can_add — first only people
    who stay under their overtime line, and only when nobody can, someone
    past it but never past their maximum: a person's legality outranks
    overtime, the owner's rule is that overtime goes to nobody while a
    teammate has room; schedule audit 10/3/26 P-2). The fix is kept only when
    the whole week, swept (incrementally — P-37) and compared by breach
    identity, shows the row's breach gone or smaller and nothing else new or
    worse: nothing about anybody, no minute without a manager, no floor or
    closer, and — taking somebody under their line — no overtime or minimum
    hours either (schedule_rules.regressions; it used to compare (row index,
    kind) sets, blind to a new breach pinned to the same row — E-1). Rows the
    evaluation budget (or `max_seconds`, a generation's deadline) did not
    reach are reported as not tried, never as impossible.

    A missed day off (schedule_rules "days_off": fewer consecutive days off
    than the rule) is fixable too, after the hard breaches: one of the
    person's shifts goes to a legal teammate, the one that costs the week
    the least score (_fix_days_off). It needs `rule_constraints` to know the
    rule and to prove no new hard breach; `only_dates` limits which rows
    may be handed over (a partial redo keeps the owner's days).

    Returns {rows, fixes: [{index, from, to, kind, reason}], unfixed: [...]}.
    """
    rows = [dict(r) for r in rows]
    c = rule_constraints
    days_off = [v for v in (violations or []) if v.get("kind") == "days_off"]
    availability = signals.get("availability") or {}
    cons = signals.get("constraints") or {}
    rules = signals.get("rules") or {}
    roster = [n for n in (signals.get("roster") or []) if n]
    families = signals.get("role_families") or getattr(c, "role_families", None) or None
    fixes, unfixed, evaluated = [], [], 0
    hard = [v for v in (violations or []) if v.get("hard") and not v.get("day_level")]
    for v in hard:
        if v.get("kind") not in PERSON_FIXABLE:
            unfixed.append({"index": v.get("index"), "employee": v.get("employee"), "kind": v.get("kind"),
                            "reason": f"{v.get('label') or v.get('kind')} — not something a different person on "
                                      "the shift would change."})
    hard = [v for v in hard if v.get("kind") in PERSON_FIXABLE]
    # over_max_hours lands on EVERY row of the person's payroll week. Fixing
    # each one would strip them of the whole week (48h → 0h); only the
    # excess of THAT payroll week should move. Keep its latest rows until
    # the week fits, and drop the rest of its entries from the work list.
    over = {}
    for v in hard:
        if v.get("kind") == "over_max_hours":
            over.setdefault(((v.get("employee") or "").strip().lower(), v.get("bucket")), []).append(v)
    if over:
        keep = set()
        for (_low, _b), vs in over.items():
            excess = float(vs[0].get("over_by") or 0)
            if excess <= 0:
                probe = _SwapIndex(rows, availability, cons, rules)
                excess = probe.total_hours(_low) - probe.cap(_low)
            vs_sorted = sorted(vs, key=lambda x: (rows[x["index"]].get("date") or "",
                                                  _slot_minutes(rows[x["index"]].get("shift_start") or "") or 0),
                               reverse=True)
            removed = 0.0
            for v in vs_sorted:
                if removed >= excess - 0.05:
                    break
                keep.add(id(v))
                removed += _row_hours(rows[v["index"]])
        hard = [v for v in hard if v.get("kind") != "over_max_hours" or id(v) in keep]

    sweep = before = None
    if c is not None:
        import schedule_rules as _rules
        sweep = _rules.IncrementalSweep(c)
        before = _rules.breach_profile(rows, c, viols=sweep.violations(rows))
    scorer = None
    pools = None
    seen_idx = set()
    budget_out = False
    import time as _time
    t0 = _time.monotonic()

    def spent() -> bool:
        return evaluated >= max_evaluations or (max_seconds is not None and _time.monotonic() - t0 > max_seconds)
    for v in sorted(hard, key=lambda x: (0 if x.get("no_show") else 1, x.get("index", 0))):
        i = v.get("index")
        if i is None or i in seen_idx or i >= len(rows):
            continue
        seen_idx.add(i)
        row = rows[i]
        cur = (row.get("employee") or "").strip()
        if row.get("_pinned"):
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"),
                            "reason": f"{cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift is fixed "
                                      "(the manager plan, or a day you kept) — change it by hand."})
            continue
        if only_dates is not None and row.get("date") not in only_dates:
            continue
        if budget_out or spent():
            budget_out = True
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"), "not_tried": True,
                            "reason": f"{cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift wasn't "
                                      "checked yet — press Apply fixes to check the rest."})
            continue
        if scorer is None:
            scorer = LocalScorer(rows, profiles=profiles, weights=weights, **signals)
            pools = _people_by_family(rows, roster, signals, families)
        index = _SwapIndex(rows, availability, cons, rules)
        pool = set(pools.get(role_family(row.get("role"), families)) or ())
        pool |= {(r.get("employee") or "").strip() for r in rows
                 if role_family(r.get("role"), families) == role_family(row.get("role"), families)}
        pool.discard("")
        if c is not None:
            # The week's rules decide the hours (can_add below: the overtime
            # line, then the maximum) — the swap index's cap is only the
            # caller's copy of them, the ceiling when none was passed.
            candidates = [n for n in sorted(pool) if n != cur and (n.lower(), row.get("date")) not in index.working
                          and index.person_fits(n, row)]
        else:
            candidates = [n for n in sorted(pool) if n != cur and index.replacement_legal(i, n)]
        target = None
        if c is not None:
            target = _rules.breach_id(v)
        legal_opts, tried = [], 0
        for under_line in (True, False) if c is not None else (True,):
            for name in candidates:
                if spent():
                    budget_out = True
                    break
                trial = list(rows)
                trial[i] = dict(row, employee=name)
                fl = hb = after = None
                if c is not None:
                    if not c.fillable(name, row.get("date") or "")[0]:
                        continue
                    if not c.can_add(trial[i], trial, overtime=under_line)[0]:
                        continue
                    viols = sweep.violations(trial)
                    after = _rules.breach_profile(trial, c, viols=viols)
                    if after["by_id"].get(target, 0.0) >= before["by_id"].get(target, 0.0) - 1e-9:
                        continue            # the breach this fix is for is still there
                    if _rules.regressions(before, after, upto=_rules.TIER_MIN_HOURS if under_line else _rules.TIER_COVERAGE,
                                          hard_only=not under_line):
                        continue            # a fix that trades one breach for another is not a fix
                    fl = {((x.get("employee") or "").strip().lower(), x.get("date") or "", x.get("shift_start") or "")
                          for x in viols if x.get("no_show")}
                    hb = [x for x in viols if x.get("hard")]
                tried += 1
                evaluated += 1
                try:
                    val = scorer.score(trial, flagged=fl, hard_breaches=hb)
                except Exception:
                    continue
                legal_opts.append((val, name, trial, after))
            if legal_opts or budget_out:
                break
        if legal_opts:
            val, name, trial, after = sorted(legal_opts, key=lambda t: (-t[0], t[1]))[0]
            rows = trial
            rows[i] = dict(rows[i])
            if after is not None:
                before = after
            note = (rows[i].get("notes") or "").strip()
            rows[i]["notes"] = (note + f" (was {cur} — {v.get('label') or v.get('kind')})").strip()
            fixes.append({"index": i, "from": cur, "to": name, "kind": v.get("kind"),
                          "reason": f"{cur} — {v.get('detail') or v.get('label')}; {name} can take it."})
        elif budget_out and not tried:
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"), "not_tried": True,
                            "reason": f"{cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift wasn't "
                                      "checked yet — press Apply fixes to check the rest."})
        else:
            unfixed.append({"index": i, "employee": cur, "kind": v.get("kind"),
                            "reason": f"Nobody on the roster can legally take {cur}'s {row.get('day') or row.get('date')} {row.get('role')} shift."})
    if days_off:
        # Its own budget: each option is scored locally (LocalScorer), a
        # fraction of what a whole-week re-score costs.
        rows, more, missed, used = _fix_days_off(rows, days_off, profiles, weights, rule_constraints,
                                                 max(MAX_CANDIDATE_EVALUATIONS, max_evaluations - evaluated),
                                                 only_dates, signals)
        fixes.extend(more)
        unfixed.extend(missed)
        evaluated += used
    return {"rows": rows, "fixes": fixes, "unfixed": unfixed, "evaluated": evaluated}


def _days_off_people(rows: list, rule_constraints, viols=None) -> set:
    from schedule_rules import violations as _viol
    viols = _viol(rows, rule_constraints) if viols is None else viols
    return {(v.get("employee") or "").strip().lower() for v in viols if v.get("kind") == "days_off"}


def _fix_days_off(rows: list, violations: list, profiles, weights, rule_constraints,
                  max_evaluations: int, only_dates, signals: dict) -> tuple:
    """Give each person the rule's run of days off together by handing the
    fewest of their shifts to legal teammates.

    For each window of consecutive days as long as the rule, the shifts the
    person works inside it are what would have to go; the windows needing
    the fewest are tried, and for each shift the teammate taken is the one
    that costs the week the least score (LocalScorer). A window is kept only
    when the rule sweep then shows the person's days off met, nothing new or
    worse by breach identity (schedule_rules.regressions — the manager every
    minute, the floors, the closer; it compared (row, kind) sets — E-1), and
    nobody newly short of their own days off; of the windows that pass, the
    best-scoring one wins. Each teammate is somebody code may choose that
    day for whom the row is legal (Constraints.fillable, can_add — P-2); a
    pinned row never moves.

    Returns (rows, fixes, unfixed, evaluations)."""
    fixes, unfixed, used = [], [], 0
    if rule_constraints is None:
        for v in violations:
            unfixed.append({"index": v.get("index"), "employee": v.get("employee"), "kind": "days_off",
                            "reason": f"{v.get('employee')} — {v.get('detail') or v.get('label')}; "
                                      "the rules were not loaded, so nothing was moved."})
        return rows, fixes, unfixed, used
    import schedule_rules as _rules
    c = rule_constraints
    sweep = _rules.IncrementalSweep(c)
    week = list(getattr(c, "week_dates", None) or [])
    availability = signals.get("availability") or {}
    cons = signals.get("constraints") or {}
    rules = signals.get("rules") or {}
    roster = [n for n in (signals.get("roster") or []) if n]
    families = signals.get("role_families") or getattr(c, "role_families", None) or None
    pools = _people_by_family(rows, roster, signals, families)
    done = set()
    for v in violations:
        name = (v.get("employee") or "").strip()
        low = name.lower()
        if not low or low in done:
            continue
        done.add(low)
        now = sweep.violations(rows)
        if low not in _days_off_people(rows, c, now):
            continue                      # an earlier fix already gave them the run
        part = (getattr(c, "employment", {}) or {}).get(low) == "part"
        req = c.compliance.get("part_time_days_off") if part else c.compliance.get("min_consecutive_days_off")
        try:
            req = int(req or 0)
        except (TypeError, ValueError):
            req = 0
        mine = [i for i, r in enumerate(rows) if (r.get("employee") or "").strip().lower() == low
                and not r.get("_pinned")]
        windows = []
        for k in range(0, max(0, len(week) - req + 1)):
            span = week[k:k + req]
            hand = [i for i in mine if rows[i].get("date") in set(span)]
            if not hand or (only_dates and any(rows[i].get("date") not in only_dates for i in hand)):
                continue
            windows.append((len({rows[i].get("date") for i in hand}), k, hand))
        if not req or not windows:
            unfixed.append({"index": v.get("index"), "employee": name, "kind": "days_off",
                            "reason": f"{name} — {v.get('detail') or v.get('label')}; no shift of theirs "
                                      "could be handed over here."})
            continue
        fewest = min(w[0] for w in windows)
        before = _rules.breach_profile(rows, c, viols=now)
        before_short = _days_off_people(rows, c, now)
        scorer = LocalScorer(rows, profiles=profiles, weights=weights, **signals)
        best = None
        out_of_budget = False
        for _n, _k, hand in [w for w in windows if w[0] == fewest]:
            trial = [dict(r) for r in rows]
            moved = []
            ok = True
            for i in hand:
                if used >= max_evaluations:
                    out_of_budget = True
                    ok = False
                    break
                index = _SwapIndex(trial, availability, cons, rules)
                pool = set(pools.get(role_family(trial[i].get("role"), families)) or ())
                pool.discard("")
                cands = [n for n in sorted(pool) if n.lower() != low and index.replacement_legal(i, n)
                         and c.fillable(n, trial[i].get("date") or "")[0]
                         and c.can_add(dict(trial[i], employee=n), trial)[0]]
                options = []
                for n in cands:
                    t2 = list(trial)
                    t2[i] = dict(trial[i], employee=n)
                    options.append((n, t2))
                used += len(options)
                pick, _val = scorer.best(options)
                if pick is None:
                    ok = False
                    break
                trial[i] = dict(trial[i], employee=pick)
                moved.append((i, pick))
            if not ok or not moved:
                continue
            after_v = sweep.violations(trial)
            after = _rules.breach_profile(trial, c, viols=after_v)
            after_short = _days_off_people(trial, c, after_v)
            if _rules.regressions(before, after, upto=_rules.TIER_COVERAGE) or low in after_short \
                    or (after_short - before_short):
                continue
            val = scorer.score(trial)
            if best is None or val > best[0] + 1e-9:
                best = (val, trial, moved)
        if best is None:
            unfixed.append({"index": v.get("index"), "employee": name, "kind": "days_off",
                            "not_tried": out_of_budget or None,
                            "reason": (f"{name}'s days off weren't checked yet — press Apply fixes to check the rest.")
                                      if out_of_budget else
                                      (f"{name} — {v.get('detail') or v.get('label')}; nobody could legally "
                                       "take one of their shifts without breaking another rule.")})
            continue
        _val, trial, moved = best
        for i, pick in moved:
            note = (trial[i].get("notes") or "").strip()
            trial[i]["notes"] = (note + f" (was {name} — days off)").strip()
            fixes.append({"index": i, "from": name, "to": pick, "kind": "days_off",
                          "reason": f"{name} had no {req} days off together; {pick} takes their "
                                    f"{trial[i].get('day') or trial[i].get('date')} {trial[i].get('role')} shift."})
        rows = trial
    return rows, fixes, unfixed, used


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

    The cut-offs are thresholds.demand_level's one table (+25 / +8 / -15),
    which the staff pre-shift line reads too.
    """
    import thresholds
    return thresholds.demand_level(vs_average_pct)


def profiles_from_config(stored: list = None, default_strength: dict = None,
                         default_leader_rules: list = None,
                         demand_by_day: dict = None, tuning: dict = None) -> list:
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
        # Settled per weekday when each shift is scored (profile_for_shift):
        # the busiest of a profile's days used to stand for all of them, so
        # the seven-day catch-all took Saturday's "peak" onto Sunday dinner
        # and Friday lunch.
        if using_builtins and demand_by_day:
            profile.per_day_demand = True
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
            source="your overall targets", per_day_demand=bool(demand_by_day), builtin=True))
    # Calibration applied to a profile no owner configured (SQ-22): its bar
    # and floors, kept over the built-in's own and over any level its demand
    # settles at (_follow_level). A profile the owner configured carries its
    # own numbers — an applied calibration writes those into it instead.
    for profile in profiles:
        t = (tuning or {}).get(profile.key)
        if t and profile.builtin:
            profile.tuning = dict(t)
            _apply_tuning(profile)
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
        per_day_demand=profile.per_day_demand, builtin=profile.builtin,
        floors=dict(profile.floors or {}),
        tuning={k: (dict(v) if isinstance(v, dict) else v) for k, v in (profile.tuning or {}).items()},
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
        # A floor for any dimension, 0-100; 0 = never caps (SQ-23).
        floors={str(k): int(_num(v, 0, 100, 0))
                for k, v in (data.get("floors") if isinstance(data.get("floors"), dict) else {}).items()
                if str(k) in DIMENSIONS and _num(v, 0, 100, None) is not None},
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
            "source": profile.source, "floors": dict(profile.floors or {}),
            "tuned": dict(profile.tuning or {})}
