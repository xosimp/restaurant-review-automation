"""
schedule_rules.py — the rules a schedule has to obey, held as facts the code
can check rather than sentences the model is asked to remember.

Two halves. The owner's settings: rest between shifts, shift length, minors,
days off, an hours ceiling, and per-role per-daypart staffing floors
(compliance_json and role_floors_json on the restaurant). And the
Constraints object built once per generation from every source the pipeline
used to consult separately — approved and pending time off, weekday and
daypart availability, per-person hours limits, hours already published in
the same payroll week (here and at sibling sites), who is still on the
roster — with one legality check (`can_work`) and one violation sweep the
backstops, the swap search, the review panel and the publish gate all share.
"""
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from models import get_conn, DB_PATH

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

DEFAULTS = {
    "min_rest_hours": 10.0,          # between one shift's end and the next start (a "clopen" is under this)
    "max_shift_hours": 12.0,
    # The shortest shift the owner wants (None = no rule): a shift the code
    # adds is never shorter, and a shorter one is a soft flag. Without it an
    # added shift is as long as the stretch it covers — a 1h manager gap no
    # longer buys a 4h shift (schedule audit 10/3/26 E-16).
    "min_shift_hours": None,
    "daily_ot_hours": None,          # e.g. 8 where daily overtime applies; None = not tracked
    "meal_break_after_hours": None,  # e.g. 5 or 6; None = not tracked
    "minor_latest_end": "10:00pm",
    "minor_max_daily_hours": 8.0,
    # Days off in a row are not a rule (owner, 10/2/26: "owners dont care about
    # people not getting multiple days off in a row"). Off for everyone, a
    # stored value included (DAYS_OFF_RETIRED); the checks below stay dormant.
    "min_consecutive_days_off": None,
    "part_time_days_off": None,
    "weekly_hours_ceiling": 40.0,
    "max_consecutive_days": 6,       # a longer run is a hard breach (the tail of the published week counts)
    "notice_days": None,             # predictive-scheduling notice: a week published with less is held (notice_shortfall)
    # A keyholder (can close, or holds a manager/keyholder certification) is
    # on until close every trading day — enforced whenever anyone on the
    # roster is a keyholder (NS5 M8). The owner can switch it off.
    "keyholder_until_close": True,
}
_BOUNDS = {"min_rest_hours": (0, 24), "max_shift_hours": (4, 24), "min_shift_hours": (1, 12), "daily_ot_hours": (4, 24),
           "meal_break_after_hours": (2, 12), "minor_max_daily_hours": (1, 12),
           "min_consecutive_days_off": (0, 4), "part_time_days_off": (0, 5),
           "weekly_hours_ceiling": (10, 80), "max_consecutive_days": (2, 14), "notice_days": (0, 30)}

# Reasons that mean the person is not really on that shift, so the row must
# not count as coverage. Everything else in HARD is a cost or a rule breach
# with a real person still on the floor.
NO_SHOW = frozenset({"off_roster", "inactive", "outside_week", "double_booked", "overlap",
                     "approved_time_off", "unavailable_day", "unavailable_daypart", "elsewhere"})
NO_SHOW = NO_SHOW | frozenset({"outside_window", "missing_cert", "note_unavailable"})
HARD = NO_SHOW | frozenset({"over_max_hours", "shift_too_long", "rest_gap", "minor_late", "minor_hours", "long_run",
                            "minor_early", "minor_week_hours",
                            "no_manager_on_duty", "coverage_floor", "keyholder_until_close", "nobody_at_close",
                            "no_manager"})
SOFT = frozenset({"days_off", "pending_time_off", "daily_ot", "meal_break", "under_min_hours", "over_section_cap",
                  "ends_before_role_close", "manager_rule_unusable", "minor_age_unknown",
                  "owner_rule", "no_manager_roster", "payroll_tail_full", "shift_too_short"})
# Soft flags that still stop an UNATTENDED publish (auto-publish and the
# delayed run of one): a meal break owed, daily overtime and a time-off
# request nobody answered are things a person decides, not a week to send
# unread (NS5 M7). A person publishing may still send them.
HOLD_UNATTENDED = frozenset({"meal_break", "daily_ot", "pending_time_off"})

LABELS = {
    "off_roster": "not on the staff list", "inactive": "no longer on the roster",
    "outside_week": "date is outside next week", "double_booked": "double-booked at the same start time",
    "overlap": "two shifts overlap", "approved_time_off": "on approved time off",
    "unavailable_day": "marked unavailable that day",
    "note_unavailable": "off by a scheduling note", "unavailable_daypart": "not available for that daypart",
    "elsewhere": "already scheduled at another location", "over_max_hours": "over their hours ceiling for the payroll week",
    "shift_too_long": "shift longer than the maximum", "rest_gap": "not enough rest since their previous shift",
    "minor_late": "a minor working past the latest allowed end", "minor_hours": "a minor over the daily hours limit",
    "days_off": "fewer consecutive days off than the rule", "pending_time_off": "has a time-off request waiting on you",
    "daily_ot": "daily overtime", "meal_break": "long enough to need a meal break",
    "under_min_hours": "under their minimum hours", "over_section_cap": "more servers than sections",
    "long_run": "too many days in a row",
    "outside_window": "outside the hours they can work that day",
    "missing_cert": "missing a certification the role needs",
    "no_manager_on_duty": "no manager or keyholder on the shift",
    "ends_before_role_close": "ends before that role is meant to stay until",
    "manager_rule_unusable": "manager on duty is on, but nobody is a closer or keyholder",
    "minor_early": "a minor starting before the earliest allowed start",
    "minor_week_hours": "a minor over the weekly hours limit",
    "minor_age_unknown": "a minor with no age band set — only the generic minor rule is checked",
    "coverage_floor": "fewer people on than the staffing floor for that role",
    "keyholder_until_close": "no closer on until close",
    "nobody_at_close": "nobody scheduled until close",
    "notice_short": "less notice than the schedule notice rule",
    "owner_rule": "fewer on than a standing rule the owner set",
    "no_manager": "no manager on the floor",
    "no_manager_roster": "nobody on the roster is a manager or owner",
    "payroll_tail_full": "at the overtime line with days of the payroll week still to come",
    "shift_too_short": "shorter than your shortest shift",
}


# ── minors: age bands ──────────────────────────────────────────────────────
#
# One "is a minor" switch with a 10pm / 8h default let a 15-year-old work
# 35 hours in a school week, 4-10pm on school nights and a 6am open with no
# hard flag (NS5 H4). The limits depend on age, so a minor carries an age
# band, and each band has a hard rule table: the US federal floor first
# (FLSA child-labor rules, 29 CFR 570.35 for 14-15 year olds), then any
# stricter values a jurisdiction adds. These are starting values the code
# checks, not legal advice — the screens and the prompt say so.
MINOR_BANDS = ("14-15", "16-17")

# The federal floor. 16-17 year olds have no federal hours or time-of-day
# limits in non-hazardous work, so that band is held to the owner's own
# minor rule (minor_latest_end / minor_max_daily_hours) and nothing more.
FEDERAL_MINOR_RULES = {
    "14-15": {
        "earliest_start": "7:00am",
        "latest_end_school": "7:00pm",        # during the school year
        "latest_end_summer": "9:00pm",        # June 1 through Labor Day
        "max_daily_school_day": 3.0,
        "max_daily_other_day": 8.0,
        "max_weekly_school_week": 18.0,
        "max_weekly_other_week": 40.0,
        "source": "federal child-labor rules for 14-15 year olds",
    },
    "16-17": {"source": "no federal hours limit for 16-17 year olds; your own minor rule applies"},
}

# The jurisdiction hook: {code: {band: {rule: value}}}, merged over the
# federal floor keeping whichever is STRICTER. Deliberately empty: a state
# or city value goes here only once it has been checked against the source,
# never from memory (the product must not state a rule it cannot stand
# behind).
JURISDICTION_MINOR_RULES = {}


def minor_rules(band, jurisdiction=None) -> dict:
    """The hard limits for one age band: the federal floor, tightened by the
    jurisdiction's entry where it has one. {} for an unknown band."""
    base = dict(FEDERAL_MINOR_RULES.get(band) or {})
    if not base:
        return {}
    extra = (JURISDICTION_MINOR_RULES.get((jurisdiction or "").strip().upper()) or {}).get(band) or {}
    for k, v in extra.items():
        if k == "source" or v is None:
            continue
        cur = base.get(k)
        if cur is None:
            base[k] = v
        elif k == "earliest_start":
            if (parse_minutes(v) or 0) > (parse_minutes(cur) or 0):
                base[k] = v
        elif k.startswith("latest_end"):
            if (parse_minutes(v) or 24 * 60) < (parse_minutes(cur) or 24 * 60):
                base[k] = v
        else:
            try:
                base[k] = min(float(cur), float(v))
            except (TypeError, ValueError):
                pass
    if extra:
        base["source"] = base.get("source", "") + f" and the {jurisdiction.strip().upper()} entry"
    return base


def minor_latest(c, key, date_str):
    """(minutes, label): the latest a minor may work on `date_str` — the
    owner's minor rule or the age band's school / summer end, whichever is
    earlier — as minutes past that business date's midnight (a latest in the
    small hours is the night's, past 1440). (None, None) when neither is set."""
    label = c.compliance.get("minor_latest_end")
    latest = parse_minutes(label or "")
    br = minor_rules(c.minor_bands.get(key), c.jurisdiction)
    band_latest = br.get("latest_end_summer" if is_summer(date_str or "") else "latest_end_school")
    if band_latest and (latest is None or parse_minutes(band_latest) < latest):
        latest, label = parse_minutes(band_latest), band_latest
    if latest is None:
        return None, None
    return (latest + 24 * 60 if latest < _OVERNIGHT_LATEST_BEFORE else latest), label


def minor_earliest(c, key):
    """The earliest start a minor's age band allows, in minutes, or None."""
    return parse_minutes(minor_rules(c.minor_bands.get(key), c.jurisdiction).get("earliest_start") or "")


def minor_daily_cap(c, key, date_str):
    """(hours, detail-maker): the most a minor may work on one date, summed
    over every leg of it (E-6) — the owner's minor rule or the age band's
    school-day / other-day cap, whichever is lower. (None, None) when
    neither applies."""
    band = c.minor_bands.get(key)
    br = minor_rules(band, c.jurisdiction)
    school_day = is_school_day(date_str or "")
    band_cap = br.get("max_daily_school_day" if school_day else "max_daily_other_day")
    owner_cap = c.compliance.get("minor_max_daily_hours")
    caps = [x for x in ((float(band_cap), "band") if band_cap else None,
                        (float(owner_cap), "owner") if owner_cap else None) if x]
    if not caps:
        return None, None
    cap, which = min(caps)
    if which == "band":
        return cap, lambda tot: (f"{tot:g}h on a {'school ' if school_day else ''}day, "
                                 f"minors {band} stop at {cap:g}h")
    return cap, lambda tot: f"{tot:g}h on the day, minors stop at {cap:g}h a day"


def _labor_day(year: int):
    from datetime import date as _date
    first = _date(year, 9, 1)
    return first + timedelta(days=(0 - first.weekday()) % 7)


def is_summer(date_str: str) -> bool:
    """June 1 through Labor Day — the federal rule's out-of-school season."""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    return (d.month, d.day) >= (6, 1) and d <= _labor_day(d.year)


def is_school_day(date_str: str) -> bool:
    """Monday to Friday outside the summer season. The product has no
    school calendar, so a weekday in term time is assumed to be a school day
    — the conservative reading: a holiday week is held to the school-week
    limits rather than a school week to the holiday ones."""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    return d.weekday() < 5 and not is_summer(date_str)


def is_school_week(date_str: str) -> bool:
    """Whether the Monday-to-Sunday week holding this date has a school day."""
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return False
    mon = d - timedelta(days=d.weekday())
    return any(is_school_day((mon + timedelta(days=i)).isoformat()) for i in range(5))


# ── notice: predictive scheduling ──────────────────────────────────────────

def _as_date(value):
    from datetime import date as _date
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def notice_shortfall(comp: dict, week_start, today):
    """{"notice_days", "days_given"} when a week published `today` would
    reach staff with less notice than the owner's notice rule, else None.
    The rule was a settings field nothing read (NS5 H2): a 14-day Fair
    Workweek notice set in Cavnar let Friday's auto-publish give three."""
    try:
        need = float((comp or {}).get("notice_days") or 0)
        given = (_as_date(week_start) - _as_date(today)).days
    except (TypeError, ValueError):
        return None
    if not need or given >= need:
        return None
    return {"notice_days": int(need), "days_given": given}


PREDICTABILITY_PAY_WARNING = (
    "{n} changed inside your {days}-day notice window. Where a predictive-scheduling law covers you, "
    "the restaurant may owe the affected staff premium pay for changes like these — check with counsel.")


def late_change_warning(comp: dict, changed_dates, today):
    """The warning an edit to a PUBLISHED week carries when it moves shifts
    inside the notice window (NS5 H2). Worded as a possibility to check,
    never as a sum owed: the product does not model predictability pay."""
    try:
        need = float((comp or {}).get("notice_days") or 0)
        t = _as_date(today)
        inside = sorted({str(d)[:10] for d in (changed_dates or [])
                         if d and (_as_date(d) - t).days < need})
    except (TypeError, ValueError):
        return None
    if not need or not inside:
        return None
    n = f"{len(inside)} day{'s' if len(inside) != 1 else ''} of shifts"
    return PREDICTABILITY_PAY_WARNING.format(n=n[0].upper() + n[1:], days=int(need))


# ── settings ───────────────────────────────────────────────────────────────

def _num(v, lo, hi):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    return max(lo, min(hi, n))


def compliance(restaurant) -> dict:
    """The rules in force for one restaurant: DEFAULTS with the owner's
    overrides. `restaurant` is a Restaurant or an id."""
    if not hasattr(restaurant, "compliance_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    out = dict(DEFAULTS)
    out["manager_on_duty"] = False
    raw = getattr(restaurant, "compliance_json", None) if restaurant else None
    if not raw:
        return _with_pack(out, restaurant, {})
    try:
        data = json.loads(raw) or {}
    except Exception:
        return _with_pack(out, restaurant, {})
    for k, (lo, hi) in _BOUNDS.items():
        if k in data:
            out[k] = _num(data[k], lo, hi) if data[k] not in (None, "", False) else None
    if isinstance(data.get("minor_latest_end"), str) and data["minor_latest_end"].strip():
        out["minor_latest_end"] = data["minor_latest_end"].strip()[:10]
    for k in ("min_consecutive_days_off", "part_time_days_off", "max_consecutive_days"):
        if out.get(k) is not None:
            out[k] = int(out[k])
    for k in DAYS_OFF_RETIRED:
        out[k] = None
    # Superseded by the manager-every-minute rule, which is not a setting
    # (owner, 10/2/26); the old per-daypart switch stays off.
    out["manager_on_duty"] = False
    if "keyholder_until_close" in data:
        out["keyholder_until_close"] = bool(data["keyholder_until_close"])
    return _with_pack(out, restaurant, data)


DAYS_OFF_RETIRED = ("min_consecutive_days_off", "part_time_days_off")


def _with_pack(out: dict, restaurant, owner_set: dict) -> dict:
    """A jurisdiction pack sits UNDER the owner's own values."""
    code = (getattr(restaurant, "jurisdiction", None) or "").strip() if restaurant else ""
    if not code:
        return out
    try:
        import compliance_packs
        out = compliance_packs.apply(out, code, owner_set or {})
    except Exception:
        return out
    for k in DAYS_OFF_RETIRED:
        out[k] = None
    return out


WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_CLOSURE_KEYS = ("closed_weekdays", "closed_dates")


def closures(restaurant) -> dict:
    """{"closed_weekdays": ["Monday"], "closed_dates": ["2026-12-25"]} — the
    days the owner says the restaurant does not trade. Stored beside the
    compliance rules; a restaurant closed Mondays could never generate a
    week before this existed, because every date had to carry shifts."""
    if not hasattr(restaurant, "compliance_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    try:
        data = json.loads(getattr(restaurant, "compliance_json", None) or "{}") or {}
    except Exception:
        data = {}
    days = [d for d in (data.get("closed_weekdays") or []) if d in WEEKDAYS]
    dates = sorted({str(d)[:10] for d in (data.get("closed_dates") or []) if _iso(str(d)[:10])})
    return {"closed_weekdays": days, "closed_dates": dates}


def _iso(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def save_closures(restaurant_id, closed_weekdays=None, closed_dates=None, db_path=DB_PATH) -> dict:
    from models import get_restaurant, update_restaurant
    r = get_restaurant(restaurant_id, db_path)
    try:
        data = json.loads(getattr(r, "compliance_json", None) or "{}") or {}
    except Exception:
        data = {}
    if closed_weekdays is not None:
        wanted = {str(d).strip().capitalize() for d in closed_weekdays or []}
        data["closed_weekdays"] = [d for d in WEEKDAYS if d in wanted]
        if len(data["closed_weekdays"]) == 7:
            raise ValueError("A restaurant closed every day of the week has nothing to schedule.")
    if closed_dates is not None:
        data["closed_dates"] = sorted({str(d).strip()[:10] for d in closed_dates or [] if _iso(str(d).strip()[:10])})[-120:]
    update_restaurant(restaurant_id, {"compliance_json": json.dumps(data) if data else None}, db_path=db_path)
    return closures(get_restaurant(restaurant_id, db_path))


def change_closed_dates(restaurant_id, add=(), remove=(), db_path=DB_PATH) -> dict:
    """Add and remove single closed dates against what is STORED now.

    The Account and Labor pages used to send their whole chip list with Save
    hours / Save rules, so a date added on one and not yet on the other - or
    a date picked but never "Add"-ed, or a chip added without saving - was
    lost or overwritten by the next save (owner, 9/28/26: Erik's two closed
    dates vanished). A change now names the dates it adds or removes and
    nothing else, so no page can write back a list it loaded earlier."""
    from models import get_restaurant
    current = set(closures(get_restaurant(restaurant_id, db_path))["closed_dates"])
    current |= {str(d).strip()[:10] for d in add or () if _iso(str(d).strip()[:10])}
    current -= {str(d).strip()[:10] for d in remove or ()}
    return save_closures(restaurant_id, closed_dates=sorted(current), db_path=db_path)


def closed_in(restaurant, week_dates) -> set:
    cl = closures(restaurant)
    out = set()
    for d in week_dates or []:
        try:
            wd = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (TypeError, ValueError):
            continue
        if wd in cl["closed_weekdays"] or d in cl["closed_dates"]:
            out.add(d)
    return out


# Settings a save that does not send them keeps as stored: newer than the
# rules screens on web and iOS, so an older screen's Save must not clear them.
_KEPT_WHEN_NOT_SENT = ("min_shift_hours",)


def save_compliance(restaurant_id, data: dict, db_path=DB_PATH) -> dict:
    from models import update_restaurant, get_restaurant as _gr
    clean = {}
    # The closures live in the same JSON; saving the rules must keep them.
    try:
        _prev = json.loads(getattr(_gr(restaurant_id, db_path), "compliance_json", None) or "{}") or {}
    except Exception:
        _prev = {}
    for k in _CLOSURE_KEYS:
        if _prev.get(k):
            clean[k] = _prev[k]
    for k, (lo, hi) in _BOUNDS.items():
        if k in (data or {}):
            v = data[k]
            clean[k] = None if v in (None, "", False) else _num(v, lo, hi)
        elif k in _KEPT_WHEN_NOT_SENT and _prev.get(k) is not None:
            # A screen that does not show this setting yet must not wipe it
            # (an owner's edit never vanishes).
            clean[k] = _prev[k]
    if isinstance((data or {}).get("minor_latest_end"), str):
        clean["minor_latest_end"] = data["minor_latest_end"].strip()[:10] or DEFAULTS["minor_latest_end"]
    if "manager_on_duty" in (data or {}):
        clean["manager_on_duty"] = bool(data["manager_on_duty"])
    if "keyholder_until_close" in (data or {}):
        clean["keyholder_until_close"] = bool(data["keyholder_until_close"])
    elif "keyholder_until_close" in _prev:
        clean["keyholder_until_close"] = bool(_prev["keyholder_until_close"])
    update_restaurant(restaurant_id, {"compliance_json": json.dumps(clean) if clean else None}, db_path=db_path)
    from models import get_restaurant
    return compliance(get_restaurant(restaurant_id, db_path))


def role_floors(restaurant) -> dict:
    """{role: {"morning": n, "night": n, "days": {day: {"morning": n, "night": n}}}}
    as the owner set it, with role names kept as typed."""
    if not hasattr(restaurant, "role_floors_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    raw = getattr(restaurant, "role_floors_json", None) if restaurant else None
    if not raw:
        return {}
    try:
        data = json.loads(raw) or {}
    except Exception:
        return {}
    out = {}
    for role, spec in data.items():
        if not isinstance(spec, dict) or not str(role).strip():
            continue
        entry = {"morning": int(_num(spec.get("morning"), 0, 50) or 0), "night": int(_num(spec.get("night"), 0, 50) or 0), "days": {}}
        for day, dspec in (spec.get("days") or {}).items():
            day = str(day).strip().capitalize()
            if day in DAYS and isinstance(dspec, dict):
                entry["days"][day] = {p: int(_num(dspec.get(p), 0, 50) or 0) for p in ("morning", "night") if dspec.get(p) is not None}
        if entry["morning"] or entry["night"] or entry["days"]:
            out[str(role).strip()] = entry
    return out


def save_role_floors(restaurant_id, data: dict, db_path=DB_PATH) -> dict:
    from models import update_restaurant, get_restaurant
    update_restaurant(restaurant_id, {"role_floors_json": json.dumps(data) if data else None}, db_path=db_path)
    return role_floors(get_restaurant(restaurant_id, db_path))


def floor_for(floors: dict, role: str, day: str, daypart: str) -> int:
    for r, spec in (floors or {}).items():
        if r.strip().lower() == (role or "").strip().lower():
            dspec = (spec.get("days") or {}).get(day) or {}
            if daypart in dspec:
                return int(dspec[daypart])
            return int(spec.get(daypart) or 0)
    return 0


# ── the floor a cut suggestion never goes below ────────────────────────────
# A cut (the pre-dinner pulse's "let X go", a model's "cut to one cook")
# needs a floor for EVERY role, not only the ones the owner set: with none
# set, the pulse used to treat one person as the floor and suggested sending
# home one of two cooks. restaurants.cut_floor_default is the restaurant's
# own answer for a role with no floor; the engine's CUT_FLOOR_DEFAULT (2)
# stands in when it is missing or unreadable. This is a floor for CUTS
# only — schedule building and the coverage_floor violation read the
# owner's role floors alone, so a published week with one bartender does
# not start raising violations.
CUT_FLOOR_MAX = 10


def clean_cut_floor_default(value):
    """An owner's "never cut below N" as an int 1..CUT_FLOOR_MAX, or None
    when it is not a whole number (the route answers 400 on None)."""
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n != n or n != int(n):
        return None
    return min(max(int(n), 1), CUT_FLOOR_MAX)


def cut_floor_default(restaurant) -> int:
    """restaurants.cut_floor_default clamped to 1..CUT_FLOOR_MAX; the
    engine's CUT_FLOOR_DEFAULT (2) when it is missing or unreadable."""
    from response_validation import CUT_FLOOR_DEFAULT
    n = clean_cut_floor_default(getattr(restaurant, "cut_floor_default", None) if restaurant else None)
    return n if n is not None else CUT_FLOOR_DEFAULT


def role_minimums(restaurant) -> dict:
    """restaurants.role_minimums_json (Will's admin whole-day override) as
    {role: people}, or {} when it is empty or junk."""
    from labor import _role_minimums_dict
    return _role_minimums_dict(getattr(restaurant, "role_minimums_json", None) if restaurant else None)


def cut_floor(restaurant, role: str, day: str, daypart: str, floors: dict = None, minimums: dict = None) -> int:
    """The fewest people a cut suggestion may leave in `role` on `day`'s
    `daypart`: the first non-zero of the owner's role floor for that slot
    (role_floors / floor_for), the admin role_minimums_json entry for the
    role (case-insensitive, as floor_for matches), then the restaurant's
    cut_floor_default. Never less than 1. For cuts only — see above."""
    if floors is None:
        floors = role_floors(restaurant) if restaurant is not None else {}
    n = floor_for(floors, role, day, daypart)
    if n > 0:
        return n
    key = (role or "").strip().lower()
    mins = role_minimums(restaurant) if minimums is None else minimums
    for r, v in (mins or {}).items():
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0 and str(r).strip().lower() == key:
            return v
    return max(1, cut_floor_default(restaurant))


def effective_role_floors(restaurant, day=None, db_path=DB_PATH) -> dict:
    """role_floors(restaurant) raised by the owner's staffing rules
    (apply_owner_rules) and the Studio note rules in force for the week
    containing `day` (default: the restaurant's today) — the floors a draft
    is built and checked to. Every cut surface (Ask's A2 check, the weekly
    plan, the pre-dinner pulse) reads these, so a confirmed "keep two cooks
    on Friday lunch" is never advised away (blind audit, 10/1/26). Falls
    back to the plain floors on any error."""
    if not hasattr(restaurant, "role_floors_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    base = role_floors(restaurant)
    try:
        from datetime import date as _date, timedelta as _td
        if day is None:
            from time_utils import restaurant_now
            day = restaurant_now(restaurant, naive=True).date()
        day = day if isinstance(day, _date) else _date.fromisoformat(str(day)[:10])
        mon = day - _td(days=day.weekday())
        c = Constraints(restaurant_id=restaurant.id, week_dates=[(mon + _td(days=i)).isoformat() for i in range(7)],
                        week_days=list(DAYS))
        c.role_floors = json.loads(json.dumps(base))
        c.open_times = _load_json(getattr(restaurant, "open_times_json", None), {})
        try:
            from models import get_close_times
            c.close_times = get_close_times(restaurant.id, db_path) or {}
        except Exception:
            pass
        try:
            apply_owner_rules(c, restaurant.id, db_path=db_path)
        except Exception:
            pass
        import schedule_note_rules
        schedule_note_rules.apply_note_rules(c, restaurant.id, db_path=db_path)
        return c.role_floors
    except Exception:
        return base


def effective_floors_ahead(restaurant, day=None, db_path=DB_PATH) -> dict:
    """effective_role_floors for this week and next, the higher of the two
    per role, day and daypart — a cut or a plan item may be about either
    week, and a one-week note rule for the week being planned must hold."""
    from datetime import date as _date, timedelta as _td
    if not hasattr(restaurant, "role_floors_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    try:
        if day is None:
            from time_utils import restaurant_now
            day = restaurant_now(restaurant, naive=True).date()
        day = day if isinstance(day, _date) else _date.fromisoformat(str(day)[:10])
    except Exception:
        day = _date.today()
    a = effective_role_floors(restaurant, day, db_path=db_path)
    b = effective_role_floors(restaurant, day + _td(days=7), db_path=db_path)
    out = {}
    for role in set(a) | set(b):
        spec = {"morning": 0, "night": 0, "days": {}}
        for d in DAYS:
            for part in ("morning", "night"):
                v = max(floor_for(a, role, d, part), floor_for(b, role, d, part))
                if v:
                    spec["days"].setdefault(d, {})[part] = v
        out[role] = spec
    return out


def cut_policy(restaurant, text: str = "") -> dict:
    """The Response Validation Layer's A2 inputs for one restaurant, as
    ValidationContext.policy keys: {"role_floors": {role: floor},
    "cut_floor_default": n}. The floors are the ones the text's day and
    daypart could mean (labor.note_floors: the smallest over those slots,
    owner floor before admin minimum); a role in neither gets the default.
    Takes a Restaurant or an id. {} when there is no restaurant or it cannot
    be read, so the engine's own CUT_FLOOR_DEFAULT stands — a validation
    context is never lost to this."""
    try:
        if restaurant is not None and not hasattr(restaurant, "role_floors_json"):
            from models import get_restaurant
            restaurant = get_restaurant(restaurant)
        if restaurant is None:
            return {}
        from labor import note_floors
        spec = effective_floors_ahead(restaurant)
        mins = role_minimums(restaurant)
        # role_floors: for a caller's own text (the smallest a vague one
        # could mean); role_floor_spec + role_minimums: the A2 check reads
        # each sentence's own day and daypart off them, so "cut Friday lunch
        # to 1 line cook" meets Friday lunch's floor, note rules included
        # (blind re-audit, 10/2/26).
        return {"role_floors": note_floors(text, spec, mins), "role_floor_spec": spec, "role_minimums": mins,
                "cut_floor_default": cut_floor_default(restaurant)}
    except Exception:
        return {}


# ── time helpers ───────────────────────────────────────────────────────────

def parse_minutes(t: str):
    """'9:30pm' / '21:30' → minutes past midnight, or None."""
    raw = (t or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            d = datetime.strptime(raw, fmt)
            return d.hour * 60 + d.minute
        except ValueError:
            continue
    return None


def _night_offset(start_m) -> int:
    """1440 when a start is in the small hours of its row's night: a row's
    date is its BUSINESS date, and a start before the business day's first
    hour (time_utils.BUSINESS_DAY_START_HOUR) is after that night's midnight
    — a 12:30am porter dated Friday works Saturday 00:30, after Friday's
    close. It was read as the start of Friday, so the porter's own Thursday
    close looked like an overlap and the row opened a false manager gap at
    dawn (schedule audit 10/3/26 E-32). The same reading as the no-show
    watch (intraday.coverage_gaps, staff_comms._due)."""
    from time_utils import BUSINESS_DAY_START_HOUR
    return 24 * 60 if start_m is not None and start_m < BUSINESS_DAY_START_HOUR * 60 else 0


def start_minutes(row):
    """A row's start as minutes past its own (business) date's midnight —
    past 1440 for a start in the small hours of its night (E-32)."""
    s = parse_minutes(row.get("shift_start", ""))
    return None if s is None else s + _night_offset(s)


def shift_span(row, tz=None) -> tuple:
    """(start_dt, end_dt) as datetimes on the row's date; an end before the
    start crosses midnight, and a start before the business day's first hour
    is that night's small hours (E-32, _night_offset). (None, None) when
    unreadable.

    With `tz` (the restaurant's IANA zone) both ends are the real instants,
    as naive UTC: a close and an open either side of a clock change are an
    hour closer or further apart than the wall clock says, and the rest rule
    is about hours actually off (SCHED-32). Without it, wall-clock time."""
    try:
        base = datetime.strptime(row.get("date", ""), "%Y-%m-%d")
    except (ValueError, TypeError):
        return None, None
    s, e = parse_minutes(row.get("shift_start", "")), parse_minutes(row.get("shift_end", ""))
    if s is None or e is None:
        return None, None
    night = _night_offset(s)
    start = base + timedelta(minutes=s + night)
    end = base + timedelta(minutes=(e if e > s else e + 24 * 60) + night)
    zone = _zone(tz)
    if zone is not None:
        start = start.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
        end = end.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def span_hours(row, tz=None) -> float:
    """A row's length in hours as actually worked: with the restaurant's zone
    the real instants, so a 5pm-2am close on the night the clocks go back
    (Halloween 10/31/26 at Simple EJ's) is 10 hours, not the 9 the wall
    clock reads (schedule audit 10/3/26 E-23). 0.0 when unreadable. Every
    pass that writes a row's times writes its hours from this."""
    s, e = shift_span(row, tz)
    return round((e - s).total_seconds() / 3600, 2) if s and e else 0.0


def hours_text(hours) -> str:
    """Hours as the rows carry them: "9", "7.5", "6.25"."""
    return str(round(float(hours or 0), 2)).rstrip("0").rstrip(".") or "0"


_ZONES = {}


def _zone(tz):
    if not tz:
        return None
    if tz not in _ZONES:
        try:
            from zoneinfo import ZoneInfo
            _ZONES[tz] = ZoneInfo(str(tz))
        except Exception:
            _ZONES[tz] = None
    return _ZONES[tz]


# A "latest" this early with no "earliest" after it is the small hours of
# the next morning ("until 1:00am"), not a one-in-the-morning curfew.
_OVERNIGHT_LATEST_BEFORE = 6 * 60


def window_allows(lo, hi, start_m, end_m) -> tuple:
    """(ok, which) for a shift from start_m to end_m (minutes past
    midnight) against a window lo..hi (either may be None). A window whose
    latest is before its earliest, or a bare latest in the small hours,
    runs past midnight; so does a shift whose end is not after its start
    (SCHED-13). `which` is 'early' or 'late' when refused. Pure — the swap
    index in shift_quality applies the same rule."""
    if start_m is None or end_m is None:
        return True, ""
    # A start in the small hours is that night's (E-32): a 12:30am porter is
    # after a "from 6pm" window opens, not eighteen hours before it, and the
    # window's own small-hours bounds are read in the same night.
    night = _night_offset(start_m)
    if night:
        lo = None if lo is None else (lo + 24 * 60 if lo < _OVERNIGHT_LATEST_BEFORE else lo)
        if hi is not None and (hi < _OVERNIGHT_LATEST_BEFORE or (lo is not None and hi < lo)):
            hi = hi + 24 * 60
    elif hi is not None and ((lo is not None and hi < lo) or (lo is None and hi < _OVERNIGHT_LATEST_BEFORE)):
        hi = hi + 24 * 60
    end = end_m if end_m > start_m else end_m + 24 * 60
    start_m, end = start_m + night, end + night
    if lo is not None and start_m < lo:
        return False, "early"
    if hi is not None and end > hi:
        return False, "late"
    return True, ""


def window_holds(window, day, week_dates=None, today=None) -> bool:
    """Whether a time window ({earliest, latest, from?, until?}, one
    weekday's entry in staff_settings.time_windows) applies this week: one
    with dates ("not before 5pm on Tuesdays until 12/15/26" — employee
    audit M5) holds on this week's `day` inside them; with no week, while
    it has not ended. A window with no dates always holds."""
    f, u = (window or {}).get("from"), (window or {}).get("until")
    if not (f or u):
        return True
    from models import bounds_apply
    dates = [d for d in (week_dates or []) if _weekday_of(d) == day]
    if dates:
        return any(bounds_apply({"from": f, "until": u}, d) for d in dates)
    if week_dates:
        return False
    from datetime import date as _date
    return not u or u >= (today or _date.today()).isoformat()


def _weekday_of(iso) -> str:
    try:
        return datetime.strptime(str(iso)[:10], "%Y-%m-%d").strftime("%A")
    except (TypeError, ValueError):
        return ""


def row_hours(row) -> float:
    try:
        return float(row.get("scheduled_hours") or 0)
    except (TypeError, ValueError):
        s, e = shift_span(row)
        return round((e - s).total_seconds() / 3600, 1) if s and e else 0.0


def _fmt_minutes(m: int) -> str:
    h, mm = divmod(int(m), 60)
    suffix = "am" if h < 12 or h == 24 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{mm:02d}{suffix}"


def daypart_of(shift_start: str) -> str:
    """Morning or night on a 3pm cutoff — and a start in the small hours is
    the night it belongs to, never the next morning (E-32, _night_offset)."""
    m = parse_minutes(shift_start)
    if m is None:
        return "unknown"
    return "night" if m >= 15 * 60 or _night_offset(m) else "morning"


# ── the constraint set ─────────────────────────────────────────────────────

# The most hours code schedules a salaried person for in a week when neither
# they nor the restaurant set a cap (schedule audit 10/3/26 E-17, E-12: the
# manager gap filler loaded salaried managers to 60-84h because their hours
# "cost nothing" — six 11-12 hour days). They owe no overtime, so the 40h
# line is not theirs; this is a person's week, not a pay line.
SALARIED_HOURS_CAP = 55.0
# The hard weekly maximum for a salaried person nobody set a limit for:
# seven 12-hour days. Over SALARIED_HOURS_CAP the owner may put them, as
# Erik and Jim work the floor most of the week (owner, 9/30/26); code never
# does on its own (Constraints.salaried_limit, overtime_line).
SALARIED_HOURS_MAX = 84.0
# A payroll week with this many of its days still to come after the
# schedule week keeps a reserve for them (E-10, Constraints.tail_reserve).
TAIL_RESERVE_MIN_DAYS = 2


@dataclass
class Constraints:
    restaurant_id: int
    week_dates: list
    week_days: list
    compliance: dict = field(default_factory=lambda: dict(DEFAULTS))
    week_start_day: int = 0
    roster_names: list = field(default_factory=list)       # display names, active only
    active: set = field(default_factory=set)               # lowercase
    inactive: set = field(default_factory=set)             # lowercase, deactivated
    blocked_dates: dict = field(default_factory=dict)      # {lower: {date: reason}}
    pending_off: dict = field(default_factory=dict)        # {lower: set(dates)}
    unavailable_days: dict = field(default_factory=dict)   # {lower: set(day)}
    daypart_avail: dict = field(default_factory=dict)      # {lower: {day: any|morning|night|off}}
    hours_limits: dict = field(default_factory=dict)       # {lower: (min, max)}
    salaried: set = field(default_factory=set)             # models.salaried_name_key of salaried people
    stations: dict = field(default_factory=dict)           # kitchen_stations.normalise config, {} when none
    employment: dict = field(default_factory=dict)         # {lower: 'full'|'part'}
    minors: set = field(default_factory=set)
    minor_bands: dict = field(default_factory=dict)        # {lower: "14-15"|"16-17"} (MINOR_BANDS)
    jurisdiction: str = ""                                 # the restaurant's pack code, for JURISDICTION_MINOR_RULES
    base_hours: dict = field(default_factory=dict)         # {lower: {bucket: hours already published}}
    base_rows: dict = field(default_factory=dict)          # {lower: [published rows outside this week]}
    notes: dict = field(default_factory=dict)              # {lower: free-text note}
    time_windows: dict = field(default_factory=dict)       # {lower: {day: (earliest_min, latest_min)}}
    certifications: dict = field(default_factory=dict)     # {lower: set(cert)}
    role_requirements: dict = field(default_factory=dict)  # {role lower: set(cert)}
    keyholders: set = field(default_factory=set)           # lower: can close, or holds a manager/keyholder cert
    # A manager or owner role on the roster, or the manager certification:
    # nobody is on the floor for a minute without one of them (owner,
    # 10/2/26 — the highest rule there is). {lower: their roster role}.
    managers: dict = field(default_factory=dict)
    preferred: dict = field(default_factory=dict)          # {name: {"preferred_dayparts": [...], "desired_hours": n}}
    foh_roles: set = field(default_factory=lambda: {"server"})
    patio_roles: set = field(default_factory=set)
    close_mins: dict = field(default_factory=dict)         # {role lower: minutes after close the role stays until}
    section_cap: int = 0
    role_floors: dict = field(default_factory=dict)
    open_times: dict = field(default_factory=dict)
    close_times: dict = field(default_factory=dict)
    role_buffers: dict = field(default_factory=dict)
    closed_dates: set = field(default_factory=set)         # iso dates the restaurant does not trade this week
    # The owner's standing rules about staffing (owner_memory rules, memory
    # re-audit 9/29/26 PROMPTS-1): parsed ones the code checks (owner_rules;
    # those naming a daypart are also floors, rule_floor_sources says which),
    # and the schedule rules it could not read, which the review names.
    owner_rules: list = field(default_factory=list)
    owner_rules_unchecked: list = field(default_factory=list)
    rule_floor_sources: dict = field(default_factory=dict)  # {(role lower, day, daypart): rule text}
    tz: str = ""                                           # IANA zone: rest is measured in real hours (SCHED-32)
    # ── schedule audit 10/3/26: the facts the fix round added ──────────
    # A per-person setting or a source that could not be read (P-1): one bad
    # time window used to drop every rule for everyone after it, silently.
    # [{name?, source, error}] — the review and the publish gate name each.
    input_problems: list = field(default_factory=list)
    # Somebody standing in as the manager on given dates (E-13: the only
    # manager on vacation): {lower: set(iso dates)}. manages() reads it.
    acting_managers: dict = field(default_factory=dict)
    # The days and hours a person always works (D-5: the owners' and
    # managers' real floor days): {lower: [{"day", "start", "end", "role"}]}.
    standing_shifts: dict = field(default_factory=dict)
    # The restaurant's role families (D-13: "Server AM" and "Server PM" are
    # one role): {role lower: family lower}; family() reads it with
    # shift_quality.role_family as the default.
    role_families: dict = field(default_factory=dict)
    # Roles a person holds beyond their roster role (people.held_roles —
    # trained, promoted; D-15): {lower: set(role lower)}.
    held_roles: dict = field(default_factory=dict)
    # Who closes for which role (D-9: a closer is chosen to close for their
    # role, the last of it to leave): {family lower: set(person lower)}.
    closers_by_role: dict = field(default_factory=dict)
    # People in training (D-16): {lower: {"target_role", "trainer", "until"}}.
    trainees: dict = field(default_factory=dict)
    # A salaried person's weekly cap when they set none of their own (E-17:
    # the gap filler loaded a salaried owner to 66h against an 84h cap).
    # None keeps SALARIED_HOURS_CAP.
    salaried_cap: float = None
    # People on the roster who have not worked in weeks (E-3, D-17):
    # {lower: last worked iso date}. Still on the roster — a row the owner
    # writes for them is legal — but never a candidate for a fill.
    dormant: dict = field(default_factory=dict)
    # A person's scheduling note nobody has confirmed as a rule (D-34):
    # {lower: {"days": set(weekday names), "dates": set(iso), "text": str}}.
    # The model is told it; the fill passes keep off those days.
    note_caution: dict = field(default_factory=dict)

    # ── lookups ────────────────────────────────────────────────────────
    def bucket(self, date_str: str) -> str:
        from labor import _week_key
        try:
            return _week_key(date_str, self.week_start_day)
        except (TypeError, ValueError):
            return ""          # a garbled date is flagged elsewhere; it counts toward no payroll week

    def bucket_tail(self, b: str) -> list:
        """The dates of payroll week `b` (its start date, bucket()) that fall
        after this schedule week: a Wednesday payroll week touched by a Monday
        week runs on through next Monday and Tuesday, which the next draft
        schedules (E-10). [] when the payroll week ends inside this one."""
        if not b or not self.week_dates:
            return []
        try:
            start = datetime.strptime(str(b)[:10], "%Y-%m-%d")
        except (TypeError, ValueError):
            return []
        last = max(self.week_dates)
        days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
        return [d for d in days if d > last]

    def tail_reserve(self, line: float, b: str) -> float:
        """Hours of the overtime line to keep free for payroll week `b`'s
        days after this schedule week — their share of the line (two of
        seven days: 2/7 of 40h), when two or more are still to come (E-10)."""
        tail = self.bucket_tail(b)
        if len(tail) < TAIL_RESERVE_MIN_DAYS or not line:
            return 0.0
        return round(float(line) * len(tail) / 7.0, 2)

    def is_salaried(self, name: str) -> bool:
        """Paid the same whatever the hours (models.salaried_staff) — Erik and
        Jim at Simple EJ's work the floor most of the week (owner, 9/30/26)."""
        return " ".join(str(name or "").lower().split()) in self.salaried

    def salaried_limit(self, name: str) -> float:
        """The most hours code schedules a salaried person for in a week:
        their own maximum, else the restaurant's salaried cap, else
        SALARIED_HOURS_CAP (schedule audit 10/3/26 E-17: the manager gap
        filler loaded a salaried owner to 66h against an 84h line, because
        their hours "cost nothing")."""
        lim = self.hours_limits.get((name or "").strip().lower())
        if lim and lim[1]:
            return float(lim[1])
        if self.salaried_cap:
            return float(self.salaried_cap)
        return SALARIED_HOURS_CAP

    def max_hours(self, name: str) -> float:
        """The weekly hours a person may not pass — the sweep's hard
        over_max_hours. The person's own maximum when the owner set one, even
        above the restaurant's weekly ceiling: that is the owner allowing
        this person the hours, overtime included, and the code, the prompt
        and the review now give one answer (schedule audit 10/3/26 P-12,
        D-18: a cook set to 40-45h was held to 40 by the sweep, 45 by the
        prompt). Otherwise the ceiling. A salaried person has no ceiling: the
        owner's limit for them (their own, else the restaurant's salaried
        cap), else SALARIED_HOURS_MAX — a default the code never schedules
        them past on its own (salaried_limit) but the owner may."""
        lim = self.hours_limits.get((name or "").strip().lower())
        if self.is_salaried(name):
            if lim and lim[1]:
                return float(lim[1])
            return float(self.salaried_cap) if self.salaried_cap else SALARIED_HOURS_MAX
        if lim and lim[1]:
            return float(lim[1])
        return float(self.compliance.get("weekly_hours_ceiling") or DEFAULTS["weekly_hours_ceiling"])

    def min_hours(self, name: str):
        lim = self.hours_limits.get((name or "").strip().lower())
        return float(lim[0]) if lim and lim[0] else None

    def can_work(self, name: str, date_str: str, daypart: str = None) -> tuple:
        """(ok, reason) — the one legality question every pass asks."""
        key = (name or "").strip().lower()
        if not key:
            return False, "no name"
        if key in self.inactive:
            return False, LABELS["inactive"]
        if self.active and key not in self.active:
            return False, LABELS["off_roster"]
        if self.week_dates and date_str not in self.week_dates:
            return False, LABELS["outside_week"]
        blocked = self.blocked_dates.get(key) or {}
        if date_str in blocked:
            return False, blocked[date_str]
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day = ""
        if day and day in (self.unavailable_days.get(key) or set()):
            return False, LABELS["unavailable_day"]
        choice = (self.daypart_avail.get(key) or {}).get(day)
        if choice == "off":
            return False, LABELS["unavailable_day"]
        if daypart and choice in ("morning", "night") and daypart not in ("unknown", choice):
            return False, LABELS["unavailable_daypart"]
        return True, ""

    def manages(self, name: str, date_str: str = None) -> bool:
        """Whether `name` counts as the manager on the floor on `date_str`: a
        manager (managers), or standing in as one that date (acting_managers)."""
        key = (name or "").strip().lower()
        if key in self.managers:
            return True
        return bool(date_str) and date_str in (self.acting_managers.get(key) or ())

    def family(self, role: str) -> str:
        """The role family a role belongs to — the restaurant's own map, else
        the name with its daypart words taken off (shift_quality.role_family)."""
        from shift_quality import role_family
        return role_family(role, self.role_families)

    def fillable(self, name: str, date_str: str) -> tuple:
        """(ok, reason): whether a pass may CHOOSE `name` to fill a gap on
        `date_str` — over and above can_add, which asks whether the row is
        legal. Somebody who has not worked in weeks (dormant) or whose own
        note about that day nobody has confirmed (note_caution) is never
        picked by code; the owner can still write them in."""
        key = (name or "").strip().lower()
        if key in self.dormant:
            return False, f"has not worked since {self.dormant[key]}"
        caution = self.note_caution.get(key) or {}
        if caution:
            day = _weekday_of(date_str)
            if date_str in (caution.get("dates") or ()) or (day and day in (caution.get("days") or ())):
                return False, f"their note: {str(caution.get('text') or '')[:80]}"
        return True, ""

    def can_add(self, row: dict, rows=(), line: float = None, overtime: bool = True) -> tuple:
        """(ok, reason): whether `row` can join `rows` without a breach about
        its person — the one question every pass that adds a shift or hands
        one to somebody asks (schedule audit 10/3/26 P-2). The fill passes
        used to ask only can_work, so a cloned closing shift landed on a
        16-17 year old and a seventh day in a row on a dishwasher. Beyond
        can_work (every daypart the row covers), the window and the
        certificate, the person's week is swept with the row in: overlaps
        and rest (the published tail too), a minor's end, start and day and
        week caps, the hours maximum, the run of days, the shift-length rule
        — anything new or worse refuses it. Unless `overtime` is False the
        row may not take them past their overtime line either."""
        name = (row.get("employee") or "").strip()
        key = name.lower()
        if not key:
            return False, "no name"
        d = row.get("date", "")
        from shift_quality import present_dayparts as _present
        for part in (_present(row) or [daypart_of(row.get("shift_start", ""))]):
            ok, why = self.can_work(name, d, part)
            if not ok:
                return False, why
        ok, why = self.window_ok(name, d, row.get("shift_start", ""), row.get("shift_end", ""))
        if not ok:
            return False, why
        ok, why = self.cert_ok(name, row.get("role", ""))
        if not ok:
            return False, why
        mine = [r for r in (rows or ()) if r is not row and (r.get("employee") or "").strip().lower() == key]
        worse = regressions(breach_profile(mine, self, person_only=True),
                            breach_profile(mine + [row], self, person_only=True), upto=TIER_PERSON)
        if worse:
            return False, worse[0]["label"]
        if overtime:
            # A salaried person's line is their weekly cap (E-17): code never
            # loads them past it, whatever their hours cost.
            b = self.bucket(d)
            total = (sum(row_hours(r) for r in mine if r.get("date") and self.bucket(r["date"]) == b)
                     + float((self.base_hours.get(key) or {}).get(b, 0.0) or 0.0) + row_hours(row))
            ot = overtime_line(self, name, line)
            if ot and total > ot + 0.05:
                if self.is_salaried(name):
                    return False, f"would take them to {total:g}h, past their {ot:g}h weekly cap"
                return False, f"would take them to {total:g}h, past their {ot:g}h overtime line"
        return True, ""

    def window_ok(self, name: str, date_str: str, start: str, end: str) -> tuple:
        """Whether a shift's times sit inside the person's window that day."""
        key = (name or "").strip().lower()
        win = self.time_windows.get(key)
        if not win:
            return True, ""
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            return True, ""
        w = win.get(day)
        if not w:
            return True, ""
        lo, hi = w
        ok, which = window_allows(lo, hi, parse_minutes(start), parse_minutes(end))
        if ok:
            return True, ""
        if which == "early":
            return False, f"{LABELS['outside_window']} (not before {_fmt_minutes(lo)})"
        return False, f"{LABELS['outside_window']} (not after {_fmt_minutes(hi)})"

    def cert_ok(self, name: str, role: str) -> tuple:
        need = self.role_requirements.get((role or "").strip().lower()) or set()
        if not need:
            return True, ""
        have = self.certifications.get((name or "").strip().lower()) or set()
        missing = sorted(need - have)
        if missing:
            return False, f"{LABELS['missing_cert']} ({', '.join(missing)})"
        return True, ""

    def rest_ok(self, name: str, candidate: dict, other_rows: list) -> tuple:
        """Whether `candidate` leaves the configured rest before and after
        this person's other shifts (this week's and the published tail)."""
        need = float(self.compliance.get("min_rest_hours") or 0)
        if need <= 0:
            return True, ""
        s, e = shift_span(candidate, self.tz)
        if not s:
            return True, ""
        key = (name or "").strip().lower()
        for r in list(other_rows) + list(self.base_rows.get(key) or []):
            if (r.get("employee") or "").strip().lower() != key:
                continue
            if r is candidate or (r.get("date") == candidate.get("date") and r.get("shift_start") == candidate.get("shift_start")):
                continue
            rs, re_ = shift_span(r, self.tz)
            if not rs:
                continue
            if rs >= e:
                gap = (rs - e).total_seconds() / 3600
            elif re_ <= s:
                gap = (s - re_).total_seconds() / 3600
            else:
                return False, LABELS["overlap"]
            # The rest rule is the overnight turnaround. Two legs on the same
            # date (lunch then dinner) are a double, which the prompt asks
            # for; only an overlap is wrong there.
            if r.get("date") == candidate.get("date"):
                continue
            if gap < need:
                return False, f"{LABELS['rest_gap']} ({gap:.1f}h, needs {need:g}h)"
        return True, ""


def cert_key(cert) -> str:
    """One spelling for a certificate name wherever it was typed: the roster's
    settings use snake_case (staff_settings.CERTIFICATIONS: "food_handler"),
    the certificate records free text ("Food handler", "food-handler")."""
    return "_".join(str(cert or "").strip().lower().replace("-", " ").split())


def _expired_certs(restaurant_id, week_dates, db_path=None) -> dict:
    """{person name_key: {cert_key}} expired by the week's first day — or by
    today, once the week has begun (staff_knowledge.expired_certs)."""
    try:
        from datetime import date as _date
        import staff_knowledge
        days = sorted(str(d)[:10] for d in (week_dates or []) if d)
        first = _date.fromisoformat(days[0]) if days else _date.today()
        on = max(first, _date.today())
        kw = {"db_path": db_path} if db_path and db_path != DB_PATH else {}
        raw = staff_knowledge.expired_certs(restaurant_id, today=on, **kw) or {}
        return {k: {cert_key(x) for x in v} for k, v in raw.items()}
    except Exception:
        return {}


def _load_json(raw, default):
    try:
        return json.loads(raw or "") or default
    except Exception:
        return default


def build_constraints(restaurant_id, week_dates, week_days, restaurant=None, db_path=DB_PATH) -> Constraints:
    """Everything the pipeline needs to know about who can work when,
    gathered once. Every source is optional; a missing one costs a rule,
    never the generation."""
    from models import get_restaurant, get_staff_availability, get_close_times, get_role_close_buffers, get_staff_notes
    restaurant = restaurant or get_restaurant(restaurant_id, db_path)
    c = Constraints(restaurant_id=restaurant_id, week_dates=list(week_dates or []), week_days=list(week_days or []))
    c.compliance = compliance(restaurant)
    try:
        from models import salaried_staff, salaried_name_key
        c.salaried = {salaried_name_key(s["name"]) for s in salaried_staff(restaurant)}
    except Exception as exc:
        c.salaried = set()
        _input_problem(c, "salaried staff", exc)
    c.week_start_day = int(getattr(restaurant, "week_start_day", 0) or 0)
    c.tz = (getattr(restaurant, "timezone", None) or "").strip()
    c.jurisdiction = (getattr(restaurant, "jurisdiction", None) or "").strip().upper()
    c.section_cap = int(getattr(restaurant, "section_count", 0) or 0)
    c.role_floors = role_floors(restaurant)
    try:
        import kitchen_stations as _ks
        c.stations = _ks.normalise(getattr(restaurant, "kitchen_stations_json", None))
    except Exception as exc:
        c.stations = {}
        _input_problem(c, "kitchen stations", exc)
    c.open_times = _load_json(getattr(restaurant, "open_times_json", None), {})
    try:
        c.closed_dates = closed_in(restaurant, c.week_dates)
    except Exception as exc:
        c.closed_dates = set()
        _input_problem(c, "closed dates", exc)
    try:
        c.close_times = get_close_times(restaurant_id, db_path) or {}
        c.role_buffers = get_role_close_buffers(restaurant_id, db_path) or {}
    except Exception as exc:
        _input_problem(c, "open and close times", exc)

    # Certificates past their expiry (staff_knowledge.staff_certs) are not
    # held: an expired food-handler card does not satisfy a role that needs
    # one (employee audit B7 handoff). Valid on the week's first day is the
    # test — cert_ok has no day of its own.
    expired = _expired_certs(restaurant_id, c.week_dates, db_path)
    # roster + per-person settings. One person at a time: a single try
    # around the whole loop meant one malformed setting (a time window that
    # would not parse) silently dropped managers, minors, hours limits and
    # certificates for everyone after that person (schedule audit 10/3/26
    # P-1). A person whose settings cannot be read keeps their place on the
    # roster and is named in input_problems — the review and the publish
    # gate say so.
    try:
        import staff_settings as _ss
        _people = list(_ss.roster(restaurant_id, db_path=db_path, include_inactive=True))
    except Exception as exc:
        _people = []
        _input_problem(c, "roster", exc)
    for e in _people:
        key = e["name"].strip().lower()
        if e["active"]:
            c.roster_names.append(e["name"])
            c.active.add(key)
        else:
            c.inactive.add(key)
        try:
            _person_settings(c, e, expired, _ss)
        except Exception as exc:
            _input_problem(c, "settings", exc, name=e.get("name"))
    try:
        from models import get_leader_flags
        c.keyholders |= {n.strip().lower() for n, v in (get_leader_flags(restaurant_id, db_path) or {}).items() if v}
    except Exception as exc:
        _input_problem(c, "closers", exc)
    c.role_requirements = {str(k).strip().lower(): {cert_key(x) for x in (v or []) if str(x).strip()}
                           for k, v in (_load_json(getattr(restaurant, "role_requirements_json", None), {}) or {}).items() if k}
    foh = _load_json(getattr(restaurant, "foh_roles_json", None), [])
    c.foh_roles = {str(x).strip().lower() for x in foh if str(x).strip()} or {"server"}
    c.patio_roles = {str(x).strip().lower() for x in (_load_json(getattr(restaurant, "patio_roles_json", None), []) or []) if str(x).strip()}
    c.close_mins = {}
    for k, v in (_load_json(getattr(restaurant, "role_close_min_json", None), {}) or {}).items():
        try:
            c.close_mins[str(k).strip().lower()] = int(v)
        except (TypeError, ValueError):
            continue

    # weekday availability + free-text notes. A weekday blocked for good is
    # a weekday rule; one blocked only between two dates ("Fridays until
    # 12/15", day_bounds — employee audit M5) blocks the dates of this week
    # inside them, and only those.
    try:
        from models import availability_day_bounds, availability_blocked_dates
        for a in get_staff_availability(restaurant_id, db_path) or []:
            key = (a.get("employee_name") or "").strip().lower()
            if not key:
                continue
            blocked = set(_load_json(a.get("unavailable_days"), []))
            avail = _load_json(a.get("available_days"), [])
            if avail:
                blocked |= {d for d in DAYS if d not in avail}
            dated = availability_day_bounds(a)
            c.unavailable_days[key] = {d for d in blocked if d not in dated}
            if dated and not c.week_dates:
                # No week to place them in: while they last, they hold.
                from models import availability_blocked_days
                c.unavailable_days[key] |= availability_blocked_days(a)
            elif dated:
                for d in availability_blocked_dates(a, c.week_dates):
                    c.blocked_dates.setdefault(key, {}).setdefault(d, LABELS["unavailable_day"])
            if (a.get("notes") or "").strip():
                c.notes[key] = a["notes"].strip()
    except Exception as exc:
        _input_problem(c, "availability", exc)
    try:
        for n in get_staff_notes(restaurant_id, db_path) or []:
            key = (n.get("employee_name") or "").strip().lower()
            if key and n.get("notes"):
                c.notes[key] = (c.notes.get(key, "") + " " + n["notes"]).strip()
    except Exception as exc:
        _input_problem(c, "staff notes", exc)

    # time off: approved blocks, pending is a warning
    if c.week_dates:
        try:
            import time_off as _to
            for name, days in (_to.approved_in_window(restaurant_id, c.week_dates[0], c.week_dates[-1], db_path=db_path) or {}).items():
                c.blocked_dates.setdefault(name.strip().lower(), {}).update({d: LABELS["approved_time_off"] for d in days})
            for name, days in (_pending_in_window(restaurant_id, c.week_dates[0], c.week_dates[-1], db_path) or {}).items():
                c.pending_off.setdefault(name.strip().lower(), set()).update(days)
        except Exception as exc:
            _input_problem(c, "time off", exc)

    # hours already published in the same payroll week(s), here and at siblings
    try:
        _published_tail(c, restaurant_id, db_path)
    except Exception as exc:
        _input_problem(c, "last week's shifts", exc)
    # the owner's standing staffing rules, as checks and floors
    try:
        apply_owner_rules(c, restaurant_id, db_path=db_path)
    except Exception as exc:
        _input_problem(c, "your staffing rules", exc)
    # The Studio notes the owner confirmed as rules, every week or this
    # week only (schedule_note_rules, 10/1/26): floors like the ones above.
    try:
        import schedule_note_rules
        schedule_note_rules.apply_note_rules(c, restaurant_id, db_path=db_path)
    except Exception as exc:
        _input_problem(c, "schedule note rules", exc)
    # A person's scheduling notes the owner confirmed as holds ("no Tuesdays
    # until 10/31", "out 12/20-12/28") are unavailability like any other
    # (person_note_holds, 10/2/26).
    try:
        import person_note_holds
        person_note_holds.apply_holds(c, restaurant_id, db_path=db_path)
    except Exception as exc:
        _input_problem(c, "scheduling note holds", exc)
    return c


def _input_problem(c: "Constraints", source: str, exc, name: str = None):
    """Record a source or a person's settings the rules could not read
    (schedule audit 10/3/26 P-1): kept on the Constraints for the review and
    the publish gate, and captured so it reaches the daily failure digest."""
    entry = {"source": source, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if name:
        entry["name"] = name
    c.input_problems.append(entry)
    try:
        import ops
        ops.capture(exc, job="schedule_constraints",
                    context=f"restaurant {c.restaurant_id} {source}" + (f" {name}" if name else ""))
    except Exception:
        pass   # the capture is a report about a failure, never one of its own


def _person_settings(c: "Constraints", e: dict, expired: dict, _ss):
    """One roster person's settings onto the Constraints (build_constraints'
    per-person step, each person on their own — see P-1 there)."""
    key = e["name"].strip().lower()
    st = e.get("settings") or {}
    if e["active"] and is_manager_role(e.get("role")):
        c.managers[key] = (e.get("role") or "").strip()
    if st.get("min_hours") is not None or st.get("max_hours") is not None:
        c.hours_limits[key] = (st.get("min_hours"), st.get("max_hours"))
    if st.get("employment_type"):
        c.employment[key] = st["employment_type"]
    if st.get("daypart_availability"):
        c.daypart_avail[key] = dict(st["daypart_availability"])
    if st.get("is_minor") or st.get("minor_age_band"):
        c.minors.add(key)
        if st.get("minor_age_band") in MINOR_BANDS:
            c.minor_bands[key] = st["minor_age_band"]
    if st.get("time_windows"):
        win = {}
        for day, w in st["time_windows"].items():
            lo, hi = parse_minutes((w or {}).get("earliest") or ""), parse_minutes((w or {}).get("latest") or "")
            if (lo is not None or hi is not None) and window_holds(w, day, c.week_dates):
                win[day] = (lo, hi)
        if win:
            c.time_windows[key] = win
    if st.get("certifications"):
        gone = expired.get(_ss.name_key(e["name"]), set())
        c.certifications[key] = {cert_key(x) for x in st["certifications"] if str(x).strip()
                                 and cert_key(x) not in gone}
        # A closer is a person the owner marked to close (stays until
        # close, the last of their role to leave), not a key holder
        # (owner, 10/2/26) — a manager or keyholder certificate no
        # longer makes anyone a closer; get_leader_flags below does.
        if "manager" in c.certifications[key] and e["active"]:
            c.managers.setdefault(key, (e.get("role") or "").strip())
    if st.get("preferred_dayparts") or st.get("desired_hours"):
        c.preferred[e["name"]] = {"preferred_dayparts": list(st.get("preferred_dayparts") or []),
                                  "desired_hours": st.get("desired_hours")}


# ── the owner's standing rules about staffing (memory re-audit 9/29/26) ──────
#
# "Never cut the host, she's our brand" reached the schedule prompt inside
# the guest fence the prompt says never to follow, and nothing deterministic
# checked it (PROMPTS-1). A rule an account holder set about staffing is
# read here in the common shapes — "never cut the host", "always two
# servers on Saturday night", "at least 1 bartender every day", "never
# below 2 cooks at dinner" — against the restaurant's own roles:
#
#   * with a daypart, it raises that role's floor for those days
#     (role_floors), so the draft is built to it (_ensure_role_floors) and
#     a draft short of it is a coverage_floor breach naming the rule;
#   * without one, it is checked per trading day (the owner_rule flag:
#     fewer of the role on that day than the rule says);
#   * a staffing rule it cannot read is named in the review, so the owner
#     checks the draft against it — never silently dropped.
#
# Only a rule the whole team may read (audience "team", as the schedule
# prompt reads memory): an owner-only line never surfaces in a shared review.

_RULE_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
# ("day", "am" and "pm" are not here: "every day" is not lunch, and "I am"
# is not a daypart.)
_RULE_DAYPARTS = {"lunch": "morning", "brunch": "morning", "morning": "morning", "mornings": "morning",
                  "breakfast": "morning", "daytime": "morning",
                  "dinner": "night", "night": "night", "nights": "night", "evening": "night", "evenings": "night",
                  "close": "night", "closing": "night"}
_RULE_MAX = 10


def _rule_days(low: str):
    """The weekdays a rule's words name, or None for every day."""
    import re as _re
    days = []
    for d in DAYS:
        if _re.search(r"\b" + d.lower() + r"s?\b", low) or _re.search(r"\b" + d.lower()[:3] + r"\b", low):
            days.append(d)
    if _re.search(r"\bweekends?\b", low):
        days += ["Saturday", "Sunday"]
    if _re.search(r"\bweekdays?\b", low):
        days += ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    out = [d for d in DAYS if d in days]
    return tuple(out) or None


def _rule_daypart(low: str):
    import re as _re
    for word, part in _RULE_DAYPARTS.items():
        if _re.search(r"\b" + word + r"\b", low):
            return part
    return None


def parse_owner_rule(text: str, roles) -> dict:
    """{"role", "min", "days", "daypart", "text"} for a staffing rule in one
    of the shapes above, naming one of `roles` (the restaurant's own role
    names, matched case-insensitively, singular or plural), or None. `days`
    None is every trading day; `daypart` None is anywhere in the day."""
    import re as _re
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    if not low:
        return None
    role = None
    for r in sorted({str(x).strip() for x in roles or () if str(x or "").strip()}, key=len, reverse=True):
        rl = r.lower()
        if _re.search(r"\b" + _re.escape(rl) + r"(s|es)?\b", low):
            role = r
            break
    if role is None:
        return None
    rl = _re.escape(role.lower()) + r"(?:s|es)?"
    num = r"(\d+|a|an|one|two|three|four|five|six)"
    n = None
    m = (_re.search(r"\b(?:at least|no fewer than|not fewer than|minimum of|min(?:imum)?)\s+" + num + r"\s+" + rl, low)
         or _re.search(r"\bnever\s+(?:go\s+|drop\s+|run\s+)?(?:below|under|fewer than|less than)\s+" + num + r"\s+" + rl, low)
         or _re.search(r"\balways\s+(?:(?:have|staff|schedule|keep|need|put on|run(?: with)?)\s+)?(?:at least\s+)?"
                       + num + r"\s+" + rl, low))
    if m:
        raw = m.group(1)
        n = int(raw) if raw.isdigit() else _RULE_NUMBERS.get(raw)
    elif (_re.search(r"\bnever\s+(?:cut|send home|drop|remove|skip|schedule without|go without|run without)\s+"
                     r"(?:the\s+|our\s+|a\s+|an\s+|any\s+)?" + rl, low)
          or _re.search(r"\balways\s+(?:have|staff|schedule|keep|need)\s+(?:the\s+|our\s+|a\s+|an\s+)?" + rl, low)
          or _re.search(r"\b(?:the\s+|our\s+|a\s+)?" + rl + r"\s+(?:is|are)\s+(?:always\s+on|never\s+cut)", low)):
        n = 1
    if not n or n < 1:
        return None
    return {"role": role, "min": min(int(n), _RULE_MAX), "days": _rule_days(low), "daypart": _rule_daypart(low),
            "text": " ".join(str(text).split())}


def _is_staffing_rule(fact) -> bool:
    mods = {m for m in str(fact.get("modules") or "").split(",") if m}
    return bool(mods & {"labor", "schedule"})


def apply_owner_rules(c: "Constraints", restaurant_id, db_path=None):
    """Read the owner's staffing rules into `c` (see above). Never raises
    into a generation: a rule that cannot be read costs a check, never the
    week."""
    import owner_memory
    import memory_context
    facts = owner_memory.facts_for(restaurant_id, viewer=memory_context.team_viewer("schedule"),
                                   surface="schedule", kinds=list(owner_memory.RULE_KINDS),
                                   db_path=None if db_path == DB_PATH else db_path)
    roles = set()
    try:
        import staff_settings as _ss
        roles |= {str(e.get("role") or "").strip() for e in _ss.roster(restaurant_id, db_path=db_path)
                  if e.get("role")}
    except Exception:
        pass
    roles |= {str(r).strip() for r in (c.role_floors or {})}
    roles.discard("")
    for f in facts:
        if not owner_memory.is_owner_rule(f):
            continue
        rule = parse_owner_rule(f.get("fact"), roles)
        if rule is None:
            if _is_staffing_rule(f):
                c.owner_rules_unchecked.append(" ".join(str(f.get("fact") or "").split()))
            continue
        c.owner_rules.append(rule)
        if not rule["daypart"]:
            continue
        # A daypart rule is a floor: the draft is built to it and checked
        # against it like any floor the owner set in the schedule settings.
        key = next((r for r in c.role_floors if r.strip().lower() == rule["role"].lower()), rule["role"])
        spec = c.role_floors.setdefault(key, {"morning": 0, "night": 0, "days": {}})
        for day in rule["days"] or DAYS:
            if floor_for(c.role_floors, key, day, rule["daypart"]) < rule["min"]:
                spec.setdefault("days", {}).setdefault(day, {})[rule["daypart"]] = rule["min"]
                c.rule_floor_sources[(key.strip().lower(), day, rule["daypart"])] = rule["text"]


def _pending_in_window(restaurant_id, start, end, db_path):
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT employee_name, start_date, end_date FROM staff_time_off WHERE restaurant_id=? "
            "AND status='pending' AND NOT (end_date < ? OR start_date > ?)", (restaurant_id, start, end)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in rows:
        a = max(datetime.strptime(r["start_date"][:10], "%Y-%m-%d"), datetime.strptime(start, "%Y-%m-%d"))
        b = min(datetime.strptime(r["end_date"][:10], "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d"))
        days = [(a + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((b - a).days + 1)]
        out.setdefault(r["employee_name"], []).extend(days)
    return {k: sorted(set(v)) for k, v in out.items()}


def _published_tail(c: Constraints, restaurant_id, db_path):
    """Rows from the last PUBLISHED schedule here (and at sibling sites)
    that fall in a payroll bucket this week touches or in the seven days
    before it — the hours count toward the ceiling, the rows toward rest,
    and a sibling's date blocks the person here."""
    from schedule_versions import rows_from_csv
    if not c.week_dates:
        return
    buckets = {c.bucket(d) for d in c.week_dates}
    first = datetime.strptime(c.week_dates[0], "%Y-%m-%d")
    window_start = (first - timedelta(days=7)).strftime("%Y-%m-%d")
    conn = get_conn(db_path)
    try:
        me = conn.execute("SELECT location_group, owner_email FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        own = conn.execute("SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
                           "AND week_end >= ? ORDER BY id DESC LIMIT 2", (restaurant_id, window_start)).fetchall()
        sibs = []
        group = ((me["location_group"] if me else "") or "").strip()
        if group:
            # Every live published sibling week that overlaps the window,
            # not just the sibling's newest: once a sibling published next
            # week, this week's shifts there dropped out and a claim or swap
            # here could double-book someone across sites (employee audit
            # LG-20).
            week_end = max(c.week_dates)
            for s in conn.execute("SELECT id, COALESCE(location_name, name) AS label FROM restaurants "
                                  "WHERE location_group=? AND owner_email=? AND id<>?", (group, me["owner_email"], restaurant_id)).fetchall():
                for row in conn.execute("SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND nw.id > schedule_history.id) "
                                        "AND substr(week_end,1,10) >= ? AND substr(week_start,1,10) <= ? ORDER BY id DESC",
                                        (s["id"], window_start, week_end)).fetchall():
                    sibs.append((s["label"], row["schedule_csv"]))
    finally:
        conn.close()
    seen = set()
    for row in own:
        for r in rows_from_csv(row["schedule_csv"]):
            key = r["employee"].strip().lower()
            sig = (key, r["date"], r["shift_start"])
            if sig in seen or r["date"] in c.week_dates:
                continue
            seen.add(sig)
            if r["date"] >= window_start:
                c.base_rows.setdefault(key, []).append(r)
            b = c.bucket(r["date"])
            if b in buckets:
                c.base_hours.setdefault(key, {})[b] = c.base_hours.get(key, {}).get(b, 0.0) + row_hours(r)
    sib_seen = set()
    for label, csv_text in sibs:
        for r in rows_from_csv(csv_text):
            key = r["employee"].strip().lower()
            sig = (label, key, r["date"], r["shift_start"])
            if sig in sib_seen:
                continue
            sib_seen.add(sig)
            if r["date"] in c.week_dates:
                c.blocked_dates.setdefault(key, {}).setdefault(r["date"], f"already scheduled at {label}")
            elif r["date"] >= window_start:
                # A close at the other site the night before is still a close
                # for the rest rule here.
                c.base_rows.setdefault(key, []).append(r)
            b = c.bucket(r["date"])
            if b in buckets:
                c.base_hours.setdefault(key, {})[b] = c.base_hours.get(key, {}).get(b, 0.0) + row_hours(r)


# ── the violation sweep ────────────────────────────────────────────────────

def violations(rows: list, c: Constraints, person_only: bool = False) -> list:
    """Every rule the finished week breaks, one entry per breach, each
    naming the person, the date, the rule and whether it is hard (the row
    cannot stand) or soft (worth a look). `person_only` keeps to the rules
    about one person's own rows (can_add sweeps a single person's week with
    it: the day-level rules — floors, a closer, a manager — need everyone)."""
    out = []
    by_person = {}
    seen_slots = set()
    from shift_quality import present_dayparts as _present
    for i, r in enumerate(rows or []):
        name = (r.get("employee") or "").strip()
        key = name.lower()
        if not key:
            continue
        by_person.setdefault(key, []).append((i, r))
        # Availability is judged on every daypart the shift covers, not its
        # start: a "mornings only" person on 2:30-11:30pm was never flagged.
        ok, why = True, ""
        for part in _present(r):
            ok, why = c.can_work(name, r.get("date", ""), part)
            if not ok:
                break
        if not ok:
            kind = next((k for k, lab in LABELS.items() if lab == why), None)
            if kind is None:
                # A held scheduling note names itself ("your note: no
                # Tuesdays", person_note_holds) — never "approved time off".
                kind = ("note_unavailable" if str(why).startswith("your note:") else
                        "elsewhere" if "another location" in why or "scheduled at" in why else "approved_time_off")
            out.append(_v(kind, i, r, why))
        slot = (key, r.get("date"), r.get("shift_start"))
        if slot in seen_slots:
            out.append(_v("double_booked", i, r, LABELS["double_booked"]))
        seen_slots.add(slot)
        hrs = row_hours(r)
        mx = c.compliance.get("max_shift_hours")
        if mx and hrs > float(mx) + 0.01:
            out.append(_v("shift_too_long", i, r, f"{hrs:g}h shift, maximum {float(mx):g}h",
                          severity=round(hrs - float(mx), 2)))
        shortest = c.compliance.get("min_shift_hours")
        if shortest and 0 < hrs < float(shortest) - 0.01:
            out.append(_v("shift_too_short", i, r, f"{hrs:g}h shift, your shortest is {float(shortest):g}h"))
        if key in c.minors:
            latest, latest_label = minor_latest(c, key, r.get("date", ""))
            # Read on the row's business date (E-32): a 12:30am start is the
            # night's, past any latest end, never a morning start.
            end_m, start_m = end_minutes(r), start_minutes(r)
            band = c.minor_bands.get(key)
            who = f"minors {band}" if band else "minors"
            if latest is not None and end_m is not None and start_m is not None and end_m > latest:
                out.append(_v("minor_late", i, r, f"ends {r.get('shift_end')}, {who} stop at {latest_label}",
                              severity=round((end_m - latest) / 60.0, 2)))
            earliest = minor_earliest(c, key)
            if earliest is not None and start_m is not None and start_m < earliest:
                out.append(_v("minor_early", i, r, f"starts {r.get('shift_start')}, {who} start no earlier than "
                                                   f"{minor_rules(band, c.jurisdiction).get('earliest_start')}",
                              severity=round((earliest - start_m) / 60.0, 2)))
        # A minor's daily cap and daily overtime are about the DAY: they are
        # summed over every leg of it with the person's other rows below (E-6).
        mb = c.compliance.get("meal_break_after_hours")
        if mb and hrs > float(mb) + 0.01:
            out.append(_v("meal_break", i, r, f"{hrs:g}h shift — schedule a meal break"))
        if c.pending_off.get(key) and r.get("date") in c.pending_off[key]:
            out.append(_v("pending_time_off", i, r, LABELS["pending_time_off"]))
        ok, why = c.window_ok(name, r.get("date", ""), r.get("shift_start", ""), r.get("shift_end", ""))
        if not ok:
            out.append(_v("outside_window", i, r, why))
        ok, why = c.cert_ok(name, r.get("role", ""))
        if not ok:
            out.append(_v("missing_cert", i, r, why))

    # a role that stays until N minutes after close: the last of that role
    # each night must end no earlier ("bartenders stay an hour after close")
    if c.close_mins and c.close_times and not person_only:
        by_date_role = {}
        for i, r in enumerate(rows or []):
            role = (r.get("role") or "").strip().lower()
            if role in c.close_mins and r.get("date"):
                by_date_role.setdefault((r["date"], role), []).append((i, r))
        for (d, role), items in by_date_role.items():
            try:
                day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
            except (ValueError, TypeError):
                continue
            close_m = close_minutes(c, day)
            if close_m is None:
                continue
            need = close_m + int(c.close_mins[role])
            ends = [(end_minutes(r), i, r) for i, r in items]
            ends = [(e, i, r) for e, i, r in ends if e is not None]
            if not ends:
                continue
            e_last, i_last, r_last = max(ends, key=lambda t: t[0])
            if e_last < need - 15:
                out.append(_v("ends_before_role_close", i_last, r_last,
                              f"last {r_last.get('role')} ends {r_last.get('shift_end')}, the rule is until {_fmt_minutes(need % (24 * 60))}"))

    # coverage the owner set and the defaults every trading day needs
    # (NS5 M8): the role floors are hard, a keyholder stays until close
    # whenever the roster has one, and somebody is on at close.
    if not person_only:
        out.extend(_coverage_violations(rows, c))
        # a manager on the floor every minute anyone is (owner, 10/2/26)
        out.extend(_manager_violations(rows, c))

    # every open daypart needs a manager or keyholder, when the owner says so
    if c.compliance.get("manager_on_duty") and not c.keyholders and rows and not person_only:
        out.append(_v("manager_rule_unusable", 0, rows[0], LABELS["manager_rule_unusable"]))
    if c.compliance.get("manager_on_duty") and c.keyholders and not person_only:
        slots = {}
        for i, r in enumerate(rows or []):
            if r.get("date") and (r.get("employee") or "").strip():
                slots.setdefault((r["date"], daypart_of(r.get("shift_start", ""))), []).append((i, r))
        for (d, part), items in sorted(slots.items()):
            if part == "unknown":
                continue
            if not any((r.get("employee") or "").strip().lower() in c.keyholders for _, r in items):
                i0, r0 = items[0]
                out.append(_v("no_manager_on_duty", i0, r0, f"{LABELS['no_manager_on_duty']} ({part})"))

    # more front-of-house on the floor at once than there are sections. A
    # shift past midnight ends on the next day's clock (12:30am is 24:30,
    # end_minutes): skipping every row whose end read as before its start
    # left Friday and Saturday at a 2am close — exactly the nights a cap
    # matters — unchecked (schedule audit 10/3/26 E-19). The roles count by
    # family, so "Server AM" and "Server PM" are servers (D-13).
    if c.section_cap and not person_only:
        counted = {c.family(x) for x in (c.foh_roles or ()) if str(x).strip()} or {"server"}
        by_date = {}
        for i, r in enumerate(rows or []):
            if r.get("date") and c.family(r.get("role")) in counted:
                by_date.setdefault(r["date"], []).append((i, r))
        for d, items in by_date.items():
            events = []
            for i, r in items:
                s_, e_ = parse_minutes(r.get("shift_start", "")), end_minutes(r)
                if s_ is not None and e_ is not None and e_ > s_:
                    events.append((s_, 1, i)); events.append((e_, -1, i))
            events.sort(key=lambda ev: (ev[0], ev[1]))
            running, peak, active, worst = 0, 0, [], []
            for t, delta, i in events:
                if delta > 0:
                    active.append(i)
                else:
                    active = [x for x in active if x != i]
                running += delta
                if running > peak:
                    peak, worst = running, list(active)
            if peak > int(c.section_cap) and worst:
                latest = max(worst, key=lambda i: parse_minutes(rows[i].get("shift_start", "")) or 0)
                out.append(_v("over_section_cap", latest, rows[latest], f"{peak} on the floor at once, {int(c.section_cap)} sections"))

    # per-person rules: overlap, rest, hours, days off
    for key, items in by_person.items():
        items.sort(key=lambda ir: (ir[1].get("date", ""), start_minutes(ir[1]) or 0))
        # hours by payroll bucket, including what is already published
        per_bucket = dict(c.base_hours.get(key) or {})
        for i, r in items:
            b = c.bucket(r.get("date", "")) if r.get("date") else ""
            per_bucket[b] = per_bucket.get(b, 0.0) + row_hours(r)
        name = items[0][1].get("employee")
        mx = c.max_hours(name)
        for b, total in per_bucket.items():
            if mx and total > mx + 0.05 and b:
                for i, r in items:
                    if c.bucket(r.get("date", "")) == b:
                        v = _v("over_max_hours", i, r, f"{total:g}h in the payroll week — over {mx:g}h")
                        # Which payroll week, and by how much, so a fix moves
                        # that week's excess and nothing more.
                        v["bucket"], v["over_by"] = b, round(total - mx, 2)
                        v["severity"] = v["over_by"]
                        out.append(v)
        # A payroll week that runs on past this schedule week (a Wednesday
        # payroll and a Monday week: next Monday and Tuesday are still in
        # it) and is already at the overtime line by Sunday leaves next
        # week's draft no room on those days but overtime (schedule audit
        # 10/3/26 E-10). Soft: a reserve to keep, not a breach — the
        # rebalance keeps it where a teammate has the room.
        if not c.is_salaried(name):
            for b, total in per_bucket.items():
                tail = c.bucket_tail(b)
                if not b or len(tail) < TAIL_RESERVE_MIN_DAYS:
                    continue
                if any(t.get("date") in tail for t in (c.base_rows.get(key) or [])):
                    continue          # those days are already scheduled and counted
                ot = overtime_line(c, name)
                mine_b = [(i, r) for i, r in items if c.bucket(r.get("date", "")) == b]
                if mine_b and ot and total >= ot - 0.05:
                    i_last, r_last = mine_b[-1]
                    out.append(_v("payroll_tail_full", i_last, r_last,
                                  f"{total:g}h of the {mdy_payroll(b)} payroll week by {_mdy(r_last.get('date'))} — "
                                  f"no room before overtime left for {_tail_days_text(tail)}",
                                  bucket=b, severity=round(total - (ot - c.tail_reserve(ot, b)), 2)))
        mn = c.min_hours(name)
        if mn:
            this_week = sum(row_hours(r) for _, r in items)
            if this_week + 0.05 < mn:
                out.append(_v("under_min_hours", items[0][0], items[0][1], f"{this_week:g}h this week, wants at least {mn:g}h",
                              severity=round(mn - this_week, 2)))
        # The day's hours, every leg of it (schedule audit 10/3/26 E-6): a
        # 16-17 host on 10am-3pm and 4-9pm works ten hours, and a server on
        # 6h + 6h in a daily-overtime state works twelve — checked one row
        # at a time, neither was flagged, while the price summed the day.
        # Counted in time order, so the leg that crosses the cap carries the
        # flag (moving or cutting it fixes the day); each flag's severity is
        # the day's excess so far, and the breach is the day's worst.
        dot = None if c.is_salaried(name) else c.compliance.get("daily_ot_hours")
        days = {}
        for i, r in items:
            days.setdefault(r.get("date") or "", []).append((i, r))
        for d, legs in days.items():
            mcap, mtext = minor_daily_cap(c, key, d) if key in c.minors else (None, None)
            run = 0.0
            for i, r in legs:
                run += row_hours(r)
                if mcap and run > mcap + 0.01:
                    out.append(_v("minor_hours", i, r, mtext(round(run, 2)), severity=round(run - mcap, 2)))
                if dot and run > float(dot) + 0.01:
                    n = len(legs)
                    out.append(_v("daily_ot", i, r, f"{run:g}h on the day{f' ({n} shifts)' if n > 1 else ''}, "
                                                    f"daily overtime starts at {float(dot):g}h",
                                  severity=round(run - float(dot), 2)))
        # a minor's age band: the week caps (school week vs out of school),
        # counted in date order so the shift that crosses the cap is the one
        # flagged — moving it fixes the breach
        if key in c.minors:
            band = c.minor_bands.get(key)
            br = minor_rules(band, c.jurisdiction)
            if not band:
                out.append(_v("minor_age_unknown", items[0][0], items[0][1],
                              f"{name} is marked a minor with no age band — set 14-15 or 16-17 so the age limits are checked"))
            if br.get("max_weekly_school_week"):
                week_tot = {}
                for r in (c.base_rows.get(key) or []):
                    d = r.get("date") or ""
                    try:
                        wk = (_as_date(d) - timedelta(days=_as_date(d).weekday())).isoformat()
                    except (TypeError, ValueError):
                        continue
                    week_tot[wk] = week_tot.get(wk, 0.0) + row_hours(r)
                for i, r in items:
                    d = r.get("date") or ""
                    try:
                        wk = (_as_date(d) - timedelta(days=_as_date(d).weekday())).isoformat()
                    except (TypeError, ValueError):
                        continue
                    week_tot[wk] = week_tot.get(wk, 0.0) + row_hours(r)
                    school_week = is_school_week(d)
                    wcap = br.get("max_weekly_school_week" if school_week else "max_weekly_other_week")
                    if wcap and week_tot[wk] > float(wcap) + 0.01:
                        out.append(_v("minor_week_hours", i, r, f"{week_tot[wk]:g}h in a {'school ' if school_week else ''}week, "
                                                                f"minors {band} stop at {float(wcap):g}h",
                                      week=wk, severity=round(week_tot[wk] - float(wcap), 2)))
        # rest and overlap, against this week's other shifts and the published tail
        spans = [(i, r, *shift_span(r, c.tz)) for i, r in items]
        spans = [(i, r, s, e) for i, r, s, e in spans if s]
        tail = [(None, r, *shift_span(r, c.tz)) for r in (c.base_rows.get(key) or [])]
        tail = [(i, r, s, e) for i, r, s, e in tail if s]
        need = float(c.compliance.get("min_rest_hours") or 0)
        allspans = sorted(spans + tail, key=lambda t: t[2])
        for a in range(1, len(allspans)):
            i_prev, r_prev, s_prev, e_prev = allspans[a - 1]
            i_cur, r_cur, s_cur, e_cur = allspans[a]
            if i_cur is None:
                continue
            if s_cur < e_prev:
                if r_cur.get("shift_start") != r_prev.get("shift_start") or r_cur.get("date") != r_prev.get("date"):
                    out.append(_v("overlap", i_cur, r_cur, f"overlaps their {r_prev.get('shift_start')}–{r_prev.get('shift_end')} on {r_prev.get('date')}"))
                continue
            gap = (s_cur - e_prev).total_seconds() / 3600
            # Same date = a double shift, not a rest breach (see rest_ok).
            if need and gap < need - 0.01 and r_cur.get("date") != r_prev.get("date"):
                out.append(_v("rest_gap", i_cur, r_cur, f"{gap:.1f}h since their previous shift, the rule is {need:g}h",
                              severity=round(need - gap, 2)))
        # too many days in a row — the published tail counts, so a Saturday
        # and Sunday already sent plus Monday to Friday here reads as seven.
        # Every run past the rule that this week's rows are part of counts:
        # the severity is the days past it summed over them, so splitting a
        # nine-day run into two of four is progress the repair can see
        # (schedule audit 10/3/26 P-30), and the flag names the longest.
        max_run = c.compliance.get("max_consecutive_days")
        if max_run:
            worked_dates = {r.get("date") for _, r in items if r.get("date")}
            worked_dates |= {r.get("date") for r in (c.base_rows.get(key) or []) if r.get("date")}
            mine_dates = {r.get("date") for _, r in items}
            over = [run for run in _date_runs(worked_dates)
                    if len(run) > int(max_run) and set(run) & mine_dates]
            if over:
                best = max(over, key=len)
                week_rows = [(i, r) for i, r in items if r.get("date") in set(best)]
                i_last, r_last = week_rows[-1]
                out.append(_v("long_run", i_last, r_last,
                              f"{len(best)} days in a row — the rule is at most {int(max_run)}",
                              severity=sum(len(run) - int(max_run) for run in over)))
        # consecutive days off inside the generated week
        req = c.compliance.get("part_time_days_off") if c.employment.get(key) == "part" else c.compliance.get("min_consecutive_days_off")
        if req and c.week_dates:
            worked = {r.get("date") for _, r in items}
            off = [d for d in c.week_dates if d not in worked]
            # Anyone working at all is checked: Monday/Wednesday/Friday has
            # four days off and no two of them together — and seven shifts
            # have none at all, which a run limit above 6 used to let
            # through unflagged (SCHED-31).
            if len(worked) >= 2:
                best = run = 0
                prev = None
                for d in c.week_dates:
                    if d in worked:
                        run = 0
                    else:
                        run += 1
                        best = max(best, run)
                if best < int(req):
                    out.append(_v("days_off", items[0][0], items[0][1],
                                  f"{len(off)} day{'s' if len(off) != 1 else ''} off, longest run {best} — the rule is {int(req)} together"))
    return out


def close_minutes(c: Constraints, day: str):
    """The close on `day` as minutes past that day's midnight — a close in
    the small hours ("12:00am", "1:30am") is the NEXT morning, past 24h. It
    was read as minute 0, so a midnight close never flagged a bartender
    leaving at 9pm under "stays an hour after close" (NS5 L14)."""
    m = parse_minutes((c.close_times or {}).get(day, ""))
    if m is None:
        return None
    return m + 24 * 60 if m < _OVERNIGHT_LATEST_BEFORE else m


def end_minutes(row):
    """A row's end as minutes past its own date's midnight: an end at or
    before its start crosses midnight, and a row starting in the small hours
    of its night ends in them too (E-32, _night_offset)."""
    s, e = parse_minutes(row.get("shift_start", "")), parse_minutes(row.get("shift_end", ""))
    if e is None:
        return None
    if s is not None and e <= s:
        e += 24 * 60
    return e + _night_offset(s)


def _coverage_violations(rows: list, c: Constraints) -> list:
    """coverage_floor, keyholder_until_close and nobody_at_close — rules
    about who is on a day, not about one person's row. Each is pinned to a
    row of that day (the review and the publish gate name a row), and none
    is something a different person on that row would fix, so the fix pass
    leaves them for the owner. Only days with shifts on them are judged: a
    day with nobody at all is visibly empty, and a closed day has none."""
    from shift_quality import present_dayparts as _present
    out = []
    by_date = {}
    for i, r in enumerate(rows or []):
        if r.get("date") and (r.get("employee") or "").strip():
            by_date.setdefault(r["date"], []).append((i, r))
    for d, items in sorted(by_date.items()):
        if d in (c.closed_dates or set()):
            continue
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue
        # role floors, per daypart, counted by who is on the floor for it
        for role, spec in (c.role_floors or {}).items():
            role_low = role.strip().lower()
            mine = [(i, r) for i, r in items if (r.get("role") or "").strip().lower() == role_low]
            for part in ("morning", "night"):
                need = floor_for(c.role_floors, role, day, part)
                if need <= 0:
                    continue
                on = {(r.get("employee") or "").strip().lower() for _i, r in mine if part in _present(r)}
                if len(on) < need:
                    i0, r0 = (mine or items)[0]
                    word = "lunch/day" if part == "morning" else "dinner/night"
                    src = (c.rule_floor_sources or {}).get((role_low, day, part))
                    out.append(_v("coverage_floor", i0, r0,
                                  f"{len(on)} {role} on for {word} {day}, your floor is {need}"
                                  + (f" (your rule: \u201c{src[:120]}\u201d)" if src else ""),
                                  floor_role=role_low, daypart=part, severity=need - len(on)))
        # the owner's rules that name no daypart: the role on the day at all
        for rule in (c.owner_rules or []):
            if rule.get("daypart") or (rule.get("days") and day not in rule["days"]):
                continue
            role_low = rule["role"].strip().lower()
            mine = [(i, r) for i, r in items if (r.get("role") or "").strip().lower() == role_low]
            on = {(r.get("employee") or "").strip().lower() for _i, r in mine}
            if len(on) < rule["min"]:
                i0, r0 = (mine or items)[0]
                out.append(_v("owner_rule", i0, r0,
                              f"{len(on)} {rule['role']} on {day} — your rule: \u201c{rule['text'][:120]}\u201d",
                              floor_role=role_low, severity=rule["min"] - len(on)))
        ends = [(end_minutes(r), i, r) for i, r in items]
        ends = [(e, i, r) for e, i, r in ends if e is not None]
        if not ends:
            continue
        close_m = close_minutes(c, day)
        e_last, i_last, r_last = max(ends, key=lambda t: t[0])
        target = close_m if close_m is not None else e_last
        if close_m is not None and e_last < close_m - 15:
            out.append(_v("nobody_at_close", i_last, r_last,
                          f"the last shift {day} ends {r_last.get('shift_end')}, you close at {_fmt_minutes(close_m % (24 * 60))}",
                          severity=close_m - e_last))
        if c.compliance.get("keyholder_until_close", True) and c.keyholders:
            covered = any(e >= target - 15 for e, _i, r in ends
                          if (r.get("employee") or "").strip().lower() in c.keyholders)
            if not covered:
                out.append(_v("keyholder_until_close", i_last, r_last,
                              f"no closer on {day} until {'close' if close_m is not None else 'the last shift ends'}"
                              f" ({_fmt_minutes(target % (24 * 60))})"))
    return out


# ── a manager on the floor every minute anyone is (owner, 10/2/26) ─────────
#
# "There can be ZERO shifts without ONE manager. Always." — Erik's first
# generated week had no manager and neither owner on any day. A manager is
# a roster person whose role is a manager or owner role, or who holds the
# manager certification; the person counts, whatever role their row is in.
# A minute with somebody on and no manager on is a hard breach: it holds
# the publish, and every pass that changes rows is refused one that makes a
# new one. cover_manager_gaps is the deterministic backstop that fills them.

import re as _re_mgr
_MANAGER_ROLE = _re_mgr.compile(r"\b(manager|mgr|gm|general manager|owner|supervisor)\b", _re_mgr.I)


def is_manager_role(role) -> bool:
    return bool(_MANAGER_ROLE.search(str(role or "")))


def _span(r):
    """(start, end) in minutes past the row's business date's midnight — a
    12:30am porter is 1470-1680 of the night it works, never a dawn stretch
    of its date with nobody managing (E-32)."""
    s, e = start_minutes(r), end_minutes(r)
    if s is None or e is None or e <= s:
        return None
    return s, e


def _merge(spans):
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _uncovered(need, have):
    """The parts of the merged spans `need` that the merged spans `have`
    leave open."""
    gaps = []
    for s, e in need:
        cur = s
        for hs, he in have:
            if he <= cur or hs >= e:
                continue
            if hs > cur:
                gaps.append((cur, hs))
            cur = max(cur, he)
            if cur >= e:
                break
        if cur < e:
            gaps.append((cur, e))
    return gaps


def manager_gaps(rows: list, c: "Constraints", dates=None) -> dict:
    """{date: [(start_min, end_min, row_index)]} — each stretch somebody is
    on and no manager is, pinned to the first row on at its start. Empty
    when the roster has no manager (no_manager_roster says so instead)."""
    if not c.managers and not c.acting_managers:
        return {}
    by_date = {}
    for i, r in enumerate(rows or []):
        d = r.get("date")
        if not d or not (r.get("employee") or "").strip() or d in (c.closed_dates or set()):
            continue
        if dates is not None and d not in dates:
            continue
        sp = _span(r)
        if sp:
            by_date.setdefault(d, []).append((sp, i, r))
    out = {}
    for d, items in sorted(by_date.items()):
        staffed = _merge([sp for sp, _i, _r in items])
        mgr = _merge([sp for sp, _i, r in items
                      if c.manages(r.get("employee"), d)
                      and c.can_work(r.get("employee"), d)[0]])
        gaps = [(s, e) for s, e in _uncovered(staffed, mgr) if e - s > 0]
        if not gaps:
            continue
        pinned = []
        for gs, ge in gaps:
            on = sorted((sp[0], i) for sp, i, _r in items if sp[0] <= gs < sp[1])
            pinned.append((gs, ge, on[0][1] if on else items[0][1]))
        out[d] = pinned
    return out


def _manager_violations(rows: list, c: "Constraints") -> list:
    """no_manager per unmanaged stretch, and the soft no_manager_roster when
    nobody on the roster manages. Each no_manager is about its DAY, not the
    row it is pinned to (`day_level`): the review shows it on the day, never
    as "needs review" on whichever server happened to be on at its start
    (schedule audit 10/3/26 E-13)."""
    out = []
    staffed = [(i, r) for i, r in enumerate(rows or []) if r.get("date") and (r.get("employee") or "").strip()
               and r.get("date") not in (c.closed_dates or set())]
    if not staffed:
        return out
    only = None
    if not c.managers:
        # Somebody standing in as the manager on given dates (E-13) holds
        # those dates to the rule; the rest of the week has nobody who could.
        only = {d for ds in (c.acting_managers or {}).values() for d in (ds or ())}
        if not c.roster_names and not only:
            return out        # no roster read (a bare rule check): nothing to say who manages
        if c.roster_names:
            i0, r0 = staffed[0]
            out.append(_v("no_manager_roster", i0, r0,
                          "nobody on the roster has a manager or owner role, so no shift can have a manager on — "
                          "give your managers their role in Team"))
        if not only:
            return out
    for d, gaps in manager_gaps(rows, c, dates=only).items():
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day = d
        for gs, ge, idx in gaps:
            out.append(_v("no_manager", idx, rows[idx],
                          f"no manager on {day} from {_fmt_minutes(gs % (24 * 60))} to {_fmt_minutes(ge % (24 * 60))}",
                          gap_start=gs, gap_end=ge, minutes=ge - gs, day_level=True))
    return out


# ── breaches compared by who and when, never by row position ───────────────
#
# Every pass compared (row index, kind) before and after a change, so a new
# breach pinned to the same row as an old one was invisible: two manager
# gaps on one day share a pin, and the overtime rebalance opened a 7-hour
# no-manager stretch on a day that already had a 1-hour one; the optimizer
# compared only counts (schedule audit 10/3/26 E-1, P-14). A breach is now
# identified by what it is about — the person and the date (or the payroll
# week, or the run), the role and daypart of a floor, the date of a close —
# with a severity (hours over, people short, days in the run), and "no
# manager" is minutes per date. A change may not add a breach, make one
# worse, or leave any day with more unmanaged minutes than before.
#
# The ranked priorities every repair is held to (P-47): a change made for a
# lower tier may never worsen a higher one.
TIER_PERSON = 0       # the person can't legally or physically work it
TIER_MANAGER = 1      # a manager on the floor every minute (owner, 10/2/26)
TIER_COVERAGE = 2     # floors, a closer until close, somebody at close
TIER_OVERTIME = 3
TIER_MIN_HOURS = 4
TIER_BUDGET = 5
TIER_QUALITY = 6

PERSON_KINDS = NO_SHOW | frozenset({"over_max_hours", "shift_too_long", "rest_gap", "minor_late", "minor_hours",
                                    "long_run", "minor_early", "minor_week_hours"})
COVERAGE_KINDS = frozenset({"coverage_floor", "keyholder_until_close", "nobody_at_close", "no_manager_on_duty",
                            "owner_rule", "ends_before_role_close"})
BREACH_TIER = {**{k: TIER_PERSON for k in PERSON_KINDS},
               "no_manager": TIER_MANAGER,
               **{k: TIER_COVERAGE for k in COVERAGE_KINDS},
               "daily_ot": TIER_OVERTIME,
               "payroll_tail_full": TIER_OVERTIME,
               "under_min_hours": TIER_MIN_HOURS}
# Breaches named on several rows that are one breach at its worst row.
_WORST_OF = frozenset({"over_max_hours", "long_run", "under_min_hours", "minor_week_hours", "minor_hours",
                       "daily_ot", "payroll_tail_full"})


def _iso_week(d) -> str:
    try:
        x = _as_date(d)
        return (x - timedelta(days=x.weekday())).isoformat()
    except (TypeError, ValueError):
        return str(d or "")


def _date_runs(dates) -> list:
    """Every run of consecutive dates (iso strings) in `dates`, each earliest
    first, the runs in date order."""
    try:
        ordered = sorted({datetime.strptime(str(d)[:10], "%Y-%m-%d") for d in dates if d})
    except (TypeError, ValueError):
        return []
    runs = []
    for d in ordered:
        if runs and (d - runs[-1][-1]).days == 1:
            runs[-1].append(d)
        else:
            runs.append([d])
    return [[d.strftime("%Y-%m-%d") for d in run] for run in runs]


def breach_id(v) -> tuple:
    """What a breach is about, independent of where in the list its row sits."""
    kind = v.get("kind")
    emp = (v.get("employee") or "").strip().lower()
    d = v.get("date") or ""
    if kind in ("over_max_hours", "payroll_tail_full"):
        return (kind, emp, v.get("bucket") or d)
    if kind in ("long_run", "under_min_hours", "minor_age_unknown", "days_off"):
        return (kind, emp)
    if kind == "minor_week_hours":
        return (kind, emp, v.get("week") or _iso_week(d))
    if kind == "coverage_floor":
        return (kind, d, v.get("floor_role") or (v.get("role") or "").strip().lower(), v.get("daypart") or "")
    if kind in ("owner_rule", "ends_before_role_close"):
        return (kind, d, v.get("floor_role") or (v.get("role") or "").strip().lower())
    if kind in ("nobody_at_close", "keyholder_until_close", "over_section_cap"):
        return (kind, d, v.get("close_role") or "")
    if kind == "no_manager":
        return (kind, d)
    if kind in ("no_manager_roster", "manager_rule_unusable"):
        return (kind,)
    if kind == "no_manager_on_duty":
        return (kind, d, daypart_of(v.get("shift_start") or ""))
    return (kind, d, emp)


def breach_profile(rows: list, c: "Constraints", viols: list = None, person_only: bool = False) -> dict:
    """{"by_id": {breach_id: severity}, "labels": {breach_id: label},
    "manager": {date: unmanaged minutes}} for `rows` — the before or after
    of a change, compared by regressions(). Pass `viols` when the sweep has
    already been run on these rows."""
    if viols is None:
        viols = violations(rows, c, person_only=person_only)
    by_id, labels, manager = {}, {}, {}
    for v in viols:
        if v.get("kind") == "no_manager":
            d = v.get("date") or ""
            manager[d] = manager.get(d, 0) + int(v.get("minutes") or 0)
            labels[("no_manager", d)] = v.get("detail") or v.get("label")
            continue
        bid = breach_id(v)
        sev = v.get("severity")
        sev = float(sev) if isinstance(sev, (int, float)) else 1.0
        # A breach repeated across rows (over_max_hours names every row of
        # the payroll week; a day's cap names every leg past it, E-6) counts
        # once, at its worst; any other repeat (two overlaps the same day)
        # adds up.
        if v.get("kind") in _WORST_OF:
            by_id[bid] = max(by_id.get(bid, 0.0), sev)
        else:
            by_id[bid] = by_id.get(bid, 0.0) + sev
        labels.setdefault(bid, v.get("detail") or v.get("label") or v.get("kind"))
    return {"by_id": by_id, "labels": labels, "manager": manager}


def regressions(before: dict, after: dict, upto: int = TIER_COVERAGE, hard_only: bool = True) -> list:
    """What `after` made worse than `before`: a breach that is new or more
    severe, in a tier at or above `upto` (TIER_PERSON is the highest), and
    any day with more unmanaged minutes when the manager tier is in range.
    Soft kinds count only when they carry a tier in range and `hard_only` is
    False. [{id, before, after, label, tier}], most important first."""
    out = []
    for bid, sev in (after.get("by_id") or {}).items():
        kind = bid[0]
        tier = BREACH_TIER.get(kind)
        if tier is None or tier > upto:
            continue
        if hard_only and kind not in HARD:
            continue
        was = (before.get("by_id") or {}).get(bid, 0.0)
        if sev > was + 0.01:
            out.append({"id": bid, "before": was, "after": sev, "tier": tier,
                        "label": (after.get("labels") or {}).get(bid) or LABELS.get(kind, kind)})
    if TIER_MANAGER <= upto:
        for d, mins in (after.get("manager") or {}).items():
            was = (before.get("manager") or {}).get(d, 0)
            if mins > was:
                out.append({"id": ("no_manager", d), "before": was, "after": mins, "tier": TIER_MANAGER,
                            "label": (after.get("labels") or {}).get(("no_manager", d)) or LABELS["no_manager"]})
    out.sort(key=lambda x: (x["tier"], str(x["id"])))
    return out


def hourly_hours(rows: list, c: "Constraints" = None, salaried=None) -> float:
    """Hours of the rows worked by hourly people — the hours an hourly budget
    is spent from. A salaried person's hours are not (schedule audit
    10/3/26 P-6, E-7, D-3): counting them made a week the review called
    within budget a publish blocker "over the ceiling" by about the
    salaried hours, and the optimizer refuse every add once managers were on.
    `salaried` (an iterable of names) stands in for `c` where no
    Constraints exist."""
    if c is not None:
        paid_same = c.is_salaried
    else:
        keys = {" ".join(str(n or "").lower().split()) for n in (salaried or ())}
        paid_same = lambda n: " ".join(str(n or "").lower().split()) in keys  # noqa: E731
    return round(sum(row_hours(r) for r in (rows or []) if not paid_same(r.get("employee"))), 2)


def hours_split(rows: list, c: "Constraints" = None, salaried=None) -> dict:
    """{"hourly", "salaried", "total"} hours of `rows`: the hourly part is
    hourly_hours (the one hourly-hours sum), the salaried part the rest.
    What schedule_history keeps (hours_hourly / hours_salaried) and what
    the publish gate holds against the hourly budget (schedule audit
    10/3/26 E-7, P-6)."""
    total = sum(row_hours(r) for r in (rows or []))
    hourly = hourly_hours(rows, c=c, salaried=salaried)
    return {"hourly": round(hourly, 1), "salaried": round(max(0.0, total - hourly), 1), "total": round(total, 1)}


def _v(kind, index, row, detail, **extra):
    """One breach. `extra` carries what identifies it beyond the person and
    date (breach_id) and how bad it is (`severity`: hours over, people
    short, days in the run) — so a change that makes it worse is seen even
    when the breach was already there."""
    out = {"kind": kind, "index": index, "employee": row.get("employee"), "date": row.get("date"),
           "day": row.get("day"), "shift_start": row.get("shift_start"), "role": row.get("role"),
           "detail": detail, "hard": kind in HARD, "no_show": kind in NO_SHOW, "label": LABELS.get(kind, kind)}
    out.update(extra)
    return out


def _mdy(iso) -> str:
    """M/D/YY, the one date an owner reads (time_utils.mdy)."""
    from time_utils import mdy
    return mdy(iso)


def mdy_payroll(b) -> str:
    """A payroll week by its dates: "10/7/26–10/13/26"."""
    from time_utils import mdy_range
    try:
        start = datetime.strptime(str(b)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return str(b or "")
    return mdy_range(start.date(), (start + timedelta(days=6)).date()).replace(" – ", "–")


def _tail_days_text(dates) -> str:
    """"Mon 10/12/26 and Tue 10/13/26"."""
    names = [f"{_weekday_of(d)[:3]} {_mdy(d)}" for d in sorted(dates or [])]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def role_cross_training(restaurant) -> dict:
    """{role lower: share 0-1} — the owner's cross-training target per role
    (restaurants.role_cross_training_json, stored as whole percents like
    {"Server": 40}). A role left out takes shift_quality's default for it."""
    raw = _load_json(getattr(restaurant, "role_cross_training_json", None), {}) if restaurant else {}
    out = {}
    for k, v in (raw or {}).items():
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if str(k).strip() and n == n:
            out[str(k).strip().lower()] = max(0.0, min(100.0, n)) / 100.0
    return out


# Soft breaches a repair pass acts on, and that no later pass (the solver)
# may bring back once repaired: a missed run of days off (shift_quality.
# apply_fixes hands one of the person's shifts to a legal teammate), and a
# person under the minimum the owner set for them (fill_min_hours gives them
# legal shifts — schedule audit 10/3/26 P-4: Erik's full-time cook, set to
# 40-45h, got 7h and only a soft line said so). Both stay soft — they never
# block publishing — but neither is a warning nothing acts on.
FIXABLE_SOFT = frozenset({"days_off", "under_min_hours"})


def fixable(viols: list) -> list:
    """The violations the fix pass is handed: every hard breach, and the soft
    ones it knows how to repair (FIXABLE_SOFT)."""
    return [v for v in (viols or []) if v.get("hard") or v.get("kind") in FIXABLE_SOFT]


def breach_text(v) -> str:
    """One breach as the owner reads it: "Ann — Wednesday 5:00pm: <detail>"
    for a breach about a person's row, and "10/7/26 — <detail>" for one about
    a DAY (`day_level`: no manager on, a floor short, nobody at close), which
    is never charged to whoever's row it happens to be pinned to (schedule
    audit 10/3/26 E-13)."""
    if v.get("day_level"):
        return f"{_mdy(v.get('date')) or v.get('day') or ''} — {v.get('detail')}".strip(" —")
    where = f"{v.get('day') or v.get('date')} {v.get('shift_start') or ''}".strip()
    return f"{v.get('employee')} — {where}: {v.get('detail')}"


def summarize(viols: list) -> dict:
    """The review's counts and lines. `hard_rows` are the rows a hard breach
    is about — never the row a day-level breach is pinned to (E-13); those
    are `hard_days` [{date, day, kind, detail}], one per breach, for the
    review to show on the day."""
    hard = [v for v in viols if v["hard"]]
    soft = [v for v in viols if not v["hard"]]
    by_kind = {}
    for v in viols:
        by_kind[v["kind"]] = by_kind.get(v["kind"], 0) + 1
    lines = []
    for v in sorted(viols, key=lambda x: (not x["hard"], x["date"] or "", x["employee"] or ""))[:12]:
        lines.append(f"{'⚠ ' if v['hard'] else ''}{breach_text(v)}")
    return {"hard": len(hard), "soft": len(soft), "by_kind": by_kind, "lines": lines,
            "hard_rows": sorted({v["index"] for v in hard if not v.get("day_level")}),
            "hard_days": [{"date": v.get("date"), "day": v.get("day") or _weekday_of(v.get("date")),
                           "kind": v["kind"], "detail": v.get("detail")}
                          for v in sorted(hard, key=lambda x: (x.get("date") or "", x["kind"])) if v.get("day_level")]}


def prompt_block(c: Constraints, manager_plan: dict = None) -> str:
    """The rules, as the model should read them — the same facts the code
    will check afterwards, so the draft has every chance to be right.
    `manager_plan` (schedule_skeleton.plan_manager_coverage) — when the
    managers' shifts were planned, the manager line points at them."""
    comp = c.compliance
    lines = []
    if c.managers:
        _mg = ", ".join(sorted(f"{n} ({c.managers.get(n.strip().lower()) or 'manager'})" for n in (c.roster_names or [])
                               if n.strip().lower() in c.managers))
        _names = {n.strip().lower(): n for n in (c.roster_names or [])}
        _acting = "; ".join(f"{_names.get(k, k)} on " + ", ".join(sorted(d for d in v if d in (c.week_dates or v)))
                            for k, v in sorted((c.acting_managers or {}).items())
                            if k not in c.managers and any(d in (c.week_dates or v) for d in v))
        # The same rule PRIORITIES 1a states, in the same rank: it used to
        # call itself "above every other rule" — above the availability and
        # time off PRIORITIES puts first (schedule audit 10/3/26 PR-1).
        lines.append("- NON-NEGOTIABLE — PRIORITIES 1a, the owner's highest staffing rule: at every minute anybody is "
                     f"on the schedule, at least ONE manager is on too. The managers: {_mg}."
                     + (f" Standing in as the manager, on those dates only: {_acting}." if _acting else "")
                     + " It gives way only to a manager's own availability, time off and legal limits. Every trading "
                       "day has a manager from the first shift's start to the last shift's end — overlap them so "
                       "there is never a gap, not even a few minutes."
                     + (" Their shifts are already planned: MANAGER COVERAGE, at the top, is fixed."
                        if (manager_plan or {}).get("rows") else ""))
    if c.closed_dates:
        _pretty = ", ".join(datetime.strptime(d, "%Y-%m-%d").strftime("%A %-m/%-d") for d in sorted(c.closed_dates))
        lines.append(f"- The restaurant is CLOSED on {_pretty}. Write no shifts at all on those dates.")
    if comp.get("min_rest_hours"):
        lines.append(f"- At least {float(comp['min_rest_hours']):g} hours between one shift's end and the same person's next start. No closing then opening.")
    if comp.get("max_shift_hours"):
        lines.append(f"- No shift longer than {float(comp['max_shift_hours']):g} hours.")
    if comp.get("min_shift_hours"):
        lines.append(f"- No shift shorter than {float(comp['min_shift_hours']):g} hours.")
    # A start in the small hours is read as the night it belongs to (E-32),
    # so the model is told to date it that way.
    lines.append("- A shift that starts after midnight belongs to the night before: write it on that night's date "
                 "(a 12:30am-4:00am porter after Friday's close is dated Friday).")
    # A person's own maximum above the ceiling is the owner allowing them the
    # hours, overtime included — the code checks the same (P-12, D-18).
    _ceiling = float(comp.get("weekly_hours_ceiling") or 40)
    _above = any(lim and lim[1] and float(lim[1]) > _ceiling and not c.is_salaried(n)
                 for n, lim in ((n, c.hours_limits.get(n.lower())) for n in (c.roster_names or [])))
    lines.append(f"- Nobody over {_ceiling:g} hours in the payroll week"
                 + (" (the week starts on " + DAYS[c.week_start_day] + ")" if c.week_start_day else "")
                 + (" — except where a person's own maximum below is higher: the owner allows them those hours, "
                    "overtime included" if _above else "") + ".")
    # A payroll week that runs on into next week's days (E-10): next week's
    # draft schedules them inside the same overtime line.
    _tails = [c.bucket_tail(b) for b in sorted({c.bucket(d) for d in (c.week_dates or [])})]
    _tail = next((t for t in _tails if len(t) >= TAIL_RESERVE_MIN_DAYS), None)
    if _tail:
        _from = DAYS[c.week_start_day] if c.week_start_day else "Monday"
        lines.append(f"- The payroll week that starts {_from} runs on into next week ({_tail_days_text(_tail)}), which "
                     f"next week's schedule fills from the same overtime line: leave each hourly person about "
                     f"{len(_tail)}/7 of their line free for those days (about {40 * len(_tail) / 7:.0f}h of 40h).")
    if comp.get("daily_ot_hours"):
        lines.append(f"- Daily overtime starts at {float(comp['daily_ot_hours']):g} hours — avoid it.")
    if comp.get("meal_break_after_hours"):
        lines.append(f"- A shift over {float(comp['meal_break_after_hours']):g} hours includes a meal break; leave room for it.")
    if comp.get("max_consecutive_days"):
        lines.append(f"- Nobody works more than {int(comp['max_consecutive_days'])} days in a row, counting days already published last week.")
    if comp.get("min_consecutive_days_off"):
        lines.append(f"- Everyone gets at least {int(comp['min_consecutive_days_off'])} consecutive days off"
                     + (f"; part-time staff {int(comp['part_time_days_off'])}." if comp.get("part_time_days_off") else "."))
    if comp.get("manager_on_duty"):
        lines.append("- Every open daypart has somebody authorized to close or holding a manager/keyholder certification on it.")
    if comp.get("keyholder_until_close", True) and c.keyholders:
        lines.append("- Every open day, one of the people marked as a closer works until close and is the last of their "
                     "role to leave. A closer is somebody chosen to close, not somebody with keys.")
    if c.role_floors:
        lines.append("- The staffing floors below are hard: a day under a floor is flagged to the owner.")
    pack = comp.get("_pack") or {}
    if pack.get("applied"):
        # Starting values from a pack, never "the law": the Illinois pack's
        # 10-hour rest is the Chicago ordinance's, not the state's (NS5 L13).
        lines.append(f"- Starting values from the {pack.get('label')} pack (set in Cavnar AI, not a statement of the law): "
                     + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in pack["applied"].items()) + ".")
    if c.role_requirements:
        lines.append("- Certifications by role: " + "; ".join(f"{r} needs {', '.join(sorted(v))}" for r, v in sorted(c.role_requirements.items())) + " — only schedule people who hold them.")
    if c.salaried:
        # Each one's weekly cap — their own, the restaurant's, or the
        # default — is the most the code gives them (E-17).
        lines.append("- Salaried (the same pay whatever the hours): "
                     + ", ".join(f"{n.title()} (at most {c.salaried_limit(n):g}h a week)" for n in sorted(c.salaried))
                     + " — no overtime and no hourly ceiling for them, and their hours are not spent from the "
                       "hourly hours budget. Never move an hourly person's shift onto them to save overtime.")
    if c.stations:
        import kitchen_stations as _ks
        lines.extend(_ks.prompt_lines(c.stations))
    if c.close_mins and c.close_times:
        lines.append("- Stays after close: " + "; ".join(f"the last {role} until {m} min after close" for role, m in sorted(c.close_mins.items())) + ".")
    if c.minors:
        # The limits Cavnar CHECKS, said as that — not as the law. Minors
        # with an age band carry the band's federal floor (NS5 H4).
        lines.append("- MINORS — the limits the code checks (starting values, not legal advice):")
        for n in sorted({n for n in c.roster_names if n.lower() in c.minors}):
            band = c.minor_bands.get(n.lower())
            br = minor_rules(band, c.jurisdiction)
            if br.get("earliest_start"):
                lines.append(f"  {n} (age {band}): start no earlier than {br['earliest_start']}; finish by "
                             f"{br['latest_end_school']} ({br['latest_end_summer']} June 1 to Labor Day); at most "
                             f"{br['max_daily_school_day']:g}h on a school day and {br['max_weekly_school_week']:g}h in a "
                             f"school week, {br['max_daily_other_day']:g}h a day and {br['max_weekly_other_week']:g}h a week "
                             f"otherwise. Treat every weekday outside June 1 to Labor Day as a school day.")
            else:
                lines.append(f"  {n}" + (f" (age {band})" if band else "") + f": finish by {comp.get('minor_latest_end')}, "
                             f"at most {float(comp.get('minor_max_daily_hours') or 8):g} hours a day.")
    per_person = []
    for n in c.roster_names:
        key = n.lower()
        bits = []
        lim = c.hours_limits.get(key)
        if lim:
            if lim[0]:
                bits.append(f"at least {float(lim[0]):g}h")
            if lim[1]:
                from labor import OVERTIME_THRESHOLD_HOURS as _OT_PB
                over = float(lim[1]) > float(_OT_PB) and not c.is_salaried(n)
                bits.append(f"at most {float(lim[1]):g}h"
                            + (f" (overtime past {float(_OT_PB):g}h allowed for them)" if over else ""))
        if c.employment.get(key):
            bits.append(f"{c.employment[key]}-time")
        dp = c.daypart_avail.get(key) or {}
        offs = [d[:3] for d in DAYS if dp.get(d) == "off"]
        mornings = [d[:3] for d in DAYS if dp.get(d) == "morning"]
        nights = [d[:3] for d in DAYS if dp.get(d) == "night"]
        if offs:
            bits.append("off " + "/".join(offs))
        if mornings:
            bits.append("lunch/day only " + "/".join(mornings))
        if nights:
            bits.append("dinner/night only " + "/".join(nights))
        win = c.time_windows.get(key) or {}
        for d in DAYS:
            w = win.get(d)
            if w:
                lo, hi = w
                span = (f"from {_fmt_minutes(lo)}" if lo is not None else "") + (f" until {_fmt_minutes(hi)}" if hi is not None else "")
                bits.append(f"{d[:3]} only {span.strip()}")
        certs = c.certifications.get(key)
        if certs:
            bits.append("holds " + ", ".join(sorted(certs)))
        base = c.base_hours.get(key) or {}
        carried = sum(h for b, h in base.items() if b in {c.bucket(d) for d in c.week_dates})
        if carried:
            bits.append(f"{carried:g}h already published in this payroll week")
        pend = c.pending_off.get(key)
        if pend:
            bits.append("has asked for " + ", ".join(sorted(pend)) + " off (not yet decided — avoid if the day can be covered)")
        if bits:
            per_person.append(f"  {n}: " + "; ".join(bits))
    block = "\n\nRULES THE SCHEDULE IS CHECKED AGAINST (every one of these is verified in code after you write the week; a breach is flagged to the owner):\n" + "\n".join(lines)
    if per_person:
        block += "\n\nPER-PERSON LIMITS AND WINDOWS:\n" + "\n".join(per_person)
    floors = []
    for role, spec in (c.role_floors or {}).items():
        base = []
        if spec.get("morning"):
            base.append(f"{spec['morning']} lunch/day")
        if spec.get("night"):
            base.append(f"{spec['night']} dinner/night")
        days = "; ".join(f"{d}: " + ", ".join(f"{n} {p}" for p, n in ds.items() if n) for d, ds in (spec.get("days") or {}).items())
        if base or days:
            floors.append(f"  {role}: at least " + ", ".join(base) + (f" — {days}" if days else ""))
    if floors:
        block += "\n\nSTAFFING FLOORS BY ROLE AND DAYPART (never below these, whatever the hours ceiling says):\n" + "\n".join(floors)
    return block


# ── overtime, avoided before anything else is fixed ────────────────────────
#
# Owners do not pay overtime they don't have to (owner, 9/26/26: "VERY
# critical"). The model is told the weekly line, but it ranks below shift
# requirements in its PRIORITIES, and a 55-person week came back with
# people at 48-52h while teammates in the same role sat at 25h. This pass
# takes a person over their line back under it, deterministically:
#   1. hand one of their shifts to a teammate who can work the role — the
#      same role family or a role they hold (trained, promoted), already on
#      that day as a second leg or not — with room under their own line,
#      least tired and fewest hours first (the latest shift in the payroll
#      week first: that is where the premium hours are);
#   2. else split one: the teammate takes the tail (or the head) of it, by
#      running their own shift on or as a shift of their own;
#   3. else trim it by the excess (at most 2.5h a shift, spread over several,
#      never below the shortest shift) where the role's family covers the
#      trimmed stretch;
#   4. otherwise leave it and say so.
# Every change is judged by what it is about (breach_profile/regressions,
# schedule audit 10/3/26 E-1): nothing about a person's legality, the
# manager rule or coverage may get new or worse, and no new daily overtime.
# A receiver keeps the reserve their payroll week owes its days in next week
# (E-10) where anybody has the room. The line is 40h
# (labor.OVERTIME_THRESHOLD_HOURS), or the person's own weekly maximum when
# the owner set it higher: that is the owner saying this one may work
# overtime. A minor's age-band weekly cap is a line too (E-27).

OT_TRIM_MAX_HOURS = 2.5
OT_MIN_SHIFT_HOURS = 4.0
OT_MAX_SWEEPS = 300
# Running a teammate's shift on, or starting it earlier, to take over a
# stretch: their shift has to end (or start) within this of it.
_ADJACENT_MIN = 3 * 60
# A split hands over the tail of a shift to a teammate whose own shift
# ends no more than this before it (running them on fills what is between).
_SPLIT_JOIN_MIN = 60


def overtime_line(c: "Constraints", name: str, line: float = None) -> float:
    """The most hours in a payroll week code gives a person: the overtime
    line (40h, labor.OVERTIME_THRESHOLD_HOURS), or their own maximum when
    the owner set it higher — the owner allowing them overtime up to it,
    one rule with max_hours (P-12, D-18) — or lower. A salaried person owes
    no overtime: their line is the salaried cap (Constraints.salaried_limit,
    E-17)."""
    from labor import OVERTIME_THRESHOLD_HOURS
    if c.is_salaried(name):
        return c.salaried_limit(name)
    ot = float(line or OVERTIME_THRESHOLD_HOURS)
    mx = c.max_hours(name)
    lim = c.hours_limits.get((name or "").strip().lower())
    if lim and lim[1] and float(lim[1]) > ot:
        return mx
    return min(mx, ot) if mx else ot


def _hard_keys(rows: list, c: "Constraints") -> set:
    """(row index, kind) of every hard breach — close_out_gaps' comparison
    until it moves to breach_profile/regressions like the passes below."""
    return {(v.get("index"), v.get("kind")) for v in violations(rows, c) if v.get("hard")}


# ── what every repair pass shares (schedule audit 10/3/26 E-1, P-2) ────────
#
# A pass changes rows only through _Repair.judge: the change is swept whole
# and refused when anything at or above the pass's tier is new or worse —
# compared by what each breach is about, never by the row it is pinned to.
# Every person a pass hands a shift to is asked can_add (is the row legal
# for them, their own week swept with it in) and fillable (code never picks
# somebody dormant, or on a day their unconfirmed note covers). A row the
# owner or the manager plan pinned ("_pinned") is never moved, re-timed or
# given away; it still counts as coverage and hours.

def _low(name) -> str:
    return (name or "").strip().lower()


def _is_pinned(row) -> bool:
    return bool((row or {}).get("_pinned"))


def shortest_shift_hours(c: "Constraints", default=None) -> float:
    """The shortest shift a pass may leave or make: the owner's
    min_shift_hours when set, else `default` (0.0 when none)."""
    v = (c.compliance or {}).get("min_shift_hours")
    try:
        if v:
            return float(v)
    except (TypeError, ValueError):
        pass
    return float(default or 0.0)


class _Repair:
    """One repair pass's working rows, their sweep and breach profile, and
    the test every change it tries must pass. `upto`: the hard tiers that
    may not get new or worse; `soft_upto`: soft kinds of the tiers after
    TIER_COVERAGE through it (daily overtime, the payroll reserve) may not
    either; `ignore`: kinds the pass weighs itself instead."""

    def __init__(self, rows, c, upto=TIER_COVERAGE, soft_upto=None, max_sweeps=OT_MAX_SWEEPS, ignore=()):
        self.c = c
        self.rows = [dict(r) for r in (rows or [])]
        self.viols = violations(self.rows, c)
        self.prof = breach_profile(self.rows, c, viols=self.viols)
        self.upto, self.soft_upto, self.ignore = upto, soft_upto, frozenset(ignore)
        self.sweeps, self.max_sweeps = 0, max_sweeps

    @property
    def spent(self) -> bool:
        return self.sweeps >= self.max_sweeps

    def judge(self, trial, upto=None, soft_upto="same"):
        """(worse, after): what `trial` makes new or worse at the pass's
        tiers ([] = nothing), and its sweep and profile to take()."""
        self.sweeps += 1
        viols = violations(trial, self.c)
        after = breach_profile(trial, self.c, viols=viols)
        upto = self.upto if upto is None else upto
        soft = self.soft_upto if soft_upto == "same" else soft_upto
        worse = regressions(self.prof, after, upto=upto, hard_only=True)
        if soft is not None and soft > TIER_COVERAGE:
            worse += [w for w in regressions(self.prof, after, upto=soft, hard_only=False)
                      if w["tier"] > TIER_COVERAGE and w["id"][0] not in HARD]
        worse = [w for w in worse if w["id"][0] not in self.ignore]
        return worse, (viols, after)

    def take(self, trial, after):
        self.rows = trial
        self.viols, self.prof = after

    def severity(self, bid) -> float:
        return float((self.prof.get("by_id") or {}).get(bid, 0.0))


def _display(c: "Constraints", rows, roster_roles=None) -> dict:
    """{lower: name} for everyone a pass may hand a shift to: the roster,
    the roster's roles, and anybody already on the rows."""
    out = {}
    for n in list(c.roster_names or []) + list((roster_roles or {}).keys()):
        if n and str(n).strip():
            out.setdefault(_low(n), str(n).strip())
    for r in rows or []:
        n = (r.get("employee") or "").strip()
        if n:
            out.setdefault(n.lower(), n)
    return out


def _role_families(c: "Constraints", roster_roles=None) -> dict:
    """{lower: set(role family)}: the roles a pass may hand a person — their
    roster role's family ("Server AM" and "Server PM" are one role, D-13),
    and every role they hold (c.held_roles: trained or promoted, D-15), so a
    server trained on bar is a teammate for a bar shift (P-30, P-31)."""
    fam = {}
    for n, role in (roster_roles or {}).items():
        if n and str(role or "").strip():
            fam.setdefault(_low(n), set()).add(c.family(role))
    for low, held in (c.held_roles or {}).items():
        for role in held or ():
            if str(role or "").strip():
                fam.setdefault(_low(low), set()).add(c.family(role))
    return fam


def _takes(c: "Constraints", fam: dict, rows, low: str, role) -> bool:
    """Whether `low` may be handed a shift in `role`. Somebody with no
    roster role on file is judged by the roles of their own rows."""
    have = fam.get(low)
    if not have:
        have = {c.family(r.get("role")) for r in rows or ()
                if _low(r.get("employee")) == low and (r.get("role") or "").strip()}
    return bool(have) and c.family(role) in have


def _bucket_hours(c: "Constraints", rows, low: str, b: str) -> float:
    return (float((c.base_hours.get(low) or {}).get(b, 0.0) or 0.0)
            + sum(row_hours(r) for r in rows or () if _low(r.get("employee")) == low
                  and r.get("date") and c.bucket(r["date"]) == b))


def _day_hours(rows, low: str, d: str) -> float:
    return sum(row_hours(r) for r in rows or () if _low(r.get("employee")) == low and r.get("date") == d)


def _run_through(c: "Constraints", rows, low: str, d: str) -> int:
    """Days in a row `low` works with `d` among them (this week and the
    published tail) — the fatigue a pass ranks who takes on more by (E-17)."""
    worked = {r.get("date") for r in rows or () if _low(r.get("employee")) == low and r.get("date")}
    worked |= {r.get("date") for r in (c.base_rows.get(low) or []) if r.get("date")}
    try:
        d0 = datetime.strptime(d, "%Y-%m-%d")
    except (TypeError, ValueError):
        return 1
    n = 1
    for step in (-1, 1):
        k = 1
        while (d0 + timedelta(days=step * k)).strftime("%Y-%m-%d") in worked:
            n += 1
            k += 1
    return n


def _fatigue_rank(c: "Constraints", rows, low: str, d: str, add_hours: float = 0.0) -> tuple:
    """(days in a row, hours in the payroll week) once `low` takes on
    `add_hours` on `d`: the least tired first, then the fewest hours — never
    "salaried first" (E-17)."""
    return (_run_through(c, rows, low, d), round(_bucket_hours(c, rows, low, c.bucket(d)) + add_hours, 2))


def _retimed(row: dict, start_m: int, end_m: int, c: "Constraints") -> dict:
    """`row` running start_m..end_m (minutes past its business date's
    midnight), its hours the real length (E-23)."""
    out = dict(row)
    out["shift_start"] = _fmt_minutes(start_m % (24 * 60))
    out["shift_end"] = _fmt_minutes(end_m % (24 * 60))
    out["scheduled_hours"] = hours_text(span_hours(out, c.tz))
    return out


def _new_row(template: dict, d: str, name: str, role: str, start_m: int, end_m: int, note: str,
             c: "Constraints") -> dict:
    row = {k: "" for k in (template or {}) if not str(k).startswith("_")}
    row.update({"date": d, "day": _weekday_of(d) or (template or {}).get("day") or "", "employee": name,
                "role": role, "notes": note})
    return _retimed(row, start_m, end_m, c)


def _covered(rows, c: "Constraints", d: str, family: str, lo: int, hi: int, skip=()) -> bool:
    """Whether people in the role family other than the rows `skip` are on
    for all of lo..hi on `d` (business minutes)."""
    spans = []
    for j, x in enumerate(rows or ()):
        if j in skip or x.get("date") != d or c.family(x.get("role")) != family:
            continue
        sp = _span(x)
        if sp:
            spans.append(sp)
    return not _uncovered([(lo, hi)], _merge(spans)) if hi > lo else True


def _with_note(row: dict, text: str) -> dict:
    out = dict(row)
    note = (out.get("notes") or "").strip()
    out["notes"] = (note + " " + text).strip() if note else text
    return out


def _receivers(c: "Constraints", rows, display: dict, fam: dict, row: dict, others, low_from: str,
               line: float = None, max_shift: float = None, salaried: bool = True, reserve: bool = False) -> list:
    """Who may take `row` off `low_from`, best first: somebody who can work
    the role (its family or a role they hold), whom code may choose that day
    (fillable), for whom the row is legal with their week swept (can_add,
    their overtime line included), and whose day stays inside the longest
    shift when it is a second leg. Ranked least tired, then fewest hours
    (E-17); with `reserve`, those who keep their payroll week's reserve for
    next week's days first (E-10). `salaried` False keeps hours moved to
    spare overtime off a salaried person's fixed week."""
    d = row.get("date") or ""
    max_shift = float(max_shift or c.compliance.get("max_shift_hours") or DEFAULTS["max_shift_hours"])
    ranked = []
    for low, nm in display.items():
        if low == low_from or not _takes(c, fam, rows, low, row.get("role")):
            continue
        if not salaried and c.is_salaried(nm):
            continue
        if not c.fillable(nm, d)[0]:
            continue
        cand = dict(row, employee=nm)
        if not c.can_add(cand, others, line=line, overtime=True)[0]:
            continue
        day = _day_hours(others, low, d)
        if day and day + row_hours(cand) > max_shift + 0.01:
            continue      # a double the code makes stays inside the longest shift
        keeps = 0
        if reserve:
            b = c.bucket(d)
            lim = overtime_line(c, nm, line)
            keeps = 0 if _bucket_hours(c, others, low, b) + row_hours(cand) <= lim - c.tail_reserve(lim, b) + 0.05 else 1
        ranked.append((keeps,) + _fatigue_rank(c, others, low, d, row_hours(cand)) + (nm,))
    ranked.sort()
    return [x[-1] for x in ranked]


def rebalance_overtime(rows: list, c: "Constraints", roster_roles: dict = None, editable=None,
                       line: float = None, max_sweeps: int = OT_MAX_SWEEPS) -> dict:
    """Returns {rows, moves: [{index, from, to, hours, kind, reason}], trims:
    [{index, employee, hours, was, now, kind, reason}], left: [{employee,
    hours, line, role, kind}], sweeps, over_before}. `editable` (a set of
    dates) limits which rows may change — a redo of some days keeps the
    owner's others. `kind` is "overtime", or "minor" for a minor's weekly cap."""
    rep = _Repair(rows, c, upto=TIER_COVERAGE, soft_upto=TIER_OVERTIME, max_sweeps=max_sweeps,
                  ignore=("payroll_tail_full",))
    moves, trims, left = [], [], []
    display = _display(c, rep.rows, roster_roles)
    fam = _role_families(c, roster_roles)
    shortest = shortest_shift_hours(c, OT_MIN_SHIFT_HOURS)
    max_shift = float(c.compliance.get("max_shift_hours") or DEFAULTS["max_shift_hours"])

    def name_of(low):
        return display.get(low, low)

    def free(r):
        return (editable is None or r.get("date") in editable) and not _is_pinned(r)

    def in_period(e, r):
        d = r.get("date") or ""
        if not d:
            return False
        return c.bucket(d) == e["key"] if e["kind"] == "overtime" else _iso_week(d) == e["key"]

    def total(e):
        if e["kind"] == "overtime":
            return _bucket_hours(c, rep.rows, e["low"], e["key"])
        base = sum(row_hours(r) for r in (c.base_rows.get(e["low"]) or []) if _iso_week(r.get("date") or "") == e["key"])
        return base + sum(row_hours(r) for r in rep.rows if _low(r.get("employee")) == e["low"] and in_period(e, r))

    def _kind(e):
        return "overtime" if e["kind"] == "overtime" else "minor"

    def _why(e):
        if e["kind"] != "overtime":
            return f"a minor's {e['limit']:g}h week"
        return f"inside the {e['limit']:g}h you set for them" if e.get("salaried") else "no overtime"

    def mine(e):
        idx = [i for i, r in enumerate(rep.rows) if _low(r.get("employee")) == e["low"] and in_period(e, r) and free(r)]
        return sorted(idx, key=lambda i: (rep.rows[i].get("date") or "", start_minutes(rep.rows[i]) or 0), reverse=True)

    # What is over: a person's payroll week past their overtime line, and a
    # minor's week past their age band's cap — the rebalance uses that cap
    # as their line (schedule audit 10/3/26 E-27).
    entries, totals = [], {}
    for low, per in (c.base_hours or {}).items():
        for b, h in (per or {}).items():
            totals[(low, b)] = totals.get((low, b), 0.0) + float(h or 0)
    for r in rep.rows:
        low = _low(r.get("employee"))
        if low and r.get("date"):
            k = (low, c.bucket(r["date"]))
            totals[k] = totals.get(k, 0.0) + row_hours(r)
    for (low, b), h in totals.items():
        # No overtime is owed on a salaried week: their line here is the hard
        # maximum (the owner's limit for them), never the default cap code
        # holds its own additions to — a model's 60h week for an owner who
        # works the floor is not moved onto paid hours.
        lim = (c.max_hours(name_of(low)) if c.is_salaried(name_of(low))
               else overtime_line(c, name_of(low), line))
        if b and lim and h > lim + 0.05:
            entries.append({"kind": "overtime", "low": low, "key": b, "limit": lim, "excess": h - lim,
                            "salaried": c.is_salaried(name_of(low))})
    for low in sorted(c.minors or ()):
        br = minor_rules(c.minor_bands.get(low), c.jurisdiction)
        if not br.get("max_weekly_school_week"):
            continue
        weeks = {}
        for r in list(c.base_rows.get(low) or []) + [r for r in rep.rows if _low(r.get("employee")) == low]:
            d = r.get("date") or ""
            try:
                wk = (_as_date(d) - timedelta(days=_as_date(d).weekday())).isoformat()
            except (TypeError, ValueError):
                continue
            weeks[wk] = weeks.get(wk, 0.0) + row_hours(r)
        for wk, h in weeks.items():
            cap = br.get("max_weekly_school_week" if is_school_week(wk) else "max_weekly_other_week")
            if cap and h > float(cap) + 0.01:
                entries.append({"kind": "minor_week", "low": low, "key": wk, "limit": float(cap), "excess": h - float(cap)})
    entries.sort(key=lambda e: (-e["excess"], e["low"], e["key"]))

    def hand_whole(e):
        name = name_of(e["low"])
        for i in mine(e):
            r = rep.rows[i]
            others = rep.rows[:i] + rep.rows[i + 1:]
            for nm in _receivers(c, rep.rows, display, fam, r, others, e["low"], line=line, max_shift=max_shift,
                                 salaried=e["kind"] != "overtime", reserve=e["kind"] == "overtime"):
                if rep.spent:
                    return False
                was = total(e)
                had = _bucket_hours(c, others, _low(nm), c.bucket(r.get("date") or ""))
                why = (f"kept them under {e['limit']:g}h" if e["kind"] == "overtime"
                       else f"a minor's {e['limit']:g}h week")
                trial = list(rep.rows)
                trial[i] = _with_note(dict(r, employee=nm), f"(was {name} — {why})")
                worse, after = rep.judge(trial)
                if worse:
                    continue
                rep.take(trial, after)
                h = row_hours(r)
                when = f"{r.get('day') or _weekday_of(r.get('date'))} {r.get('shift_start')}–{r.get('shift_end')}"
                moves.append({"index": i, "from": name, "to": nm, "hours": h, "kind": _kind(e),
                              "reason": (f"{name} would have run {was:g}h this payroll week; {nm} had room "
                                         f"({had:g}h) — {_why(e)}." if e["kind"] == "overtime" else
                                         f"{name} is a minor: {was:g}h in the week is past the {e['limit']:g}h cap; "
                                         f"{nm} takes {when}.")})
                return True
        return False

    def split(e):
        """Hand part of a shift over: a teammate runs their own shift on
        into its tail, or takes the tail (or head) as a shift of their own
        — never leaving either shorter than the shortest shift (P-31)."""
        name = name_of(e["low"])
        need = math.ceil((total(e) - e["limit"]) * 4) / 4.0
        m = int(round(need * 60))
        if m <= 0:
            return False
        for i in mine(e):
            r = rep.rows[i]
            sp = _span(r)
            if not sp:
                continue
            s, en = sp
            d, fam_r = r.get("date") or "", c.family(r.get("role"))
            day = r.get("day") or _weekday_of(d)
            # a. a teammate whose shift that day ends just before the tail runs on through it
            if (en - s - m) / 60.0 >= shortest - 0.01:
                cs = en - m
                for j, x in enumerate(rep.rows):
                    if rep.spent:
                        return False
                    if j == i or x.get("date") != d or not free(x) or c.family(x.get("role")) != fam_r:
                        continue
                    lowx = _low(x.get("employee"))
                    if lowx == e["low"] or (e["kind"] == "overtime" and c.is_salaried(x.get("employee"))):
                        continue
                    xs = _span(x)
                    if not xs or not (cs - _SPLIT_JOIN_MIN <= xs[1] <= cs) or not c.fillable(x.get("employee"), d)[0]:
                        continue
                    cut, ext = _retimed(r, s, cs, c), _retimed(x, xs[0], en, c)
                    others = [y for k, y in enumerate(rep.rows) if k not in (i, j)] + [cut]
                    if not c.can_add(ext, others, line=line, overtime=True)[0]:
                        continue
                    trial = list(rep.rows)
                    trial[i] = cut
                    trial[j] = _with_note(ext, f"(runs on for {name})")
                    worse, after = rep.judge(trial)
                    if worse:
                        continue
                    rep.take(trial, after)
                    trims.append({"index": i, "employee": name, "hours": need,
                                  "was": f"{r.get('shift_start')}–{r.get('shift_end')}",
                                  "now": f"{cut['shift_start']}–{cut['shift_end']}", "kind": _kind(e),
                                  "reason": (f"{x.get('employee')} stays on to {ext['shift_end']} and takes the last "
                                             f"{need:g}h of {name}'s {day} shift — {_why(e)}.")})
                    return True
            # b. the tail or the head as a teammate's own shift
            part = max(need, shortest)
            pm = int(round(part * 60))
            if (en - s - pm) / 60.0 < shortest - 0.01:
                continue
            for side in ("end", "start"):
                lo, hi = (en - pm, en) if side == "end" else (s, s + pm)
                cut = _retimed(r, s, en - pm, c) if side == "end" else _retimed(r, s + pm, en, c)
                others = [y for k, y in enumerate(rep.rows) if k != i] + [cut]
                piece = _new_row(r, d, "", r.get("role") or "", lo, hi, f"(split from {name}'s shift)", c)
                for nm in _receivers(c, rep.rows, display, fam, piece, others, e["low"], line=line, max_shift=max_shift,
                                     salaried=e["kind"] != "overtime", reserve=e["kind"] == "overtime"):
                    if rep.spent:
                        return False
                    trial = list(rep.rows)
                    trial[i] = cut
                    trial.append(dict(piece, employee=nm))
                    worse, after = rep.judge(trial)
                    if worse:
                        continue
                    rep.take(trial, after)
                    trims.append({"index": i, "employee": name, "hours": round(part, 2),
                                  "was": f"{r.get('shift_start')}–{r.get('shift_end')}",
                                  "now": f"{cut['shift_start']}–{cut['shift_end']}", "kind": _kind(e),
                                  "reason": (f"Split {name}'s {day} shift: {nm} takes {piece['shift_start']}–"
                                             f"{piece['shift_end']} — {_why(e)}.")})
                    return True
        return False

    def trim(e):
        """Trim a shift by the excess, at most OT_TRIM_MAX_HOURS of it, where
        the role's family covers the trimmed stretch."""
        name = name_of(e["low"])
        cut_h = min(math.ceil((total(e) - e["limit"]) * 4) / 4.0, OT_TRIM_MAX_HOURS)
        m = int(round(cut_h * 60))
        if m <= 0:
            return False
        for i in mine(e):
            r = rep.rows[i]
            sp = _span(r)
            if not sp or (sp[1] - sp[0] - m) / 60.0 < shortest - 0.01:
                continue
            s, en = sp
            for side in ("end", "start"):
                if rep.spent:
                    return False
                lo, hi = (en - m, en) if side == "end" else (s, s + m)
                if not _covered(rep.rows, c, r.get("date"), c.family(r.get("role")), lo, hi, skip={i}):
                    continue
                new = _retimed(r, s, en - m, c) if side == "end" else _retimed(r, s + m, en, c)
                trial = list(rep.rows)
                trial[i] = new
                worse, after = rep.judge(trial)
                if worse:
                    continue
                rep.take(trial, after)
                trims.append({"index": i, "employee": name, "hours": cut_h,
                              "was": f"{r.get('shift_start')}–{r.get('shift_end')}",
                              "now": f"{new['shift_start']}–{new['shift_end']}", "kind": "overtime",
                              "reason": f"Cut {name}'s {r.get('day') or r.get('date')} shift by {cut_h:g}h, "
                                        f"covered by the rest of the {r.get('role') or 'role'} — no overtime."})
                return True
        return False

    for e in entries:
        while total(e) > e["limit"] + 0.05 and not rep.spent:
            if hand_whole(e) or split(e) or (e["kind"] == "overtime" and trim(e)):
                continue
            break
        still = total(e)
        if still > e["limit"] + 0.05:
            left.append({"employee": name_of(e["low"]), "hours": round(still, 2), "line": e["limit"],
                         "kind": "overtime" if e["kind"] == "overtime" else "minor",
                         "role": next(((x.get("role") or "") for x in rep.rows if _low(x.get("employee")) == e["low"]), "")})
    return {"rows": rep.rows, "moves": moves, "trims": trims, "left": left, "sweeps": rep.sweeps,
            "over_before": [{"employee": name_of(e["low"]), "over_by": round(e["excess"], 2)}
                            for e in entries if e["kind"] == "overtime"]}


def close_out_gaps(rows: list, c: "Constraints", editable=None, line: float = None,
                   max_sweeps: int = 60) -> dict:
    """A night nobody (or no keyholder) is on until close, when a keyholder
    already works that night: their shift runs on to close (plus their
    role's minutes after close), if that keeps them under the overtime line
    and the shift-length rule and the full sweep shows no new hard breach.
    The model reads "Close: 12:00am Thu-Sat" and still ends the night at
    11:30pm; this is the deterministic backstop, the same shape as
    _enforce_close_time. Returns {rows, extended: [{index, employee, from,
    to, day, reason}]}."""
    rows = [dict(r) for r in rows]
    extended = []
    if not c.keyholders:
        return {"rows": rows, "extended": extended}
    base = _hard_keys(rows, c)
    sweeps = 0
    hours = {}
    for r in rows:
        low = (r.get("employee") or "").strip().lower()
        if low and r.get("date"):
            k = (low, c.bucket(r["date"]))
            hours[k] = hours.get(k, 0.0) + row_hours(r)
    for low, per in (c.base_hours or {}).items():
        for b, h in (per or {}).items():
            hours[(low, b)] = hours.get((low, b), 0.0) + float(h or 0)
    max_shift = float(c.compliance.get("max_shift_hours") or DEFAULTS.get("max_shift_hours") or 12)
    dates = sorted({r.get("date") for r in rows if r.get("date")})
    for d in dates:
        if d in (c.closed_dates or set()) or (editable is not None and d not in editable) or sweeps >= max_sweeps:
            continue
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue
        close_m = close_minutes(c, day)
        if close_m is None:
            continue
        items = [(i, r) for i, r in enumerate(rows) if r.get("date") == d and (r.get("employee") or "").strip()]
        ends = [(end_minutes(r), i, r) for i, r in items if end_minutes(r) is not None]
        if not ends:
            continue
        kh = [(e, i, r) for e, i, r in ends if (r.get("employee") or "").strip().lower() in c.keyholders]
        if any(e >= close_m - 15 for e, _i, _r in kh):
            continue
        for e, i, r in sorted(kh, key=lambda t: -t[0]):
            if sweeps >= max_sweeps:
                break
            s = parse_minutes(r.get("shift_start", ""))
            if s is None or e < close_m - 3 * 60:
                continue          # a day shift is not stretched into the night
            role = (r.get("role") or "").strip().lower()
            new_end = close_m + int((c.close_mins or {}).get(role, 0) or 0)
            add = (new_end - e) / 60.0
            new_len = (new_end - s) / 60.0
            name = (r.get("employee") or "").strip()
            k = (name.lower(), c.bucket(d))
            if new_len > max_shift + 0.01 or hours.get(k, 0.0) + add > overtime_line(c, name, line) + 0.05:
                continue
            trial = [dict(x) for x in rows]
            trial[i]["shift_end"] = _fmt_minutes(new_end % (24 * 60))
            trial[i]["scheduled_hours"] = str(round(new_len, 2)).rstrip("0").rstrip(".")
            sweeps += 1
            keys = _hard_keys(trial, c)
            if keys - base:
                continue
            extended.append({"index": i, "employee": name, "day": day, "from": r.get("shift_end"),
                             "to": trial[i]["shift_end"], "kind": "close",
                             "reason": f"Kept {name} on until close ({_fmt_minutes(close_m % (24 * 60))}) on {day} — "
                                       "nobody who can lock up was on to the end."})
            rows, base = trial, keys
            hours[k] = hours.get(k, 0.0) + add
            break
    return {"rows": rows, "extended": extended}


# A minor's shift is cut to their limits only when this much of it is left;
# a shorter remainder goes to a legal teammate instead.
MINOR_MIN_LEFT_HOURS = 2.0
PERSON_FIX_MAX_SWEEPS = 300
_MINOR_KINDS = ("minor_late", "minor_early", "minor_hours", "minor_week_hours")


def _run_excess(dates, max_run: int) -> int:
    """Days past the run rule, summed over every run in `dates`."""
    return sum(max(0, len(run) - max_run) for run in _date_runs(dates))


def fix_person_breaches(rows: list, c: "Constraints", roster_roles: dict = None, editable=None,
                        line: float = None, max_sweeps: int = PERSON_FIX_MAX_SWEEPS) -> dict:
    """The person breaches the replace-the-person pass kept leaving in a
    draft (Erik's first week, 10/2/26: a 16-17 minor until 11pm three nights,
    two cooks seven days in a row; schedule audit 10/3/26 P-5, E-27, P-30),
    repaired where the repair is legal:

    - a minor past their latest end, before their earliest start, over the
      day's cap (summed over every leg, E-6) or the week's: the shift is cut
      to the limit when two hours or more are left of it, and the stretch
      cut off is covered again by the role's family — a teammate already on
      that day runs on, or takes it as a shift of their own — so a cut minor
      no longer leaves a hole at close; else the shift goes to a legal
      teammate; a minor over the week's cap hands the shift that crosses it
      to a teammate first. Only when nobody can, the cut stands alone: the
      minor's own legality comes before coverage (owner's rule), and the
      hole is named;
    - a run of days past the rule: the run's days go to teammates — the
      role's family or a role they hold — one day at a time, the day that
      splits the run best first, until the run is legal or nobody can take
      another (counting the published tail: an 8-9 day run needs two or
      three days moved).

    Every change is judged by breach identity (E-1): no new or worse breach
    about a person, the manager rule or coverage; a minor's cut with no
    teammate to cover its stretch is the one change held only to the
    person rules. Pinned rows are never touched. Returns {rows, fixes:
    [{index, from, to, kind, reason}], sweeps}."""
    rep = _Repair(rows, c, upto=TIER_COVERAGE, soft_upto=TIER_OVERTIME, max_sweeps=max_sweeps)
    fixes = []
    display = _display(c, rep.rows, roster_roles)
    fam = _role_families(c, roster_roles)
    max_shift = float(c.compliance.get("max_shift_hours") or DEFAULTS["max_shift_hours"])
    max_shift_m = int(max_shift * 60)
    shortest_m = int(round(shortest_shift_hours(c) * 60))

    def free(r):
        return (editable is None or r.get("date") in editable) and not _is_pinned(r)

    def improved(bid, after) -> bool:
        return float((after[1].get("by_id") or {}).get(bid, 0.0)) < rep.severity(bid) - 0.01

    def recover(trial, i, r, cs, ce, tail):
        """Trials that cover the cut stretch cs..ce of `r` (now trial[i]) by
        the role's family: none needed, a teammate's shift run on into it, or
        a teammate's own shift over it (at least the owner's shortest)."""
        d, fam_r, low = r.get("date") or "", c.family(r.get("role")), _low(r.get("employee"))
        if _covered(trial, c, d, fam_r, cs, ce, skip={i}):
            yield trial, None
            return
        for j, x in enumerate(trial):
            if j == i or x.get("date") != d or not free(x) or c.family(x.get("role")) != fam_r:
                continue
            if _low(x.get("employee")) == low or not c.fillable(x.get("employee"), d)[0]:
                continue
            xs = _span(x)
            if not xs:
                continue
            if tail and xs[0] < cs and cs - _ADJACENT_MIN <= xs[1] < ce:
                ns, ne = xs[0], ce
            elif not tail and xs[1] > ce and cs < xs[0] <= ce + _ADJACENT_MIN:
                ns, ne = cs, xs[1]
            else:
                continue
            if ne - ns > max_shift_m:
                continue
            ext = _retimed(x, ns, ne, c)
            if not c.can_add(ext, trial[:j] + trial[j + 1:], line=line, overtime=True)[0]:
                continue
            t2 = list(trial)
            t2[j] = _with_note(ext, f"(covers {r.get('employee')}'s stretch)")
            yield t2, f"{x.get('employee')} covers {_fmt_minutes(cs % 1440)}–{_fmt_minutes(ce % 1440)}"
        lo, hi = (min(cs, ce - shortest_m), ce) if tail else (cs, max(ce, cs + shortest_m))
        piece = _new_row(r, d, "", r.get("role") or "", lo, hi, f"(covers {r.get('employee')}'s stretch)", c)
        for nm in _receivers(c, trial, display, fam, piece, trial, low, line=line, max_shift=max_shift):
            yield trial + [dict(piece, employee=nm)], f"{nm} takes {piece['shift_start']}–{piece['shift_end']}"

    def fix_minor(v) -> bool:
        i = v["index"]
        r = rep.rows[i]
        if not free(r):
            return False
        low, name, d = _low(r.get("employee")), r.get("employee"), r.get("date") or ""
        sp = _span(r)
        if not sp:
            return False
        s, en = sp
        kind, bid = v["kind"], breach_id(v)
        if kind == "minor_late":
            latest, _label = minor_latest(c, low, d)
            if latest is None or latest <= s:
                keep = None
            else:
                keep = (s, min(en, latest))
            stretch, tail = (max(s, latest or s), en), True
        elif kind == "minor_early":
            earliest = minor_earliest(c, low)
            keep = (max(s, earliest), en) if earliest is not None and earliest < en else None
            stretch, tail = (s, min(en, earliest if earliest is not None else s)), False
        else:
            # the day's or the week's cap: the crossing leg loses the excess at its end
            m = int(math.ceil(float(v.get("severity") or 0) * 4) / 4.0 * 60)
            keep = (s, en - m) if 0 < m < en - s else None
            stretch, tail = (en - m, en), True
        if keep and keep[1] - keep[0] < MINOR_MIN_LEFT_HOURS * 60:
            keep = None
        cut = _retimed(r, keep[0], keep[1], c) if keep else None
        why = v.get("detail") or LABELS.get(kind, kind)
        order = ("hand", "cut", "hole") if kind == "minor_week_hours" else ("cut", "hand", "hole")
        for opt in order:
            if opt == "hand":
                others = rep.rows[:i] + rep.rows[i + 1:]
                for nm in _receivers(c, rep.rows, display, fam, r, others, low, line=line, max_shift=max_shift):
                    if rep.spent:
                        return False
                    trial = list(rep.rows)
                    trial[i] = _with_note(dict(r, employee=nm), f"(was {name} — {LABELS.get(kind, kind)})")
                    worse, after = rep.judge(trial)
                    if worse or not improved(bid, after):
                        continue
                    rep.take(trial, after)
                    fixes.append({"index": i, "from": name, "to": nm, "kind": "minor",
                                  "reason": f"{name} is a minor: {why} — {nm} takes the shift."})
                    return True
            elif opt == "cut" and cut is not None:
                base = list(rep.rows)
                base[i] = _with_note(cut, f"(cut to a minor's limit: {LABELS.get(kind, kind)})")
                for trial, cover in recover(base, i, r, stretch[0], stretch[1], tail):
                    if rep.spent:
                        return False
                    worse, after = rep.judge(trial)
                    if worse or not improved(bid, after):
                        continue
                    rep.take(trial, after)
                    fixes.append({"index": i, "from": f"{name} {r.get('shift_start')}–{r.get('shift_end')}",
                                  "to": f"{cut['shift_start']}–{cut['shift_end']}", "kind": "minor",
                                  "reason": (f"{name} is a minor: {why} — the shift now keeps inside the limit"
                                             + (f"; {cover}." if cover else "."))})
                    return True
            elif opt == "hole" and cut is not None and not rep.spent:
                # Nobody in the role can take the stretch: the minor's own
                # legality still comes first, and the gap is named.
                trial = list(rep.rows)
                trial[i] = _with_note(cut, f"(cut to a minor's limit: {LABELS.get(kind, kind)})")
                worse, after = rep.judge(trial, upto=TIER_PERSON, soft_upto=None)
                if worse or not improved(bid, after):
                    continue
                rep.take(trial, after)
                fixes.append({"index": i, "from": f"{name} {r.get('shift_start')}–{r.get('shift_end')}",
                              "to": f"{cut['shift_start']}–{cut['shift_end']}", "kind": "minor",
                              "reason": (f"{name} is a minor: {why} — cut to the limit; nobody in the role could "
                                         f"take {_fmt_minutes(stretch[0] % 1440)}–{_fmt_minutes(stretch[1] % 1440)}, "
                                         "so that stretch is open.")})
                return True
        return False

    # 1. minors
    tried = set()
    while not rep.spent:
        todo = [v for v in rep.viols if v["kind"] in _MINOR_KINDS and (breach_id(v), v["index"]) not in tried]
        if not todo:
            break
        v = todo[0]
        tried.add((breach_id(v), v["index"]))
        fix_minor(v)

    # 2. too many days in a row: hand the run's days over until it is legal
    people = sorted({_low(v.get("employee")) for v in rep.viols if v["kind"] == "long_run"})
    max_run = int(c.compliance.get("max_consecutive_days") or 0)
    for low in people:
        stuck = set()
        while not rep.spent:
            v = next((x for x in rep.viols if x["kind"] == "long_run" and _low(x.get("employee")) == low), None)
            if v is None:
                break
            bid = breach_id(v)
            worked = {r.get("date") for r in rep.rows if _low(r.get("employee")) == low and r.get("date")}
            worked |= {r.get("date") for r in (c.base_rows.get(low) or []) if r.get("date")}
            by_date, middle = {}, {}
            for i, r in enumerate(rep.rows):
                if _low(r.get("employee")) == low and r.get("date"):
                    by_date.setdefault(r["date"], []).append(i)
            for run in _date_runs(worked):
                if len(run) > max_run:
                    for k, dd in enumerate(run):
                        middle[dd] = abs(k - (len(run) - 1) / 2.0)
            days = [d for d in middle if d in by_date and d not in stuck and all(free(rep.rows[i]) for i in by_date[d])]
            # the day that leaves the fewest days past the rule, the middle of
            # its run first (it splits the run in two)
            days.sort(key=lambda d: (_run_excess(worked - {d}, max_run), middle[d], d))
            done = False
            for d in days:
                if rep.spent:
                    break
                legs = by_date[d]
                first = rep.rows[legs[0]]
                if len(legs) == 1:
                    i = legs[0]
                    others = rep.rows[:i] + rep.rows[i + 1:]
                    tries = [[(i, nm)] for nm in _receivers(c, rep.rows, display, fam, first, others, low,
                                                             line=line, max_shift=max_shift)]
                else:
                    # every leg of the day to somebody, or the day is not freed
                    plan, trial = [], list(rep.rows)
                    for i in legs:
                        others = trial[:i] + trial[i + 1:]
                        names = _receivers(c, trial, display, fam, trial[i], others, low, line=line, max_shift=max_shift)
                        if not names:
                            plan = None
                            break
                        plan.append((i, names[0]))
                        trial[i] = dict(trial[i], employee=names[0])
                    tries = [plan] if plan else []
                for plan in tries:
                    trial = list(rep.rows)
                    for i, nm in plan:
                        trial[i] = _with_note(dict(rep.rows[i], employee=nm),
                                              f"(was {first.get('employee')} — too many days in a row)")
                    worse, after = rep.judge(trial)
                    if worse or not improved(bid, after):
                        continue
                    rep.take(trial, after)
                    who = first.get("employee")
                    for i, nm in plan:
                        r = rep.rows[i]
                        fixes.append({"index": i, "from": who, "to": nm, "kind": "long_run",
                                      "reason": (f"{who} was on {v['detail'].split(' —')[0]} (the rule is at most "
                                                 f"{max_run}); {nm} takes {_weekday_of(d)} "
                                                 f"{r.get('shift_start')}–{r.get('shift_end')}.")})
                    done = True
                    break
                if done:
                    break
                stuck.add(d)
            if not done:
                break
    return {"rows": rep.rows, "fixes": fixes, "sweeps": rep.sweeps}


# ── a manager on the floor every minute anyone is: the backstop ───────────
#
# Each stretch somebody is on and nobody managing is covered, cheapest and
# least tiring first (schedule audit 10/3/26 E-12, E-13, E-16, E-17):
#   * a manager already on that day runs on, or comes in earlier (their
#     shift within three hours of the stretch);
#   * a manager already on that day takes a second, separate leg over it —
#     a split or a double, rest and the day's length checked (E-16);
#   * a manager off that day comes in, for a shift as long as the stretch,
#     or the owner's shortest shift when they set one (E-16: a 1h gap no
#     longer buys a 4h shift);
# and somebody standing in as the manager on that date (acting_managers,
# E-13) covers what no manager can. Candidates are ranked by fatigue — days
# in a row — then hours this payroll week, never "salaried first" (E-17); a
# salaried person is never taken past their weekly cap (salaried_limit),
# and an hourly one past their overtime line only when no manager could
# cover the stretch inside it (the manager rule outranks overtime; it yields
# only to a person's own legality — owner, 10/2/26). A stretch nobody can
# legally cover stays a hard, day-level no_manager, returned in `left` with
# why each manager could not, and the week's `shortfall` says so once.

MANAGER_FILL_MAX_SWEEPS = 120
# Four hours: the gap filler sized every added manager shift to at least
# this, so a 1h gap bought 4h (E-16) — it now sizes to the stretch or the
# owner's min_shift_hours. Kept because the manager plan
# (schedule_skeleton.MIN_SHIFT_MIN) reads it as its planned-shift minimum.
MANAGER_MIN_SHIFT_MIN = 4 * 60


def cover_manager_gaps(rows: list, c: "Constraints", editable=None, line: float = None,
                       max_sweeps: int = MANAGER_FILL_MAX_SWEEPS) -> dict:
    """Returns {rows, extended: [{index, employee, day, date, from, to, kind,
    reason}], added: [... "leg": True for a second leg], left: [{date, day,
    from, to, reasons: [{employee, why}], could_act: [names]}], shortfall:
    {unmanaged_hours, dates, text} or None}. A change is kept only when the
    day's unmanaged minutes fall and nothing about a person's legality or
    coverage gets new or worse (E-1)."""
    rep = _Repair(rows, c, upto=TIER_COVERAGE, soft_upto=TIER_OVERTIME, max_sweeps=max_sweeps)
    extended, added, left = [], [], []
    if not c.managers and not c.acting_managers:
        return {"rows": rep.rows, "extended": extended, "added": added, "left": left, "shortfall": None}
    display = _display(c, rep.rows)
    roles = {_low(n): r for n, r in (getattr(c, "roster_roles", None) or {}).items() if n}
    max_shift_m = int(float(c.compliance.get("max_shift_hours") or DEFAULTS["max_shift_hours"]) * 60)
    shortest_m = int(round(shortest_shift_hours(c) * 60))

    def free(r):
        return (editable is None or r.get("date") in editable) and not _is_pinned(r)

    def managing(d) -> dict:
        lows = set(c.managers) | {low for low, ds in (c.acting_managers or {}).items() if d in (ds or ())}
        return {low: display[low] for low in sorted(lows) if low in display}

    def legal(new, others, strict):
        nm, d = new.get("employee"), new.get("date")
        ok, why = c.fillable(nm, d)
        if not ok:
            return False, why
        ok, why = c.can_add(new, others, line=line, overtime=strict)
        if not ok:
            return False, why
        if c.is_salaried(nm):
            cap = c.salaried_limit(nm)
            have = _bucket_hours(c, others, _low(nm), c.bucket(d)) + row_hours(new)
            if have > cap + 0.05:
                return False, f"at their {cap:g}h weekly cap"
        return True, ""

    def sized(gs, ge, day_lo, day_hi, avoid=()):
        """A new shift over the stretch: as long as the stretch, or the
        owner's shortest shift, inside the day and the longest shift, clear
        of the person's other shifts that day."""
        cover_end = min(ge, gs + max_shift_m)
        length = min(max(ge - gs, shortest_m), max_shift_m)
        lo, hi = gs, gs + length
        if hi > day_hi:
            # Never past the last person out: the shift ends with the day and
            # starts earlier — before anyone else when the owner's shortest
            # shift is longer than the day left.
            hi = max(cover_end, day_hi)
            lo = min(gs, hi - length)
        for s, e in avoid:
            if lo < e <= gs:
                lo = e
            if cover_end <= s < hi:
                hi = s
        if shortest_m and hi - lo < shortest_m:
            return None, None
        return lo, hi

    def options(d, gs, ge, strict):
        out = []
        mgrs = managing(d)
        day_rows = [(j, x) for j, x in enumerate(rep.rows) if x.get("date") == d and (x.get("employee") or "").strip()]
        spans = [sp for sp in (_span(x) for _j, x in day_rows) if sp]
        if not spans:
            return out
        day_lo, day_hi = min(s for s, _e in spans), max(e for _s, e in spans)
        tmpl = day_rows[0][1] if day_rows else {}
        note = "Cavnar AI: a manager on the floor every minute anyone is"
        # 1. a manager already on that day runs on, or comes in earlier
        for j, x in day_rows:
            low = _low(x.get("employee"))
            if low not in mgrs or not free(x) or not c.can_work(x.get("employee"), d)[0]:
                continue
            sp = _span(x)
            if not sp:
                continue
            s0, e0 = sp
            if gs - _ADJACENT_MIN <= e0 <= gs:
                ns, ne = s0, min(ge, s0 + max_shift_m)
            elif ge <= s0 <= ge + _ADJACENT_MIN:
                ns, ne = max(gs, e0 - max_shift_m), e0
            else:
                continue
            if ne - ns <= e0 - s0:
                continue
            new = _retimed(x, ns, ne, c)
            others = rep.rows[:j] + rep.rows[j + 1:]
            if not legal(new, others, strict)[0]:
                continue
            run, hours = _fatigue_rank(c, others, low, d, row_hours(new))
            out.append((low not in c.managers, run, 0, hours, 0 if c.is_salaried(mgrs[low]) else 1, mgrs[low],
                        "extend", j, new))
        # 2. a second leg for a manager already on; 3. a manager off that day comes in
        for low, nm in mgrs.items():
            mine = [_span(x) for _j, x in day_rows if _low(x.get("employee")) == low]
            mine = [sp for sp in mine if sp]
            if any(s < ge and e > gs for s, e in mine):
                continue          # on during the stretch but not counted: they cannot work it
            lo, hi = sized(gs, ge, day_lo, day_hi, avoid=mine)
            if lo is None:
                continue
            role = c.managers.get(low) or roles.get(low) or "Manager"
            new = _new_row(tmpl, d, nm, role, lo, hi, note, c)
            if mine:
                day_h = sum((e - s) for s, e in mine) / 60.0
                if day_h + row_hours(new) > max_shift_m / 60.0 + 0.01:
                    continue      # the day stays inside the longest shift
            if not legal(new, rep.rows, strict)[0]:
                continue
            kind = "leg" if mine else "add"
            run, hours = _fatigue_rank(c, rep.rows, low, d, row_hours(new))
            out.append((low not in c.managers, run, 1 if mine else 2, hours, 0 if c.is_salaried(nm) else 1, nm,
                        kind, None, new))
        # A manager before somebody only standing in as one (an acting
        # manager covers a date no manager can, E-13); then least tired
        # first (days in a row), the least disruption — a shift run on, then
        # a second leg, then a new shift — and the fewest hours this payroll
        # week. Being salaried only breaks a tie (their hours cost nothing
        # more); it never puts them first (E-17).
        out.sort(key=lambda t: t[:6])
        return out

    def why_not(d, gs, ge):
        reasons = []
        for low, nm in managing(d).items():
            ok, why = c.can_work(nm, d)
            if ok:
                ok, why = c.fillable(nm, d)
            if ok and c.is_salaried(nm):
                cap = c.salaried_limit(nm)
                have = _bucket_hours(c, rep.rows, low, c.bucket(d))
                if have + 0.25 > cap:
                    ok, why = False, f"at their {cap:g}h weekly cap ({have:g}h this payroll week)"
            if ok:
                probe = _new_row({}, d, nm, c.managers.get(low) or roles.get(low) or "Manager",
                                 gs, min(ge, gs + max_shift_m), "", c)
                ok, why = c.can_add(probe, rep.rows, line=line, overtime=False)
                if ok:
                    why = "could not be fitted around their other shifts that day"
            reasons.append({"employee": nm, "why": why})
        return reasons

    only = None
    if not c.managers:
        only = {dd for ds in (c.acting_managers or {}).values() for dd in (ds or ())}
    for d in sorted(manager_gaps(rep.rows, c, dates=only).keys()):
        if editable is not None and d not in editable:
            continue
        day = _weekday_of(d) or d
        skipped = set()
        while not rep.spent:
            gaps = [g for g in (manager_gaps(rep.rows, c, dates={d}).get(d) or []) if (g[0], g[1]) not in skipped]
            if not gaps:
                break
            gs, ge, _pin = gaps[0]
            before = int((rep.prof.get("manager") or {}).get(d, 0))
            done = None
            for strict in (True, False):
                for opt in options(d, gs, ge, strict):
                    if rep.spent:
                        break
                    nm, kind, j, new = opt[-4], opt[-3], opt[-2], opt[-1]
                    trial = list(rep.rows)
                    if kind == "extend":
                        trial[j] = new
                    else:
                        trial.append(new)
                    worse, after = rep.judge(trial, soft_upto=TIER_OVERTIME if strict else None)
                    if worse or int((after[1].get("manager") or {}).get(d, 0)) >= before:
                        continue
                    done = (kind, j, nm, new, trial, after, strict)
                    break
                if done or rep.spent:
                    break
            if done is None:
                skipped.add((gs, ge))
                left.append({"date": d, "day": day, "from": _fmt_minutes(gs % (24 * 60)),
                             "to": _fmt_minutes(ge % (24 * 60)), "minutes": ge - gs,
                             "reasons": why_not(d, gs, ge),
                             "could_act": sorted({(x.get("employee") or "").strip() for x in rep.rows
                                                  if x.get("date") == d and _low(x.get("employee")) in c.keyholders
                                                  and _low(x.get("employee")) not in managing(d)})})
                continue
            kind, j, nm, new, trial, after, strict = done
            was = rep.rows[j] if kind == "extend" else None
            rep.take(trial, after)
            idx = j if kind == "extend" else len(trial) - 1
            span_txt = f"{_fmt_minutes(gs % (24 * 60))}–{_fmt_minutes(ge % (24 * 60))}"
            ot = "" if strict else " (overtime: nobody who manages could cover it inside their hours)"
            now = f"{new['shift_start']}–{new['shift_end']}"
            if kind == "extend":
                extended.append({"index": idx, "employee": nm, "day": day, "date": d,
                                 "from": f"{was.get('shift_start')}–{was.get('shift_end')}", "to": now, "kind": "manager",
                                 "reason": f"No manager was on {day} {span_txt}: {nm} stays on to cover it{ot}."})
            else:
                added.append({"index": idx, "employee": nm, "day": day, "date": d, "from": "", "to": now,
                              "kind": "manager", "leg": kind == "leg",
                              "reason": (f"No manager was on {day} {span_txt}: added {nm} {now}"
                                         + (" as a second shift that day" if kind == "leg" else "") + f"{ot}.")})
    shortfall = None
    if left:
        dates = sorted({x["date"] for x in left})
        mins = sum(int((rep.prof.get("manager") or {}).get(d, 0)) for d in dates)
        shortfall = {"unmanaged_hours": round(mins / 60.0, 2), "dates": dates,
                     "text": (f"{mins / 60.0:g}h of the week has no manager on ({', '.join(_mdy(d) for d in dates)}): "
                              "nobody who manages could legally cover it — each day says why. Name an acting "
                              "manager for those dates, or raise a manager's weekly cap.")}
    return {"rows": rep.rows, "extended": extended, "added": added, "left": left, "shortfall": shortfall}


# ── owner-set minimum hours, filled (schedule audit 10/3/26 P-4) ───────────
#
# A minimum the owner set for somebody was only ever a soft line: Erik's
# full-time cook, set to 40-45h, got 7h. This pass gives a person under
# their minimum legal shifts in roles they can work — a shift taken from a
# teammate who stays at or above their own target (their minimum or the
# hours they asked for), the furthest above it first; else, when the week
# has an hours budget, a shift of the kind the role already works that day,
# added inside the budget. Nothing about a person's legality, the manager
# rule, coverage or overtime (weekly, daily, the payroll reserve) may get
# new or worse (tiers 0-3), and the receiver is never taken past their
# overtime line. What it cannot fill stays the soft under_min_hours, with why.

MIN_HOURS_MAX_SWEEPS = 200


def fill_min_hours(rows: list, c: "Constraints", roster_roles: dict = None, editable=None,
                   hours_budget: float = None, line: float = None,
                   max_sweeps: int = MIN_HOURS_MAX_SWEEPS) -> dict:
    """Returns {rows, moves: [{index, from, to, hours, kind, reason}], added:
    [{index, employee, hours, kind, reason}], left: [{employee, hours, min,
    short_by, reason}], sweeps}. `hours_budget` (the week's hourly hours) is
    the ceiling for added shifts; without one only transfers are made."""
    rep = _Repair(rows, c, upto=TIER_COVERAGE, soft_upto=TIER_OVERTIME, max_sweeps=max_sweeps)
    moves, added, left = [], [], []
    display = _display(c, rep.rows, roster_roles)
    fam = _role_families(c, roster_roles)
    max_shift = float(c.compliance.get("max_shift_hours") or DEFAULTS["max_shift_hours"])

    def free(r):
        return (editable is None or r.get("date") in editable) and not _is_pinned(r)

    def week(low):
        return sum(row_hours(r) for r in rep.rows if _low(r.get("employee")) == low)

    def target(low):
        nm = display.get(low, low)
        want = 0.0
        for k, p in (c.preferred or {}).items():
            if _low(k) == low:
                try:
                    want = float((p or {}).get("desired_hours") or 0)
                except (TypeError, ValueError):
                    want = 0.0
        return max(float(c.min_hours(nm) or 0.0), want)

    def transfer(low, nm, mn) -> bool:
        short = mn - week(low)
        cands = []
        for j, r in enumerate(rep.rows):
            donor = _low(r.get("employee"))
            if not donor or donor == low or not free(r) or not _takes(c, fam, rep.rows, low, r.get("role")):
                continue
            h = row_hours(r)
            if week(donor) - h + 0.05 < target(donor):
                continue          # never below the donor's own minimum or what they asked for
            cands.append((-(week(donor) - target(donor)), abs(short - h), r.get("date") or "",
                          start_minutes(r) or 0, j))
        cands.sort()
        for *_k, j in cands:
            if rep.spent:
                return False
            r = rep.rows[j]
            d = r.get("date") or ""
            if not c.fillable(nm, d)[0]:
                continue
            new = dict(r, employee=nm)
            others = rep.rows[:j] + rep.rows[j + 1:]
            if not c.can_add(new, others, line=line, overtime=True)[0]:
                continue
            day = _day_hours(others, low, d)
            if day and day + row_hours(new) > max_shift + 0.01:
                continue
            trial = list(rep.rows)
            trial[j] = _with_note(new, f"(was {r.get('employee')} — {nm} is under their {mn:g}h minimum)")
            worse, after = rep.judge(trial)
            if worse:
                continue
            rep.take(trial, after)
            moves.append({"index": j, "from": r.get("employee"), "to": nm, "hours": row_hours(r), "kind": "min_hours",
                          "reason": (f"{nm} had {week(low) - row_hours(r):g}h of the {mn:g}h minimum you set; they take "
                                     f"{r.get('employee')}'s {r.get('day') or _weekday_of(d)} "
                                     f"{r.get('shift_start')}–{r.get('shift_end')}.")})
            return True
        return False

    def add(low, nm, mn) -> bool:
        if not hours_budget or float(hours_budget) <= 0:
            return False
        short = mn - week(low)
        cands = []
        dates = sorted(set(c.week_dates or []) or {r.get("date") for r in rep.rows if r.get("date")})
        for d in dates:
            if d in (c.closed_dates or set()) or (editable is not None and d not in editable):
                continue
            if any(_low(x.get("employee")) == low and x.get("date") == d for x in rep.rows):
                continue          # a day they do not work yet
            if not c.fillable(nm, d)[0] or not c.can_work(nm, d)[0]:
                continue
            seen = set()
            for x in rep.rows:
                if x.get("date") != d or not _takes(c, fam, rep.rows, low, x.get("role")):
                    continue
                sig = ((x.get("role") or "").strip().lower(), x.get("shift_start"), x.get("shift_end"))
                if sig in seen or not _span(x):
                    continue
                seen.add(sig)
                on = len({_low(y.get("employee")) for y in rep.rows
                          if y.get("date") == d and c.family(y.get("role")) == c.family(x.get("role"))})
                cands.append((on, _run_through(c, rep.rows, low, d), abs(short - row_hours(x)), d,
                              start_minutes(x) or 0, x))
        cands.sort(key=lambda t: t[:5])
        before_cap = sum(v for k, v in (rep.prof.get("by_id") or {}).items() if k[0] == "over_section_cap")
        for *_k, x in cands:
            if rep.spent:
                return False
            d = x.get("date")
            s, e = _span(x)
            new = _new_row(x, d, nm, x.get("role") or "", s, e,
                           f"Cavnar AI: {nm} is under their {mn:g}h minimum", c)
            if not c.is_salaried(nm) and hourly_hours(rep.rows, c) + row_hours(new) > float(hours_budget) + 0.05:
                continue          # added inside the hours budget only
            if not c.can_add(new, rep.rows, line=line, overtime=True)[0]:
                continue
            trial = list(rep.rows) + [new]
            worse, after = rep.judge(trial)
            if worse:
                continue
            if sum(v for k, v in (after[1].get("by_id") or {}).items() if k[0] == "over_section_cap") > before_cap:
                continue          # never more servers on than there are sections
            rep.take(trial, after)
            added.append({"index": len(trial) - 1, "employee": nm, "hours": row_hours(new), "kind": "min_hours",
                          "reason": (f"{nm} had {week(low) - row_hours(new):g}h of the {mn:g}h minimum you set; added "
                                     f"{_weekday_of(d)} {new['shift_start']}–{new['shift_end']} inside the hours budget.")})
            return True
        return False

    def why_left(low, nm):
        days = [d for d in (c.week_dates or []) if d not in (c.closed_dates or set())]
        if days and not any(c.fillable(nm, d)[0] for d in days):
            return c.fillable(nm, days[0])[1] or "not one we can choose this week"
        if days and not any(c.can_work(nm, d)[0] for d in days):
            return "not available any day this week"
        if not hours_budget:
            return ("no teammate in their roles had a shift to spare above their own target, and there is no "
                    "hours budget to add one")
        return ("no legal shift in their roles could move to them, and none fits inside the hours budget "
                "without breaking a rule")

    under = []
    for low, nm in display.items():
        if c.active and low not in c.active:
            continue
        mn = c.min_hours(nm)
        if mn and mn - week(low) > 0.05:
            under.append((-(mn - week(low)), low))
    under.sort()
    for _s, low in under:
        nm, mn = display[low], float(c.min_hours(display[low]))
        while mn - week(low) > 0.05 and not rep.spent:
            if transfer(low, nm, mn) or add(low, nm, mn):
                continue
            break
        have = week(low)
        if mn - have > 0.05:
            left.append({"employee": nm, "hours": round(have, 2), "min": mn, "short_by": round(mn - have, 2),
                         "reason": why_left(low, nm)})
    return {"rows": rep.rows, "moves": moves, "added": added, "left": left, "sweeps": rep.sweeps}
