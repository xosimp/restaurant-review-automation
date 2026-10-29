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
import json
import re
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
# move the usual crew: a +80% holiday does not need 80% more of every
# role, and a quiet day never takes the crew below three quarters of its
# usual. The owner's floors still hold under it and the section cap over
# it.
DEMAND_SCALE_BOUNDS = (0.75, 1.4)
# The late segment: from 10pm to close, on a day that closes past 11pm —
# shift_quality.LATE_WINDOW_START / LATE_CLOSE_AFTER.
LATE_PART = "late"


def window_allows(w, part) -> bool:
    """Whether a role whose hours are `w` ({"earliest", "latest"} minutes,
    either None) can be on the floor for `part` — present for the daypart's
    core window as the scorer counts presence (shift_quality.
    present_dayparts): "evening only, no earlier than 3:00pm" rules out
    lunch, "first arrives 10:30am" does not. The late segment needs an hour
    of it after LATE_WINDOW_START."""
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
    RULES: a role outside the hours they give it is asked for only its
    floor (none, normally — a floor is never set outside them). Their "most
    on at once" ceiling cuts a number only when it is itself people on at
    once (`concurrent` — the late segment's crew); a daypart's count of
    different people is never cut to it (staggered starts fit more people
    than the most on at once's rule for the section cap, the same for the
    owner's ceiling): the row says the ceiling instead, and the half-hour
    needs are held to it. `need` is {key: [display, required, floor,...]},
    changed in place."""
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
    snow day. 1.0 when nothing moves it."""
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
    upper forecast, not the median) — else the date's one ratio (an older
    stored entry)."""
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


def _hold_to_record(driven: dict, concurrent: dict, ratios: dict, typical: dict) -> None:
    """A MEASURED labor standard's ask (staffing_model.needs), in place, held
    to the larger of what the date's demand makes of the usual crew and the
    meal's own record — the most this restaurant has run of the role on the
    floor (labor.concurrent_patterns' `most_people`) and on at once (`most`)
    over the dates the usual crew is read from. The record already holds the
    meal's busiest nights, its event nights among them, so the demand never
    lifts it twice. A standard measured over every lunch of the week asked
    EJ's Bears Sunday brunch (11/8/26) for 13 cooks, 5 bussers and 5 hosts
    from a meal that has never run more than 6, 1 and 3 (its three Bears
    Sundays included): no roster could staff it, and its cost came out of
    every other shift under the labor budget. The owner's own standard
    ("yours") is their word and is never held."""
    low = {str(k).strip().lower(): v for k, v in (concurrent or {}).items() if isinstance(v, dict)}
    for r, dn in (driven or {}).items():
        if dn.get("source") == "yours":
            continue
        rec = low.get(r.strip().lower()) or {}
        try:
            lift = float(ratios.get(r, 1.0) or 1.0)
        except (TypeError, ValueError):
            lift = 1.0
        usual = int(typical.get(r, 0) or 0)
        most_people, most_on = int(rec.get("most_people") or 0), int(rec.get("most") or 0)
        held = False
        if most_people:
            cap = max(usual, _scaled(usual, lift), most_people)
            if int(dn.get("distinct") or 0) > cap:
                dn["distinct"], held = cap, True
        if most_on and dn.get("half"):
            cap_on = max(_scaled(int(rec.get("peak") or 0), lift), most_on)
            if max(dn["half"].values()) > cap_on:
                dn["half"] = {t: min(int(n), cap_on) for t, n in dn["half"].items()}
                dn["peak"] = max(dn["half"].values())
                held = True
        if held:
            dn["reason"] = ((dn.get("reason") or "") + f"; held to the most this meal has run here "
                            f"({most_people or most_on}) or its usual crew with the date's demand")


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
    used to tell the model to add a bartender to a lunch with no bar. A
    closing rule binds the daypart the day's close falls in
    (closing_part): asked with the closing shift "not known", it was
    printed on every morning row too — a closing bartender at lunch, the
    rule said twice a day."""
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
                       staffing_model: dict = None, concurrent: dict = None, half_curve: dict = None,
                       elasticity: dict = None, learned: dict = None) -> list:
    """One entry per date × daypart that needs anybody.

    Each role's number is the larger of the owner's floor and what this
    restaurant typically runs on that weekday and daypart — that usual crew
    scaled by the date's measured demand (date_demand) and held to the
    sales-per-labor-hour target (splh_hold), inside DEMAND_SCALE_BOUNDS;
    the floor is kept alongside so the prompt can mark which part is a hard
    minimum. Role names match case-insensitively and the owner's spelling
    wins. Every row carries `reasons` [str] — why its numbers moved off the
    usual — and `factor`. The number used to be the usual crew whatever the
    date: a +30% measured event Friday was asked for a typical Friday's
    people, and the demand changed only the label.

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
                the cap.
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
                or the reviews' +1); `firm` False keeps it out of `firm`, so
                the score never marks a shift short of a soft ask.
    late_headcount — {weekday: {role: people}} on the floor from 10pm
                (labor.historical_patterns' `late_headcount`): on a date that
                closes past 11pm a `late` row follows the night's, scaled by
                the same demand.
    standard_needs — {(date, daypart): {role lower: {"people", "reason"}}}:
                the owner's own labor standard for a role (labor_standards),
                which replaces the usual crew for it on that shift.
    role_windows / role_caps — the owner's RESTAURANT HOURS & SHIFT RULES
                read by the code (schedule_rules.apply_hours_rules,
                Constraints.limit_maps): {role or family lower: {weekday:
                {"earliest", "latest"}}} and {role or family lower: {weekday:
                {daypart | "any": most}}}. A role is asked for nothing
                outside its hours (only a floor stands there) and never more
                than its ceiling. The usual crew from punches used to ask for
                an evening-only bartender at Wednesday lunch.
    staffing_model — staffing_model.measure(): where a role's family has a
                labor standard (the owner's, or measured at the rush and
                shrunk toward a prior), its number is the forecast work of
                the meal at each half hour ÷ that standard, plus the meal's
                fixed crew — people on at once, mapped to different people by
                the restaurant's own stagger — never the usual crew x a
                ratio. Without a standard the usual crew stands, moved by the
                meal's and the role's own demand. Its `lags` shift each role
                along the curve.
    concurrent — the staffing baseline's people on at once ({(weekday,
                daypart): {role: {"peak"}}}, contract #2): each role's
                half-hour peak is its usual most on at once, demand-scaled —
                no longer 0.75 x its different people.
    half_curve — {weekday: {minute: share}}, the curve measured at the half
                hour from the ticket archive (schedule_economics.
                measured_half_hours), used over the hourly one.
    elasticity — {(role family, daypart): {"move": +1 | -1, "reason",
                "target", "weekdays"}} (staffing_elasticity.moves): one person
                more or fewer where what one more person is worth here was
                measured with an interval clear of zero — never past `target`
                (the level it was measured above plus the move) and only on
                `weekdays` when given (-7); entered in the ledger like every
                lever, and held by the arbiter where the manager's own learned
                level already moved the role.
    learned — {(weekday, daypart): {role: {"delta", "settled", "base",
                "applied"}}} (labor.apply_learned_to_patterns): what the
                manager's learned headcount added to the usual crew. It is
                taken back out of the baseline and applied as a LEVEL after
                the driver, the standard and the demand — headcount_to_apply
                on the figure the table reached — so a labor standard's
                stagger never amplifies it (settled 5 read as required 8).
    Each row also carries `half_need` — {minute: {role: people on}}, every
    half hour's full need after the caps — the one the score judges
    (half_hours_map).

    THE LEDGER: every lever that moves a role's number — the usual crew, the
    date's demand, the labor standard (driver or the owner's), the manager's
    learned level, the measured elasticity, the owner's floors and minimums,
    the asks (events, reviews, reports), the owner's hours and ceilings —
    proposes its move here, in this one function, and each role entry
    carries `ledger`: [{lever, delta, to, evidence, provenance, applied,
    held_by}] in the order applied. One arbiter rule holds across them: an
    automatic lever (elasticity) never moves a slot the manager's own
    learned level already moved the same way (the two used to stack), and
    explain_role() says why a shift asks for what it asks.
    """
    import staffing_curve as _curve
    from shift_quality import shift_role_requirements
    skip = set(skip_dates or ())
    # Where each role works, by the same requirements the scorer reads: a
    # leader rule naming no daypart is asked only there.
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
            # The usual crew less what the manager's learned headcount added
            # to it: that comes back as a level at the end.
            learned_here = {str(k).strip().lower(): v for k, v in ((learned or {}).get((day, part)) or {}).items()
                            if isinstance(v, dict)}

            def _applied(r):
                return int((learned_here.get(r.strip().lower()) or {}).get("applied") or 0)
            typical_raw = {r: max(0, int(n or 0) - _applied(r)) for r, n in raw_by_part[part].items()}
            typical_raw = {r: n for r, n in typical_raw.items() if n > 0 or not _applied(r)}
            ledger = {}

            def _log(role_, lever, before, after, evidence="", provenance="", applied=True, held_by=None):
                ledger.setdefault(role_.strip().lower(), []).append(
                    {"lever": lever, "delta": int(after) - int(before), "to": int(after), "evidence": evidence,
                     "provenance": provenance, "applied": applied, "held_by": held_by})
            hold = ((splh_hold or {}).get(day) or {}).get(part)
            # The meal's own demand and each role's: a dinner game never lifts
            # lunch, a bar night never the dish pit.
            busy_here = _sq.DEMAND_RANK.get(profile_here.demand, 1) >= _sq.DEMAND_RANK[_sq.HARD_DEMAND]
            p_ratio, role_ratios, measured, why_part = part_demand(dd, part, busy_here)
            factor = demand_factor(p_ratio, hold, measured)

            def _role_factor(r):
                rr = role_ratios.get(r.strip().lower())
                return demand_factor(rr, hold, measured) if rr is not None else factor
            typical_here = {r: (_scaled(n, _role_factor(r)) if _role_factor(r) != 1.0 else int(n or 0))
                            for r, n in typical_raw.items()}
            for r, n in typical_raw.items():
                _log(r, "usual", 0, int(n or 0), "what this restaurant usually runs on this weekday and meal",
                     "history")
                if typical_here[r] != int(n or 0):
                    _log(r, "demand", int(n or 0), typical_here[r],
                         "; ".join(why_date + [w for w in why_part if w not in why_date]) or
                         f"this date's demand x{_role_factor(r):.2f}", "forecast")
            # Driver-based: a family with a labor standard is asked for the
            # work it will carry, not its habit. The hold is not applied —
            # the budget is priced in dollars elsewhere.
            driven = {}
            if staffing_model and typical_raw:
                import staffing_model as _sm
                lo_d, hi_d = _service_window(day, part, open_times, close_times)
                ratios_d = {r: demand_factor(role_ratios.get(r.strip().lower(), p_ratio), 1.0, True)
                            for r in typical_raw}
                # The stagger reads the usual crew as it was before the
                # learned headcount (people on at once taken back in step).
                conc_base = {}
                for k_c, v_c in ((concurrent or {}).get((day, part)) or {}).items():
                    if isinstance(v_c, dict) and v_c.get("peak") is not None:
                        conc_base[k_c] = dict(v_c, peak=max(0, int(v_c["peak"] or 0) - _applied(str(k_c))))
                    else:
                        conc_base[k_c] = v_c
                driven = _sm.needs(staffing_model, day, part, ratios_d, typical_raw, conc_base, window=(lo_d, hi_d))
                _hold_to_record(driven, conc_base, ratios_d, typical_raw)
                for r, dn in driven.items():
                    _log(r, "driver", typical_here.get(r, 0), int(dn["distinct"]), dn.get("reason") or "",
                         "yours" if dn.get("source") == "yours" else "measured")
                    typical_here[r] = int(dn["distinct"])
            standard_here = (standard_needs or {}).get((d, part)) or {}
            for r in list(typical_here):
                std = standard_here.get(r.strip().lower())
                if std and std.get("people") is not None and r not in driven:
                    _log(r, "standard", typical_here[r], int(std["people"]), str(std.get("reason") or ""), "owner")
                    typical_here[r] = int(std["people"])
            # The manager's learned headcount, as a LEVEL on the figure the
            # table reached: only while that figure is short of (an add) or
            # over (a cut) the level they settled on.
            _h2a = headcount_to_apply
            learned_moved = {}
            for key_l, e_l in learned_here.items():
                r_l = next((r for r in typical_here if r.strip().lower() == key_l), None)
                if r_l is None:
                    disp = next((r for r in raw_by_part[part] if r.strip().lower() == key_l), key_l)
                    r_l = disp
                    typical_here[r_l] = 0
                now_l = int(typical_here[r_l] or 0)
                step_l = _h2a(now_l, e_l.get("delta"), e_l.get("settled"))
                settled_l = e_l.get("settled")
                ev_l = (f"the manager has {'added' if (e_l.get('delta') or 0) > 0 else 'cut'} "
                        f"{abs(int(round(float(e_l.get('delta') or 0))))} by hand"
                        + (f", settling on {float(settled_l):g}" if settled_l is not None else ""))
                if step_l:
                    _log(r_l, "learned", now_l, now_l + step_l, ev_l, "manager")
                    typical_here[r_l] = max(0, now_l + step_l)
                    learned_moved[key_l] = 1 if step_l > 0 else -1
                elif e_l.get("delta"):
                    _log(r_l, "learned", now_l, now_l, ev_l, "manager", applied=False,
                         held_by="the table already asks for the level the manager settled on")
                if not typical_here[r_l]:
                    typical_here.pop(r_l, None)
            # What one more (or one fewer) of a role on this meal measured
            # here, where its interval cleared zero (staffing_elasticity):
            # one person, on the role of the family the restaurant runs most
            # of; never the last of a role, and the owner's floors still
            # hold over it.
            moved_by = {}
            if elasticity:
                from intelligence.staffing import family_of as _fo_e
                for (fam_e, part_e), mv in elasticity.items():
                    if part_e != part or not int((mv or {}).get("move") or 0):
                        continue
                    # Only the weekdays at the demand it was measured at
                    # (a move measured on the busy dinners added a person
                    # to all seven).
                    if mv.get("weekdays") is not None and day not in set(mv.get("weekdays") or ()):
                        continue
                    cands = [r for r in typical_here if _fo_e(r) == fam_e and int(typical_here[r] or 0) > 0]
                    if not cands:
                        continue
                    r_e = max(cands, key=lambda r: (int(typical_here[r]), r))
                    step = int(mv["move"])
                    if step < 0 and int(typical_here[r_e]) <= 1:
                        continue
                    # The arbiter: an automatic lever never stacks on the
                    # manager's own level moving the same way.
                    if learned_moved.get(r_e.strip().lower()) == (1 if step > 0 else -1):
                        _log(r_e, "elasticity", typical_here[r_e], typical_here[r_e], str(mv.get("reason") or ""),
                             "measured", applied=False, held_by="the manager's own learned level already moved it")
                        continue
                    # A target LEVEL (like
                    # schedule_learning.headcount_to_apply): the level one
                    # more was measured above plus the move — a baseline
                    # that already absorbed it is not moved again.
                    if mv.get("target") is not None:
                        gap = int(mv["target"]) - int(typical_here[r_e])
                        step = max(0, min(step, gap)) if step > 0 else min(0, max(step, gap))
                    if not step:
                        _log(r_e, "elasticity", typical_here[r_e], typical_here[r_e], str(mv.get("reason") or ""),
                             "measured", applied=False,
                             held_by=f"already at the level it was measured for ({int(mv['target'])})")
                        continue
                    _log(r_e, "elasticity", typical_here[r_e], int(typical_here[r_e]) + step,
                         str(mv.get("reason") or ""), "measured")
                    typical_here[r_e] = int(typical_here[r_e]) + step
                    moved_by[r_e.strip().lower()] = dict(mv, move=step)
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
                if key in moved_by and moved_by[key].get("reason"):
                    why.append(f"{int(moved_by[key]['move']):+d}: {moved_by[key]['reason']}")
                lv = next((e for e in reversed(ledger.get(key) or []) if e["lever"] == "learned" and e["applied"]),
                          None)
                if lv:
                    why.append(f"{lv['delta']:+d}: {lv['evidence']}")
                had = max([int(v) for r2, v in typical_here.items() if r2.strip().lower() == key] or [0])
                if int(n) != had:
                    _log(role, {"your staffing floors": "floor", "your role minimums": "minimum",
                                "the shift profile": "critical"}.get(str(_src), "minimum"), had, int(n),
                         str(_src), "owner")
                need[key] = [role.strip(), int(n), floor, typ, 0, why]
            # Asks folded in: +1 of a role, with its reason.
            for key, word, a in _adjust_for(adjustments, d, part, need, raw_by_part):
                try:
                    delta = int(a["delta"]) if a.get("delta") is not None else 1
                except (TypeError, ValueError):
                    delta = 1
                if key is None:
                    key = word
                    need[key] = [str(a.get("role_display") or a.get("role") or word).strip().title(), 0, 0, 0, 0, []]
                v = need[key]
                _log(v[0], "ask", v[1], v[1] + delta, str(a.get("reason") or ""), str(a.get("source") or "ask"))
                v[1] += delta
                if not a.get("firm", False):
                    v[4] += delta
                # A position that is one of the usual crew
                # (schedule_dedicated): no +1 on top, but never fewer than it.
                try:
                    at_least = int(a.get("at_least") or 0)
                except (TypeError, ValueError):
                    at_least = 0
                if at_least and v[1] < at_least:
                    v[1] = at_least
                if a.get("reason"):
                    v[5].append(f"{delta:+d} {a['reason']}" if delta else str(a["reason"]))
            # Held to the owner's hours and ceilings by role.
            _before_lim = {k: v[1] for k, v in need.items()}
            _apply_limits(need, day, part, role_windows, role_caps)
            for k, v in need.items():
                if v[1] != _before_lim.get(k, v[1]):
                    _log(v[0], "cap", _before_lim[k], v[1], "your hours & shift rules for the role", "owner")
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
            # wrong" when it was not. The cap holds over the half-hour needs
            # below, which ARE people on at once, and the sweep's
            # over_section_cap holds the draft.
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
                # its curve shifted by its family's measured lead or lag. A
                # role with a labor standard brings its own.
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
                # it.
                leader.append(f"a manager, somebody scoring {profile.leader_min_score:g}+ or "
                              "somebody authorized to close")
            roles_out = []
            lent = (borrowed or {}).get((day, part)) or set()
            for _k, (name, required, floor, typical, asked, why) in sorted(need.items(), key=lambda kv: (-kv[1][1], kv[1][0].lower())):
                entry = {"role": name, "required": required, "floor": floor, "typical": typical}
                if ledger.get(_k):
                    entry["ledger"] = ledger[_k]
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
                   # Whether that level rests on anything: the restaurant's
                   # own sales, the owner's word for the date, a profile the
                   # owner configured, or the date's measured demand — never
                   # the built-in guess alone.
                   "demand_measured": _demand_measured(profile, measured),
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


LEVER_WORDS = {"usual": "usual", "demand": "demand", "driver": "the work at the rush", "standard": "your standard",
               "learned": "the manager's own level", "elasticity": "measured value of one more",
               "floor": "your floor", "minimum": "your minimum", "critical": "the shift profile",
               "ask": "asked", "cap": "your ceiling"}


def explain_role(entry: dict) -> str:
    """The owner's sentence for why one role on one shift asks for what it
    asks, from its ledger: "8: usual 6; demand +1 (game night); the
    manager's own level +1 (added 1 by hand, settling on 8)". The
    requirement's own number with no ledger."""
    entry = entry or {}
    parts = []
    for e in entry.get("ledger") or []:
        word = LEVER_WORDS.get(e.get("lever"), e.get("lever"))
        if e.get("lever") == "usual":
            parts.append(f"usual {e['to']}")
            continue
        if not e.get("applied"):
            parts.append(f"{word} not applied ({e.get('held_by')})")
            continue
        if e.get("delta"):
            parts.append(f"{word} {e['delta']:+d}" + (f" ({e['evidence']})" if e.get("evidence") else ""))
    return f"{entry.get('required')}: " + "; ".join(parts) if parts else str(entry.get("required"))


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
    """The late segment's row: from LATE_WINDOW_START to close, on a date
    whose close is past LATE_CLOSE_AFTER — its own usual crew from the
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
        # [display, required, floor] for the shared limits.
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
    judges a draft against, the same ones the model is given. `firm` leaves
    a soft ask out: it is asked for, never marked short."""
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
    every half hour against, the same numbers the passes fill to (the
    scorer used to rebuild them as 0.75 x the requirement's different
    people). Keys read back from JSON as strings are turned back into
    minutes."""
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
    # midnight (schedule_time.close_on_timeline).
    open_m, close_m = _stime.day_hours((open_times or {}).get(day), (close_times or {}).get(day))
    if part == "morning":
        return (open_m if open_m is not None and open_m < split else 0), split
    return split, (close_m if close_m is not None and close_m > split else 24 * 60)


# The profile sources that say a shift's demand level was measured or stated
# (shift_quality.profile_for_shift): the restaurant's sales by weekday, the
# owner's lift for a date.
MEASURED_DEMAND_SOURCES = ("your sales history", "what you told us about this date")


def _demand_measured(profile, measured=False) -> bool:
    """Whether a shift's demand level rests on a measurement or the owner's
    own word: the date's measured demand (date_demand), a profile levelled by
    the restaurant's sales or the owner's lift, or a profile the owner set
    up — not the built-in profile's default level."""
    if measured:
        return True
    if getattr(profile, "source", "default") in MEASURED_DEMAND_SOURCES:
        return True
    return not bool(getattr(profile, "builtin", True))


def requirements_block(rows: list, unread_rules: bool = False) -> str:
    """The SHIFT REQUIREMENTS table: one line per shift. How coverage is
    counted, the half-hour ramp and the late-night line are standing
    instructions (schedule_prompt, the same on every call); the table is
    this request's numbers and why they moved, each said once. It carries
    the owner's floors, hours and ceilings the code read; `unread_rules` —
    a line of the owner's RESTAURANT HOURS & SHIFT RULES it could not read
    — says that line wins over the table, never that the table already
    holds it."""
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
            # A level nothing measured is not said as one: it sat beside "NO
            # DEMAND HISTORY — do not describe any day as busy, slow, peak" as
            # "peak demand", from the built-in profile's guess.
            bits.append(f"{r['demand']} demand" if r.get("demand_measured", True) else "demand not measured")
        if r.get("leader"):
            bits.append("leader: " + "; ".join(r["leader"]))
        if r.get("half_hours"):
            from staffing_curve import ramp_text
            bits.append("by the half hour: " + ramp_text(r["half_hours"]))
        # Why a number moved off the usual crew (the date's measured demand,
        # the sales-per-labor-hour hold, the section cap) — said once, here,
        # in the row it moved.
        if r.get("reasons"):
            bits.append("why: " + "; ".join(_model_words(x) for x in r["reasons"]))
        lines.append(" | ".join(bits))
    return ("\n\nSHIFT REQUIREMENTS — priority 2. One line per shift of this request: the people each role needs on "
            "it (\"(floor N)\" the owner's hard minimum inside the number, \"(borrowed)\" a figure lent by similar "
            "restaurants, \"(usual N)\" what the number was before this date's demand moved it), the date's hours "
            "target, the demand level the shift is scored at (\"demand not measured\" where no sales or owner's word "
            "sets one), who it needs to run it, its half-hour numbers and why "
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
    # schedule_time.present_dayparts in words: presence decides, the start
    # only when a shift covers neither core window.
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
    carried {payroll week: hours already published}}}), what they still need
    to reach their minimum and the room left before overtime in each payroll
    week the week touches (`payroll_weeks` {date: payroll week}: the seam
    said "Xh so far" and nothing about what that left).

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
    prompt writes them, weekday and ISO."""
    named = [f"{_day_name(d, d)[:3]} {d}" for d in sorted(set(dates or []))]
    if len(named) <= 1:
        return "".join(named)
    return ", ".join(named[:-1]) + " and " + named[-1]


def focus_block(focus: list, dates: list = None) -> str:
    """What the previous draft of these days was scored weak on, named, so
    a regeneration of chosen dates fixes those things rather than
    reshuffling at random — with the dates it is about (the header said
    "THESE DAYS" and never named them). The owner's own reason for a redo
    is their `instruction` (labor), said once at priority 5, never here as
    well."""
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


_NOTE_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


_NOTE_MORNING_RE = re.compile(r"\b(?:morning|brunch|breakfast|lunch|opening|opener|day\s*shift|am)\b", re.I)


_NOTE_NIGHT_RE = re.compile(r"\b(?:dinner|night|evening|close|closing|closer|late|pm)\b", re.I)


def note_floors(bullet, role_floors=None, role_minimums=None) -> dict:
    """{role: floor} for the day and daypart one note bullet talks about,
    from the owner's floors (schedule_rules.role_floors: per role, per
    daypart, per-day overrides) and whole-day role minimums — the engine's
    A2 reads {role: n}. A bullet naming no day or daypart gets the smallest
    floor over the slots it could mean, so only a cut that is below every
    one of them is called unsafe. The order is schedule_rules.cut_floor's:
    the owner's floor first, the admin minimum only for a role the owner
    set none for. A role in neither is left out — the engine holds it to
    policy["cut_floor_default"], the restaurant's own default."""
    text = str(bullet or "")
    days = [d for d in _NOTE_DAYS if re.search(r"\b" + d + r"s?\b", text, re.I)] or list(_NOTE_DAYS)
    parts = [p for p, pat in (("morning", _NOTE_MORNING_RE), ("night", _NOTE_NIGHT_RE)) if pat.search(text)] \
        or ["morning", "night"]
    out = {}
    for role, spec in (role_floors or {}).items():
        if not isinstance(spec, dict):
            continue
        vals = [floor_for(spec, d, p) for d in days for p in parts]
        vals = [v for v in vals if v > 0]
        if vals:
            out[str(role)] = min(vals)
    for role, n in (role_minimums or {}).items():
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if n > 0 and not any(r.strip().lower() == str(role).strip().lower() for r in out):
            out[str(role)] = n
    return out


def _role_minimums_dict(raw) -> dict:
    """restaurants.role_minimums_json as {role: people}, or {}."""
    import json as _j
    try:
        data = _j.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
    except Exception:
        return {}
    out = {}
    for role, n in (data or {}).items() if isinstance(data, dict) else []:
        try:
            if int(n) > 0:
                out[str(role)] = int(n)
        except (TypeError, ValueError):
            continue
    return out


def headcount_to_apply(base, delta, settled) -> int:
    """How many of a learned headcount `delta` a requirement whose own
    baseline is `base` still needs: an add only while the baseline is below
    the level the manager settled on — never past it — and a cut only while
    it is above. With no settled level read, the delta."""
    try:
        base, delta = int(base or 0), int(round(float(delta or 0)))
    except (TypeError, ValueError):
        return 0
    if settled is None:
        return delta
    target = int(round(float(settled)))
    if delta > 0:
        return max(0, min(delta, target - base))
    if delta < 0:
        return min(0, max(delta, target - base))
    return 0


def _parse_role_minimums(raw):
    """{role: floor} from the settings textarea, or {} when it is junk.

    An owner typing malformed JSON into a settings box must never be the
    reason a schedule fails to generate.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k): int(v) for k, v in (parsed or {}).items() if int(v or 0) > 0}
    except Exception:
        return {}
