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

DAYPARTS = ("morning", "night")
_PART_LABEL = {"morning": "morning", "night": "night"}
_DAY_ORDER = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# The chunk-seam summary treats these as weekend shifts, the same set the
# scorer's fairness dimension counts.
WEEKEND_DAYS = ("Friday", "Saturday", "Sunday")
# Busy shifts when no profile says otherwise: Friday and Saturday night.
DEFAULT_BUSY = (("Friday", "night"), ("Saturday", "night"))
# The usual-pattern block is a courtesy to the model, not a roster dump: a
# 250-person restaurant stops listing after this many characters.
PATTERN_CHAR_CAP = 4000
FOCUS_MAX_ITEMS = 12
FOCUS_MAX_CHARS = 240


def _day_name(date_str: str, fallback: str = "") -> str:
    try:
        return datetime.strptime((date_str or "")[:10], "%Y-%m-%d").strftime("%A")
    except (TypeError, ValueError):
        return fallback


def _minutes(value):
    """Minutes past midnight from "4:00pm", "4pm" or "16:00"; None if unreadable."""
    raw = str(value or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(raw, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    return None


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


def _rule_applies(rule: dict, day: str, part: str) -> bool:
    if not (rule.get("role") or "").strip():
        return False
    days = {x.strip().lower() for x in (rule.get("days") or []) if x}
    if days and day.lower() not in days:
        return False
    rp = (rule.get("daypart") or "").strip().lower()
    return not rp or rp == part


def shift_requirements(dates: list, typical_headcount: dict = None, role_floors: dict = None,
                       daily_targets: dict = None, profiles: list = None,
                       demand_by_day: dict = None, demand_by_date: dict = None,
                       leader_rules: list = None,
                       leadership_known: bool = False, roles: set = None,
                       skip_dates=(), role_minimums: dict = None, borrowed: dict = None) -> list:
    """One entry per date × daypart that needs anybody.

    Each role's number is the larger of the owner's floor and what this
    restaurant typically runs on that weekday and daypart; the floor is kept
    alongside so the prompt can mark which part is a hard minimum. Role
    names match case-insensitively and the owner's spelling wins.

    roles     — lowercase roles the people in this request can work; a role
                nobody here can fill is left to the request that can (a
                roster split by department). None means every role.
    leadership_known — whether anybody is rated or authorised to close. A
                profile's "needs a leader" cannot be judged without one of
                those, so it is not asked of the model either.
    borrowed  — {(weekday, daypart): {lower role}}: typical figures lent by
                similar restaurants (intelligence.staffing) to a restaurant
                with no history of its own; the row says so.
    """
    from shift_quality import shift_role_requirements
    skip = set(skip_dates or ())
    out = []
    for d in dates or []:
        if d in skip:
            continue
        day = _day_name(d)
        if not day:
            continue
        for part in DAYPARTS:
            # The same requirement the coverage score holds the draft to
            # (shift_quality.shift_role_requirements): usual headcount, the
            # owner's floors, their whole-day minimums for roles working this
            # daypart, and the profile's critical positions — the largest of
            # each per role.
            profile_here = shift_profile(day, part, d, profiles, demand_by_day, demand_by_date)
            typical_here = (typical_headcount or {}).get((day, part)) or {}
            floors_here = {}
            for role, spec in (role_floors or {}).items():
                f = floor_for(spec or {}, day, part)
                if f:
                    floors_here[role] = f
            reqs = shift_role_requirements(typical_here, floors_here, role_minimums,
                                           getattr(profile_here, "critical_positions", None))
            need = {}      # lower -> [display, required, floor, typical]
            for role, (n, _src) in reqs.items():
                key = role.strip().lower()
                floor = max([int(v) for r2, v in floors_here.items() if r2.strip().lower() == key] or [0])
                typ = max([int(v or 0) for r2, v in typical_here.items() if r2.strip().lower() == key] or [0])
                need[key] = [role.strip(), int(n), floor, typ]
            if roles is not None:
                need = {k: v for k, v in need.items() if k in roles}
            if not need:
                continue
            profile = shift_profile(day, part, d, profiles, demand_by_day, demand_by_date)
            demand = profile.demand
            leader = []
            for rule in leader_rules or []:
                if not _rule_applies(rule, day, part):
                    continue
                if roles is not None and (rule.get("role") or "").strip().lower() not in roles:
                    continue
                bit = f"{int(rule.get('count') or 1)} {rule['role'].strip()}"
                if rule.get("attribute"):
                    bit += " authorised to close"
                elif rule.get("min_score") is not None:
                    bit += f" scoring {float(rule['min_score']):g}+"
                if rule.get("closing"):
                    bit += " on the closing shift"
                leader.append(bit)
            if profile.requires_leader and leadership_known and not leader:
                leader.append(f"somebody scoring {profile.leader_min_score:g}+ or authorised to close")
            roles_out = []
            lent = (borrowed or {}).get((day, part)) or set()
            for _k, (name, required, floor, typical) in sorted(need.items(), key=lambda kv: (-kv[1][1], kv[1][0].lower())):
                entry = {"role": name, "required": required, "floor": floor, "typical": typical}
                if _k in lent and typical and required == typical and typical > floor:
                    entry["borrowed"] = True
                roles_out.append(entry)
            out.append({"date": d, "day": day, "daypart": part, "roles": roles_out,
                        "target_hours": (daily_targets or {}).get(d), "demand": demand,
                        "leader": leader})
    return out


def requirements_block(rows: list) -> str:
    """The SHIFT REQUIREMENTS table: one line per shift."""
    if not rows:
        return ""
    lines = []
    for r in rows:
        people = ", ".join(f"{x['role']} {x['required']}" + (f" (floor {x['floor']})" if x["floor"] else "")
                           + (" (borrowed)" if x.get("borrowed") else "")
                           for x in r["roles"])
        bits = [f"  {r['day'][:3]} {r['date']} {_PART_LABEL[r['daypart']]} | {people}"]
        if r.get("target_hours"):
            bits.append(f"day target {float(r['target_hours']):g}h")
        bits.append(f"{r['demand']} demand")
        if r.get("leader"):
            bits.append("leader: " + "; ".join(r["leader"]))
        lines.append(" | ".join(bits))
    return ("\n\nSHIFT REQUIREMENTS — priority 2. One line per shift you are writing: the people each role "
            "needs on it, the day's hours target, the demand level the shift is scored at, and who it needs "
            "to run it. Each role's number is the larger of the owner's staffing floor and what this "
            "restaurant typically runs on that weekday and daypart; \"(floor N)\" marks the owner's hard "
            "minimum inside it. Schedule to these numbers — go above one only for a flagged event or a "
            "measured volume spike on that date, and never to use up hours.\n"
            "  Coverage is counted by who is on the floor. " + presence_rule() + " The owner's floors are "
            "also checked half-hour by half-hour across each daypart, so a floor must hold from opening "
            "through close, not only at the peak.\n"
            "  The day target is that day's share of the weekly hours budget: use up to it when the day's "
            "shifts need the hours to meet these numbers, and leave it unspent when they do not — a day "
            "under its target with every shift covered is a good day.\n"
            + "\n".join(lines))


def experience_block(tenure: dict, names: list, leader_flags: dict = None,
                     experienced: set = None) -> str:
    """Who is experienced, who is still developing, who can run a shift.

    Experienced is the scorer's rule: EXPERIENCE_SHIFTS on file, or the
    owner's word for it (staff_settings.experienced_names). Silent when
    nobody on this list qualifies either way: a short history window makes
    everybody look new, and "nobody here is experienced" would be a false
    statement the model would act on — the scorer withdraws the dimension
    in exactly that case."""
    names = [n for n in (names or []) if n]
    out = ""
    ten = tenure or {}
    flagged = {str(n).strip().lower() for n in (experienced or ()) if n}

    def _veteran(n):
        return n.strip().lower() in flagged or int(ten.get(n) or 0) >= _sq.EXPERIENCE_SHIFTS

    veterans = sorted(n for n in names if _veteran(n))
    if veterans:
        developing = sorted(n for n in names if n in ten and not _veteran(n)
                            and int(ten.get(n) or 0) < _sq.DEVELOPING_SHIFTS)
        out += (f"\n\nEXPERIENCED STAFF — {_sq.EXPERIENCE_SHIFTS}+ shifts here, or marked experienced by the owner: "
                + ", ".join(veterans) + ".")
        if developing:
            out += (f"\n  Still developing — under {_sq.DEVELOPING_SHIFTS} shifts here: " + ", ".join(developing)
                    + ". Put each of them on with an experienced hand in the same role, never two of them "
                    "together on a busy shift.")
        out += ("\n  Every shift is scored on its share of experienced hands (the profile's experience mix); "
                "spread the experienced people across shifts rather than stacking them on one.")
    leaders = sorted(n for n in names if (leader_flags or {}).get(n))
    if leaders:
        out += ("\n\nAUTHORISED TO CLOSE — each counts as somebody able to run a shift: "
                + ", ".join(leaders) + ".")
    return out


def _days_label(days: list) -> str:
    ds = [d for d in _DAY_ORDER if d in set(days or [])]
    if len(ds) == 7:
        return "any day"
    return "/".join(d[:3] for d in ds)


def _parts_label(parts: list) -> str:
    ps = sorted({(p or "").strip().lower() for p in parts or [] if p})
    if set(ps) >= {"morning", "night"}:
        return "day or night"
    if ps == ["morning"]:
        return "days"
    if ps == ["night"]:
        return "nights"
    return ""


def usual_pattern_block(prior_pattern: dict, names: list, cap: int = PATTERN_CHAR_CAP) -> str:
    """Each person's usual weekdays and dayparts, people with the same
    pattern on one line. Stability is scored per shift: somebody on a
    weekday AND a daypart they usually work counts as familiar."""
    groups = {}
    for n in names or []:
        p = (prior_pattern or {}).get(n) or {}
        days = _days_label(p.get("days") or [])
        if not days:
            continue
        parts = _parts_label(p.get("dayparts") or [])
        groups.setdefault((days, parts), []).append(n)
    if not groups:
        return ""
    lines, used, dropped = [], 0, 0
    for (days, parts), people in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        line = f"  {days}{', ' + parts if parts else ''}: " + ", ".join(sorted(people))
        if used + len(line) > cap:
            dropped += len(people)
            continue
        lines.append(line)
        used += len(line) + 1
    if dropped:
        lines.append(f"  ({dropped} more people not listed.)")
    return ("\n\nUSUAL PATTERN — the weekdays and dayparts each person has worked here. Schedule stability "
            "is scored: a person on a weekday and daypart they usually work counts as familiar. Keep people "
            "on their pattern unless a higher priority needs the move:\n" + "\n".join(lines))


def _row_parts(row: dict) -> set:
    """The dayparts a row is on the floor for, by the scorer's own rule
    (shift_quality.present_dayparts)."""
    return {p for p in _sq.present_dayparts(row) if p in DAYPARTS}


def _clock(m) -> str:
    h, mm = divmod(int(m), 60)
    return f"{(h % 12) or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def presence_rule() -> str:
    """The daypart-presence rule in words, built from the scorer's own
    constants so the sentence cannot drift from what is counted."""
    lo_m, hi_m = _sq.CORE_WINDOWS["morning"]
    lo_n, hi_n = _sq.CORE_WINDOWS["night"]
    need = _sq.PRESENCE_MIN_OVERLAP
    return (f"A shift counts toward the daypart it starts in (morning before 3:00pm, night from 3:00pm), "
            f"and ALSO toward the other daypart when it covers at least {need} minutes of that daypart's core "
            f"window (lunch {_clock(lo_m)}-{_clock(hi_m)}, dinner {_clock(lo_n)}-{_clock(hi_n)}). So an "
            f"11:30am-7:00pm shift counts at lunch AND at dinner; a 10:00am-5:00pm shift counts at lunch only.")


def seam_lines(prior_rows: list, busy: set = None) -> list:
    """Per person, what the earlier parts of a chunked week already gave
    them: hours, days, their last shift, and the closes, weekend shifts and
    busy shifts that fairness is scored on across the whole week.

    A close is a shift ending within 30 minutes of the latest end that day
    (the rotation ledger's tolerance). busy is {(date, daypart)}; without it
    Friday and Saturday nights count."""
    latest = {}
    for pr in prior_rows or []:
        s, e = _minutes(pr.get("shift_start")), _minutes(pr.get("shift_end"))
        if e is None:
            continue
        if s is not None and e <= s:
            e += 24 * 60
        d = pr.get("date") or ""
        latest[d] = max(latest.get(d, -1), e)
    seen = {}
    for pr in prior_rows or []:
        nm = (pr.get("employee") or "").strip()
        if not nm:
            continue
        e = seen.setdefault(nm, {"hours": 0.0, "days": set(), "last": "", "_k": ("", ""),
                                 "closes": 0, "weekend": 0, "busy": 0})
        try:
            e["hours"] += float(pr.get("scheduled_hours") or 0)
        except (TypeError, ValueError):
            pass
        date = pr.get("date") or ""
        day = pr.get("day") or _day_name(date) or date
        e["days"].add(day[:3])
        key = (date, pr.get("shift_end") or "")
        if key > e["_k"]:
            e["_k"] = key
            e["last"] = f"{pr.get('day') or date} until {pr.get('shift_end')}"
        s, end = _minutes(pr.get("shift_start")), _minutes(pr.get("shift_end"))
        if end is not None:
            if s is not None and end <= s:
                end += 24 * 60
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
    out = []
    for n, e in sorted(seen.items()):
        line = f"  {n}: {e['hours']:g}h so far on {'/'.join(sorted(e['days']))}"
        if e["last"]:
            line += f", last shift {e['last']}"
        line += f"; {e['closes']} close{'' if e['closes'] == 1 else 's'}, {e['weekend']} weekend, {e['busy']} busy"
        out.append(line)
    return out


def focus_block(focus: list) -> str:
    """What the previous draft of these days was scored weak on, named, so
    a regeneration of chosen dates fixes those things rather than
    reshuffling at random."""
    items = []
    for f in focus or []:
        text = " ".join(str(f or "").split())[:FOCUS_MAX_CHARS]
        if text and text not in items:
            items.append(text)
        if len(items) >= FOCUS_MAX_ITEMS:
            break
    if not items:
        return ""
    return ("\n\nTHE PREVIOUS DRAFT OF THESE DAYS SCORED WEAK ON:\n" + "\n".join(f"  * {t}" for t in items)
            + "\n  Fix these specifically in this draft, within the PRIORITIES order — never by breaking "
            "anything ranked above the thing being fixed.")
