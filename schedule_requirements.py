"""
schedule_requirements.py — what each shift of the week needs, written the
way the schedule-generation model has to read it.

The Shift Quality Engine (shift_quality.py) scores a week against concrete
facts: how many of each role a shift needs, how busy it is, whether it needs
somebody able to run it, who is experienced, and who usually works when.
The generation prompt used to carry those facts as prose ("about 40%
experienced hands", "6 night") and never named a person or gave a number per
shift, so the model was scored on things it was never told. Every block here
renders one of those facts from the same inputs the scorer reads, so the
draft is written against the bar it is judged by.

Pure: no I/O, no model call. labor.generate_optimized_schedule calls these
while it builds the prompt; schedule_engine gathers the inputs.
"""
from datetime import datetime

import shift_quality as _sq
import schedule_time as _stime

DAYPARTS = ("morning", "night")
_PART_LABEL = {"morning": "morning", "night": "night", "late": "late night"}
_DAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The chunk-seam summary treats these as weekend shifts, the same set the
# scorer's fairness dimension counts.
WEEKEND_DAYS = ("Friday", "Saturday", "Sunday")
# Busy shifts when no profile says otherwise: Friday and Saturday night.
DEFAULT_BUSY = (("Friday", "night"), ("Saturday", "night"))
FOCUS_MAX_ITEMS = 12
FOCUS_MAX_CHARS = 240
# How far a date's measured demand (and the sales-per-labor-hour hold) may
# move the usual crew (schedule audit 10/3/26 D-23, P-19): a +80% holiday
# does not need 80% more of every role, and a quiet day never takes the
# crew below three quarters of its usual. The owner's floors still hold
# under it and the section cap over it.
DEMAND_SCALE_BOUNDS = (0.75, 1.4)
# The late segment (D-32): from 10pm to close, on a day that closes past
# 11pm — shift_quality.LATE_WINDOW_START / LATE_CLOSE_AFTER.
LATE_PART = "late"


def window_allows(w, part) -> bool:
    """Whether a role whose hours are `w` ({"earliest", "latest"} minutes,
    either None) can be on the floor for `part` — present for the
    daypart's core window as the scorer counts presence (shift_quality.
    present_dayparts): "evening only, no earlier than 3:00pm" rules out
    lunch, "first arrives 10:30am" does not (schedule re-audit 10/4/26
    PROMPT-1). The late segment needs an hour of it after LATE_WINDOW_START."""
    if not w:
        return True
    if part == LATE_PART:
        lo, hi = _sq.LATE_WINDOW_START, 48 * 60
    else:
        lo, hi = _sq.CORE_WINDOWS.get(part, (0, 48 * 60))
    a = max(lo, w["earliest"]) if w.get("earliest") is not None else lo
    b = min(hi, w["latest"]) if w.get("latest") is not None else hi
    return b - a >= min(_sq.PRESENCE_MIN_OVERLAP, hi - lo)


def _limit_for(limits, role, day):
    """The owner's limit for `role` on `day` from {role or family lower:
    {weekday: value}} — the job code's own, else its family's."""
    if not limits:
        return None
    key = " ".join(str(role or "").lower().split())
    got = limits.get(key)
    if got is None:
        got = limits.get(_sq.role_family(role))
    return (got or {}).get(day) if isinstance(got, dict) else None


def _role_cap(role_caps, role, day, part):
    """The owner's "most of a role on at once" for a shift, or None."""
    caps = _limit_for(role_caps, role, day) or {}
    cap = caps.get(part) if isinstance(caps, dict) else None
    if cap is None and isinstance(caps, dict):
        cap = caps.get("any")
    try:
        return int(cap) if cap is not None else None
    except (TypeError, ValueError):
        return None


def _apply_limits(need, day, part, role_windows, role_caps, idx_name=0, idx_req=1, idx_floor=2, idx_why=5,
                  concurrent=False):
    """Hold one shift's numbers to the owner's RESTAURANT HOURS & SHIFT
    RULES (PROMPT-1): a role outside the hours they give it is asked for
    only its floor (none, normally — a floor is never set outside them).
    Their "most on at once" ceiling cuts a number only when it is itself
    people on at once (`concurrent` — the late segment's crew); a daypart's
    count of different people is never cut to it (staggered starts fit more
    people than the most on at once — schedule audit 10/10/26, LOGIC-7's
    rule for the section cap, the same for the owner's ceiling): the row
    says the ceiling instead, and the half-hour needs are held to it.
    `need` is {key: [display, required, floor, ...]}, changed in place."""
    for k in list(need):
        v = need[k]
        w = _limit_for(role_windows, v[idx_name], day)
        if w and not window_allows(w, part):
            if v[idx_floor]:
                v[idx_req] = int(v[idx_floor])
            else:
                del need[k]
                continue
        cap = _role_cap(role_caps, v[idx_name], day, part)
        if cap is not None and v[idx_req] > cap:
            if concurrent:
                v[idx_req] = max(cap, int(v[idx_floor] or 0))
                if idx_why is not None:
                    v[idx_why].append(f"held to your maximum of {cap} on at once")
            elif idx_why is not None:
                v[idx_why].append(f"your maximum of {cap} on at once: stagger them")


def demand_factor(ratio=1.0, hold=1.0, measured=False) -> float:
    """The factor the usual crew is scaled by on one shift: the date's
    demand against a typical one (schedule_economics.date_demand's ratio)
    times the sales-per-labor-hour hold (schedule_economics.splh_objective's
    `hold`, ≤ 1 when the record runs over the labor target), held inside
    DEMAND_SCALE_BOUNDS — or, where the figure is the owner's own budget or
    measured here (`measured`: schedule_economics.MEASURED_SCALE_BOUNDS),
    the wider bounds: an +80% festival the owner budgeted, a measured -50%
    snow day (scheduler audit 10/10/26 LOGIC-9). 1.0 when nothing moves it."""
    try:
        r = float(ratio if ratio is not None else 1.0) * float(hold if hold is not None else 1.0)
    except (TypeError, ValueError):
        return 1.0
    if r <= 0:
        return 1.0
    lo, hi = MEASURED_SCALE_BOUNDS if measured else DEMAND_SCALE_BOUNDS
    return round(min(max(r, lo), hi), 3)


# schedule_economics.MEASURED_SCALE_BOUNDS, kept here so this module stays
# free of schedule_economics' imports.
MEASURED_SCALE_BOUNDS = (0.4, 2.5)


def part_demand(dd: dict, part: str, busy: bool = False) -> tuple:
    """(ratio, roles {role lower: ratio}, measured, reasons) for one daypart
    of one date from schedule_economics.date_demand's entry: the meal's own
    figure (`parts`) where the entry carries one — its busy-shift quantile
    when `busy` (demand_model.BUSY_QUANTILE: a busy shift is staffed to the
    upper forecast, not the median — LEARNING-19) — else the date's one
    ratio (an older stored entry)."""
    dd = dd or {}
    pd = (dd.get("parts") or {}).get(part)
    if not pd:
        r = dd.get("ratio", 1.0) if dd.get("ratio") is not None else 1.0
        return r, {}, bool(set(dd.get("sources") or ()) & {"budget", "measured_effects"}), []
    base = float(pd.get("ratio") if pd.get("ratio") is not None else 1.0)
    r = float(pd.get("busy_ratio") or base) if busy else base
    lift = (r / base) if base else 1.0
    roles = {str(k).strip().lower(): float(v) * lift for k, v in (pd.get("roles") or {}).items()}
    why = list(pd.get("reasons") or [])
    if busy and abs(lift - 1.0) >= 0.01:
        why.append(f"a busy shift, staffed to the upper forecast ({int(round((lift - 1) * 100)):+d}%)")
    return r, roles, bool(pd.get("measured")), why


def _scaled(n, factor) -> int:
    """n people scaled, half up; a role that runs keeps at least one."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return max(1, int(n * float(factor) + 0.5))


def _day_name(date_str: str, fallback: str = "") -> str:
    try:
        return datetime.strptime((date_str or "")[:10], "%Y-%m-%d").strftime("%A")
    except (TypeError, ValueError):
        return fallback


def _minutes(value):
    """Minutes past midnight from "4:00pm", "4pm", "16:00", "noon"
    (schedule_time.parse_minutes); None if unreadable."""
    return _stime.parse_minutes(value)


def floor_for(spec: dict, day: str, part: str) -> int:
    """The owner's floor for one role on one weekday and daypart — a
    per-day override when there is one, else the role's daypart default.
    Read exactly as shift_quality's contexts read it."""
    dspec = (spec.get("days") or {}).get(day) or {}
    n = dspec.get(part) if part in dspec else spec.get(part)
    try:
        return int(n or 0)
    except (TypeError, ValueError):
        return 0


def shift_profile(day: str, part: str, date: str = None, profiles: list = None,
                  demand_by_day: dict = None, demand_by_date: dict = None):
    """The profile one shift is scored against, demand settled, through the
    scorer's own shift_quality.profile_for_shift: the built-in set when none
    is configured (an empty list reads as none, as the scorer reads it), the
    weekday's own sales level, and a lift the owner recorded for the date."""
    lift = ((demand_by_date or {}).get(date) or {}).get("lift_pct") if date else None
    return _sq.profile_for_shift(day, part, profiles or None, demand_by_day, lift)


def busy_shifts(dates: list, profiles: list = None, demand_by_day: dict = None,
                demand_by_date: dict = None) -> set:
    """{(date, daypart)} scored at high demand or above."""
    out = set()
    for d in dates or []:
        day = _day_name(d)
        if not day:
            continue
        for part in DAYPARTS:
            demand = shift_profile(day, part, d, profiles, demand_by_day, demand_by_date).demand
            if _sq.DEMAND_RANK.get(demand, 1) >= _sq.DEMAND_RANK[_sq.HARD_DEMAND]:
                out.add((d, part))
    return out


def closing_part(day: str, close_times: dict = None) -> str:
    """The daypart a date's close falls in: the morning for a close before
    the 3pm changeover (a breakfast-and-lunch place), else the night."""
    close_m = _minutes((close_times or {}).get(day))
    if close_m is not None and _stime.SMALL_HOURS_BEFORE <= close_m <= _stime.daypart_split():
        return "morning"
    return "night"


def _rule_applies(rule: dict, day: str, part: str, runs: dict = None, close_times: dict = None) -> bool:
    """Whether a leader rule binds this shift: the scorer's own test
    (shift_quality.leader_rule_applies over shift_quality.role_runs), so a
    rule naming no daypart is asked only where its role works — the table
    used to tell the model to add a bartender to a lunch with no bar
    (schedule audit 10/3/26 SQ-2). A closing rule binds the daypart the
    day's close falls in (closing_part): asked with the closing shift "not
    known", it was printed on every morning row too — a closing bartender
    at lunch, the rule said twice a day (schedule re-audit 10/4/26
    PROMPT-6)."""
    return _sq.leader_rule_applies(rule, day, part, is_closing=part == closing_part(day, close_times), runs=runs)


def _adjust_for(adjustments, d, part, need, typical_raw) -> list:
    """The adjustments (soft asks: +1 of a role on a night) that land on
    this date and daypart, each matched to the role it names — the role in
    this shift whose name carries the asked word, the busiest of them. A
    whole-day ask lands on the daypart where that role usually runs more
    (night on a tie)."""
    out = []
    for a in adjustments or []:
        if str(a.get("date") or "")[:10] != d:
            continue
        word = str(a.get("role") or "").strip().lower()
        if not word:
            continue
        ap = (a.get("daypart") or "").strip().lower() or None
        if ap and ap != part:
            continue
        if not ap:
            other = "morning" if part == "night" else "night"
            here = max([int(n or 0) for r, n in (typical_raw.get(part) or {}).items() if word in r.lower()] or [0])
            there = max([int(n or 0) for r, n in (typical_raw.get(other) or {}).items() if word in r.lower()] or [0])
            if there > here or (there == here and part == "morning"):
                continue
        keys = [k for k in need if word == k or word in k]
        key = max(keys, key=lambda k: (need[k][1], k)) if keys else None
        out.append((key, word, a))
    return out


def shift_requirements(dates: list, typical_headcount: dict = None, role_floors: dict = None,
                       daily_targets: dict = None, profiles: list = None,
                       demand_by_day: dict = None, demand_by_date: dict = None,
                       leader_rules: list = None,
                       leadership_known: bool = False, roles: set = None,
                       skip_dates=(), role_minimums: dict = None, borrowed: dict = None,
                       demand_curve: dict = None, open_times: dict = None, close_times: dict = None,
                       section_cap: int = 0, cap_roles=None, date_demand: dict = None, splh_hold: dict = None,
                       adjustments: list = None, late_headcount: dict = None, standard_needs: dict = None,
                       role_windows: dict = None, role_caps: dict = None, crew_answers: dict = None,
                       staffing_model: dict = None, concurrent: dict = None, half_curve: dict = None) -> list:
    """One entry per date × daypart that needs anybody.

    Each role's number is the larger of the owner's floor and what this
    restaurant typically runs on that weekday and daypart — that usual crew
    scaled by the date's measured demand (date_demand) and held to the
    sales-per-labor-hour target (splh_hold), inside DEMAND_SCALE_BOUNDS;
    the floor is kept alongside so the prompt can mark which part is a hard
    minimum. Role names match case-insensitively and the owner's spelling
    wins. Every row carries `reasons` [str] — why its numbers moved off the
    usual — and `factor`. The number used to be the usual
    crew whatever the date: a +30% measured event Friday was asked for a
    typical Friday's people, and the demand changed only the label
    (schedule audit 10/3/26 D-23, P-19, PR-7).

    roles     — lowercase roles the people in this request can work; a role
                nobody here can fill is left to the request that can (a
                roster split by department). None means every role.
    leadership_known — whether anybody is rated, authorized to close or a
                manager. A profile's "needs a leader" cannot be judged
                without one of those, so it is not asked of the model either.
    borrowed  — {(weekday, daypart): {lower role}}: typical figures lent by
                similar restaurants (intelligence.staffing) to a restaurant
                with no history of its own; the row says so.
    demand_curve — {weekday: {hour: share}} measured sales by the hour; with
                it each shift also carries its half-hour needs across service
                (staffing_curve.half_hour_needs, interpolated from the hourly
                readings), the same needs the coverage-by-the-hour score
                judges. Without it nothing changes.
    section_cap / cap_roles — the section count and the roles it counts (by
                role family): no half-hour need — people on at once — asks
                for more of them than the cap (staffing_curve.
                cap_requirement). The whole-shift number is different people
                across the daypart, a unit the cap does not limit (staggered
                starts fit more people than sections), so it is never cut to
                the cap (schedule audit 10/10/26 LOGIC-7).
    crew_answers — {(weekday, daypart): {role: {"people", "was", "since"}}}
                (staffing_baseline's `crew_answers`, crew_matrix.bind_answers):
                the shifts the owner said "leaner is fine" about in Crew,
                whose usual crew is already held to what they accepted —
                named as the reason.
    date_demand — {date: {"ratio", "pct", "reasons"}}
                (schedule_economics.date_demand): how far each date sits
                from a typical night of its weekday, and why.
    splh_hold — {weekday: {daypart: factor}} (splh_objective's `hold`): how
                far the usual crew comes in for its sales to meet the
                sales-per-labor-hour target.
    adjustments — [{date, daypart|None, role, delta, reason, firm}]: asks
                folded into the number with their reason (a nightly report's
                or the reviews' +1 — PR-7); `firm` False keeps it out of
                `firm`, so the score never marks a shift short of a soft ask.
    late_headcount — {weekday: {role: people}} on the floor from 10pm
                (labor.historical_patterns' `late_headcount`): on a date that
                closes past 11pm a `late` row follows the night's (D-32),
                scaled by the same demand.
    standard_needs — {(date, daypart): {role lower: {"people", "reason"}}}:
                the owner's own labor standard for a role (labor_standards),
                which replaces the usual crew for it on that shift (D-25).
    role_windows / role_caps — the owner's RESTAURANT HOURS & SHIFT RULES
                read by the code (schedule_rules.apply_hours_rules,
                Constraints.limit_maps — schedule re-audit 10/4/26 PROMPT-1):
                {role or family lower: {weekday: {"earliest", "latest"}}}
                and {role or family lower: {weekday: {daypart | "any":
                most}}}. A role is asked for nothing outside its hours (only
                a floor stands there) and never more than its ceiling. The
                usual crew from punches used to ask for an evening-only
                bartender at Wednesday lunch.
    staffing_model — staffing_model.measure(): where a role's family has a
                labor standard (the owner's, or measured at the rush and
                shrunk toward a prior), its number is the forecast work of
                the meal at each half hour ÷ that standard, plus the meal's
                fixed crew — people on at once, mapped to different people
                by the restaurant's own stagger — never the usual crew x a
                ratio (scheduler audit 10/10/26 RM-17, LEARNING-4, LOGIC-10,
                LOGIC-12). Without a standard the usual crew stands, moved by
                the meal's and the role's own demand (LOGIC-9, LEARNING-20).
                Its `lags` shift each role along the curve (LOGIC-11).
    concurrent — the staffing baseline's people on at once ({(weekday,
                daypart): {role: {"peak"}}}, contract #2): each role's
                half-hour peak is its usual most on at once, demand-scaled —
                no longer 0.75 x its different people (LOGIC-11).
    half_curve — {weekday: {minute: share}}, the curve measured at the half
                hour from the ticket archive (schedule_economics.
                measured_half_hours), used over the hourly one.
    Each row also carries `half_need` — {minute: {role: people on}}, every
    half hour's full need after the caps — the one the score judges
    (half_hours_map).
    """
    import staffing_curve as _curve
    from shift_quality import shift_role_requirements
    skip = set(skip_dates or ())
    # Where each role works, by the same requirements the scorer reads: a
    # leader rule naming no daypart is asked only there (SQ-2).
    runs = _sq.role_runs(typical_headcount, role_floors, role_minimums, profiles or None)
    out = []
    for d in dates or []:
        if d in skip:
            continue
        day = _day_name(d)
        if not day:
            continue
        dd = (date_demand or {}).get(d) or {}
        ratio = dd.get("ratio", 1.0) if dd.get("ratio") is not None else 1.0
        why_date = [str(x) for x in (dd.get("reasons") or []) if x]
        raw_by_part = {p: ((typical_headcount or {}).get((day, p)) or {}) for p in DAYPARTS}
        for part in DAYPARTS:
            # The same requirement the coverage score holds the draft to
            # (shift_quality.shift_role_requirements): usual headcount, the
            # owner's floors, their whole-day minimums for roles working this
            # daypart, and the profile's critical positions — the largest of
            # each per role.
            profile_here = shift_profile(day, part, d, profiles, demand_by_day, demand_by_date)
            typical_raw = raw_by_part[part]
            hold = ((splh_hold or {}).get(day) or {}).get(part)
            # The meal's own demand and each role's (LOGIC-9, LEARNING-20):
            # a dinner game never lifts lunch, a bar night never the dish pit.
            busy_here = _sq.DEMAND_RANK.get(profile_here.demand, 1) >= _sq.DEMAND_RANK[_sq.HARD_DEMAND]
            p_ratio, role_ratios, measured, why_part = part_demand(dd, part, busy_here)
            factor = demand_factor(p_ratio, hold, measured)

            def _role_factor(r):
                rr = role_ratios.get(r.strip().lower())
                return demand_factor(rr, hold, measured) if rr is not None else factor
            typical_here = {r: (_scaled(n, _role_factor(r)) if _role_factor(r) != 1.0 else int(n or 0))
                            for r, n in typical_raw.items()}
            # Driver-based (RM-17): a family with a labor standard is asked
            # for the work it will carry, not its habit. The hold is not
            # applied — the budget is priced in dollars elsewhere (LOGIC-10).
            driven = {}
            if staffing_model and typical_raw:
                import staffing_model as _sm
                lo_d, hi_d = _service_window(day, part, open_times, close_times)
                ratios_d = {r: demand_factor(role_ratios.get(r.strip().lower(), p_ratio), 1.0, True)
                            for r in typical_raw}
                driven = _sm.needs(staffing_model, day, part, ratios_d, typical_raw,
                                   (concurrent or {}).get((day, part)) or {}, window=(lo_d, hi_d))
                for r, dn in driven.items():
                    typical_here[r] = int(dn["distinct"])
            standard_here = (standard_needs or {}).get((d, part)) or {}
            for r in list(typical_here):
                std = standard_here.get(r.strip().lower())
                if std and std.get("people") is not None and r not in driven:
                    typical_here[r] = int(std["people"])
            floors_here = {}
            for role, spec in (role_floors or {}).items():
                f = floor_for(spec or {}, day, part)
                if f:
                    floors_here[role] = f
            reqs = shift_role_requirements(typical_here, floors_here, role_minimums,
                                           getattr(profile_here, "critical_positions", None))
            need = {}      # lower -> [display, required, floor, usual, asked, [reasons]]
            for role, (n, _src) in reqs.items():
                key = role.strip().lower()
                floor = max([int(v) for r2, v in floors_here.items() if r2.strip().lower() == key] or [0])
                typ = max([int(v or 0) for r2, v in typical_raw.items() if r2.strip().lower() == key] or [0])
                why = []
                std = standard_here.get(key)
                dn = next((v for r2, v in driven.items() if r2.strip().lower() == key), None)
                rf = _role_factor(role)
                if dn is not None:
                    why.append(f"usual {typ}; {dn['reason']}" if typ and int(n) != typ else dn["reason"])
                elif std and std.get("reason"):
                    why.append(str(std["reason"]))
                elif typ and rf != 1.0 and _scaled(typ, rf) != typ and int(n) != typ:
                    why.append(f"usual {typ}" + (f"; this role's own record on nights like it "
                                                 f"{int(round((rf - 1) * 100)):+d}%"
                                                 if key in role_ratios and abs(rf - factor) >= 0.005 else ""))
                need[key] = [role.strip(), int(n), floor, typ, 0, why]
            # Asks folded in (PR-7): +1 of a role, with its reason.
            for key, word, a in _adjust_for(adjustments, d, part, need, raw_by_part):
                try:
                    delta = int(a["delta"]) if a.get("delta") is not None else 1
                except (TypeError, ValueError):
                    delta = 1
                if key is None:
                    key = word
                    need[key] = [str(a.get("role_display") or a.get("role") or word).strip().title(), 0, 0, 0, 0, []]
                v = need[key]
                v[1] += delta
                if not a.get("firm", False):
                    v[4] += delta
                # A position that is one of the usual crew (schedule_dedicated,
                # LOGIC-16): no +1 on top, but never fewer than it.
                try:
                    at_least = int(a.get("at_least") or 0)
                except (TypeError, ValueError):
                    at_least = 0
                if at_least and v[1] < at_least:
                    v[1] = at_least
                if a.get("reason"):
                    v[5].append(f"{delta:+d} {a['reason']}" if delta else str(a["reason"]))
            # Held to the owner's hours and ceilings by role (PROMPT-1).
            _apply_limits(need, day, part, role_windows, role_caps)
            # A shift the owner said "leaner is fine" about in Crew carries
            # it as its reason (crew_matrix.bind_answers already held the
            # usual crew to what they accepted).
            for role_c, bound in ((crew_answers or {}).get((day, part)) or {}).items():
                v = need.get(role_c.strip().lower())
                if v is not None and int(bound.get("was") or 0) > int(bound.get("people") or 0):
                    from time_utils import mdy as _mdy_c
                    v[5].append(f"leaner is fine (you, {_mdy_c(bound.get('since'))}; usual "
                                f"{int(bound.get('was') or 0)})")
            # The section cap is NOT applied here: it counts people on at
            # once, and these are the different people on the floor for the
            # daypart — six staggered servers who are never more than five on
            # at once fit five sections, and holding the six to five left the
            # sixth never asked for and told the owner the cap was "probably
            # wrong" when it was not (schedule audit 10/10/26 LOGIC-7). The
            # cap holds over the half-hour needs below, which ARE people on
            # at once, and the sweep's over_section_cap holds the draft.
            held = {}
            if roles is not None:
                need = {k: v for k, v in need.items() if k in roles}
            if not need:
                continue
            half = {}
            full_needs = {}
            curve_here = (demand_curve or {}).get(day) or {}
            hcurve = (half_curve or {}).get(day) or None
            if (curve_here or hcurve or driven) and typical_here:
                lo, hi = _service_window(day, part, open_times, close_times)
                # Normalised over the daypart's half of the day, as the scorer
                # does, then kept to the hours the restaurant is open.
                split = _stime.daypart_split()
                whole = (0, split) if part == "morning" else (split, 48 * 60)
                # The roles on the usual crew: each one's half-hour peak is
                # its usual most ON AT ONCE (the baseline's concurrent peak),
                # moved by its demand — not 0.75 x its different people — and
                # its curve shifted by its family's measured lead or lag
                # (LOGIC-11). A role with a labor standard brings its own.
                hist_roles = {r: n for r, n in typical_here.items() if r not in driven}
                conc = (concurrent or {}).get((day, part)) or {}
                peaks, offs = {}, {}
                fam_lags = {}
                if staffing_model:
                    import staffing_model as _sm
                    fam_lags = _sm.lags(staffing_model)
                for r, n in hist_roles.items():
                    cfig = next((v for k, v in conc.items() if str(k).strip().lower() == r.strip().lower()), None)
                    if isinstance(cfig, dict) and cfig.get("peak"):
                        peaks[r] = min(int(n or 0) or 1, _scaled(cfig["peak"], _role_factor(r)))
                    lag = None
                    if fam_lags:
                        from intelligence.staffing import family_of as _fo
                        lag = fam_lags.get(_fo(r))
                    if lag:
                        offs[r] = lag
                needs = {}
                if curve_here or hcurve:
                    needs = {m: n for m, n in _curve.half_hour_needs(curve_here, hist_roles, *whole, peaks=peaks,
                                                                      offsets=offs, shares=hcurve).items()
                             if lo <= m < hi}
                for r, dn in driven.items():
                    for m, k in (dn.get("half") or {}).items():
                        if lo <= m < hi and k:
                            needs.setdefault(m, {})[r] = int(k)
                if section_cap:
                    # People on at once at each half hour: the unit the
                    # section count is in.
                    capped_needs = {}
                    for m, n in needs.items():
                        capped_n, trimmed = _curve.cap_requirement(n, section_cap, cap_roles)
                        capped_needs[m] = capped_n
                        for r_t, k_t in trimmed.items():
                            held[r_t] = max(held.get(r_t, 0), k_t)
                    needs = capped_needs
                if role_caps:
                    # The owner's "most of a role on at once", in its own unit.
                    for m, rn in needs.items():
                        for r_c in list(rn):
                            cap_c = _role_cap(role_caps, r_c, day, part)
                            if cap_c is not None and rn[r_c] > cap_c:
                                rn[r_c] = cap_c
                if roles is not None:
                    needs = {m: {r: n for r, n in rn.items() if r.strip().lower() in roles} for m, rn in needs.items()}
                half = {r: pts for r, pts in _curve.ramp_runs(needs).items() if len(pts) > 1
                        and r.strip().lower() in need}
                full_needs = {m: {r: n for r, n in rn.items() if r.strip().lower() in need and n}
                              for m, rn in needs.items()}
                full_needs = {m: rn for m, rn in full_needs.items() if rn}
            profile = shift_profile(day, part, d, profiles, demand_by_day, demand_by_date)
            demand = profile.demand
            leader = []
            for rule in leader_rules or []:
                if not _rule_applies(rule, day, part, runs, close_times):
                    continue
                # A rule for a role the people in this request work, its
                # AM/PM job codes counted as the role — as the scorer judges
                # it (a "Bartender" rule is the "Bartender PM" crew's).
                if roles is not None and _sq.role_family(rule.get("role")) not in {_sq.role_family(r) for r in roles}:
                    continue
                bit = f"{int(rule.get('count') or 1)} {rule['role'].strip()}"
                if rule.get("attribute"):
                    bit += " authorized to close"
                elif rule.get("min_score") is not None:
                    bit += f" scoring {float(rule['min_score']):g}+"
                if rule.get("closing"):
                    bit += " on the closing shift"
                leader.append(bit)
            if profile.requires_leader and leadership_known and not leader:
                # A manager on the floor runs the shift, as the scorer counts
                # it (SQ-13).
                leader.append(f"a manager, somebody scoring {profile.leader_min_score:g}+ or "
                              "somebody authorized to close")
            roles_out = []
            lent = (borrowed or {}).get((day, part)) or set()
            for _k, (name, required, floor, typical, asked, why) in sorted(need.items(), key=lambda kv: (-kv[1][1], kv[1][0].lower())):
                entry = {"role": name, "required": required, "floor": floor, "typical": typical}
                if asked:
                    entry["asked"] = asked
                    entry["firm"] = required - asked
                if why:
                    entry["reason"] = "; ".join(why)
                if _k in lent and typical and typical > floor:
                    entry["borrowed"] = True
                roles_out.append(entry)
            reasons = _row_reasons(factor, p_ratio, hold, why_date + [w for w in why_part if w not in why_date],
                                   held, bounded_by=MEASURED_SCALE_BOUNDS if measured else DEMAND_SCALE_BOUNDS)
            if driven:
                reasons.append("sized from the work: " + ", ".join(sorted({dn["family"].replace("_", " ")
                                                                           for dn in driven.values()}))
                               + " by the labor standard at the busiest half hour")
            row = {"date": d, "day": day, "daypart": part, "roles": roles_out,
                   "target_hours": (daily_targets or {}).get(d), "demand": demand,
                   "leader": leader, "factor": factor, "reasons": reasons}
            if abs(float(p_ratio or 1.0) - float(ratio or 1.0)) >= 0.005:
                row["part_ratio"] = round(float(p_ratio), 3)
            if driven:
                row["driven"] = sorted(driven)
            if half:
                row["half_hours"] = half
            if full_needs:
                row["half_need"] = full_needs
            if held:
                row["held_to_cap"] = {"cap": int(section_cap), "trimmed": held, "unit": "on_at_once"}
            out.append(row)
        late = _late_row(d, day, (late_headcount or {}).get(day) or {}, close_times, part_demand(dd, "night")[0],
                         ((splh_hold or {}).get(day) or {}).get(LATE_PART), why_date, roles, section_cap, cap_roles,
                         (daily_targets or {}).get(d), role_windows=role_windows, role_caps=role_caps)
        if late:
            out.append(late)
    return out


def _row_reasons(factor, ratio, hold, why_date, held, bounded_by=None) -> list:
    """Why a shift's numbers moved off its usual crew, in the owner's words.
    `bounded_by` — the bounds the factor was held inside (named when they
    bit)."""
    out = []
    if factor != 1.0:
        moved = int(round((factor - 1) * 100))
        bits = []
        try:
            if ratio is not None and abs(float(ratio) - 1.0) >= 0.005:
                bits.append(f"this date's demand {int(round((float(ratio) - 1) * 100)):+d}% against a typical one"
                            + (": " + "; ".join(why_date) if why_date else ""))
        except (TypeError, ValueError):
            pass
        try:
            if hold is not None and float(hold) < 0.995:
                bits.append(f"sales per labor hour held to its target ({int(round((float(hold) - 1) * 100)):+d}%)")
        except (TypeError, ValueError):
            pass
        bounded = ""
        try:
            raw = float(ratio if ratio is not None else 1.0) * float(hold if hold is not None else 1.0)
            if abs(raw - factor) >= 0.005:
                bounded = f", held to {int(round((factor - 1) * 100)):+d}%"
        except (TypeError, ValueError):
            pass
        out.append(f"usual crew {moved:+d}%{bounded} — " + "; ".join(bits) if bits else f"usual crew {moved:+d}%")
    if held:
        out.append("on at once held to the section count (" + ", ".join(f"{r} -{n}" for r, n in sorted(held.items()))
                   + ")")
    return out


def _late_row(d, day, late_typical, close_times, ratio, hold, why_date, roles, section_cap, cap_roles,
              target_hours, role_windows=None, role_caps=None):
    """The late segment's row (D-32): from LATE_WINDOW_START to close, on a
    date whose close is past LATE_CLOSE_AFTER — its own usual crew from the
    history, scaled by the date's demand like the others. None otherwise."""
    if not late_typical:
        return None
    window = _sq.late_window(_close_minutes(day, close_times))
    if window is None:
        return None
    import staffing_curve as _curve
    factor = demand_factor(ratio, hold)
    need = {}
    for r, n in late_typical.items():
        v = _scaled(n, factor) if factor != 1.0 else int(n or 0)
        if v > 0 and (roles is None or r.strip().lower() in roles):
            need[r.strip()] = [v, int(n or 0)]
    if role_windows or role_caps:
        # [display, required, floor] for the shared limits (PROMPT-1).
        view = {r: [r, v[0], 0] for r, v in need.items()}
        # The late crew is people on across the window (labor's late_headcount
        # is the median on at each half hour): the ceiling's own unit.
        _apply_limits(view, day, LATE_PART, role_windows, role_caps, idx_why=None, concurrent=True)
        need = {r: [view[r][1], v[1]] for r, v in need.items() if r in view}
    if not need:
        return None
    held = {}
    if section_cap:
        capped, held = _curve.cap_requirement({r: v[0] for r, v in need.items()}, section_cap, cap_roles)
        for r, v in need.items():
            v[0] = int(capped.get(r, v[0]))
    roles_out = [{"role": r, "required": v[0], "floor": 0, "typical": v[1]}
                 for r, v in sorted(need.items(), key=lambda kv: (-kv[1][0], kv[0].lower()))]
    return {"date": d, "day": day, "daypart": LATE_PART, "roles": roles_out, "target_hours": target_hours,
            "demand": None, "leader": [], "factor": factor, "window": list(window),
            "reasons": _row_reasons(factor, ratio, hold, why_date, held)}


def _close_minutes(day: str, close_times: dict = None):
    """The day's close as minutes past its own midnight (a small-hours close
    is past 24h), or None when no close time is on file."""
    m = _minutes((close_times or {}).get(day))
    if m is None:
        return None
    return _stime.close_on_timeline(m)


def requirements_map(rows: list, firm: bool = True) -> dict:
    """{(date, daypart): {role: people}} — the numbers the coverage score
    judges a draft against (P-19), the same ones the model is given. `firm`
    leaves a soft ask out: it is asked for, never marked short."""
    out = {}
    for r in rows or []:
        slot = out.setdefault((r["date"], r["daypart"]), {})
        for x in r.get("roles") or []:
            # A figure lent by similar restaurants is the model's starting
            # point, never what the score marks a shift short of — the
            # scorer's requirement stays the restaurant's own.
            if x.get("borrowed"):
                continue
            n = int(x.get("firm", x["required"]) if firm else x["required"])
            if n > 0:
                slot[x["role"]] = max(n, slot.get(x["role"], 0))
    return {k: v for k, v in out.items() if v}


def half_hours_map(rows: list) -> dict:
    """{(date, daypart): {minute: {role: people on}}} — each shift's full
    half-hour need (the rows' `half_need`): what the coverage score judges
    every half hour against, the same numbers the passes fill to (scheduler
    audit 10/10/26 LOGIC-11 — the scorer used to rebuild them as 0.75 x
    the requirement's different people). Keys read back from JSON as
    strings are turned back into minutes."""
    out = {}
    for r in rows or []:
        hn = r.get("half_need") if isinstance(r, dict) else None
        if not hn or not r.get("date") or not r.get("daypart"):
            continue
        slot = out.setdefault((r["date"], r["daypart"]), {})
        for m, rn in hn.items():
            try:
                m = int(m)
            except (TypeError, ValueError):
                continue
            here = slot.setdefault(m, {})
            for role, n in (rn or {}).items():
                try:
                    here[role] = max(int(n), int(here.get(role) or 0))
                except (TypeError, ValueError):
                    continue
    return out


def requirement_reasons(rows: list) -> dict:
    """{(date, daypart): [reasons]} — why each shift's numbers moved."""
    return {(r["date"], r["daypart"]): list(r.get("reasons") or []) for r in rows or [] if r.get("reasons")}


def _service_window(day: str, part: str, open_times: dict = None, close_times: dict = None) -> tuple:
    """(lo, hi) minutes of one daypart's service: opening to the 3pm
    changeover, or the changeover to close, from the hours on file; the
    whole half of the day when they are not."""
    split = _stime.daypart_split()
    # A close at or before the open, or in the small hours, is after
    # midnight (schedule_time.close_on_timeline, BUGSGEN-22).
    open_m, close_m = _stime.day_hours((open_times or {}).get(day), (close_times or {}).get(day))
    if part == "morning":
        return (open_m if open_m is not None and open_m < split else 0), split
    return split, (close_m if close_m is not None and close_m > split else 24 * 60)


def requirements_block(rows: list, unread_rules: bool = False) -> str:
    """The SHIFT REQUIREMENTS table: one line per shift. How coverage is
    counted, the half-hour ramp and the late-night line are standing
    instructions (schedule_prompt, the same on every call); the table is
    this request's numbers and why they moved, each said once (schedule
    audit 10/3/26 PR-7, PR-8, PR-24). It carries the owner's floors, hours
    and ceilings the code read; `unread_rules` — a line of the owner's
    RESTAURANT HOURS & SHIFT RULES it could not read — says that line wins
    over the table, never that the table already holds it (schedule
    re-audit 10/4/26 PROMPT-1)."""
    if not rows:
        return ""
    lines = []
    for r in rows:
        people = ", ".join(f"{x['role']} {x['required']}" + (f" (floor {x['floor']})" if x["floor"] else "")
                           + (" (borrowed)" if x.get("borrowed") else "")
                           + (f" ({_model_words(x['reason'])})" if x.get("reason") else "")
                           for x in r["roles"])
        label = _PART_LABEL.get(r["daypart"], r["daypart"])
        if r.get("window"):
            label += f" {_clock(r['window'][0])}-{_clock(r['window'][1])}"
        bits = [f"  {r['day'][:3]} {r['date']} {label} | {people}"]
        if r.get("target_hours") and r["daypart"] != LATE_PART:
            bits.append(f"day target {float(r['target_hours']):g}h")
        if r.get("demand"):
            bits.append(f"{r['demand']} demand")
        if r.get("leader"):
            bits.append("leader: " + "; ".join(r["leader"]))
        if r.get("half_hours"):
            from staffing_curve import ramp_text
            bits.append("by the half hour: " + ramp_text(r["half_hours"]))
        # Why a number moved off the usual crew (the date's measured demand,
        # the sales-per-labor-hour hold, the section cap) — said once, here,
        # in the row it moved (schedule audit 10/3/26 PR-7, PR-8).
        if r.get("reasons"):
            bits.append("why: " + "; ".join(_model_words(x) for x in r["reasons"]))
        lines.append(" | ".join(bits))
    return ("\n\nSHIFT REQUIREMENTS — priority 2. One line per shift of this request: the people each role needs on "
            "it (\"(floor N)\" the owner's hard minimum inside the number, \"(borrowed)\" a figure lent by similar "
            "restaurants, \"(usual N)\" what the number was before this date's demand moved it), the date's hours "
            "target, the demand level the shift is scored at, who it needs to run it, its half-hour numbers and why "
            "a number moved. Schedule to these numbers — they carry every event, holiday, measured volume change and "
            "staffing ask on the date, and every staffing floor, hour a role starts or ends and ceiling of the owner's "
            "that the code reads — and never above them to use up hours."
            + (" A line of the RESTAURANT HOURS & SHIFT RULES that the code could not read (named there) is not in "
               "these numbers: where it says otherwise, it wins over this table." if unread_rules else "")
            + " The day target is the hours "
            "that date's shifts are expected to take: use up to it when the day's shifts need the hours to meet these "
            "numbers, and leave it unspent when they do not — a day under its target with every shift covered is a "
            "good day.\n" + "\n".join(lines))


def _model_words(text) -> str:
    """A reason as the model may read it: one line, its fence markers
    broken — an event's label in it is the owner's (or a catalog's) words,
    shown unfenced inside the table row (ai_guard)."""
    import ai_guard
    return ai_guard._neutralise_markers(" ".join(str(text or "").split())[:240])


def _days_label(days: list) -> str:
    """"Fri/Sat", or "any day": the weekdays of a ROSTER line's USUAL column
    (labor._roster_people)."""
    ds = [d for d in _DAY_ORDER if d in set(days or [])]
    if len(ds) == 7:
        return "any day"
    return "/".join(d[:3] for d in ds)


def _parts_label(parts: list) -> str:
    """"days", "nights" or "day or night": the dayparts of a ROSTER line's
    USUAL column."""
    ps = sorted({(p or "").strip().lower() for p in parts or [] if p})
    if set(ps) >= {"morning", "night"}:
        return "day or night"
    if ps == ["morning"]:
        return "days"
    if ps == ["night"]:
        return "nights"
    return ""


def _row_parts(row: dict) -> set:
    """The dayparts a row is on the floor for, by the scorer's own rule
    (shift_quality.present_dayparts)."""
    return {p for p in _sq.present_dayparts(row) if p in DAYPARTS}


def _clock(m) -> str:
    h, mm = divmod(int(m) % (24 * 60), 60)      # a close past midnight (26:00) is 2:00am
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def presence_rule() -> str:
    """The daypart-presence rule in words, built from the scorer's own
    constants so the sentence cannot drift from what is counted."""
    lo_m, hi_m = _sq.CORE_WINDOWS["morning"]
    lo_n, hi_n = _sq.CORE_WINDOWS["night"]
    need = _sq.PRESENCE_MIN_OVERLAP
    split = _clock(_stime.daypart_split())
    # schedule_time.present_dayparts in words (BUGSGEN-25): presence decides,
    # the start only when a shift covers neither core window.
    return (f"A shift counts toward each daypart whose core window it covers for at least {need} minutes "
            f"(lunch {_clock(lo_m)}-{_clock(hi_m)}, dinner {_clock(lo_n)}-{_clock(hi_n)}); one that covers "
            f"neither counts toward the daypart it starts in (morning before {split}, night from {split}). So an "
            f"11:30am-7:00pm shift counts at lunch AND at dinner; a 10:00am-5:00pm shift counts at lunch only; "
            f"a 2:00pm-10:00pm shift counts at dinner only.")


def _weekday_span(dates) -> str:
    """"Mon-Tue" for a run of dates, "Mon/Wed" otherwise."""
    ds = sorted({str(d)[:10] for d in (dates or []) if d})
    if not ds:
        return ""
    names = [_day_name(d)[:3] for d in ds]
    try:
        run = (datetime.strptime(ds[-1], "%Y-%m-%d") - datetime.strptime(ds[0], "%Y-%m-%d")).days == len(ds) - 1
    except ValueError:
        run = False
    return f"{names[0]}-{names[-1]}" if len(ds) > 1 and run else "/".join(names)


def seam_lines(prior_rows: list, busy: set = None, limits: dict = None, payroll_weeks: dict = None) -> list:
    """Per person, what the earlier parts of a chunked week (or the kept days
    of a redo) already gave them: hours, days, their last shift, and the
    closes, weekend shifts and busy shifts that fairness is scored on across
    the whole week — and, with `limits` ({name: {min, ot, salaried, cap,
    carried {payroll week: hours already published}}}), what they still
    need to reach their minimum and the room left before overtime in each
    payroll week the week touches (`payroll_weeks` {date: payroll week};
    schedule audit 10/3/26 PR-4, PR-16, E-9, D-19: the seam said "Xh so
    far" and nothing about what that left).

    A close is a shift ending within 30 minutes of the latest end that day
    (the rotation ledger's tolerance). busy is {(date, daypart)}; without it
    Friday and Saturday nights count."""
    latest = {}
    for pr in prior_rows or []:
        e = _stime.end_of(pr)
        if e is None:
            continue
        d = pr.get("date") or ""
        latest[d] = max(latest.get(d, -1), e)
    weeks = dict(payroll_weeks or {})
    seen = {}
    for pr in prior_rows or []:
        nm = (pr.get("employee") or "").strip()
        if not nm:
            continue
        e = seen.setdefault(nm, {"hours": 0.0, "days": set(), "last": "", "_k": ("", ""),
                                 "closes": 0, "weekend": 0, "busy": 0, "by_week": {}})
        date = pr.get("date") or ""
        try:
            h = float(pr.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            h = 0.0
        e["hours"] += h
        wk = weeks.get(date, "")
        e["by_week"][wk] = e["by_week"].get(wk, 0.0) + h
        day = pr.get("day") or _day_name(date) or date
        e["days"].add(day[:3])
        key = (date, pr.get("shift_end") or "")
        if key > e["_k"]:
            e["_k"] = key
            e["last"] = f"{_day_name(date)[:3] or pr.get('day') or ''} {date} until {pr.get('shift_end')}".strip()
        end = _stime.end_of(pr)
        if end is not None:
            if end >= latest.get(date, 10 ** 6) - 30:
                e["closes"] += 1
        full_day = _day_name(date, pr.get("day") or "")
        if full_day in WEEKEND_DAYS:
            e["weekend"] += 1
        parts = _row_parts(pr)
        if busy is not None:
            if any((date, p) in busy for p in parts):
                e["busy"] += 1
        elif any((full_day, p) in DEFAULT_BUSY for p in parts):
            e["busy"] += 1
    spans = {}
    for d, wk in weeks.items():
        spans.setdefault(wk, []).append(d)
    out = []
    for n, e in sorted(seen.items()):
        line = f"  {n}: {e['hours']:g}h so far on {'/'.join(sorted(e['days']))}"
        if e["last"]:
            line += f", last shift {e['last']}"
        line += f"; {e['closes']} close{'' if e['closes'] == 1 else 's'}, {e['weekend']} weekend, {e['busy']} busy"
        lim = (limits or {}).get(n)
        if lim:
            extra = []
            mn = float(lim.get("min") or 0)
            if mn and e["hours"] + 0.05 < mn:
                extra.append(f"needs {mn - e['hours']:g}h more for their {mn:g}h minimum")
            cap = lim.get("cap") if lim.get("salaried") else lim.get("ot")
            if cap:
                carried = lim.get("carried") or {}
                rooms = []
                for wk in sorted(set(spans) or {""}):
                    used = float(carried.get(wk) or 0) + e["by_week"].get(wk, 0.0)
                    rooms.append((wk, max(0.0, float(cap) - used)))
                what = "their cap" if lim.get("salaried") else "overtime"
                if len(rooms) == 1:
                    extra.append(f"{rooms[0][1]:g}h left before {what}")
                else:
                    extra.append(f"{what} room " + ", ".join(f"{room:g}h {_weekday_span(spans.get(wk))}"
                                                             for wk, room in rooms))
            if extra:
                line += "; " + "; ".join(extra)
        out.append(line)
    return out


def _dates_named(dates: list) -> str:
    """'Sat 2026-10-10 and Sun 2026-10-11' — the dates as the rest of the
    prompt writes them, weekday and ISO (schedule audit 10/3/26 PR-20)."""
    named = [f"{_day_name(d, d)[:3]} {d}" for d in sorted(set(dates or []))]
    if len(named) <= 1:
        return "".join(named)
    return ", ".join(named[:-1]) + " and " + named[-1]


def focus_block(focus: list, dates: list = None) -> str:
    """What the previous draft of these days was scored weak on, named, so
    a regeneration of chosen dates fixes those things rather than
    reshuffling at random — with the dates it is about (schedule audit
    10/3/26 PR-18: the header said "THESE DAYS" and never named them). The
    owner's own reason for a redo is their `instruction` (labor), said once
    at priority 5, never here as well."""
    items = []
    for f in focus or []:
        text = " ".join(str(f or "").split())[:FOCUS_MAX_CHARS]
        if text and text not in items:
            items.append(text)
        if len(items) >= FOCUS_MAX_ITEMS:
            break
    if not items:
        return ""
    which = _dates_named(dates) if dates else "THESE DAYS"
    return (f"\n\nTHE PREVIOUS DRAFT OF {which} SCORED WEAK ON:\n" + "\n".join(f"  * {t}" for t in items)
            + "\n  Fix these specifically in this draft, within the PRIORITIES order — never by breaking "
            "anything ranked above the thing being fixed.")
