"""
schedule_objective.py — one objective for a schedule week, in dollars
(scheduler re-audit 10/11/26, cross-workstream contract #1; LOGIC-1,
LOGIC-3, LOGIC-24, STRESS-1, LOGIC-5, STRESS-15, LOGIC-10).

The solver's two stages (schedule_shapes: which shifts; schedule_cpsat: who
works them) price a week from here, and the optimizer, the quality gate and
Shift Quality's "cost" read the same function (P, wave 2), so a week is
judged by ONE currency, not five objectives that disagree:

  slot_value(ctx, date, minute, role)
        what one person short for that half hour costs: the stated value of a
        person-hour short at an average half hour (SHORT_PERSON_HOUR_USD)
        x that half hour's demand weight (its share of the week's demand:
        a busy Friday dinner far above a quiet Monday 3pm) x the role's
        criticality (ROLE_CRITICALITY: a dishwasher or line cook at the rush
        is not as droppable as a second host; the owner's own weights win,
        then what the restaurant's history shows — a role it never runs
        without is critical). The budget is priced against it: a dollar past
        the hourly budget costs BUDGET_OVER_FACTOR dollars on top of the wage,
        so where the budget binds the LOWEST-value half hours are cut first.
  price_week(rows, ctx)
        {"total", "parts": {labor, overtime, unfilled, budget_over,
        stability, shape, fairness}, "by_slot": {...}, "rates": {...}} —
        every figure in dollars at the rates stated in `rates`, never summed
        with a measured or a projected value (CLAUDE.md "Value delivered").

Hard rules — the law, the manager on the floor, locked rows, confirmed typed
owner rules, the floors — are CONSTRAINTS in the solver, never prices here:
nothing in this module lets money buy a breach of one.

Every rate is stated inline below (the owner can be shown each). Pure: no
database, no model, no clock; module-scope imports are stdlib and L0 only.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime

import schedule_time as _stime

SLOT = 30

# ── the stated rates (dollars) ───────────────────────────────────────────
# One person-hour short at the week's average half hour, for a role of
# criticality 1.0: the sales and service lost (slower tables, a longer
# ticket time, a walk-out) — about two-thirds of an average restaurant's
# sales per labor hour. A half hour is half of it.
SHORT_PERSON_HOUR_USD = 45.0
# A dollar past the hourly labor budget costs this many dollars on top of
# the wage itself (schedule_cpsat's BUDGET_OVER_FACTOR — one number).
BUDGET_OVER_FACTOR = 3.0
# One more shift: a handover, a clock-in, a trip in for somebody.
SHIFT_OVERHEAD_USD = 12.0
# A day in beyond somebody's usual days a week (their history, else
# DEFAULT_DAYS_A_WEEK): 6 x 4h is not a part-timer's 4 x 6h.
EXTRA_DAY_USD = 20.0
DEFAULT_DAYS_A_WEEK = 5
# Past the role's usual shift length (the restaurant's own median, else
# DEFAULT_SHIFT_HOURS + 2): this x the hours past it, squared.
LONG_SHIFT_USD = 6.0
DEFAULT_SHIFT_HOURS = 6.0
# Each hour a shift falls short of the role's usual length (the same
# median): a 3h fragment is a trip in for little pay.
SHORT_SHIFT_USD = 5.0
# A lunch-to-close double (a shift on the floor for both core windows)
# where the restaurant's own history works almost none (DOUBLES_USUAL_SHARE).
DOUBLE_USD = 60.0
DOUBLES_USUAL_SHARE = 0.10
# A person on a weekday and daypart they usually work (credit), and on a
# weekday they almost never work (cost) — their own punches and published
# weeks, USUAL_MIN_WEEKS of them at least.
USUAL_SLOT_USD = 12.0
OFF_USUAL_DAY_USD = 20.0
USUAL_MIN_WEEKS = 3
USUAL_SHARE = 0.5          # worked on at least this share of the weeks: usual
RARE_SHARE = 0.15          # at most this share: a day they almost never work
# Each hour under somebody's usual week past USUAL_HOURS_BAND of it.
USUAL_HOURS_USD = 3.0
USUAL_HOURS_BAND = 0.2
# Each hour under STARVED_SHARE of somebody's usual week, for a person who
# usually works STARVED_MIN_USUAL_HOURS or more (blind re-audit 10/11/26, P):
# a person given under half the week they live on is the one who leaves —
# replacing an hourly employee costs far more than any one week's hours. At
# STARVED_HOURS_USD on top of (1 + BUDGET_OVER_FACTOR) x their own rate an
# hour, no budget saving ever pays for it: a cut is spread across the crew
# or the week stays over its budget, said — a 40-hour cook is never taken
# to 12 to fit a budget (the schedule benchmark's 24-hour diner). Never
# enough to buy a breach: the rules are constraints, not prices.
STARVED_SHARE = 0.5
STARVED_MIN_USUAL_HOURS = 15.0
STARVED_HOURS_USD = 15.0
# One shift kept as last published week had it (the same person on the same
# weekday, role family, daypart and start within 30 minutes).
STABILITY_USD = 15.0
STABILITY_START_MINUTES = 30
# A shift shape the restaurant's own history works on that weekday.
HISTORY_PATTERN_USD = 6.0
# Each close or weekend shift a person carries past an even share.
FAIRNESS_USD = 4.0
# Overtime pay over the line (shift_quality.OVERTIME_PREMIUM).
OVERTIME_PREMIUM = 0.5
# The one exchange rate between Shift Quality's week points and these
# dollars (blind re-audit 10/11/26 LOGIC-6): this many points for each 1% of
# the draft's labor dollars — CP-SAT's quality tier, the optimizer's moves
# (charged for dollars added AND credited for dollars saved, LOGIC-23) and
# the quality gate's judge all convert at it.
POINTS_PER_PCT_OF_LABOR = 0.5
# A half hour of demand nobody measured (prep before the doors, the close
# after them): this demand weight.
UNMEASURED_DEMAND_WEIGHT = 0.6
DEMAND_WEIGHT_BOUNDS = (0.15, 5.0)

# How hard a role is to run without, by family words (a family matches the
# first entry whose word it contains). Owner weights (restaurants.
# role_criticality_json) and the restaurant's own history move these.
ROLE_CRITICALITY = (
    ("manager", 2.0), ("chef", 1.5), ("cook", 1.5), ("kitchen", 1.5), ("line", 1.5), ("grill", 1.5),
    ("fry", 1.5), ("saute", 1.5), ("prep", 1.2), ("dish", 1.4), ("porter", 1.2), ("steward", 1.2),
    ("barista", 1.2), ("bar", 1.2), ("server", 1.1), ("waiter", 1.1), ("waitress", 1.1), ("cashier", 1.1),
    ("counter", 1.1), ("expo", 1.0), ("host", 0.8), ("greeter", 0.8), ("bus", 0.8), ("runner", 0.8),
)
CRITICALITY_BOUNDS = (0.3, 3.0)
# "Never without": a role on at a half hour on this share of its same
# weekdays — its first person there is the requirement's FIRM part (LOGIC-3),
# above the budget; the rest of the demand is priced (one definition: Stage 1
# and every pass after it).
FIRM_PRESENCE = 0.8
# How far the restaurant's own record moves a role's default (0: not at all).
LEARNED_CRITICALITY_SHARE = 0.5
# Typical lead before the doors and stay after the close by family words,
# where the restaurant's punches say nothing (LOGIC-11): cooks in an hour
# before open, servers half an hour, the dish pit and the closers 45
# minutes after the close.
DEFAULT_LEAD_MINUTES = (("cook", 60), ("chef", 60), ("kitchen", 60), ("line", 60), ("prep", 90),
                        ("barista", 30), ("server", 30), ("bar", 30))
DEFAULT_STAY_MINUTES = (("dish", 45), ("porter", 45), ("steward", 45))
CLOSER_STAY_MINUTES = 45


def _low(s) -> str:
    return " ".join(str(s or "").strip().lower().split())


def _family(role, families=None) -> str:
    from shift_quality import role_family
    return role_family(role, families)


def _weekday(d) -> str:
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%A")
    except (TypeError, ValueError):
        return ""


def _by_words(table, fam, default=None):
    f = _low(fam)
    for word, v in table:
        if word in f:
            return v
    return default


def default_criticality(role) -> float:
    """The stated weight of a role family (ROLE_CRITICALITY), 1.0 when its
    words match none."""
    return float(_by_words(ROLE_CRITICALITY, _family(role), 1.0))


def default_lead(role) -> int:
    """Minutes a role is typically in before the doors (DEFAULT_LEAD_MINUTES)."""
    return int(_by_words(DEFAULT_LEAD_MINUTES, _family(role), 0) or 0)


def default_stay(role, closer=False) -> int:
    """Minutes a role typically stays after the close: the dish pit, and
    any role's closer (CLOSER_STAY_MINUTES)."""
    v = int(_by_words(DEFAULT_STAY_MINUTES, _family(role), 0) or 0)
    return max(v, CLOSER_STAY_MINUTES if closer else 0)


SHOULDER_MIN_DAYS = 3       # days of a family's punches before its stay or lead is its own
STAY_BOUNDS = (0, 120)
LEAD_BOUNDS = (0, 180)


def shoulder_habits(c, rows) -> tuple:
    """({family: stay minutes}, {family: lead minutes}) — how long each role
    family's last person stays past the restaurant's close, and how early
    its first person comes in before the open, as the median over the days
    its punches (`rows`) show it (at least SHOULDER_MIN_DAYS), else the
    stated default (DEFAULT_STAY_MINUTES / CLOSER_STAY_MINUTES for a family
    with closers, DEFAULT_LEAD_MINUTES) for every family the roster works
    (LOGIC-11: with no setting a role was hard-capped at the close and the
    kitchen opened cold). A family the owner set a stay or a close
    allowance for is the owner's, never these."""
    import schedule_time as _st
    if c is None:
        return {}, {}
    fam = c.family
    last, first = defaultdict(dict), defaultdict(dict)
    for r in rows or []:
        d = str(r.get("date") or "")[:10]
        sp = _span(r)
        f = fam(r.get("role"))
        if not d or not sp or not f:
            continue
        last[f][d] = max(last[f].get(d, sp[1]), sp[1])
        first[f][d] = min(first[f].get(d, sp[0]), sp[0])
    stays, leads = {}, {}
    for f, days in last.items():
        vals = []
        for d, e in days.items():
            wd = _weekday(d)
            o = _st.parse_minutes((getattr(c, "open_times", None) or {}).get(wd) or "")
            cl = _st.close_on_timeline(_st.parse_minutes((getattr(c, "close_times", None) or {}).get(wd) or ""), o)
            if cl is not None and e >= cl - 30:
                vals.append(e - cl)
        if len(vals) >= SHOULDER_MIN_DAYS:
            vals.sort()
            stays[f] = max(STAY_BOUNDS[0], min(STAY_BOUNDS[1], int(vals[len(vals) // 2])))
    for f, days in first.items():
        vals = []
        for d, s in days.items():
            wd = _weekday(d)
            o = _st.parse_minutes((getattr(c, "open_times", None) or {}).get(wd) or "")
            if o is not None and s <= o + 30:
                vals.append(o - s)
        if len(vals) >= SHOULDER_MIN_DAYS:
            vals.sort()
            leads[f] = max(LEAD_BOUNDS[0], min(LEAD_BOUNDS[1], int(vals[len(vals) // 2])))
    roles = set()
    for rs in (getattr(c, "known_roles", None) or {}).values():
        roles |= {str(x) for x in rs or () if str(x).strip()}
    roles |= {str(x) for x in (getattr(c, "role_names", None) or {}).values() if str(x).strip()}
    closers = getattr(c, "closers_by_role", None) or {}
    for role in sorted(roles):
        f = fam(role)
        if not f:
            continue
        if f not in stays:
            v = default_stay(role, closer=bool(closers.get(f)))
            if v:
                stays[f] = max(stays.get(f, 0), v)
        if f not in leads:
            v = default_lead(role)
            if v:
                leads[f] = max(leads.get(f, 0), v)
    return stays, leads



# ── what the history says ─────────────────────────────────────────────────

def _span(row):
    sp = _stime.span(row)
    if not sp or sp[1] <= sp[0]:
        return None
    return sp


def history_profile(rows, key=None, family=None, open_times=None, close_times=None) -> dict:
    """What the restaurant's own shifts (punches and published weeks) say,
    as the objective reads it:
      people   {person key: {"weeks", "days": {weekday: share of weeks},
                "parts": {(weekday, daypart): share}, "hours" (median week),
                "days_per_week" (median), "doubles" (share of days worked as
                a double)}}
      families {family: {"median_minutes", "doubles_share", "present":
                {(weekday, daypart): share of the weeks the family was on},
                "lead" (median minutes in before the open, measured),
                "stay" (median minutes the last of the role stays past the
                close, measured)}}
    `key` names a person (Constraints.key), `family` a role."""
    key = key or _low
    fam_of = family or _family
    by_person_week = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    weeks_seen = set()
    fam_len = defaultdict(list)
    fam_days = defaultdict(lambda: defaultdict(list))       # fam -> date -> spans
    seen = set()
    for r in rows or []:
        name = (r.get("employee") or "").strip()
        sp = _span(r)
        d = str(r.get("date") or "")[:10]
        role = (r.get("role") or "").strip()
        if not sp or not d or not role:
            continue
        # one shift once: the same published week can arrive as history and
        # as last week, and read twice it looked like a double
        ident = (key(name) if name else "", d, sp, _low(role))
        if name and ident in seen:
            continue
        seen.add(ident)
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            continue
        wk = dt.isocalendar()[:2]
        weeks_seen.add(wk)
        fam = fam_of(role)
        fam_len[fam].append(sp[1] - sp[0])
        fam_days[fam][d].append(sp)
        if name:
            by_person_week[key(name)][wk][d].append((sp, fam))
    n_weeks = max(1, len(weeks_seen))
    people = {}
    for k, weeks in by_person_week.items():
        days, parts, hours, dpw, doubles, worked_days = Counter(), Counter(), [], [], 0, 0
        for wk, ds in weeks.items():
            wd_seen, part_seen = set(), set()
            h = 0.0
            for d, items in ds.items():
                wd = _weekday(d)
                wd_seen.add(wd)
                spans = sorted(sp for sp, _f in items)
                h += sum(e - s for s, e in spans) / 60.0
                pres = set()
                for s, e in spans:
                    for p in _stime.span_dayparts((s, e)):
                        pres.add(p)
                for p in pres:
                    part_seen.add((wd, p))
                worked_days += 1
                if len(spans) > 1 or {"morning", "night"} <= pres:
                    doubles += 1
            for wd in wd_seen:
                days[wd] += 1
            for x in part_seen:
                parts[x] += 1
            hours.append(h)
            dpw.append(len(wd_seen))
        w = max(1, len(weeks))
        hours.sort()
        dpw.sort()
        people[k] = {"weeks": len(weeks), "days": {d: n / float(n_weeks) for d, n in days.items()},
                     "parts": {x: n / float(n_weeks) for x, n in parts.items()},
                     "hours": hours[len(hours) // 2] if hours else 0.0,
                     "days_per_week": dpw[len(dpw) // 2] if dpw else 0,
                     "doubles": doubles / float(max(1, worked_days)), "seen_weeks": w, "history_weeks": n_weeks}
    families = {}
    for fam, lens in fam_len.items():
        lens.sort()
        dbl = 0
        tot = 0
        present = Counter()
        leads, stays = [], []
        for d, spans in fam_days[fam].items():
            wd = _weekday(d)
            for s, e in spans:
                tot += 1
                if {"morning", "night"} <= set(_stime.span_dayparts((s, e))):
                    dbl += 1
            parts_here = {p for s, e in spans for p in _stime.span_dayparts((s, e))}
            for p in parts_here:
                present[(wd, p)] += 1
            o = _stime.parse_minutes((open_times or {}).get(wd) or "")
            cl = _stime.close_on_timeline(_stime.parse_minutes((close_times or {}).get(wd) or ""), o)
            if o is not None:
                leads.append(o - min(s for s, _e in spans))
            if cl is not None:
                stays.append(max(e for _s, e in spans) - cl)
        leads.sort()
        stays.sort()
        families[fam] = {"median_minutes": lens[len(lens) // 2], "doubles_share": dbl / float(max(1, tot)),
                         "present": {x: n / float(n_weeks) for x, n in present.items()},
                         "lead": leads[len(leads) // 2] if len(leads) >= 3 else None,
                         "stay": stays[len(stays) // 2] if len(stays) >= 3 else None,
                         "days": len(fam_days[fam])}
    return {"people": people, "families": families, "weeks": len(weeks_seen)}


# ── the context ───────────────────────────────────────────────────────────

class ObjectiveContext:
    """Everything a week is priced against. Build with `build`; read with
    slot_value / price_week and the helpers below."""

    def __init__(self):
        self.restaurant_id = None
        self.week_dates = []
        self.budget = None
        self.last_week_rows = []
        self.profile = {"people": {}, "families": {}, "weeks": 0}
        self.dw = {}                       # (date, minute) -> demand weight
        self.need = {}                     # (date, role lower) -> {minute: people}
        self.crit_override = {}            # family -> weight (the owner's)
        self.crit_cache = {}
        self.rates = {}
        self.person_rates = {}
        self.blended = 15.0
        self.ot_line = 40.0
        self.salaried = set()
        self.families = None
        self.key = _low
        self.value_per_hour = SHORT_PERSON_HOUR_USD
        self.presence = {}                 # (family, weekday, minute) -> share of those weekdays on

    # ── building ──
    @classmethod
    def build(cls, restaurant_id, week_dates, *, budget=None, last_week_rows=None, usual=None,
              requirements=None, demand_curve=None, history_rows=None, criticality=None, rates=None,
              person_rates=None, blended_rate=None, ot_line=None, salaried=None, families=None,
              constraints=None, value_per_hour=None, key=None, need=None):
        """The week's pricing context. `requirements` (schedule_requirements
        rows, their `half_need` and `factor`) give the need and — with
        `demand_curve` ({weekday: {hour: share}}) when there is one — the
        demand weights; `history_rows` the usual patterns (or `usual`, a
        history_profile already read); `criticality` the owner's role weights
        ({role or family: weight}); `constraints` fills what is not passed
        (families, key, salaried, the owner's weights)."""
        x = cls()
        c = constraints
        x.restaurant_id = restaurant_id
        x.week_dates = sorted(week_dates or [])
        try:
            x.budget = float(budget) if budget else None
        except (TypeError, ValueError):
            x.budget = None
        x.last_week_rows = list(last_week_rows or [])
        x.families = families if families is not None else (getattr(c, "role_families", None) if c is not None else None)
        x.key = key or (c.key if c is not None and hasattr(c, "key") else _low)
        x.salaried = set(salaried if salaried is not None else (getattr(c, "salaried", None) or ()))
        x.rates = {_low(k): float(v) for k, v in (rates or {}).items() if k and k != "_default" and _num(v)}
        x.person_rates = {_low(k): float(v) for k, v in (person_rates or {}).items() if _num(v)}
        if _num(blended_rate) and float(blended_rate) > 0:
            x.blended = float(blended_rate)
        elif x.rates:
            x.blended = sum(x.rates.values()) / len(x.rates)
        x.ot_line = float(ot_line) if _num(ot_line) and float(ot_line) > 0 else 40.0
        x.value_per_hour = float(value_per_hour) if _num(value_per_hour) and float(value_per_hour) > 0 \
            else SHORT_PERSON_HOUR_USD
        owner = dict(criticality or {})
        if not owner and c is not None:
            owner = dict(getattr(c, "role_criticality", None) or {})
        for k, v in owner.items():
            if _num(v):
                x.crit_override[x.fam(k)] = float(v)
        open_times = getattr(c, "open_times", None) if c is not None else None
        close_times = getattr(c, "close_times", None) if c is not None else None
        x.profile = usual if usual is not None else history_profile(history_rows, key=x.key, family=x.fam,
                                                                    open_times=open_times,
                                                                    close_times=close_times)
        x.presence = presence(history_rows, family=x.fam)
        x.need = dict(need or {})
        if not x.need:
            for row in requirements or []:
                for m, rn in (row.get("half_need") or {}).items():
                    for role, n in (rn or {}).items():
                        cur = x.need.setdefault((row.get("date"), _low(role)), {})
                        cur[int(m)] = max(cur.get(int(m), 0), int(n or 0))
        x.dw = _demand_weights(x.week_dates, requirements, demand_curve, x.need)
        return x

    def fam(self, role) -> str:
        return _family(role, self.families)

    # ── reading ──
    def demand_weight(self, date, minute) -> float:
        """The half hour's share of the week's demand (1.0 = an average
        half hour); UNMEASURED_DEMAND_WEIGHT where nothing measured it."""
        t = int(minute) - int(minute) % SLOT
        v = self.dw.get((date, t))
        if v is None:
            return UNMEASURED_DEMAND_WEIGHT
        return v

    def criticality(self, role) -> float:
        """The owner's weight for the role's family, else the stated default
        moved by the restaurant's record (how often the family is on at
        all where it ever is — a role it never runs without is critical)."""
        fam = self.fam(role)
        if fam in self.crit_cache:
            return self.crit_cache[fam]
        if fam in self.crit_override:
            v = self.crit_override[fam]
        else:
            v = float(_by_words(ROLE_CRITICALITY, fam, 1.0))
            fp = (self.profile.get("families") or {}).get(fam) or {}
            pres = [s for s in (fp.get("present") or {}).values() if s > 0]
            if len(pres) >= 4 and (self.profile.get("weeks") or 0) >= USUAL_MIN_WEEKS:
                learned = 0.6 + 0.9 * (sum(min(1.0, s) for s in pres) / len(pres))
                v = (1 - LEARNED_CRITICALITY_SHARE) * v + LEARNED_CRITICALITY_SHARE * learned
        v = max(CRITICALITY_BOUNDS[0], min(CRITICALITY_BOUNDS[1], v))
        self.crit_cache[fam] = v
        return v

    def firm(self, date, minute, role) -> bool:
        """Whether the role's first person at this half hour is the FIRM part
        of the requirement (LOGIC-3): the restaurant is never without the
        role then — on in FIRM_PRESENCE of its same weekdays. The firm part
        ranks above the budget (the requirement tier); the rest of the
        demand is priced against it (slot_value)."""
        t = int(minute) - int(minute) % SLOT
        return self.presence.get((self.fam(role), _weekday(date), t), 0.0) >= FIRM_PRESENCE

    def firm_part(self, date, part, role) -> bool:
        """firm() for a daypart's whole crew: the role is never off that
        weekday's daypart (the family's measured presence)."""
        fp = (self.profile.get("families") or {}).get(self.fam(role)) or {}
        return float((fp.get("present") or {}).get((_weekday(date), part)) or 0.0) >= FIRM_PRESENCE

    def rate(self, person=None, role=None) -> float:
        if person and self.key(person) in self.salaried:
            return 0.0
        if person:
            v = self.person_rates.get(_low(person))
            if v:
                return v
        r = _low(role)
        return self.rates.get(r) or self.rates.get(self.fam(role)) or self.blended

    def usual_minutes(self, role) -> int:
        """The role family's usual shift length (its history's median), else
        DEFAULT_SHIFT_HOURS."""
        fp = (self.profile.get("families") or {}).get(self.fam(role)) or {}
        m = fp.get("median_minutes")
        return int(m) if m else int(DEFAULT_SHIFT_HOURS * 60)

    def doubles_usual(self, role) -> bool:
        fp = (self.profile.get("families") or {}).get(self.fam(role)) or {}
        return float(fp.get("doubles_share") or 0) >= DOUBLES_USUAL_SHARE

    def length_usd(self, role, start, end) -> float:
        """The shape cost of one shift (STRESS-1, LOGIC-5): its overhead,
        past the role's usual length squared, a double where the history
        works none."""
        mins = max(0, int(end) - int(start))
        fp = (self.profile.get("families") or {}).get(self.fam(role)) or {}
        top = int(fp["median_minutes"]) if fp.get("median_minutes") else int(DEFAULT_SHIFT_HOURS * 60) + 120
        over = max(0.0, (mins - top) / 60.0)
        short = max(0.0, (min(top, int(DEFAULT_SHIFT_HOURS * 60)) - mins) / 60.0)
        cost = SHIFT_OVERHEAD_USD + LONG_SHIFT_USD * over * over + SHORT_SHIFT_USD * short
        if {"morning", "night"} <= set(_stime.span_dayparts((int(start), int(end)))) and not self.doubles_usual(role):
            cost += DOUBLE_USD
        return cost

    def person_usual(self, person) -> dict:
        return (self.profile.get("people") or {}).get(self.key(person)) or {}

    def usual_slot_usd(self, person, date, parts) -> float:
        """Negative: a credit for a weekday and daypart they usually work;
        positive: a weekday they almost never work (STRESS-15). 0 without
        USUAL_MIN_WEEKS of their history."""
        pu = self.person_usual(person)
        if (pu.get("seen_weeks") or 0) < USUAL_MIN_WEEKS:
            return 0.0
        wd = _weekday(date)
        day_share = float((pu.get("days") or {}).get(wd) or 0.0)
        if day_share <= RARE_SHARE:
            return OFF_USUAL_DAY_USD
        best = max((float((pu.get("parts") or {}).get((wd, p)) or 0.0) for p in parts or ()), default=0.0)
        return -USUAL_SLOT_USD if best >= USUAL_SHARE else 0.0

    def days_target(self, person) -> int:
        pu = self.person_usual(person)
        if (pu.get("seen_weeks") or 0) >= USUAL_MIN_WEEKS and pu.get("days_per_week"):
            return int(pu["days_per_week"])
        return DEFAULT_DAYS_A_WEEK

    def usual_hours(self, person) -> float:
        pu = self.person_usual(person)
        return float(pu.get("hours") or 0.0) if (pu.get("seen_weeks") or 0) >= USUAL_MIN_WEEKS else 0.0

    def doubles_ok(self, person, constraints=None) -> bool:
        """Whether a person works doubles: the owner's word (Constraints.
        doubles_ok, {key: bool}) first, else their own history (a double on
        at least a quarter of the days they worked)."""
        k = self.key(person)
        if constraints is not None and k in (getattr(constraints, "minors", None) or ()):
            return False                  # a minor's cap is the whole day's: one shift a date
        said = (getattr(constraints, "doubles_ok", None) or {}) if constraints is not None else {}
        if k in said:
            return bool(said[k])
        return float(self.person_usual(person).get("doubles") or 0.0) >= 0.25


def points_per_dollar(week_labor_usd) -> float:
    """Shift Quality week points one objective dollar is worth on a week
    whose draft costs `week_labor_usd` in labor (POINTS_PER_PCT_OF_LABOR for
    each 1% of it); 0 with no labor dollars to scale by."""
    try:
        base = float(week_labor_usd or 0)
    except (TypeError, ValueError):
        return 0.0
    return POINTS_PER_PCT_OF_LABOR * 100.0 / base if base > 0 else 0.0


def starved_hour_usd(ctx, person) -> float:
    """What one hour under STARVED_SHARE of `person`'s usual week costs:
    STARVED_HOURS_USD on top of (1 + BUDGET_OVER_FACTOR) x their own rate —
    more than any budget saving that hour could buy."""
    k = ctx.key(person)
    if k in ctx.salaried:
        rate = 0.0
    else:
        rate = max([float(ctx.person_rates.get(_low(person)) or 0.0), float(ctx.blended or 0.0)]
                   + [float(v) for v in (ctx.rates or {}).values()])
    return STARVED_HOURS_USD + (1.0 + BUDGET_OVER_FACTOR) * rate


def presence(rows, family=None) -> dict:
    """{(family, weekday, minute): share of that weekday's history dates the
    family had somebody on at that half hour} — where the restaurant is
    never without a role (the firm part of a requirement, LOGIC-3). One
    reading for Stage 1 (schedule_shapes) and every pass after it. A shift
    past midnight counts on its own date's timeline."""
    fam_of = family or _family
    on = defaultdict(set)
    dates_by_day = defaultdict(set)
    for r in rows or []:
        sp = _span(r)
        d = str(r.get("date") or "")[:10]
        if not sp or not d or not (r.get("employee") or "").strip():
            continue
        day = _weekday(d)
        if not day:
            continue
        dates_by_day[day].add(d)
        f = fam_of(r.get("role"))
        for t in range(sp[0] - sp[0] % SLOT, sp[1], SLOT):
            if sp[0] <= t and t + SLOT <= sp[1]:
                on[(f, day, t)].add(d)
    return {k: len(v) / float(len(dates_by_day[k[1]]) or 1) for k, v in on.items()}


def _num(v) -> bool:
    try:
        float(v)
        return not isinstance(v, bool) and v is not None
    except (TypeError, ValueError):
        return False


def _demand_weights(dates, requirements, demand_curve, need) -> dict:
    """{(date, minute): weight} with the week's average half hour 1.0: the
    measured sales curve x the date's demand factor where there is a curve,
    else the people the requirement needs on at once (a busy half hour needs
    more of them), else the daypart's required count spread over it."""
    raw = {}
    by_part = {}
    for row in requirements or []:
        if row.get("daypart") in ("morning", "night"):
            by_part[(row.get("date"), row["daypart"])] = row
    split = _stime.daypart_split()
    for d in dates or []:
        wd = _weekday(d)
        curve = (demand_curve or {}).get(wd) or {}
        shares = {}
        if curve:
            import staffing_curve as _curve
            shares = _curve.half_hour_shares(curve)
        if shares:
            for m, v in shares.items():
                part = "morning" if m < split else "night"
                row = by_part.get((d, part)) or {}
                try:
                    f = float(row.get("factor") or 1.0)
                except (TypeError, ValueError):
                    f = 1.0
                if v > 0:
                    raw[(d, int(m))] = float(v) * f
            continue
        tot = defaultdict(float)
        for (dd, _role), curve_n in (need or {}).items():
            if dd != d:
                continue
            for m, n in curve_n.items():
                tot[int(m)] += float(n or 0)
        if tot:
            for m, v in tot.items():
                if v > 0:
                    raw[(d, m)] = v
            continue
        for part in ("morning", "night"):
            row = by_part.get((d, part))
            if not row:
                continue
            n = sum(int(e.get("required") or 0) for e in row.get("roles") or [])
            if n <= 0:
                continue
            lo, hi = _stime.CORE_WINDOWS[part]
            for m in range(lo - lo % SLOT, hi, SLOT):
                raw[(d, m)] = float(n)
    if not raw:
        return {}
    mean = sum(raw.values()) / len(raw)
    lo, hi = DEMAND_WEIGHT_BOUNDS
    return {k: max(lo, min(hi, v / mean)) for k, v in raw.items()}


def slot_value(ctx, date, slot_min, role) -> float:
    """Dollars one person short of `role` for the half hour at `slot_min`
    on `date` costs (contract #1): SHORT_PERSON_HOUR_USD / 2 x the half
    hour's demand weight x the role's criticality."""
    return ctx.value_per_hour / 2.0 * ctx.demand_weight(date, slot_min) * ctx.criticality(role)


# ── pricing a whole week ──────────────────────────────────────────────────

def price_week(rows, ctx) -> dict:
    """The week priced in dollars, part by part (contract #1):
      labor        hours x each person's rate (salaried: their pay is not
                   moved by a shift);
      overtime     the premium past each person's overtime line;
      unfilled     each person-half-hour short of the need x slot_value;
      budget_over  dollars past the hourly budget x BUDGET_OVER_FACTOR;
      stability    STABILITY_USD for each shift not kept from last
                   published week (where there is one);
      shape        each shift's overhead and length, a day past somebody's
                   usual days, a weekday they never work less one they do,
                   hours under their usual week — and, past STARVED_SHARE of
                   it, STARVED_HOURS_USD an hour (blind re-audit 10/11/26, P);
      fairness     closes and weekend shifts past an even share.
    {"total", "parts", "by_slot": {(date, minute): {"need", "on",
    "short_usd"}}, "rates": the stated rates used}. One implementation
    (WeekPricer), so a pass pricing one change at a time reads exactly the
    figure a whole week priced at once reads."""
    p = WeekPricer(rows, ctx)
    parts = p.parts()
    return {"total": round(sum(parts.values()), 2), "parts": parts, "by_slot": p.by_slot(),
            "rates": stated_rates(ctx)}


class WeekPricer:
    """price_week, kept up to date one change at a time (blind re-audit
    10/11/26 LOGIC-6, ARCH-2): the trim, the top-up and the optimizer judge
    each candidate move by the ONE objective without pricing the whole week
    again — a change re-prices the person, the date and the week-level sums
    it touches. `try_change(old, new)` is the total with row `old` replaced
    by `new` (None: removed; `old` None: `new` added), the week untouched;
    `apply(old, new)` makes it so. Rows are known by identity (the object
    the caller holds)."""

    def __init__(self, rows, ctx):
        self.ctx = ctx
        self.rows = {}                      # id(row) -> row
        self.info = {}                      # id(row) -> its facts
        self.labor = 0.0
        self.hours_by = defaultdict(float)
        self.days_by = defaultdict(Counter)     # key -> date -> shifts
        self.weekends = defaultdict(int)
        self.row_shape = 0.0
        self.kept = 0
        self.named = 0
        self.on = defaultdict(lambda: defaultdict(int))       # (date, family) -> minute -> people
        self.ends = defaultdict(Counter)    # date -> end minute -> rows
        self.need_by_date = defaultdict(list)
        for (d, role), curve in (ctx.need or {}).items():
            self.need_by_date[d].append((role, ctx.fam(role), curve))
        self._last = None
        if ctx.last_week_rows:
            self._last = defaultdict(list)
            for r in ctx.last_week_rows:
                sp = _span(r)
                name = (r.get("employee") or "").strip()
                if sp and name:
                    self._last[(ctx.key(name), _weekday(r.get("date")), ctx.fam(r.get("role")))].append(sp[0])
        self._unf = {}                      # date -> unfilled dollars
        for r in rows or []:
            self._add(r)
        for d in set(self.need_by_date) | {i["d"] for i in self.info.values()}:
            self._unf[d] = self._unfilled(d)

    # ── one row ──
    def _facts(self, r):
        sp = _span(r)
        d = r.get("date")
        if not sp or not d:
            return None
        ctx = self.ctx
        name = (r.get("employee") or "").strip()
        f = {"sp": sp, "d": d, "fam": ctx.fam(r.get("role")), "name": name, "k": ctx.key(name) if name else None}
        if name:
            h = (sp[1] - sp[0]) / 60.0
            f["h"] = h
            f["labor"] = h * ctx.rate(name, r.get("role"))
            f["shape"] = ctx.length_usd(r.get("role"), sp[0], sp[1]) + \
                ctx.usual_slot_usd(name, d, _stime.span_dayparts(sp))
            f["weekend"] = _weekday(d) in ("Friday", "Saturday", "Sunday")
            f["kept"] = self._is_kept(f, r) if self._last is not None else False
        return f

    def _is_kept(self, f, r) -> bool:
        starts = self._last.get((f["k"], _weekday(f["d"]), f["fam"])) or ()
        return any(abs(s - f["sp"][0]) <= STABILITY_START_MINUTES for s in starts)

    def _add(self, r):
        f = self._facts(r)
        self.rows[id(r)] = r
        self.info[id(r)] = f
        if f is None:
            return
        self._count(f, +1)

    def _count(self, f, sign):
        sp, d = f["sp"], f["d"]
        cur = self.on[(d, f["fam"])]
        for t in range(sp[0] - sp[0] % SLOT, sp[1], SLOT):
            if sp[0] <= t and t + SLOT <= sp[1]:
                cur[t] += sign
        self.ends[d][sp[1]] += sign
        if not f["name"]:
            return
        k = f["k"]
        self.labor += sign * f["labor"]
        self.hours_by[k] += sign * f["h"]
        self.days_by[k][d] += sign
        if self.days_by[k][d] <= 0:
            del self.days_by[k][d]
        self.row_shape += sign * f["shape"]
        if f["weekend"]:
            self.weekends[k] += sign
        self.named += sign
        if f["kept"]:
            self.kept += sign

    # ── the parts ──
    def _overtime_of(self, k, h) -> float:
        ctx = self.ctx
        if k in ctx.salaried:
            return 0.0
        over = h - ctx.ot_line
        if over <= 0:
            return 0.0
        return over * (ctx.person_rates.get(k) or ctx.blended) * OVERTIME_PREMIUM

    def _person_shape(self, k, h, days) -> float:
        ctx = self.ctx
        out = 0.0
        extra = days - ctx.days_target(k)
        if extra > 0:
            out += extra * EXTRA_DAY_USD
        uh = ctx.usual_hours(k)
        if uh:
            under = uh * (1 - USUAL_HOURS_BAND) - h
            if under > 0:
                out += under * USUAL_HOURS_USD
            if uh >= STARVED_MIN_USUAL_HOURS:
                starved = uh * STARVED_SHARE - h
                if starved > 0:
                    out += starved * starved_hour_usd(ctx, k)
        return out

    def _unfilled(self, d) -> float:
        ctx = self.ctx
        usd = 0.0
        for role, fam, curve in self.need_by_date.get(d) or ():
            have_at = self.on.get((d, fam)) or {}
            for m, n in curve.items():
                short = int(n) - have_at.get(int(m), 0)
                if short > 0:
                    usd += short * slot_value(ctx, d, int(m), role)
        return usd

    def _closes(self) -> dict:
        closes = defaultdict(int)
        for i, f in self.info.items():
            if not f or not f["name"]:
                continue
            ends = self.ends.get(f["d"])
            last = max((e for e, n in ends.items() if n > 0), default=None) if ends else None
            if last is not None and f["sp"][1] >= last - 15:
                closes[f["k"]] += 1
        return closes

    def parts(self, *, labor=None, hours_by=None, days_by=None, row_shape=None, unf=None, named=None, kept=None,
              closes=None, weekends=None) -> dict:
        ctx = self.ctx
        labor = self.labor if labor is None else labor
        hours_by = self.hours_by if hours_by is None else hours_by
        days_by = self.days_by if days_by is None else days_by
        overtime = sum(self._overtime_of(k, h) for k, h in hours_by.items() if h > 1e-9)
        shape = (self.row_shape if row_shape is None else row_shape) + sum(
            self._person_shape(k, h, len(days_by.get(k) or ())) for k, h in hours_by.items() if h > 1e-9)
        unfilled = sum((self._unf if unf is None else unf).values())
        budget_over = 0.0
        if ctx.budget:
            over = labor + overtime - ctx.budget
            if over > 0:
                budget_over = over * BUDGET_OVER_FACTOR
        stability = 0.0
        if ctx.last_week_rows:
            stability = max(0, (self.named if named is None else named) - (self.kept if kept is None else kept)) \
                * STABILITY_USD
        fairness = 0.0
        for counts in ((self._closes() if closes is None else closes), (self.weekends if weekends is None
                                                                        else weekends)):
            vals = [n for n in counts.values() if n > 0]
            if vals:
                even = sum(vals) / float(len(vals))
                fairness += sum(max(0.0, n - math.ceil(even)) for n in vals) * FAIRNESS_USD
        parts = {"labor": labor, "overtime": overtime, "unfilled": unfilled, "budget_over": budget_over,
                 "stability": stability, "shape": shape, "fairness": fairness}
        return {k: round(v, 2) for k, v in parts.items()}

    def total(self) -> float:
        return round(sum(self.parts().values()), 2)

    def by_slot(self) -> dict:
        ctx = self.ctx
        out = {}
        for d, needs in self.need_by_date.items():
            for role, fam, curve in needs:
                have_at = self.on.get((d, fam)) or {}
                for m, n in curve.items():
                    have = have_at.get(int(m), 0)
                    short = max(0, int(n) - have)
                    cell = out.setdefault((d, int(m)), {"need": 0, "on": 0, "short_usd": 0.0})
                    cell["need"] += int(n)
                    cell["on"] += min(have, int(n))
                    if short:
                        cell["short_usd"] += short * slot_value(ctx, d, int(m), role)
        return out

    # ── one change ──
    def try_change(self, old=None, new=None) -> float:
        """The week's total with `old` (a row in the week, or None) replaced
        by `new` (a row, or None) — the week as it stands is unchanged."""
        self.apply(old, new)
        try:
            return self.total()
        finally:
            self._undo()

    def apply(self, old=None, new=None) -> None:
        """Replace `old` with `new` in the priced week (either may be None)."""
        dates = set()
        self._undo_log = []
        if old is not None and id(old) in self.rows:
            f = self.info.pop(id(old))
            self.rows.pop(id(old))
            self._undo_log.append(("removed", old, f))
            if f is not None:
                self._count(f, -1)
                dates.add(f["d"])
        if new is not None:
            self._add(new)
            self._undo_log.append(("added", new, None))
            f = self.info.get(id(new))
            if f is not None:
                dates.add(f["d"])
        prev = {d: self._unf.get(d) for d in dates}
        self._undo_log.append(("unf", prev, None))
        for d in dates:
            self._unf[d] = self._unfilled(d)

    def _undo(self) -> None:
        for kind, a, b in reversed(getattr(self, "_undo_log", [])):
            if kind == "unf":
                for d, v in a.items():
                    if v is None:
                        self._unf.pop(d, None)
                    else:
                        self._unf[d] = v
            elif kind == "added":
                f = self.info.pop(id(a), None)
                self.rows.pop(id(a), None)
                if f is not None:
                    self._count(f, -1)
            elif kind == "removed":
                self.rows[id(a)] = a
                self.info[id(a)] = b
                if b is not None:
                    self._count(b, +1)
        self._undo_log = []

    def commit(self) -> None:
        """Keep the last apply()."""
        self._undo_log = []


def _kept_from_last(rows, ctx) -> int:
    p = WeekPricer([], ctx)
    if p._last is None:
        return 0
    kept = 0
    for r in rows or []:
        f = p._facts(r)
        if f and f["name"] and f["kept"]:
            kept += 1
    return kept


def stated_rates(ctx=None) -> dict:
    """Every rate the objective prices with, as stated (the owner can be
    shown each)."""
    return {"short_person_hour_usd": (ctx.value_per_hour if ctx is not None else SHORT_PERSON_HOUR_USD),
            "budget_over_factor": BUDGET_OVER_FACTOR, "shift_overhead_usd": SHIFT_OVERHEAD_USD,
            "extra_day_usd": EXTRA_DAY_USD, "short_shift_usd_per_hour": SHORT_SHIFT_USD, "long_shift_usd_per_hour_sq": LONG_SHIFT_USD, "double_usd": DOUBLE_USD,
            "usual_slot_usd": USUAL_SLOT_USD, "off_usual_day_usd": OFF_USUAL_DAY_USD,
            "usual_hours_usd": USUAL_HOURS_USD, "starved_hours_usd": STARVED_HOURS_USD,
            "starved_share": STARVED_SHARE, "stability_usd": STABILITY_USD,
            "history_pattern_usd": HISTORY_PATTERN_USD, "fairness_usd": FAIRNESS_USD,
            "overtime_premium": OVERTIME_PREMIUM}
