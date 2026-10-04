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
              off and sibling-site dates, personal time windows, a minor's
              band (latest end, earliest start, the day's cap), certificates,
              the roles a person holds (their roster role, held roles, roles
              worked here, a trainee's target — never a role the draft's own
              rows gave them), whom code may choose (Constraints.fillable:
              never somebody dormant, never a day their unconfirmed note
              covers — a drafted person stays theirs), never a training
              shift handed to somebody new
  hard rules  no overlap and no new doubles, minimum rest between shifts
              (the published tail counts), each person's hours per payroll
              week under their maximum AND under their overtime line — never
              a new hour of overtime for anybody (the owner's rule: a same-
              role teammate with room takes it, or the draft's own person
              keeps it) — the longest run of consecutive days (the tail
              counts), and the rules the day is held to whoever works it:
              a MANAGER ON THE FLOOR EVERY MINUTE anybody is (Constraints.
              manages — a manager, or an acting manager on their dates; the
              owner's highest rule, schedule audit 10/3/26 PR-32: every
              stretch the draft had managed stays managed, a stretch newly
              staffed must be, and an unmanaged stretch the draft left costs
              what the score charges it), and each role's closer as the last
              of the role out (D-9). A row the manager plan or the owner
              pinned ("_pinned") is never reassigned.
  objective   a cost that mirrors the Shift Quality scorer an assignment can
              move, dimension by dimension, in the scorer's own units (a
              dimension's shortfall × its weight, on a shift's demand weight;
              a critical dimension under its floor costs the cap it puts on
              the shift): leadership as the scorer judges it (each rule's
              credit for what it found, the bar capped at the role's people
              on the shift, the weakest check, managers able to run it),
              strength as the rated people's average against target ÷ crew
              (unrated people unknown, never zero, never the role's average),
              demand match against the restaurant's own ratings, training
              cover (a manager backs a one-person station), experience
              against the profile's mix (managers and salaried people
              experienced by default), cross-training by role family,
              reliability (somebody late often never the only opener or
              closer — D-44), pairings (a preferred pair split costs, L-20),
              stations, fairness and the rotation, fatigue (this week and
              sustained), staff preferences (stated, and learned at half
              weight — L-19, D-36), minimum hours, overtime premium, labor
              dollars (each person's own rate; salaried hours cost nothing
              more — P-32), stability, and what the restaurant's scheduling
              memory holds (signals["learned"], as the scorer's learned-
              patterns measure reads it through schedule_memory.misses:
              somebody kept off a slot or on it, the role the managers make
              them, the usual opener or closer, a team on the same shifts,
              somebody who habitually runs past their shift kept their
              headroom under the overtime line — L-3, D-35). The dimensions
              only the shape can move (coverage, coverage by the hour, labor
              efficiency, sales per labor hour) are constants to it. The
              final choice between the draft and the solver's answers is
              made by shift_quality.score_rows over every dimension in
              shift_quality.DIMENSIONS, the same judge the owner's number
              comes from, with the breaches of each candidate's own rows,
              less what it costs in dollars and in memory broken — and only
              among candidates that make nothing at or above the budget tier
              new or worse (schedule_rules.regressions, by breach identity —
              E-1, P-14).

The search is complete: backtracking over the most-constrained unit first,
forward checking after every assignment (a person removed from every unit
the assignment makes illegal for them; an emptied domain, or a role's open
units on a date with fewer distinct candidates than units, backtracks at
once; a stretch that must have a manager, or a role's close that must have
its closer, with nobody left who could), and branch-and-bound on an
admissible lower bound (the cheapest remaining person for each open unit).
Interchangeable units (same date, role and times) are filled in order with
people in ascending cost rank, so no arrangement is searched twice. Per
independent part (people who share no shift, rule or pairing with another
part): the draft itself is the first incumbent when it is legal; the
heuristic's dive finds a week in milliseconds; a short complete pass proves
the small parts; then large-neighbourhood rounds re-solve a few units at a
time exactly with the same propagation and bound; and a last complete pass
with what time is left proves the week optimal for the cost model when it
finishes.

Hard rules are never traded for score. A shift no legal week can staff —
nobody legal, more shifts of a role on a date than legal people, more than
the role's people can carry this week, or a person the draft already
overcommitted (their week is then kept whole) — is kept as drafted and
named with why, and the rest of the week is solved around it.

Nothing here calls a model, reads the database or writes anything.
"""
import math
import time as _time
from datetime import datetime

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
NOTE_TAG = "Cavnar AI:"
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

# ── the cost model: the scorer's own units ─────────────────────────────────
# A cost of 1 is one point of one shift's score on a shift of demand weight
# 1 (a peak shift's point is worth 2) — the week score is the demand-weighted
# mean of its shifts, so the solver's total over the week's demand weight is
# what it costs the week. A dimension's shortfall costs its points × its
# weight ÷ W_REF, the weight a shift's score is typically spread over; a
# critical dimension under its floor costs what the cap does to the shift —
# CAP_REF, a sound shift, down to the dimension's own score. A week-level
# measure (fatigue, preferences, minimum hours, overtime) costs its points
# at its share of the week, spread over the week's demand weight.
W_REF = 100.0
CAP_REF = 85.0
# An unmanaged stretch the draft left caps every shift it touches at the
# hard-rule cap (shift_quality.HARD_BREACH_CAP), and costs this per minute
# on top so a shorter gap beats a longer one.
K_MANAGER_MINUTE = 0.02
K_PENDING = 40.0          # a day they have asked off (pending, soft)
K_CHANGE = 0.02           # a different person from the draft's: ties keep the draft
K_OFF_ROLE = 0.3          # a cross-trained fill-in rather than the role's own
K_CLOSE = 0.5             # per close they already have this week (× the fairness weight's share)
K_WEEKEND = 0.3           # per weekend shift they already have
K_HARD = 0.1              # per busy shift they already have
K_DAYS_OFF = 25.0         # fewer consecutive days off than the rule, for somebody the draft already had short
K_DAYS_OFF_NEW = 1000.0   # ...for somebody the draft (after the fix pass) gave their run: never worth it
K_MIN_HOURS_NEW = 1000.0  # per hour under what the draft kept somebody at toward their minimum: never worth it
# What the edit predictor costs: points of the shift a row the manager is
# expected to change is kept on (schedule_optimizer.LIKELY_EDIT_SHIFT_POINTS
# — the weight the repair loop holds a move to). The scheduling memory is a
# week-level measure of the score (shift_quality.week_learned), costed at its
# own weight (Problem.k_miss).


def _ordinal(date):
    try:
        return datetime.strptime(date, "%Y-%m-%d").toordinal()
    except (ValueError, TypeError):
        return None


def _low(name) -> str:
    return (name or "").strip().lower()


def _val(m) -> dict:
    """A memory's value (schedule_memory.enforced_signals), {} when it is not
    the dict its kind carries."""
    v = (m or {}).get("value")
    return v if isinstance(v, dict) else {}


def _where(day: str, part: str) -> str:
    return f"{day} {'lunch' if part == 'morning' else 'dinner' if part == 'night' else part}".strip()


class _Unit:
    __slots__ = ("id", "rows", "idx", "date", "day", "dord", "bucket", "roles", "role_label", "hours",
                 "spans", "parts", "primary", "dw", "demand", "hard", "closing", "weekend", "draft",
                 "fixed", "why_fixed", "sig", "groups", "kgroups", "sym", "sym_pos", "dgroup",
                 "role_at", "fams", "edges", "bspans", "start_at", "kitchen")

    def __init__(self, uid):
        self.id = uid
        self.groups = []
        self.kgroups = []
        self.sym = None
        self.sym_pos = 0
        self.fixed = False
        self.why_fixed = ""
        self.edges = set()
        self.kitchen = set()


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
        self.draft_days_off = set()
        self.c = constraints
        # One person, one key, as the rules file them (Constraints.key:
        # spacing folded, the roster's spelling of an alias — D-8).
        self.key = constraints.key if (constraints is not None and hasattr(constraints, "key")) else _low
        self.signals = signals = dict(signals or {})
        self.profiles = sq.profile_set(profiles, signals.get("demand_by_day"))
        w = dict(sq.DEFAULT_WEIGHTS)
        for k, v in (weights or {}).items():
            try:
                if k in w and v is not None:
                    w[k] = float(v)
            except (TypeError, ValueError):
                pass
        self.weights = w
        self.wn = {k: (w[k] / float(sq.DEFAULT_WEIGHTS[k]) if sq.DEFAULT_WEIGHTS[k] else 0.0) for k in w}
        self.notes = []
        self._build_people(roster_roles, pending)
        if constraints is not None:
            per_person = {"over_max_hours", "long_run", "rest_gap", "overlap", "double_booked"}
            for v in _rules.violations(self.rows, constraints):
                p = self.pidx.get(self.key(v.get("employee")))
                if v.get("hard") and v["kind"] in per_person and p is not None:
                    self.draft_overcommitted.add(p)
                if v["kind"] == "days_off" and p is not None:
                    self.draft_days_off.add(p)
        # The run of days off the draft gives somebody (after the fix pass,
        # which repairs a missed one) is theirs to keep: a propagated rule in
        # the search, not a cost. Only whoever the draft already had short
        # is left to the soft penalty. (Retired 10/2/26 for every restaurant;
        # only a rule set built by hand still carries it.)
        self.week_ords = sorted(o for o in (_ordinal(d) for d in self.week_dates) if o is not None)
        self.week_ord_set = set(self.week_ords)
        full_req, part_req = self.days_off_rule
        self.off_req = []
        for p in range(len(self.names)):
            req = part_req if self.employment[p] == "part" else full_req
            try:
                req = int(req or 0)
            except (TypeError, ValueError):
                req = 0
            self.off_req.append(0 if p in self.draft_days_off else req)
        self._build_units(only_dates)
        self._build_caps()
        self._build_scale()
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
            k = self.key(n)
            if k and k not in self.pidx:
                self.pidx[k] = len(self.names)
                self.names.append(n.strip())
        P = len(self.names)
        self.families = dict(s.get("role_families") or (getattr(c, "role_families", None) or {} if c is not None else {}))
        self.roles = [set() for _ in range(P)]
        self.primary_role = [""] * P
        for n, role in (roster_roles or {}).items():
            p = self.pidx.get(self.key(n))
            if p is not None and (role or "").strip():
                self.roles[p].add(_low(role))
                self.primary_role[p] = _low(role)
        for n, rls in cross.items():
            p = self.pidx.get(self.key(n))
            if p is not None:
                for role in rls or []:
                    if str(role).strip():
                        self.roles[p].add(_low(str(role)))
        # The roles a person holds or has worked here (Constraints
        # .known_roles: roster, people.held_roles — trained, promoted, the
        # POS job list — and history), and a trainee the role they are
        # learning. Never the roles the draft's own rows gave them: a role
        # the model wrongly wrote for somebody was treated as one they hold
        # (schedule audit 10/3/26 D-15). Each held role brings every job
        # code of its family in use here — "Server AM" and "Server PM" are
        # one role (D-13). The rows only name the codes in use.
        for k, rls in ((getattr(c, "known_roles", None) or {}) if c is not None else {}).items():
            p = self.pidx.get(k)
            if p is not None:
                self.roles[p] |= {_low(x) for x in rls or () if str(x).strip()}
        for k, t in ((getattr(c, "trainees", None) or {}) if c is not None else {}).items():
            p = self.pidx.get(k)
            if p is not None and (t or {}).get("target_role"):
                self.roles[p].add(_low(t["target_role"]))
        codes = {_low(r.get("role")) for r in self.rows if (r.get("role") or "").strip()}
        for rls in self.roles:
            codes |= rls
        fam = self.family
        by_family = {}
        for code in codes:
            by_family.setdefault(fam(code), set()).add(code)
        for p in range(P):
            for f in {fam(x) for x in self.roles[p]}:
                self.roles[p] |= by_family.get(f, set())
        # Flexible: two or more role FAMILIES among the roles worked here,
        # held and on the roster (SQ-10, D1b) — "Server AM" and "Server PM"
        # are one station.
        held = {self.key(k): set(v or ()) for k, v in ((getattr(c, "held_roles", None) if c is not None else None)
                                                 or s.get("held_roles") or {}).items()}
        self.fams_of = []
        for p, n in enumerate(self.names):
            f = {fam(x) for x in (cross.get(n) or []) if str(x).strip()}
            f |= {fam(x) for x in held.get(self.key(n)) or ()}
            if self.primary_role[p]:
                f.add(fam(self.primary_role[p]))
            self.fams_of.append({x for x in f if x})
        self.flexible = [len(f) > 1 for f in self.fams_of]
        self.cross_on = bool(cross or held)
        self.constrained = {self.key(n) for n, note in (s.get("constraints") or {}).items()
                            if n and str(note or "").strip()}
        pend = pending if pending is not None else (getattr(self.c, "pending_off", None) or {})
        self.pending = {self.key(k): set(v or ()) for k, v in (pend or {}).items()}
        scores = s.get("scores") or {}
        lscores = {self.key(k): v for k, v in scores.items() if v is not None}
        self.score = [lscores.get(self.key(n)) for n in self.names]
        # A person's score for one role, over the overall one, wherever a
        # unit's role is known (schedule audit 10/3/26 D-12, F2's per-role
        # ratings): {family: score}.
        lroles = {self.key(k): {str(f).strip().lower(): v for f, v in (fams or {}).items() if v is not None}
                  for k, fams in (s.get("role_scores") or {}).items()}
        self.role_score = [lroles.get(self.key(n)) or {} for n in self.names]
        self.rated_any = bool(lscores) or any(self.role_score)
        flags = s.get("leader_flags") or {}
        lflags = {self.key(k): bool(v) for k, v in flags.items()}
        self.leader = [bool(lflags.get(self.key(n))) for n in self.names]
        self.blind = not lscores and not any(lflags.values())
        rel = s.get("reliability") or {}
        lrel = {self.key(k): v for k, v in rel.items()}
        self.no_show, self.late_risk, self.rel_known = [], [], []
        for n in self.names:
            r = lrel.get(self.key(n)) or {}
            try:
                self.no_show.append(float(r.get("no_show_rate") or 0))
            except (TypeError, ValueError):
                self.no_show.append(0.0)
            self.late_risk.append(bool(r.get("late_risk")))
            self.rel_known.append(self.key(n) in lrel)
        tenure = {self.key(k): int(v or 0) for k, v in (s.get("tenure") or {}).items() if v is not None}
        marked = {self.key(n) for n in (s.get("experienced") or ()) if n}
        # Managers, acting managers and salaried people are experienced by
        # default (D-6) — they never turn the measure on by themselves.
        by_default = {sq.name_key(n) for n in (s.get("experienced_default") or ()) if n}
        if not by_default and c is not None:
            by_default = sq.experienced_by_default(getattr(c, "managers", None), getattr(c, "acting_managers", None),
                                                   getattr(c, "salaried", None))
        self.experience_on = sq.experience_judged(tenure, marked)
        self.exp_known = [(self.key(n) in tenure) or (self.key(n) in marked) or (sq.name_key(n) in by_default)
                          for n in self.names]
        self.veteran = [(self.key(n) in marked) or (sq.name_key(n) in by_default)
                        or tenure.get(self.key(n), 0) >= sq.EXPERIENCE_SHIFTS for n in self.names]
        pp = {self.key(k): v or {} for k, v in (s.get("prior_pattern") or {}).items()}
        self.pattern = []
        for n in self.names:
            pat = pp.get(self.key(n))
            if pat:
                self.pattern.append(({_low(d) for d in (pat.get("days") or [])},
                                     {_low(x) for x in (pat.get("dayparts") or [])}))
            else:
                self.pattern.append(None)
        prefs = {self.key(k): v or {} for k, v in (s.get("preferences") or {}).items()}
        learned_prefs = {self.key(k): v or {} for k, v in (s.get("learned_preferences") or {}).items()}
        self.pref_parts, self.desired, self.l_avoid, self.l_prefer, self.l_weight = [], [], [], [], []
        for n in self.names:
            p = prefs.get(self.key(n)) or {}
            self.pref_parts.append({x for x in (p.get("preferred_dayparts") or []) if x in ("morning", "night")})
            try:
                self.desired.append(float(p.get("desired_hours")) if p.get("desired_hours") else None)
            except (TypeError, ValueError):
                self.desired.append(None)
            # What they keep dropping and picking up (schedule_intel.
            # behaviour_preferences), weighed as the scorer weighs it:
            # LEARNED_PREFERENCE_WEIGHT of a stated preference (L-19, D-36).
            avoid, prefer, wt = sq._learned(learned_prefs.get(self.key(n)) or p.get("learned") or {})
            self.l_avoid.append(avoid)
            self.l_prefer.append(prefer)
            self.l_weight.append(wt)
        self.preferred_by = {}
        for p in range(P):
            for slot in self.l_prefer[p]:
                self.preferred_by.setdefault(tuple(slot), set()).add(p)
        pairs = s.get("pairs") or {}
        self.avoid = [set() for _ in range(P)]
        self.avoid_pairs, self.prefer_pairs = [], []
        for kind, out in (("avoid", self.avoid_pairs), ("prefer", self.prefer_pairs)):
            for pair in (pairs.get(kind) or ()):
                members = [self.pidx.get(self.key(x)) for x in pair]
                if len(members) == 2 and None not in members and members[0] != members[1]:
                    out.append(tuple(members))
        for a, b in self.avoid_pairs:
            self.avoid[a].add(b)
            self.avoid[b].add(a)
        self.unavailable = {self.key(k): set(v or ()) for k, v in (s.get("availability") or {}).items()}
        # The multi-week rotation (schedule_intel.rotation_plan), per role:
        # who is due a weekend off, who should rest from closing — what
        # fairness now judges a week against.
        self.rot_weekend, self.rot_rest = {}, {}
        for role, plan in (((s.get("rotation") or {}).get("roles")) or {}).items():
            key = _low(role)
            self.rot_weekend[key] = {self.pidx[self.key(n)] for n in (plan.get("weekend_due") or []) if self.key(n) in self.pidx}
            self.rot_rest[key] = {self.pidx[self.key(n)] for n in (plan.get("rest_from_close") or []) if self.key(n) in self.pidx}
        c = self.c
        self.maxh = [float(c.max_hours(n)) if c is not None else sq.WEEKLY_HOURS_CEILING for n in self.names]
        self.line = []
        for n in self.names:
            try:
                self.line.append(float(_rules.overtime_line(c, n)) if c is not None else sq.WEEKLY_HOURS_CEILING)
            except Exception:
                self.line.append(sq.WEEKLY_HOURS_CEILING)
        self.salaried = [bool(c is not None and c.is_salaried(n)) for n in self.names]
        self.base_hours = [dict((getattr(c, "base_hours", None) or {}).get(self.key(n)) or {}) for n in self.names]
        self.base_dates, self.base_ords = [], []
        for n in self.names:
            ds = set()
            for r in (getattr(c, "base_rows", None) or {}).get(self.key(n)) or []:
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
        try:
            self.daily_line = float(comp.get("daily_ot_hours") or 0)
        except (TypeError, ValueError):
            self.daily_line = 0.0
        self.min_hours = [c.min_hours(n) if c is not None else None for n in self.names]
        self.employment = [(getattr(c, "employment", None) or {}).get(self.key(n)) for n in self.names]
        self.days_off_rule = (comp.get("min_consecutive_days_off"), comp.get("part_time_days_off"))
        self.week_dates = list(getattr(c, "week_dates", None) or [])
        # Labor dollars (P-32): each person's own rate, else the role's, the
        # role's typical, the blended; salaried people's hours cost no more.
        ot = s.get("overtime") or {}
        self.rates = {str(k).strip().lower(): float(v) for k, v in (ot.get("rates") or {}).items()
                      if k and k != "_default" and _num(v)}
        self.person_rates = {" ".join(str(k).lower().split()): float(v) for k, v in (ot.get("person_rates") or {}).items()
                             if _num(v)}
        self.role_typical = {str(k).strip().lower(): float(v) for k, v in (ot.get("role_typical") or {}).items() if _num(v)}
        # Overtime pay starts past this line in a payroll week (labor.
        # OVERTIME_THRESHOLD_HOURS, the scorer's week_overtime line) — an
        # owner's higher maximum allows a person the hours, it does not make
        # them cost less.
        try:
            self.ot_threshold = float(ot.get("line") or sq.WEEKLY_HOURS_CEILING)
        except (TypeError, ValueError):
            self.ot_threshold = sq.WEEKLY_HOURS_CEILING
        blended = ot.get("default_rate") or (ot.get("rates") or {}).get("_default")
        self.blended = float(blended) if _num(blended) else (sum(self.rates.values()) / len(self.rates) if self.rates else 0.0)
        self.priced = bool(self.rates or self.person_rates or self.blended)
        # What the scheduling memory holds (schedule_memory.enforced_signals —
        # L-3, D-35), read as the scorer's learned-patterns measure reads it
        # (shift_quality.week_learned → schedule_memory.misses, the one
        # meaning of each kind), and the edit predictor's rows (L-15) when
        # the engine passes them.
        self.mem = []
        for m in s.get("learned") or []:
            if not isinstance(m, dict) or not m.get("kind") or str(m.get("enforcement") or "soft").lower() == "prompt":
                continue
            try:
                conf = float(m.get("confidence") or 0)
            except (TypeError, ValueError):
                conf = 0.0
            if conf > 0:
                self.mem.append(dict(m, confidence=conf))
        # About one person on one slot or one day: off it, in a role, the
        # role's opener or closer — a cost on the assignment itself.
        self.mem_person = {}
        for m in self.mem:
            if m["kind"] in ("moved_off", "role_change", "opener", "closer") and m.get("person"):
                self.mem_person.setdefault(self.key(m["person"]), []).append(m)
        # A team on the same shifts (pair): the people, and the memory's weight.
        self.mem_pairs = []
        for m in self.mem:
            if m["kind"] == "pair":
                members = {self.key(m.get("person"))} | {self.key(x) for x in (_val(m).get("with") or [])}
                members.discard("")
                if len(members) > 1:
                    self.mem_pairs.append((frozenset(members), m["confidence"]))
        # Somebody who habitually runs past their shift (ot_risk, L-16): their
        # overtime line is the line less the headroom they usually run over —
        # for the caps (never past it beyond the draft) and for the measure.
        self.headroom = {}
        for m in self.mem:
            if m["kind"] != "ot_risk":
                continue
            p = self.pidx.get(self.key(m.get("person")))
            try:
                head = float(_val(m).get("headroom_hours") or 0)
            except (TypeError, ValueError):
                head = 0.0
            if p is not None and head > 0 and head >= self.headroom.get(p, (0.0, 0.0))[0]:
                self.headroom[p] = (head, m["confidence"])
        self.likely = {}
        for f in s.get("likely_edits") or []:
            if isinstance(f, dict) and _num(f.get("weight")):
                self.likely[(self.key(f.get("employee")), f.get("date") or "", f.get("shift_start") or "")] = float(f["weight"])
        # Sustained fatigue across the published weeks (SQ-27): the busy
        # shifts this week, and the hours, at which somebody's last weeks
        # make them strained.
        self.sustained_busy_at, self.sustained_hours_at = {}, {}
        ledger = {self.key(k): v for k, v in (s.get("load_ledger") or {}).items()}
        if ledger:
            busy_slots = set()
            for d in sq._WEEKDAYS:
                for part in ("morning", "night"):
                    if sq._is_busy({"demand": sq.profile_for_shift(d, part, self.profiles,
                                                                    s.get("demand_by_day") or {}).demand}):
                        busy_slots.add((d, part))
            for p, n in enumerate(self.names):
                past = ledger.get(self.key(n)) or []
                if not past:
                    continue
                busy = [sum(1 for x in (w.get("slots") or []) if tuple(x) in busy_slots) for w in past][-(sq.SUSTAINED_WINDOW - 1):]
                hours = [float(w.get("hours") or 0) for w in past][-(sq.SUSTAINED_WINDOW - 1):]
                if len(busy) + 1 >= sq.SUSTAINED_MIN_WEEKS:
                    k = max(sq.HARD_SHIFT_CEILING, int(math.ceil(sq.HARD_SHIFT_CEILING * (len(busy) + 1) - sum(busy) - 1e-9)))
                    self.sustained_busy_at[p] = k
                ceiling = self.maxh[p]
                if hours and len(hours) + 1 >= sq.SUSTAINED_MIN_WEEKS and ceiling:
                    need = (ceiling + 0.05) * (len(hours) + 1) - sum(hours)
                    self.sustained_hours_at[p] = max(ceiling * sq.RELIEF_SHARE, need + 0.01)

    def family(self, role) -> str:
        if self.c is not None and hasattr(self.c, "family"):
            return self.c.family(role)
        return sq.role_family(role, self.families)

    def _sc(self, p, roles):
        """Person p's score in `roles` (a unit's): their score for one of
        those roles when the owner rated them in it — the best of them —
        else their overall score (None: unrated)."""
        mine = self.role_score[p] if p < len(self.role_score) else None
        if mine:
            got = [mine[f] for f in {sq.role_family(r, self.families) for r in (roles or ())} | set(roles or ())
                   if f in mine]
            if got:
                return max(got)
        return self.score[p]

    def manages(self, p, date) -> bool:
        """p counts as the manager on the floor that date: a manager, or an
        acting manager on their dates (Constraints.manages), who may work it."""
        c = self.c
        if c is None:
            return False
        name = self.names[p]
        return c.manages(name, date) and c.can_work(name, date)[0]

    def rate(self, p, role) -> float:
        if self.salaried[p]:
            return 0.0
        return (self.person_rates.get(" ".join(self.names[p].lower().split())) or self.rates.get(role)
                or self.role_typical.get(role) or self.blended or 0.0)

    # ── units ─────────────────────────────────────────────────────────────
    def _build_units(self, only_dates):
        editable = set(only_dates) if only_dates else None
        groups, order = {}, []
        for i, r in enumerate(self.rows):
            name = (r.get("employee") or "").strip()
            key = (self.key(name), r.get("date")) if name else (f"#{i}", r.get("date"))
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
        c = self.c
        stations = s.get("stations") or (getattr(c, "stations", None) if c is not None else None) or {}
        self.stations = stations
        if stations:
            import kitchen_stations as _ks
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
            u.fams = {self.family(x) for x in u.roles}
            u.role_label = (rs[0].get("role") or "").strip()
            u.hours = sum(_rules.row_hours(r) for r in rs)
            u.spans = [_rules.shift_span(r, tz) for r in rs]
            u.bspans = [sp for sp in (_rules._span(r) for r in rs) if sp]
            u.parts = sorted({(u.date, p) for r in rs for p in sq.present_dayparts(r) if p != "unknown"})
            u.primary = [sq.present_dayparts(r)[0] for r in rs]
            # The role a unit works on each daypart it covers (a double's legs
            # can be two roles).
            u.role_at = {}
            for r in rs:
                for p in sq.present_dayparts(r):
                    u.role_at.setdefault(p, _low(r.get("role")))
            u.start_at = min((sp[0] for sp in u.bspans), default=None)
            if stations:
                u.kitchen = {p for r in rs if _ks.is_kitchen(stations, r.get("role")) for p in _ks.parts_of(r)}
            demands = [profile(u.date, u.day, p).demand for p in u.primary if p != "unknown"] or ["normal"]
            u.demand = max(demands, key=lambda d: sq.DEMAND_RANK.get(d, 1))
            u.dw = sq.DEMAND_WEIGHT.get(u.demand, 1.0)
            u.hard = sq.DEMAND_RANK.get(u.demand, 1) >= sq.DEMAND_RANK[sq.HARD_DEMAND]
            u.closing = any(p == self.closes_on.get(u.date) or
                            (sq._end_minutes(r.get("shift_end")) >= 0 and sq._end_minutes(r.get("shift_end")) == latest_end.get(u.date))
                            for r, p in zip(rs, u.primary))
            u.weekend = u.day in ("Friday", "Saturday", "Sunday")
            name = (rs[0].get("employee") or "").strip()
            u.draft = self.pidx.get(self.key(name)) if name else None
            u.sig = (u.date, tuple(sorted(u.roles)),
                     tuple(sorted((r.get("shift_start") or "", r.get("shift_end") or "") for r in rs)))
            if any(r.get("_pinned") for r in rs):
                # The manager plan's rows, a day the owner kept: never handed
                # to somebody else (PR-32, the pinned-rows contract).
                u.fixed, u.why_fixed = True, "pinned"
            elif editable is not None and u.date not in editable:
                u.fixed, u.why_fixed = True, "a day the owner kept"
            elif name and self.key(name) in self.constrained:
                u.fixed, u.why_fixed = True, "has a written note the solver cannot read"
            elif c is not None and name and any(c.training_row(r) for r in rs):
                u.fixed, u.why_fixed = True, "a training shift, placed beside a trainer by the owner"
            elif not u.date or u.dord is None or any(sp[0] is None for sp in u.spans) or not u.roles:
                u.fixed, u.why_fixed = True, "times or date could not be read"
            self.units.append(u)
        # The edges of each role's day (D-44): a unit that is the only one of
        # its role starting first, or ending last, that date — somebody with
        # a lateness risk there is the only opener or closer.
        by = {}
        for u in self.units:
            for r, sp in zip(u.rows, (_rules._span(x) for x in u.rows)):
                if sp:
                    by.setdefault((u.date, _low(r.get("role"))), []).append((sp[0], sp[1], u.id))
        for items in by.values():
            first, last = min(t[0] for t in items), max(t[1] for t in items)
            at_first = {uid for s0, _e, uid in items if s0 == first}
            at_last = {uid for _s, e0, uid in items if e0 == last}
            if len(at_first) == 1:
                self.units[next(iter(at_first))].edges.add("opens")
            if len(at_last) == 1:
                self.units[next(iter(at_last))].edges.add("closes")

    def _build_caps(self):
        """Each person's ceiling per payroll week: their maximum, and never
        past their overtime line (a salaried person's weekly cap) beyond what
        the draft already had them at — the solver may take overtime away,
        never add it (the owner's rule). A payroll week that runs on past
        this one keeps its reserve for those days (E-10, payroll_tail_full).
        A minor's band caps their week too."""
        c = self.c
        draft = [dict(h) for h in self.base_hours]
        for u in self.units:
            if u.draft is not None:
                draft[u.draft][u.bucket] = draft[u.draft].get(u.bucket, 0.0) + u.hours
        self.draft_hours = draft
        self.capb = []
        for p, n in enumerate(self.names):
            caps = {}
            line = self.line[p]
            if p in self.headroom and not self.salaried[p]:
                line = max(0.0, line - self.headroom[p][0])
            for b in {u.bucket for u in self.units} | set(draft[p]):
                cap = min(self.maxh[p], max(line, draft[p].get(b, 0.0)))
                if c is not None and not self.salaried[p]:
                    tail = c.bucket_tail(b) if b else []
                    has_tail_rows = any(r.get("date") in tail for r in (c.base_rows.get(self.key(n)) or []))
                    if len(tail) >= _rules.TAIL_RESERVE_MIN_DAYS and not has_tail_rows:
                        cap = min(cap, max(line - 0.06, draft[p].get(b, 0.0)))
                if c is not None and self.key(n) in c.minors:
                    br = _rules.minor_rules(c.minor_bands.get(self.key(n)), c.jurisdiction)
                    wk = br.get("max_weekly_school_week")
                    if wk:
                        cap = min(cap, max(float(wk), draft[p].get(b, 0.0)))
                caps[b] = cap
            self.capb.append(caps)

    def capf(self, p, b) -> float:
        return self.capb[p].get(b, min(self.maxh[p], self.line[p]))

    def _build_scale(self):
        """The week's demand weight (what a week point is worth in the
        solver's units), the week-level measures' unit, and the labor
        dollars' — so every cost is in the scorer's own terms."""
        shifts = {}
        for u in self.units:
            for (d, part) in u.parts:
                shifts[(d, part)] = sq.DEMAND_WEIGHT.get(self.profile(d, u.day, part).demand, 1.0)
        self.shift_dw = shifts
        self.sigma_dw = sum(shifts.values()) or 1.0
        # The scheduling memory's measure is on the week only once there is a
        # memory to hold it to (week_learned withdraws without one).
        week_keys = [k for k in sq.WEEK_LEVEL_DIMENSIONS if self.weights.get(k, 0) > 0
                     and (k != "learned" or self.mem)]
        self.week_w = sum(self.weights[k] for k in week_keys)
        dollars = 0.0
        for u in self.units:
            if u.draft is not None:
                dollars += u.hours * self.rate(u.draft, u.role_at.get(u.primary[0], "") if u.primary else "")
        self.week_dollars = dollars
        import schedule_optimizer as _opt
        # week points per dollar, in the solver's units (× the week's demand weight)
        self.per_dollar = (_opt.LABOR_POINTS_PER_PCT * 100.0 / dollars * self.sigma_dw) if dollars > 0 else 0.0
        # What a unit of broken memory weight costs the learned-patterns
        # measure (shift_quality.learned_score: geometric), at the draft's
        # own level — its slope there — in the solver's units.
        self.k_miss = 0.0
        if self.mem:
            import schedule_memory as _smem
            try:
                ot_line = float((self.signals.get("overtime") or {}).get("line") or 0) or None
            except (TypeError, ValueError):
                ot_line = None
            try:
                lost = sum(float(x.get("weight") or 0) for x in _smem.misses(
                    [r for r in self.rows if (r.get("employee") or "").strip()], self.mem,
                    families=self.families or None, line=ot_line))
            except Exception:
                lost = 0.0
            keep = 1 - sq.LEARNED_MISS_POINTS / 100.0
            self.k_miss = self.week_unit("learned") * 100.0 * keep ** lost * -math.log(keep)
        self.k_likely = _opt.LIKELY_EDIT_SHIFT_POINTS
        # The items each week-level measure counts, as the draft has them —
        # what one item is worth of the measure.
        self.n_pref = max(1, sum(1 for u in self.units if u.draft is not None
                                 and (self.pref_parts[u.draft] or self.l_avoid[u.draft] or self.l_prefer[u.draft]))
                          + sum(1 for d in self.desired if d))
        tracked = {u.draft for u in self.units if u.draft is not None}
        self.n_tracked = max(1, len(tracked))
        owed = sum(float(m) for m in self.min_hours if m)
        self.min_owed = owed

    def week_unit(self, key) -> float:
        """What one point of a week-level measure is worth in the solver's
        units: a measure's points move the week by its weight against the
        week-level and shift weights (shift_quality.week_score_raw's share),
        over the week's demand weight."""
        w = self.weights.get(key, 0.0)
        if w <= 0:
            return 0.0
        return 100.0 * w / (self.week_w + W_REF) * self.sigma_dw / 100.0

    def dim_cost(self, key, shortfall, dw, floor=None, score=None) -> float:
        """A shift dimension `shortfall` points under 100: its weight's share
        of the shift's score; under its critical `floor`, what the cap does to
        the shift (CAP_REF down to the dimension's score), whichever is more."""
        if shortfall <= 0:
            return 0.0
        cost = shortfall * self.weights.get(key, 0.0) / W_REF * dw
        if floor is not None and score is not None and score < floor:
            cost = max(cost, (CAP_REF - score) * dw)
        return cost

    # ── feasibility ───────────────────────────────────────────────────────
    def fits(self, u, p):
        """None when person p may legally work unit u on their own; else the
        reason, in the rule sweep's words."""
        c, name = self.c, self.names[p]
        low = self.key(name)
        if not u.roles <= self.roles[p]:
            return "does not work " + "/".join(sorted(u.roles))
        if low in self.constrained and u.draft != p:
            return "has a written note the solver cannot read"
        if u.day in self.unavailable.get(low, ()):
            return _rules.LABELS["unavailable_day"]
        drafted = p == u.draft
        if not drafted and c is not None:
            # Code never chooses somebody who has not worked in weeks, or on a
            # day their unconfirmed note covers (P-2); the draft's own person
            # stays theirs.
            ok, why = c.fillable(name, u.date)
            if not ok:
                return why
        for r in u.rows:
            # Any minute in a blocked half of the day (RULES-11), the card as
            # it stands that date (RULES-14) — the sweep's own tests.
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
            if not ok:
                return why
        if low in c.minors:
            mcap, _t = _rules.minor_daily_cap(c, low, u.date)
            if mcap and u.hours > mcap + 0.01:
                return _rules.LABELS["minor_hours"]
        if self.daily_line and not drafted and not self.salaried[p] and u.hours > self.daily_line + 0.01:
            return _rules.LABELS["daily_ot"]
        if u.hours + float(self.base_hours[p].get(u.bucket, 0.0)) > self.capf(p, u.bucket) + 0.05:
            return "past their overtime line" if u.hours + float(self.base_hours[p].get(u.bucket, 0.0)) \
                <= self.maxh[p] + 0.05 else _rules.LABELS["over_max_hours"]
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
                room = sum(max(0.0, self.capf(p, b) + 0.05 - self.fx_hours[p].get(b, 0.0)) for b in buckets)
                capacity += min(days, sum(int(max(0.0, self.capf(p, b) + 0.05 - self.fx_hours[p].get(b, 0.0)) // shortest)
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

    def _off_ok(self, p, ords, add=None) -> bool:
        """Whether p keeps a run of `off_req` days off inside the week with
        the dates in `ords` (and `add`) worked — the sweep's days_off rule,
        asked only once two or more of the week's dates are worked. Adding a
        date can only make it harder, so the search checks it forward."""
        req = self.off_req[p]
        if not req or not self.week_ords:
            return True
        worked = {o for o in ords if o in self.week_ord_set}
        if add is not None:
            worked.add(add)
        if len(worked) < 2:
            return True
        run = best = 0
        for o in self.week_ords:
            if o in worked:
                run = 0
            else:
                run += 1
                best = max(best, run)
        return best >= req

    def _fixed_blocks(self, u, p):
        for f in self.fx_units[p]:
            if self._conflict(u, f):
                return "already on another shift that day" if f.date == u.date else _rules.LABELS["rest_gap"]
        if u.hours + self.fx_hours[p].get(u.bucket, 0.0) > self.capf(p, u.bucket) + 0.05:
            return "past their overtime line"
        if self.max_run and self._run_with(self.fx_ords[p], u.dord) > self.max_run:
            return _rules.LABELS["long_run"]
        if not self._off_ok(p, self.fx_ords[p], u.dord):
            return _rules.LABELS["days_off"]
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

    # ── groups: each shift as the scorer sees it, and the day's rules ──────
    def _build_groups(self):
        s, c = self.signals, self.c
        unmeetable = sq._unmeetable_rules(self.rows, s)
        self.rules = [r for r in (s.get("leader_rules") or []) if id(r) not in unmeetable]
        self.crews = sq.strength_crews(self.profiles, s.get("typical_headcount") or {}, s.get("role_floors") or {},
                                       s.get("role_minimums") or {}, int(s.get("section_cap") or 0),
                                       set(s.get("cap_roles") or ()), self.families)
        self.runs = sq.role_runs(s.get("typical_headcount") or {}, s.get("role_floors") or {},
                                 s.get("role_minimums") or {}, self.profiles, self.families)
        self.ratings = sq.role_ratings(s.get("scores") or {}, s.get("roster_roles") or {}, self.rows, self.families)
        # Who can flex into which family, by what is on file (the scorer's
        # can_flex, SQ-10): a family nobody who works it can flex is not
        # judged.
        self.can_flex = set()
        for f in self.fams_of:
            if len(f) > 1:
                self.can_flex |= f
        owner_targets = {}
        for r, v in (s.get("cross_training_targets") or {}).items():
            try:
                fam = sq.role_family(r, self.families)
                owner_targets[fam] = max(float(owner_targets.get(fam, v)), float(v))
            except (TypeError, ValueError):
                continue
        self.cross_targets = owner_targets
        self.cross_default = float(s.get("cross_training_target") or 0.34)
        self.groups = {}          # key -> {"units": [...], "kind", ...}
        req_by_date = s.get("requirements_by_date") or {}
        typical = s.get("typical_headcount") or {}
        self.shift_info = {}
        for u in self.units:
            for (date, part) in u.parts:
                k = ("S", date, part)
                if k not in self.groups:
                    prof = self.profile(date, u.day, part)
                    is_closing = self.closes_on.get(date) == part
                    rules = [r for r in self.rules
                             if sq.leader_rule_applies(r, u.day, part, is_closing, self.runs, self.families)]
                    try:
                        reqs = sq.shift_role_requirements(req_by_date.get((date, part)) or typical.get((u.day, part)) or {},
                                                          sq._floors_for(s.get("role_floors") or {}, u.day, part),
                                                          s.get("role_minimums") or {}, prof.critical_positions)
                    except Exception:
                        reqs = {}
                    need = {}
                    for role, (n, _src) in (reqs or {}).items():
                        f = self.family(role)
                        need[f] = max(need.get(f, 0), int(n or 0))
                    # The memories about who is on this slot at all: somebody
                    # the managers keep putting on it, one of the people they
                    # keep putting on the role's slot.
                    on_slot = [m for m in self.mem if m["kind"] in ("moved_on", "leader_swap")
                               and m.get("day") == u.day and m.get("daypart") == part]
                    self.groups[k] = {"units": [], "date": date, "part": part, "day": u.day, "kind": "S",
                                      "prof": prof, "dw": sq.DEMAND_WEIGHT.get(prof.demand, 1.0), "rules": rules,
                                      "need": need, "learned": on_slot}
                self.groups[k]["units"].append(u.id)
            for part in u.kitchen:
                k = ("T", u.date, part)
                g = self.groups.setdefault(k, {"units": [], "date": u.date, "part": part, "day": u.day, "kind": "T",
                                               "dw": self.shift_dw.get((u.date, part), 1.0)})
                g["units"].append(u.id)
        # A team the memory keeps on the same shifts (pair): each date any of
        # them can work, over the units they could be on.
        if self.mem_pairs:
            member_p = {self.pidx[k] for ms, _w in self.mem_pairs for k in ms if k in self.pidx}
            for u in self.units:
                if u.draft in member_p or (self.dom[u.id] & member_p):
                    self.groups.setdefault(("L", u.date), {"units": [], "date": u.date, "kind": "L"})["units"].append(u.id)
        # Only groups with something the solver decides are kept: a constant
        # is no help to the search.
        self.groups = {k: g for k, g in self.groups.items() if not all(self.units[i].fixed for i in g["units"])}
        if self.stations:
            import kitchen_stations as _ks
            for k, g in list(self.groups.items()):
                if g["kind"] == "T" and not _ks.required(self.stations, g["day"], g["part"]):
                    del self.groups[k]
        self.kgroups, self.kok, self.kwhy = {}, {}, {}
        self._manager_groups()
        self._closer_groups()
        for k, g in self.groups.items():
            g["units"] = sorted(set(g["units"]))
            for i in g["units"]:
                self.units[i].groups.append(k)
        for k, members in self.kgroups.items():
            for i in members:
                self.units[i].kgroups.append(k)

    def _cover(self, key, members, ok, why, hard, cost_fn=None, date=None):
        """A rule about who is on a day: at least one of `members` (unit ids)
        worked by somebody in `ok`. Hard: the search holds it; else a group
        whose cost applies when nobody in `ok` is on."""
        live = [i for i in members if not self.units[i].fixed]
        if any(self.units[i].fixed and self.units[i].draft in ok for i in members):
            return                     # a kept row already holds it
        if not live:
            return
        if hard:
            if not any(self.dom[i] & ok for i in live):
                self.notes.append(f"{why} — nobody who could is free to work it, so it could not be held.")
                hard = False
            else:
                self.kgroups[key] = sorted(live)
                self.kok[key] = set(ok)
                self.kwhy[key] = why
                return
        if cost_fn is not None:
            self.groups[key] = {"units": sorted(live), "kind": "C", "ok": set(ok), "cost": cost_fn, "date": date,
                                "why": why}

    def _manager_groups(self):
        """A manager on the floor every minute anybody is (owner, 10/2/26 —
        the highest rule; schedule audit 10/3/26 PR-32): each stretch of a
        date between the units' starts and ends, with the units on for all
        of it. A stretch the draft had a manager on stays managed, and one
        only an unnamed row covered (it becomes somebody's) must be — both
        hard. A stretch the draft left unmanaged costs what the score
        charges a broken hard rule (the shifts it touches held at the cap)
        while it stays so. Read as schedule_rules.manager_gaps reads it:
        business minutes, a manager who may work that date, closed dates out,
        only the acting dates when nobody else manages."""
        c = self.c
        if c is None or not (c.managers or c.acting_managers):
            return
        only = None if c.managers else {d for ds in (c.acting_managers or {}).values() for d in (ds or ())}
        mgr_people = {}
        by_date = {}
        for u in self.units:
            if not u.date or u.date in (c.closed_dates or set()) or (only is not None and u.date not in only):
                continue
            for sp in u.bspans:
                by_date.setdefault(u.date, []).append((sp[0], sp[1], u.id))
        for d, items in by_date.items():
            ok = mgr_people.setdefault(d, {p for p in range(len(self.names)) if self.manages(p, d)})
            cuts = sorted({t for s0, e0, _u in items for t in (s0, e0)})
            hard, soft = {}, {}
            for a, b in zip(cuts, cuts[1:]):
                members = frozenset(uid for s0, e0, uid in items if s0 <= a and b <= e0)
                if not members:
                    continue
                staffed = any(self.units[i].draft is not None for i in members)
                managed = any(self.units[i].draft is not None and self.units[i].draft in ok for i in members)
                if managed or not staffed:
                    hard.setdefault(members, []).append((a, b))
                else:
                    soft.setdefault(members, []).append((a, b))
            # A stretch whose units include every unit of a smaller required
            # stretch is held by it already.
            kept = []
            for m in sorted(hard, key=len):
                if not any(k <= m for k in kept):
                    kept.append(m)
            for m in kept:
                a, b = hard[m][0]
                self._cover(("K", d, tuple(sorted(m))), m, ok,
                            f"a manager on {sq._day_name(d)} from {_rules._fmt_minutes(a % 1440)}", True)
            for m, spans in soft.items():
                minutes = sum(b - a for a, b in spans)
                parts = {("morning" if a < sq.DAYPART_CUTOVER else "night") for a, _b in spans} | \
                        {("night" if b > sq.DAYPART_CUTOVER else "morning") for _a, b in spans}
                dw = sum(self.shift_dw.get((d, p), 0.0) for p in parts) or 1.0
                cap_loss = (CAP_REF - sq.HARD_BREACH_CAP) * dw
                self._cover(("m", d, tuple(sorted(m))), m, ok, f"no manager on {sq._day_name(d)}", False,
                            cost_fn=lambda mins=minutes, loss=cap_loss: loss + K_MANAGER_MINUTE * mins, date=d)

    def _closer_groups(self):
        """Each role's closer (D-9): on every trading day, the last of a role
        with closers to leave is one of them, staying until close plus the
        role's minutes (schedule_rules._closer_breaches) — hard where the
        draft held it, and never further from it where it did not; a close
        the draft left without its closer costs the cap on the closing shift
        until a closer is on it."""
        c = self.c
        if c is None or not c.closers_by_role or not c.compliance.get("keyholder_until_close", True):
            return
        by = {}
        for u in self.units:
            if not u.date or u.date in (c.closed_dates or set()):
                continue
            for r, sp in zip(u.rows, (_rules._span(x) for x in u.rows)):
                if sp and not (u.draft is not None and c.training_row(r)):
                    by.setdefault((u.date, self.family(r.get("role"))), []).append((sp[1], u.id))
        for (d, fam), items in by.items():
            closers = (c.closers_by_role or {}).get(fam)
            if not closers or not _rules._closer_rule_runs(c, fam, d):
                continue
            # Who the sweep counts as the role's closer on (its own reading:
            # marked to close for the role); a domain only ever holds people
            # who may work the date.
            ok = {self.pidx[k] for k in closers if k in self.pidx}
            day = sq._day_name(d)
            e_last = max(e for e, _u in items)
            # The sweep's own target for the role (closer_need, RULES-6).
            need, _until = _rules.closer_need(c, fam, day, e_last)
            full = max(need - 15, e_last - 15)
            draft_best = max((e for e, uid in items if self.units[uid].draft in ok), default=None)
            members_full = [uid for e, uid in items if e >= full]
            dw = self.shift_dw.get((d, self.closes_on.get(d) or "night"), 1.0)
            if draft_best is not None and draft_best >= full:
                self._cover(("C", d, fam), members_full, ok, f"the {fam} closer on {day}", True)
                continue
            if draft_best is not None:
                # never further from the rule than the draft
                self._cover(("c", d, fam), [uid for e, uid in items if e >= draft_best], ok,
                            f"the {fam} closer on {day}", True)
            self._cover(("cc", d, fam), members_full, ok, f"the {fam} closer on {day}", False,
                        cost_fn=lambda dw=dw: (CAP_REF - sq.HARD_BREACH_CAP) * dw, date=d)

    def _rules_for(self, date, part, role):
        """The leader rules binding `role`'s family on one shift (the
        scorer's leader_rule_applies, with where each role works)."""
        g = self.groups.get(("S", date, part))
        rules = g["rules"] if g else [r for r in self.rules if sq.leader_rule_applies(
            r, sq._day_name(date), part, self.closes_on.get(date) == part, self.runs, self.families)]
        fam = self.family(role)
        return [r for r in rules if self.family(r.get("role")) == fam]

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
        for a, b in self.prefer_pairs:
            if a in first_of and b in first_of:
                union(first_of[a], first_of[b])
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
        low = self.key(self.names[p])
        if u.date in self.pending.get(low, ()):
            out["pending"] = K_PENDING
        for (d, part) in u.parts:
            dw = self.shift_dw.get((d, part), 1.0)
            n_here = max(1, len((self.groups.get(("S", d, part)) or {}).get("units") or ()) or 1)
            # Stability: a shift they do not usually work (dim_stability; not
            # on a date the owner flagged).
            pat = self.pattern[p]
            prof = self.profile(d, u.day, part)
            if pat is not None and prof.source != "what you told us about this date":
                days, parts = pat
                if (days and _low(u.day) not in days) or (parts and part not in parts):
                    out["stability"] = out.get("stability", 0.0) + self.dim_cost("stability", 100.0 / n_here, dw)
            # Reliability: a no-show risk alone in the role or on a busy shift
            # is the scorer's exposure; a lateness risk as the only one of the
            # role opening or closing the day (D-44).
            if self.rel_known[p]:
                exposed = self.no_show[p] >= sq.UNRELIABLE_RATE and (
                    u.hard or len([i for i in (self.groups.get(("S", d, part)) or {}).get("units") or ()
                                   if self.units[i].role_at.get(part) == u.role_at.get(part)]) <= 1)
                late = self.late_risk[p] and bool(u.edges)
                if exposed or late:
                    out["reliability"] = out.get("reliability", 0.0) + self.dim_cost("reliability", 100.0 / n_here, dw)
        # Fairness: the rotation — somebody due a weekend off, or resting
        # from closes (ROTATION_PENALTY on the shift's fairness).
        dw0 = u.dw
        if u.weekend and any(p in self.rot_weekend.get(r, ()) for r in u.roles):
            out["rotation_weekend"] = self.dim_cost("fairness", sq.ROTATION_PENALTY, dw0)
        if u.closing and any(p in self.rot_rest.get(r, ()) for r in u.roles):
            out["rotation_close"] = self.dim_cost("fairness", sq.ROTATION_PENALTY, dw0)
        # Preferences, a week-level measure: a daypart they said they do not
        # want, a slot they keep dropping (half weight), a slot somebody else
        # keeps picking up taken by somebody who does not (L-19, D-36).
        item = self.week_unit("preferences") / self.n_pref * 100.0
        if self.pref_parts[p] and any(x not in self.pref_parts[p] for x in u.primary if x != "unknown"):
            out["preference"] = item
        slot_hits = [tuple(sl) for sl in self.l_avoid[p] if sl[0] == u.day and sl[1] in u.primary]
        if slot_hits:
            out["learned_preference"] = item * self.l_weight[p]
        for part in u.primary:
            fans = self.preferred_by.get((u.day, part)) or set()
            if fans and p not in fans and (fans & self._dom_of(u)):
                out["learned_preference"] = out.get("learned_preference", 0.0) + item * min(
                    self.l_weight[q] for q in fans)
        if self.primary_role[p] and self.primary_role[p] not in u.roles:
            out["role"] = K_OFF_ROLE
        # Labor dollars (P-32): what their own rate costs over the draft's
        # person on the shift (a cheaper person earns nothing — the solver
        # finishes the draft's quality, it never trades it for dollars).
        if self.per_dollar:
            role = u.role_at.get(u.primary[0], "") if u.primary else ""
            ref = self.rate(u.draft, role) if u.draft is not None else 0.0
            dollars = u.hours * max(0.0, self.rate(p, role) - ref)
            if dollars:
                out["dollars"] = dollars * self.per_dollar
        # The scheduling memory (L-3, D-35), as schedule_memory.misses reads
        # it: somebody the managers keep taking off this slot (a row's slot is
        # its first daypart), or keep making another role on it; the role's
        # usual opener or closer working that day without opening or closing
        # it. The edit predictor (L-15).
        mem = 0.0
        for m in self.mem_person.get(low) or ():
            kind = m["kind"]
            if m.get("day") != u.day:
                continue
            if kind in ("moved_off", "role_change"):
                for r, first in zip(u.rows, u.primary):
                    if first != m.get("daypart"):
                        continue
                    if kind == "moved_off" or " ".join(_low(r.get("role")).split()) != \
                            " ".join(_low(_val(m).get("role")).split()):
                        mem += m["confidence"]
                        break
            elif kind in ("opener", "closer"):
                fam = self.family(m.get("role")) if m.get("role") else None
                if (fam is None or fam in u.fams) and not self.at_edge(u, kind, fam):
                    mem += m["confidence"]
        if mem:
            out["learned"] = mem * self.k_miss
        if self.likely:
            for r in u.rows:
                w = self.likely.get((low, r.get("date") or "", r.get("shift_start") or ""))
                if w:
                    out["likely_edit"] = out.get("likely_edit", 0.0) + self.k_likely * w * u.dw
        drafted = self.sym_draft[u.sym] if u.sym is not None else ({u.draft} if u.draft is not None else set())
        if p not in drafted:
            out["change"] = K_CHANGE
        return out

    def _dom_of(self, u):
        return self.dom[u.id] if getattr(self, "dom", None) and u.id < len(self.dom) else set()


    def static(self):
        if not hasattr(self, "_static"):
            self._static = [None] * len(self.units)
            for u in self.units:
                if not u.fixed:
                    self._static[u.id] = {p: sum(self.components_of(u, p).values()) for p in self.dom[u.id]}
        return self._static

    def group_penalty(self, key, assign) -> float:
        g = self.groups[key]
        if g["kind"] == "S":
            return sum(self.shift_parts(key, assign).values())
        if g["kind"] == "T":
            return self.station_cost(key, assign)
        if g["kind"] == "C":
            return 0.0 if any(assign[i] in g["ok"] for i in g["units"]) else g["cost"]()
        if g["kind"] == "L":
            return self.team_cost(key, assign)
        return 0.0

    def team_cost(self, key, assign) -> float:
        """A team the scheduling memory keeps on the same shifts, split across
        one date's dayparts (schedule_memory.misses' pair: two or more of them
        on that date, not all on each daypart any of them is on)."""
        g = self.groups[key]
        by_part = {}
        for i in g["units"]:
            u = self.units[i]
            p = assign[i] if not u.fixed else u.draft
            if p is None:
                continue
            k = self.key(self.names[p])
            for first in u.primary:
                by_part.setdefault(first, set()).add(k)
        cost = 0.0
        for members, w in self.mem_pairs:
            parts = [ks & members for ks in by_part.values() if ks & members]
            there = set().union(*parts) if parts else set()
            if len(there) > 1 and any(len(x) < len(there) for x in parts):
                cost += w
        return cost * self.k_miss

    def station_cost(self, key, assign) -> float:
        """Kitchen stations (dim_stations): every station this daypart needs
        held by a cook trained on it, matched as the station pass matches."""
        import kitchen_stations as _ks
        g = self.groups[key]
        need = _ks.required(self.stations, g["day"], g["part"])
        if not need:
            return 0.0
        cooks = []
        for i in self._all_units(key):
            p = assign[i] if not self.units[i].fixed else self.units[i].draft
            if p is not None and self.names[p] not in cooks:
                cooks.append(self.names[p])
        _placed, unmet = _ks.assign(cooks, need, self.stations)
        score = sq._pct(len(need) - len(unmet), len(need))
        prof = self.profile(g["date"], g["day"], g["part"])
        floor = prof.floor("stations") if hasattr(prof, "floor") and prof.floor("stations") is not None else sq.STATIONS_FLOOR
        return self.dim_cost("stations", 100 - score, g["dw"], floor, score)

    def _all_units(self, key):
        """Every unit on a group's shift, the kept ones too."""
        g = self.groups[key]
        if "_all" not in g:
            if g["kind"] == "S":
                g["_all"] = [u.id for u in self.units if (g["date"], g["part"]) in u.parts]
            elif g["kind"] == "T":
                g["_all"] = [u.id for u in self.units if u.date == g["date"] and g["part"] in u.kitchen]
            else:
                g["_all"] = list(g["units"])
        return g["_all"]

    def shift_parts(self, key, assign) -> dict:
        """The shift-level dimensions an assignment moves, mirrored from the
        scorer (shift_quality.dim_*) on the people this assignment puts on the
        shift, each as its cost: {dimension: cost}."""
        g = self.groups[key]
        date, part, prof, dw = g["date"], g["part"], g["prof"], g["dw"]
        people, by_fam, role_of = [], {}, {}
        for i in self._all_units(key):
            u = self.units[i]
            p = assign[i] if not u.fixed else u.draft
            if p is None or p in role_of:
                continue
            role = u.role_at.get(part) or (sorted(u.roles)[0] if u.roles else "")
            role_of[p] = (role, i)
            people.append(p)
            by_fam.setdefault(self.family(role), []).append(p)
        out = {}
        # Leadership (dim_leadership, SQ-1/2/13/16): each rule earns credit
        # for what it found, a bar or attribute never asks for more of the
        # role than the shift has; the profile's "somebody able to run it"
        # counts a manager; the dimension is the weakest check.
        rules = g["rules"]
        credits = []
        answerable = []
        for rule in rules:
            if not self.rated_any and rule.get("min_score") is not None and not rule.get("attribute"):
                continue
            answerable.append(rule)
        for rule in answerable:
            try:
                need = max(1, int(rule.get("count") or 1))
            except (TypeError, ValueError):
                need = 1
            pool = by_fam.get(self.family(rule.get("role"))) or []
            if rule.get("attribute"):
                q = [p for p in pool if self.leader[p]]
            elif rule.get("min_score") is None:
                q = list(pool)
            else:
                q = [p for p in pool if (self._sc(p, {_low(rule.get("role"))}) or 0) >= float(rule["min_score"])]
            if (rule.get("attribute") or rule.get("min_score") is not None) and pool:
                need = min(need, len(pool))
            credits.append(min(len(q), need) / float(need))
        can_identify = self.rated_any or any(self.leader) or bool(self.c is not None and (self.c.managers or self.c.acting_managers))
        if prof.requires_leader and can_identify:
            ok = any(self.manages(p, date) for p in people)
            if not ok:
                pool = people
                if prof.leader_roles:
                    wanted = {self.family(r) for r in prof.leader_roles}
                    pool = [p for f, ps in by_fam.items() if f in wanted for p in ps]
                ok = any(self.leader[p] or (self._sc(p, {role_of[p][0]}) or 0) >= float(prof.leader_min_score)
                         for p in pool)
            credits.append(1.0 if ok else 0.0)
        if credits:
            lead = int(round(sq.LEADER_MISS_SCORE + (100 - sq.LEADER_MISS_SCORE) * min(credits)))
            floor = prof.floor("leadership") if answerable else None
            out["leadership"] = self.dim_cost("leadership", 100 - lead, dw, floor, lead)
        # Strength (dim_operational_strength, SQ-3/4/5): the rated people's
        # average in each family against target ÷ crew; unrated people are
        # unknown, not zero; the weakest role sets it.
        if self.rated_any:
            ratios = []
            for role, target in (prof.min_strength or {}).items():
                if not target or sq.job_code_daypart(role) not in (None, part):
                    continue
                fam = self.family(role)
                ps = by_fam.get(fam) or []
                rated = [self._sc(p, {role_of[p][0]}) for p in ps]
                rated = [float(x) for x in rated if x is not None]
                if not rated:
                    continue
                hit = self.crews.get((fam, float(target)))
                crew = int(hit[0]) if hit and int(hit[0] or 0) > 0 else max(1, int(math.ceil(float(target) / 5 - 1e-9)))
                bar = max(1.0, min(5.0, float(target) / crew))
                ratios.append(min(1.0, (sum(rated) / len(rated)) / bar))
            if ratios:
                st = int(round(min(ratios) * 100))
                out["operational_strength"] = self.dim_cost("operational_strength", 100 - st, dw,
                                                            prof.floor("operational_strength"), st)
        # Demand match (dim_demand_match, SQ-12): the rated team's average
        # against this restaurant's own level for a shift this busy.
        every = list(self.ratings.get("*") or [])
        if self.rated_any and len(every) >= sq.DEMAND_MIN_RATED:
            offset = sq.DEMAND_BAR_OFFSET.get(prof.demand or "normal", 0.0)
            have_s = want_s = 0.0
            n = 0
            for fam, ps in by_fam.items():
                here = [float(x) for x in (self._sc(p, {role_of[p][0]}) for p in ps) if x is not None]
                if not here:
                    continue
                own = sorted((float(v) for v in (self.ratings.get(fam) or [])), reverse=True)
                pool = own if len(own) >= sq.DEMAND_MIN_RATED else sorted(every, reverse=True)
                mean = sum(pool) / len(pool)
                best = (own or pool)[:len(here)]
                want = max(1.0, min(mean + offset, sum(best) / len(best)))
                have_s += sum(here)
                want_s += want * len(here)
                n += len(here)
            if n:
                avg, wanted = have_s / n, want_s / n
                dm = 100 if avg >= wanted - 1e-9 else max(0, int(round(100 - (wanted - avg) * sq.DEMAND_POINT_STEP)))
                out["demand_match"] = self.dim_cost("demand_match", 100 - dm, dw)
        # Training balance (dim_training_balance, SQ-9): somebody rated 2 or
        # under with a mentor 2 points stronger in the family, else on the
        # floor who works it, else — at a one-person station — the manager.
        if self.rated_any:
            checked = mentored = 0
            for fam, ps in by_fam.items():
                sc = {p: self._sc(p, {role_of[p][0]}) for p in ps}
                weak = [p for p in ps if sc[p] is not None and float(sc[p]) <= 2]
                if not weak:
                    continue
                one = int(g["need"].get(fam, len(ps)) or 0) <= 1
                checked += len(weak)
                for w_ in weak:
                    bar = float(sc[w_]) + 2
                    if any(q != w_ and sc[q] is not None and float(sc[q]) >= bar for q in ps):
                        mentored += 1
                    elif any(q not in ps and fam in self.fams_of[q] and (self._sc(q, {fam}) or 0) >= bar
                             for q in people):
                        mentored += 1
                    elif one and any(q != w_ and self.manages(q, date) for q in people):
                        mentored += 1
            if checked:
                out["training_balance"] = self.dim_cost("training_balance", 100 - sq._pct(mentored, checked), dw)
        # Experience (dim_experience_balance, D-6): veterans' share against
        # the profile's mix, managers and salaried people experienced.
        if self.experience_on:
            known = [p for p in people if self.exp_known[p]]
            if known:
                want = float(prof.experience_mix or 0)
                have = sum(1 for p in known if self.veteran[p]) / float(len(known))
                ex = 100 if want <= 0 else sq._pct(have, want)
                out["experience_balance"] = self.dim_cost("experience_balance", 100 - ex, dw)
        # Cross-training (dim_cross_training, SQ-10): by family, a family
        # nobody who works it can flex not judged.
        if self.cross_on and people:
            tot = tw = 0.0
            for fam, ps in by_fam.items():
                target = sq.cross_training_target_for(fam, self.cross_targets, self.cross_default)
                if target <= 0 or fam not in self.can_flex:
                    continue
                flex = sum(1 for p in ps if len(self.fams_of[p] | {fam}) > 1)
                tot += min(100, int(round(flex / float(len(ps)) / target * 100))) * len(ps)
                tw += len(ps)
            if tw:
                out["cross_training"] = self.dim_cost("cross_training", 100 - int(round(tot / tw)), dw)
        # Pairings (dim_pairings, L-20): a kept-apart pair on together, a
        # preferred pair split.
        if self.avoid_pairs or self.prefer_pairs:
            on = set(people)
            clashes = sum(1 for a, b in self.avoid_pairs if a in on and b in on)
            splits = sum(1 for a, b in self.prefer_pairs if (a in on) != (b in on))
            matches = sum(1 for a, b in self.prefer_pairs if a in on and b in on)
            if clashes or splits or matches:
                pr = max(0, 100 - 45 * clashes - sq.PAIR_SPLIT_COST * splits)
                out["pairings"] = self.dim_cost("pairings", 100 - pr, dw)
        # The scheduling memory on this slot (schedule_memory.misses): somebody
        # the managers keep putting on it, one of the people they keep putting
        # on the role's slot — read by who has a row whose slot (first
        # daypart) this is.
        if g.get("learned"):
            first_on = {}
            for i in self._all_units(key):
                uu = self.units[i]
                p = assign[i] if not uu.fixed else uu.draft
                if p is None:
                    continue
                for r, first in zip(uu.rows, uu.primary):
                    if first == part:
                        first_on.setdefault(p, set()).add(self.family(r.get("role")))
            if first_on:
                keys = {p: self.key(self.names[p]) for p in first_on}
                cost = 0.0
                for m in g["learned"]:
                    if m["kind"] == "moved_on":
                        if self.key(m.get("person")) not in keys.values():
                            cost += m["confidence"]
                    else:
                        fam = self.family(m.get("role")) if m.get("role") else None
                        mine = [p for p, fams in first_on.items() if fam is None or fam in fams]
                        names = {self.key(n) for n in (_val(m).get("names") or [])}
                        if mine and not any(keys[p] in names for p in mine):
                            cost += m["confidence"]
                if cost:
                    out["learned"] = cost * self.k_miss
        return out

    def at_edge(self, u, kind, fam) -> bool:
        """Whether unit u opens (starts within OPENER_TIE_MINUTES of the first
        of `fam` in that date) or closes (ends as near the last) its role
        family's day — schedule_memory's own reading of an opener and a
        closer, over every row of the family that date, kept ones too."""
        if not hasattr(self, "_edges_mem"):
            import schedule_memory as _smem
            tie = _smem.OPENER_TIE_MINUTES
            by = {}
            for v in self.units:
                for r, sp in zip(v.rows, (_rules._span(x) for x in v.rows)):
                    if sp:
                        by.setdefault((v.date, self.family(r.get("role"))), []).append((sp[0], sp[1], v.id))
            edges = {}
            for (d, f), items in by.items():
                first, last = min(t[0] for t in items), max(t[1] for t in items)
                edges[(d, f, "opener")] = {uid for s0, _e, uid in items if s0 - first <= tie}
                edges[(d, f, "closer")] = {uid for _s, e0, uid in items if last - e0 <= tie}
            self._edges_mem = edges
        fams = [fam] if fam else sorted(u.fams)
        return any(u.id in self._edges_mem.get((u.date, f, kind), ()) for f in fams)

    def dynamic(self, u, p, st) -> float:
        """What p on u adds given what p already carries this week. Never
        negative, so the static lower bound stays admissible."""
        wn = self.wn
        c = 0.0
        # Fairness: spreading the closes, weekends and busy shifts.
        if u.closing and st.closes[p]:
            c += K_CLOSE * st.closes[p] * wn.get("fairness", 1)
        if u.weekend and st.weekends[p]:
            c += K_WEEKEND * st.weekends[p] * wn.get("fairness", 1)
        if u.hard and st.hards[p]:
            c += K_HARD * st.hards[p] * wn.get("fairness", 1)
        # Fatigue (week-level): this unit making them strained — past the
        # busy-shift ceiling, or past what their last weeks allow (SQ-27).
        if not self._strained(p, st.hards[p], st.total[p]) and \
                self._strained(p, st.hards[p] + (1 if u.hard else 0), st.total[p] + u.hours):
            c += self.week_unit("fatigue") / self.n_tracked * 100.0
        # Preferences: past the hours they asked for (+20%), once.
        want = self.desired[p]
        if want:
            band = want * (1 + sq.PREFERENCE_HOURS_BAND)
            if st.total[p] <= band + 1e-9 < st.total[p] + u.hours:
                c += self.week_unit("preferences") / self.n_pref * 100.0
        # Overtime premium (only ever less than the draft's: the caps hold
        # anybody's hours under their line past what the draft gave them).
        if not self.salaried[p]:
            line = self.ot_threshold
            have = st.hours[p].get(u.bucket, 0.0)
            ot = max(0.0, have + u.hours - line) - max(0.0, have - line)
            if ot > 0:
                rate = self.person_rates.get(" ".join(self.names[p].lower().split())) or self.blended or 15.0
                premium = ot * rate * sq.OVERTIME_PREMIUM
                pay = self.week_dollars or 1.0
                c += premium * (self.per_dollar + self.week_unit("overtime") * sq.OVERTIME_POINTS * 100.0 / pay)
        # Somebody who habitually runs past their shift (ot_risk, L-16):
        # drafted into the headroom under the line, the memory weighs more the
        # further in (schedule_memory.misses — this week's rows only).
        if p in self.headroom and self.k_miss:
            head, w = self.headroom[p]
            have = st.hours[p].get(u.bucket, 0.0) - float(self.base_hours[p].get(u.bucket, 0.0) or 0.0)
            start = self.ot_threshold - head

            def into(h):
                return min(1.0, max(0.0, h - start) / head)
            c += w * (into(have + u.hours) - into(have)) * self.k_miss
        return c

    def _strained(self, p, hards, total) -> bool:
        if hards > sq.HARD_SHIFT_CEILING:
            return True
        k = self.sustained_busy_at.get(p)
        if k is not None and hards >= k:
            return True
        h = self.sustained_hours_at.get(p)
        return h is not None and total >= h

    def leaf_penalty(self, persons, st) -> float:
        """Soft rules that are only known once a person's week is complete."""
        pen = 0.0
        full_req, part_req = self.days_off_rule
        for p in persons:
            mn = self.min_hours[p]
            if mn:
                # Under the minimum the owner set: the measure's points for
                # each hour short — and never further under it than the draft
                # (after the minimum-hours pass) had them (P-4).
                short = max(0.0, mn - st.total[p])
                if short > 0.05 and self.min_owed:
                    pen += self.week_unit("min_hours") * 100.0 * short / self.min_owed
                kept = min(float(mn), sum(self.draft_hours[p].values()) - sum(self.base_hours[p].values()))
                if st.total[p] + 0.05 < kept:
                    pen += K_MIN_HOURS_NEW * (kept - st.total[p])
            want = self.desired[p]
            if want and st.total[p] < want * (1 - sq.PREFERENCE_HOURS_BAND) - 1e-9 and st.total[p] > 0:
                pen += self.week_unit("preferences") / self.n_pref * 100.0
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
                        pen += K_DAYS_OFF if p in self.draft_days_off else K_DAYS_OFF_NEW
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
            if any(h > self.capf(p, b) + 0.05 for b, h in hours.items()):
                return False
            if self.max_run and any(self._run_with(ords - {x}, x) > self.max_run for x in ords):
                return False
            if not self._off_ok(p, ords):
                return False
        for k, members in self.kgroups.items():
            if not any(assign[i] in self.kok[k] for i in members):
                return False
        return True


def _num(v) -> bool:
    try:
        return float(v) == float(v) and float(v) > 0
    except (TypeError, ValueError):
        return False


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
        # The day's rules each assignment can satisfy (a manager on a
        # stretch, a role's closer): how many members are worked by
        # somebody who satisfies it so far.
        self.kh = {k: 0 for k in prob.kgroups}
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
        for k in unit.kgroups:
            if p in prob.kok[k]:
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
                touched.update(prob.units[v].kgroups)
            if ok and later and len(self.dom[later[0]]) < len(later):
                ok = False
                self.fails[later[0]] = self.fails.get(later[0], 0) + 1
        # hours ceiling and the run of consecutive days for p's other units
        if ok:
            ords = self.st.ords[p]
            for v in self.p_units.get(p, ()):
                if self.assign[v] is not None or p not in self.dom[v]:
                    continue
                vu = prob.units[v]
                bad = vu.hours + self.st.hours[p].get(vu.bucket, 0.0) > prob.capf(p, vu.bucket) + 0.05
                if not bad and prob.max_run and vu.dord is not None and abs(vu.dord - unit.dord) <= prob.max_run:
                    bad = prob._run_with(ords, vu.dord) > prob.max_run
                if not bad and prob.off_req[p] and vu.dord not in ords:
                    bad = not prob._off_ok(p, ords, vu.dord)
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
        # a manager on every stretch that must have one, each role's closer:
        # still possible
        if ok:
            for k in touched:
                if self.kh.get(k, 1):
                    continue
                ok_people = prob.kok[k]
                if not any(self.assign[i] is None and (self.dom[i] & ok_people) for i in prob.kgroups[k]):
                    ok = False
                    for i in prob.kgroups[k]:
                        self.fails[i] = self.fails.get(i, 0) + 1
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
        for k in unit.kgroups:
            if p in prob.kok[k]:
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
            for k in self.prob.units[i].kgroups:
                if p in self.prob.kok[k]:
                    self.kh[k] += 1


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
                                 "(hours, rest, days in a row, time off and the manager and closer rules together).")
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
                      "(hours, rest, days in a row, time off and the manager and closer rules together).")
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

def _flagged(rows, constraints, viols=None):
    """(person, date, start) of the rows the sweep says will not stand —
    never counted as somebody on the floor (the scorer's `flagged`)."""
    if constraints is None:
        return set()
    viols = _rules.violations(rows, constraints) if viols is None else viols
    return {(_low(v.get("employee")), v.get("date") or "", v.get("shift_start") or "")
            for v in viols if v.get("no_show")}


def _objective(quality) -> float:
    """The week score before rounding, week-level measures included — the
    repair loop's own objective, so the two can never judge a week apart."""
    import schedule_optimizer as _opt
    return _opt.objective(quality)


def _soft_repaired(rows, constraints, viols=None) -> set:
    """(person, kind) for the soft breaches the fix pass repairs
    (schedule_rules.FIXABLE_SOFT, e.g. a missed run of days off): the solver
    may not bring one back that the draft no longer has."""
    if constraints is None:
        return set()
    viols = _rules.violations(rows, constraints) if viols is None else viols
    return {(_low(v.get("employee")), v["kind"]) for v in viols
            if not v.get("hard") and v["kind"] in _rules.FIXABLE_SOFT}


def improve(rows, inputs=None, signals=None, weights=None, constraints=None, only_dates=None,
            max_seconds=DEFAULT_SECONDS, min_gain=MIN_GAIN, hours_budget=None) -> dict:
    """Solve the draft's assignment and keep the answer only when it is
    worth more to the owner: Shift Quality higher (each candidate scored
    with the rows that will not stand and the hard breaches of ITS rows;
    what it breaks of the scheduling memory is the score's learned-patterns
    measure), less the labor dollars it adds and the rows it keeps that the
    manager is expected to change — schedule_optimizer.week_value, the
    repair loop's own value — and only when it makes nothing at or
    above the budget tier new or worse, compared by breach identity
    (schedule_rules.regressions: a person's legality, the manager every
    minute, the floors and the closer, overtime, minimum hours — schedule
    audit 10/3/26 E-1, P-14), brings back no soft breach a pass repaired,
    and puts nobody newly past their overtime line (for somebody who
    habitually runs past their shift, the line less that headroom — L-16)
    nor the week further over its hourly hours budget (`hours_budget`, else
    inputs["hours_budget"]) — each candidate held to what the repair loop
    holds the stage to (schedule_engine._judge), so a good answer is never
    lost to a bad one.

    Returns {applied, rows, before_score, after_score, quality, changes,
    stats, reason}. The input rows are never modified.
    """
    import schedule_optimizer as _opt
    t0 = _time.monotonic()
    inputs = inputs or {}
    signals = dict(signals or {})
    constraints = constraints if constraints is not None else inputs.get("constraints")
    profiles = inputs.get("shift_profiles") or None
    base = [dict(r) for r in rows or []]
    sweep = _rules.IncrementalSweep(constraints) if constraints is not None else None

    def judged(rs):
        """(violations, profile, the scorer's signals) for `rs` (P-28)."""
        if sweep is None:
            return None, None, signals
        viols = sweep.violations(rs)
        sig = dict(signals)
        sig["flagged"] = _flagged(rs, constraints, viols)
        sig["hard_breaches"] = [v for v in viols if v.get("hard")]
        return viols, _rules.breach_profile(rs, constraints, viols=viols), sig

    def score(rs, sig):
        return sq.score_rows(rs, profiles=profiles, weights=weights, **sig)

    before_viols, before_prof, before_sig = judged(base)
    before_q = score(base, before_sig)
    out = {"applied": False, "rows": base, "before_score": before_q.get("score"), "after_score": before_q.get("score"),
           "quality": before_q, "changes": [], "stats": {}, "reason": ""}
    if not before_q.get("checked"):
        out["reason"] = "nothing to score"
        return out
    if constraints is None:
        out["reason"] = "no rule set"
        return out
    pricing = _opt.pricing_inputs(signals, inputs, constraints)
    likely = [f for f in (signals.get("likely_edits") or []) if isinstance(f, dict)]
    headroom = _opt.ot_headroom(signals, constraints)
    base_dollars = _opt.labor_dollars(base, pricing) if pricing else 0.0
    # The hourly hours budget, judged as the repair loop judges it (schedule_
    # engine._judge: hourly hours past the budget and the trim's tolerance):
    # handing a salaried person's shift to somebody hourly spends from it, and
    # a week further over it is refused — candidate by candidate, so one that
    # goes over never costs the loop the solver's other answers.
    try:
        budget = float(hours_budget if hours_budget is not None else (inputs.get("hours_budget") or 0) or 0)
    except (TypeError, ValueError):
        budget = 0.0

    def over_budget(rs) -> float:
        if budget <= 0:
            return 0.0
        import schedule_economics as _econ
        return max(0.0, _rules.hourly_hours(rs, constraints) - budget * (1 + _econ.TRIM_TOLERANCE))
    base_over = over_budget(base)

    def value(q, rs):
        return _opt.week_value(q, rs, pricing, base_dollars, likely)
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
    before_soft = _soft_repaired(base, constraints, before_viols)
    base_val = value(before_q, base)
    best = None
    judged_n, refused = 0, 0
    for cand in solved.get("candidates") or []:
        if cand == base:
            continue
        viols, prof, sig = judged(cand)
        if _rules.regressions(before_prof, prof, upto=_rules.TIER_BUDGET, hard_only=False):
            refused += 1
            continue            # never: a better score is not worth a breach
        if _soft_repaired(cand, constraints, viols) - before_soft:
            refused += 1
            continue            # nor undoing a repair a pass made
        if _opt.overtime_created(base, cand, constraints, headroom=headroom):
            refused += 1
            continue            # nor an hour of overtime the draft did not have
        if over_budget(cand) > base_over + 0.01:
            refused += 1
            continue            # nor a week further over its hours budget
        q = score(cand, sig)
        judged_n += 1
        if not q.get("checked"):
            continue
        val = value(q, cand)
        if best is None or val > best[0]:
            best = (val, cand, q)
    stats["judged"] = judged_n
    stats["refused"] = refused
    stats["seconds_total"] = round(_time.monotonic() - t0, 2)
    if best is None or best[0] < base_val + min_gain:
        out["reason"] = ("the draft's own assignment was already the best the solver found"
                         if best is None or best[0] <= base_val else "the solver's week scored no better")
        stats["kept"] = "draft"
        stats["after_score"] = best[2].get("score") if best else before_q.get("score")
        return out
    val, cand, q = best
    stats["kept"] = "solver"
    rows_out = [dict(r) for r in cand]
    out.update(applied=True, rows=rows_out, after_score=q.get("score"), quality=q,
               changes=_describe(prob, base, rows_out, before_q, q, val - base_val))
    if pricing:
        stats["dollars_before"] = round(base_dollars, 0)
        stats["dollars_after"] = round(_opt.labor_dollars(rows_out, pricing), 0)
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
        if prob.key(old) != prob.key(new):
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
    head = (f"Cavnar AI re-solved who works each shift ({len(changed)} change{'s' if len(changed) != 1 else ''}), "
            f"Shift Quality {before_q.get('score')} → {after_q.get('score')}"
            + (f": {', '.join(ups)}" if ups else "") + ".")
    out = [{"kind": "solve", "reason": head, "gain": round(gain, 1)}]
    st = _State(prob)
    after_assign = [prob.pidx.get(prob.key(after_rows[u.idx[0]].get("employee"))) for u in prob.units]
    for u in prob.units:
        if after_assign[u.id] is not None:
            st.add(u, after_assign[u.id])
    for u, old, new in changed[:MAX_LISTED_CHANGES]:
        r = after_rows[u.idx[0]]
        why = _why(prob, u, prob.pidx.get(prob.key(old)), prob.pidx.get(prob.key(new)), st)
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
    "reliability": lambda P, u, o, n: (f"{P.names[o]} is often late and would have been the only one "
                                       f"{'opening' if 'opens' in u.edges else 'closing'}"
                                       if P.late_risk[o] and u.edges else
                                       f"{P.names[o]} has missed {int(round(P.no_show[o] * 100))}% of scheduled shifts"),
    "stability": lambda P, u, o, n: f"{P.names[n]} usually works {u.day}s",
    "preference": lambda P, u, o, n: f"{P.names[o]} asked for {'nights' if 'night' in P.pref_parts[o] else 'days'}",
    "learned_preference": lambda P, u, o, n: f"{P.names[o]} keeps dropping that shift",
    "rotation_weekend": lambda P, u, o, n: f"{P.names[o]} is due a weekend off by the rotation",
    "rotation_close": lambda P, u, o, n: f"{P.names[o]} has closed more than their share and is due a rest from it",
    "role": lambda P, u, o, n: f"it is {P.names[n]}'s own role",
    "dollars": lambda P, u, o, n: "the same shift at a lower wage",
    "learned": lambda P, u, o, n: "it is what the manager keeps doing on that shift",
    "likely_edit": lambda P, u, o, n: f"the manager usually changes {P.names[o]} on that shift",
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
    if u.kgroups:
        return "so a manager, or the role's closer, is on when the day needs one"
    if u.groups:
        return "so the shift's leadership, strength, training and experience add up"
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
            "dollars_before": stats.get("dollars_before"), "dollars_after": stats.get("dollars_after"),
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
                                                "after_score", "infeasible", "notes", "dollars_before", "dollars_after")}
    if not solver.get("applied"):
        return opt
    changes = list(solver.get("changes") or []) + list(opt.get("changes") or [])
    before = solver.get("before_score")
    after = opt.get("after_score") if opt.get("ran") and opt.get("after_score") is not None else solver.get("after_score")
    opt.update(ran=True, applied=True, changes=changes, before_score=before, after_score=after,
               improvement=(after or 0) - (before or 0) if before is not None and after is not None else 0)
    n = len([c for c in changes if c.get("kind") != "solve"])
    opt["verdict"] = (f"Cavnar AI made {n} change{'s' if n != 1 else ''} to the draft, raising Shift Quality "
                      f"from {before} to {after}. Each is listed with why.")
    opt.setdefault("unresolved", [])
    return opt
