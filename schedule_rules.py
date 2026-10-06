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
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache

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
    # What "full-time" means when the owner set no minimum for the person
    # (schedule audit 10/3/26 D-41: employment was only a prompt word, so
    # the field said nothing): at least this many hours a week. 30 is the
    # usual US full-time line (the ACA's); 0 or blank turns it off.
    "full_time_min_hours": 30.0,
}
_BOUNDS = {"min_rest_hours": (0, 24), "max_shift_hours": (4, 24), "min_shift_hours": (1, 12), "daily_ot_hours": (4, 24),
           "meal_break_after_hours": (2, 12), "minor_max_daily_hours": (1, 12),
           "min_consecutive_days_off": (0, 4), "part_time_days_off": (0, 5),
           "weekly_hours_ceiling": (10, 80), "max_consecutive_days": (2, 14), "notice_days": (0, 30),
           "full_time_min_hours": (0, 60)}

# Reasons that mean the person is not really on that shift, so the row must
# not count as coverage. Everything else in HARD is a cost or a rule breach
# with a real person still on the floor.
NO_SHOW = frozenset({"off_roster", "inactive", "outside_week", "double_booked", "overlap",
                     "approved_time_off", "unavailable_day", "unavailable_daypart", "elsewhere"})
NO_SHOW = NO_SHOW | frozenset({"outside_window", "missing_cert", "note_unavailable", "closed_day"})
HARD = NO_SHOW | frozenset({"over_max_hours", "shift_too_long", "rest_gap", "minor_late", "minor_hours", "long_run",
                            "minor_early", "minor_week_hours",
                            "no_manager_on_duty", "coverage_floor", "keyholder_until_close", "nobody_at_close",
                            "no_manager", "role_not_held", "role_window"})
SOFT = frozenset({"days_off", "pending_time_off", "daily_ot", "meal_break", "under_min_hours", "over_section_cap",
                  "ends_before_role_close", "manager_rule_unusable", "minor_age_unknown",
                  "owner_rule", "no_manager_roster", "payroll_tail_full", "shift_too_short",
                  "closer_unavailable", "trainee_unpaired", "role_time", "past_close", "over_role_max"})
# Soft flags about the roster, not the week, that still hold every publish:
# with nobody counted as a floor manager the highest rule is unmet on every
# shift and no draft can mend it, so the week waits until the owner names
# who runs the floor (schedule re-audit 10/4/26 RULES-13 — it published
# under a soft note).
ROSTER_BLOCKERS = frozenset({"no_manager_roster"})
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
    "role_not_held": "not a role they hold",
    "closer_unavailable": "none of that role's closers can work that day",
    "trainee_unpaired": "a trainee with nobody to train them",
    "role_time": "starts or ends off a time rule you set for the role",
    "role_window": "outside the hours your hours & shift rules give the role",
    "over_role_max": "more of a role on at once than your hours & shift rules allow",
    # A row on a date the restaurant is closed (re-audit 10/4/26 PIPE-4):
    # nobody is really on it, and it was saved and sent unflagged.
    "closed_day": "scheduled on a day you're closed",
    # A shift running past close by more than its role may stay (re-audit
    # 10/4/26 PIPE-10): the cap the job's parser holds every model row to.
    "past_close": "runs past the time after close you allow the role",
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


def compliance_set_by(restaurant) -> dict:
    """{rule key: "owner" | "pack"} — which of compliance()'s values the
    owner set, and which a jurisdiction pack started them at; a key in
    neither is the code's default. The prompt said "the owner's limit on
    days in a row" for a DEFAULTS value the owner never set (schedule
    re-audit 10/4/26 PROMPT-10)."""
    if restaurant is None:
        return {}
    if not hasattr(restaurant, "compliance_json"):
        from models import get_restaurant
        restaurant = get_restaurant(restaurant)
    try:
        data = json.loads(getattr(restaurant, "compliance_json", None) or "{}") or {}
    except Exception:
        data = {}
    out = {k: "owner" for k, v in data.items() if v not in (None, "") and not str(k).startswith("_")}
    pack = (compliance(restaurant).get("_pack") or {}) if restaurant else {}
    for k in (pack.get("applied") or {}):
        out.setdefault(k, "pack")
    return out


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


def stored_compliance(restaurant) -> dict:
    """restaurants.compliance_json as stored (the owner's own values and the
    closures), {} when empty or unreadable."""
    try:
        data = json.loads(getattr(restaurant, "compliance_json", None) or "{}") or {}
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def merged_closures(stored: dict, closed_weekdays=None, closed_dates=None) -> dict:
    """The stored compliance JSON with the closures a save names; the rest
    as it was. Raises ValueError for a week closed every day. Pure — the
    caller writes, once, after every other field passed (schedule re-audit
    10/4/26 UI-9)."""
    data = dict(stored or {})
    if closed_weekdays is not None:
        wanted = {str(d).strip().capitalize() for d in closed_weekdays or []}
        data["closed_weekdays"] = [d for d in WEEKDAYS if d in wanted]
        if len(data["closed_weekdays"]) == 7:
            raise ValueError("A restaurant closed every day of the week has nothing to schedule.")
    if closed_dates is not None:
        data["closed_dates"] = sorted({str(d).strip()[:10] for d in closed_dates or [] if _iso(str(d).strip()[:10])})[-120:]
    return data


def save_closures(restaurant_id, closed_weekdays=None, closed_dates=None, db_path=DB_PATH) -> dict:
    from models import get_restaurant, update_restaurant
    data = merged_closures(stored_compliance(get_restaurant(restaurant_id, db_path)),
                           closed_weekdays=closed_weekdays, closed_dates=closed_dates)
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


# The rules a save can put back to their default (the pack's value, else
# DEFAULTS) by naming them in `reset`.
RESETTABLE = tuple(_BOUNDS) + ("minor_latest_end", "keyholder_until_close")


def merged_compliance(stored: dict, data: dict, reset=()) -> dict:
    """The stored compliance JSON after a rules save: each rule the save
    sends is set (null, blank or false is "not tracked"), each rule it
    names in `reset` goes back to its default, and EVERY other stored value
    — rules it did not send, the closures — stays as stored.

    A save used to be the whole object: a rule it did not send fell back to
    the default, so a rules sheet that failed to load and was saved blank
    reset the weekly ceiling, the notice days and daily overtime, and a
    screen older than a setting cleared it (schedule re-audit 10/4/26 UI-2;
    the one-key `_KEPT_WHEN_NOT_SENT` patch this replaces covered only the
    shortest shift). Pure — the caller writes."""
    data = data if isinstance(data, dict) else {}
    clean = dict(stored or {})
    for k in reset or ():
        if k in RESETTABLE:
            clean.pop(k, None)
    for k, (lo, hi) in _BOUNDS.items():
        if k in data:
            v = data[k]
            clean[k] = None if v in (None, "", False) else _num(v, lo, hi)
    if isinstance(data.get("minor_latest_end"), str):
        clean["minor_latest_end"] = data["minor_latest_end"].strip()[:10] or DEFAULTS["minor_latest_end"]
    if "manager_on_duty" in data:
        clean["manager_on_duty"] = bool(data["manager_on_duty"])
    if "keyholder_until_close" in data:
        clean["keyholder_until_close"] = bool(data["keyholder_until_close"])
    return clean


def save_compliance(restaurant_id, data: dict, db_path=DB_PATH, reset=()) -> dict:
    """Merge a rules save into what is stored (merged_compliance) and write it."""
    from models import update_restaurant, get_restaurant
    clean = merged_compliance(stored_compliance(get_restaurant(restaurant_id, db_path)), data, reset=reset)
    update_restaurant(restaurant_id, {"compliance_json": json.dumps(clean) if clean else None}, db_path=db_path)
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


def merge_role_map(stored: dict, sent: dict, clean) -> dict:
    """A per-role setting after a save that names some roles: each role
    sent is set to clean(value), or removed when clean gives None (the
    client sends null for a role it cleared); each role NOT sent stays as
    stored. Roles match case-insensitively and take the spelling sent.
    `clean` may raise ValueError, which the caller turns into a refusal
    before anything is written.

    Each map used to be the whole value, so a sheet that failed to load
    sent {} and wiped every floor, arrival lead, certificate requirement and
    cross-training target (schedule re-audit 10/4/26 UI-2)."""
    out = dict(stored) if isinstance(stored, dict) else {}
    for role, value in (sent or {}).items():
        name = str(role).strip()[:60]
        if not name:
            continue
        for k in [k for k in out if str(k).strip().casefold() == name.casefold()]:
            out.pop(k)
        v = clean(value)
        if v is not None:
            out[name] = v
    return out


def clean_floor_spec(spec):
    """One role's floor as the save stores it; None (remove) for null or a
    spec with nothing set."""
    if not isinstance(spec, dict):
        return None
    days = spec.get("days") if isinstance(spec.get("days"), dict) else {}
    has = any(spec.get(p) not in (None, "") for p in ("morning", "night")) or any(
        isinstance(d, dict) and any(d.get(p) not in (None, "") for p in ("morning", "night"))
        for d in days.values())
    return spec if has else None


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
        # A floor named by a role family is held on the job code for each
        # half of the day, as build_constraints holds it (D-13, D-14).
        import staff_settings as _ss
        for e in _ss.roster(restaurant.id, db_path=db_path):
            if (e.get("role") or "").strip():
                c.role_names.setdefault(e["role"].strip().lower(), " ".join(e["role"].split()))
        c.role_families = role_families(restaurant)
        # The floors the RESTAURANT HOURS & SHIFT RULES state (PROMPT-1).
        c.close_mins = {}
        apply_hours_rules(c, restaurant)
        _floors_on_job_codes(c)
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
    """'9:30pm' / '21:30' → minutes past midnight, or None. The parse is
    remembered per spelling (_parse_clock): the sweep reads the same few
    dozen times on every trial a pass weighs (schedule audit 10/3/26 P-37)."""
    raw = (t or "").strip().lower().replace(" ", "")
    if not raw:
        return None
    return _parse_clock(raw)


@lru_cache(maxsize=4096)
def _parse_clock(raw: str):
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            d = datetime.strptime(raw, fmt)
            return d.hour * 60 + d.minute
        except ValueError:
            continue
    return None


# A start between these hours with no end to judge by is early prep for the
# morning (a 4:30am baker), not last night's tail.
_EARLY_PREP_FROM = 4 * 60


def _night_offset(start_m, end_m=None) -> int:
    """1440 when a shift is in the small hours of its row's night: a row's
    date is its BUSINESS date, and a shift that starts before the business
    day's first hour (time_utils.BUSINESS_DAY_START_HOUR) AND is over by
    6am is after that night's midnight — a 12:30am–4:00am porter dated
    Friday works Saturday 00:30, after Friday's close. It was read as the
    start of Friday, so the porter's own Thursday close looked like an
    overlap and the row opened a false manager gap at dawn (schedule audit
    10/3/26 E-32). A shift that starts that early and runs into the morning
    is the morning's (a 4:30am–12:30pm baker); with no end to judge by, a
    start from 4am on is too. The same night as the no-show watch
    (intraday.coverage_gaps, staff_comms._due)."""
    from time_utils import BUSINESS_DAY_START_HOUR
    if start_m is None or start_m >= BUSINESS_DAY_START_HOUR * 60:
        return 0
    if end_m is None:
        return 24 * 60 if start_m < _EARLY_PREP_FROM else 0
    end = end_m if end_m > start_m else end_m + 24 * 60
    return 24 * 60 if end <= _OVERNIGHT_LATEST_BEFORE else 0


def start_minutes(row):
    """A row's start as minutes past its own (business) date's midnight —
    past 1440 for a start in the small hours of its night (E-32)."""
    s = parse_minutes(row.get("shift_start", ""))
    return None if s is None else s + _night_offset(s, parse_minutes(row.get("shift_end", "")))


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
    night = _night_offset(s, e)
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
    night = _night_offset(start_m, end_m)
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
    # Part of a date a person can't work (schedule audit 10/3/26 D-39):
    # {lower: {date: [{"from": min|None, "until": min|None, "daypart":
    # "morning"|"night"|None, "reason"}]}} — approved time off "until 4pm"
    # or for dinner, and a held note's lunch or dinner on dates that only
    # part of the week is inside. can_work reads the daypart ones,
    # window_ok the timed ones.
    blocked_parts: dict = field(default_factory=dict)
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
    cert_expiry: dict = field(default_factory=dict)        # {lower: {cert: expires_on}} for cards ending mid-week
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
    # The role families whose punches show them on until the restaurant's
    # close, and those they show ending before it (closing_split over the
    # last weeks): what closer_need holds each role's closer to (RULES-6).
    close_roles: set = field(default_factory=set)
    early_close_roles: set = field(default_factory=set)
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
    # An owner-only rule the code could not read (D-38): never put on the
    # shared review; for a surface only the account holders read.
    owner_rules_unchecked_private: list = field(default_factory=list)
    # The pairings the owner's rules state (parse_pair_rule):
    # [{"kind": "avoid"|"prefer", "a", "b", "text", "private"}].
    owner_pairs: list = field(default_factory=list)
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
    # People in training (D-16): {lower: {"target_role", "trainer", "from",
    # "until"}} — training_row() says which of their rows are training.
    trainees: dict = field(default_factory=dict)
    # The owner's weekly cap for a salaried person with none of their own
    # (restaurants.salaried_cap), or None when the owner set none. Only a
    # cap the owner set is a hard maximum: unset, code holds its own
    # additions to SALARIED_CAP_DEFAULT (salaried_limit) and the owner may
    # schedule them to SALARIED_HOURS_MAX (max_hours). build_constraints
    # stored the 55h default here, so it became a hard maximum every week
    # and the rebalance moved an owner's own hours onto paid staff "inside
    # the 55h you set" (schedule re-audit 10/4/26 RULES-5).
    salaried_cap: float = None
    # People on the roster who have not worked in weeks (E-3, D-17):
    # {lower: last worked iso date}. Still on the roster — a row the owner
    # writes for them is legal — but never a candidate for a fill.
    dormant: dict = field(default_factory=dict)
    # A person's scheduling note nobody has confirmed as a rule (D-34):
    # {lower: {"days": set(weekday names), "dates": set(iso), "text": str}}.
    # The model is told it; the fill passes keep off those days.
    note_caution: dict = field(default_factory=dict)
    # One person, one key (D-8, E-25): every spelling people's identity
    # knows for a roster person — an alias, the name before a POS rename,
    # an owner's "Mike" for the roster's "Michael" — to the key the roster
    # spells them by ({spelling lower: roster key}); key() reads it. Every
    # per-person input is filed under key(), so a time-off block typed
    # under an old spelling still holds.
    aliases: dict = field(default_factory=dict)
    # Roster people an open "same person?" question joins: one person in
    # the sweep until the owner answers (E-25). {roster key: group key}.
    linked: dict = field(default_factory=dict)
    # {roster key: the roster's spelling}, deactivated people included.
    display: dict = field(default_factory=dict)
    # Full-timers whose minimum is the restaurant's full-time line, not one
    # the owner set for them (D-41) — said so wherever the minimum is.
    full_time_default: set = field(default_factory=set)
    # Per-person inputs whose name matches nobody on the roster, active or
    # not (D-8): [{"source", "name", "detail"}] — the review names each,
    # so a fact typed under a spelling nobody goes by is never silent.
    unmatched: list = field(default_factory=list)
    # Every role a person can be scheduled in (F1, D-15): their roster role,
    # the roles they hold (held_roles) and every role they have worked here.
    # {lower: set(role lower)}; holds() reads it by family. A role written
    # for somebody outside it is the role_not_held breach.
    known_roles: dict = field(default_factory=dict)
    role_names: dict = field(default_factory=dict)          # {role lower: the spelling in use here}
    # Why each person counts as a manager or was left out (manager_basis —
    # P-7, E-14): {lower: {"counts", "basis", "role", "why"}}; the owner's
    # "Managers: …" confirmation reads it.
    manager_basis: dict = field(default_factory=dict)
    # People salaried-style because their role is Owner and the owner has
    # not marked them paid hourly (E-12): a subset of `salaried` (lower).
    salaried_style: set = field(default_factory=set)
    # Everyone on the roster marked to close, whatever their role (lower) —
    # the share the closer data-quality warning reads (D-9).
    closer_flags: set = field(default_factory=set)
    # Which roles have the closer rule, and why: "yours" (the owner chose
    # them), "history" (the roles the restaurant's own punches show on until
    # close, until the owner chooses) or "all" (no history yet: every role
    # somebody is marked to close for).
    closer_roles_basis: str = "all"
    # When a role starts or ends on a daypart, by the owner's rule (L-33 —
    # a retime the manager kept making, made a rule; schedule_note_rules
    # kind start/end): {(role lower, weekday, daypart): {"start": minutes,
    # "end": minutes, "source": {"start"|"end": the rule's words}}}.
    role_times: dict = field(default_factory=dict)
    # Which compliance values the owner set ("owner") or a jurisdiction pack
    # started ("pack"); a key in neither is the code's default
    # (compliance_set_by, PROMPT-10). None — a Constraints built by hand —
    # reads every value as the owner's.
    compliance_set: dict = None
    # The owner's RESTAURANT HOURS & SHIFT RULES (restaurants.hours_notes),
    # read into rules the code holds the week to (apply_hours_rules —
    # schedule re-audit 10/4/26 PROMPT-1). The floors go into role_floors
    # (rule_floor_sources names the line) and the stays into close_mins;
    # here: the hours a role works — {family: {weekday: {"earliest",
    # "latest", "source"}}} (minutes; None where the rule says nothing) —
    # the most of a role on at once — {family: [{"max", "days",
    # "daypart", "source"}]} — each line as the code reads it
    # ([{"text", "reads_as", "kind", "tag"}]), and the lines it could not
    # read, which the review names.
    role_windows: dict = field(default_factory=dict)
    role_caps: dict = field(default_factory=dict)
    hours_rules: list = field(default_factory=list)
    hours_rules_unchecked: list = field(default_factory=list)

    # ── lookups ────────────────────────────────────────────────────────
    def key(self, name) -> str:
        """The key every map here files `name`'s facts under: the roster's
        own spelling of the one person it means (aliases — people's
        identity), else the name folded (spacing collapsed, lower case).
        One person, one key (schedule audit 10/3/26 D-8)."""
        k = " ".join(str(name or "").split()).lower()
        if not self.aliases:
            return k
        hit = self.aliases.get(k)
        if hit is None:
            hit = self.aliases.get(k.casefold())
        return k if hit is None else hit

    def sweep_key(self, name) -> str:
        """key(), with roster people an open "same person?" question joins
        read as one (E-25): until the owner answers, "Kim T." and "Kimberly
        Tran" share one week of overlaps, rest, hours and days in a row."""
        k = self.key(name)
        return self.linked.get(k, k)

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
        Jim at Simple EJ's work the floor most of the week (owner, 9/30/26).
        Through the person's identity: a salaried entry typed "Gabriel
        Huerta" holds for the "Gabe Huerta" he punches as (D-7)."""
        k = self.key(name)
        return k in self.salaried or " ".join(str(name or "").lower().split()) in self.salaried

    def salaried_limit(self, name: str) -> float:
        """The most hours code schedules a salaried person for in a week:
        their own maximum, else the restaurant's salaried cap, else
        SALARIED_HOURS_CAP (schedule audit 10/3/26 E-17: the manager gap
        filler loaded a salaried owner to 66h against an 84h line, because
        their hours "cost nothing")."""
        lim = self.hours_limits.get(self.key(name))
        if lim and lim[1]:
            return float(lim[1])
        if self.salaried_cap:
            return float(self.salaried_cap)
        return SALARIED_CAP_DEFAULT

    def salaried_cap_set(self, name: str) -> bool:
        """Whether the owner set the salaried person's weekly limit — their
        own maximum or the restaurant's salaried cap — so it may be called
        theirs ("the 50h you set"); the 55h default is not (RULES-5)."""
        lim = self.hours_limits.get(self.key(name))
        return bool((lim and lim[1]) or self.salaried_cap)

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
        lim = self.hours_limits.get(self.key(name))
        if self.is_salaried(name):
            if lim and lim[1]:
                return float(lim[1])
            return float(self.salaried_cap) if self.salaried_cap else SALARIED_HOURS_MAX
        if lim and lim[1]:
            return float(lim[1])
        return float(self.compliance.get("weekly_hours_ceiling") or DEFAULTS["weekly_hours_ceiling"])

    def min_hours(self, name: str):
        lim = self.hours_limits.get(self.key(name))
        return float(lim[0]) if lim and lim[0] else None

    def can_work(self, name: str, date_str: str, daypart: str = None) -> tuple:
        """(ok, reason) — the one legality question every pass asks."""
        key = self.key(name)
        if not key:
            return False, "no name"
        if key in self.inactive:
            return False, LABELS["inactive"]
        if self.active and key not in self.active:
            return False, LABELS["off_roster"]
        if self.week_dates and date_str not in self.week_dates:
            return False, LABELS["outside_week"]
        # Nobody works a day the restaurant is closed (re-audit 10/4/26
        # PIPE-4): no pass may put a row there, and a row on it is a breach.
        if date_str and date_str in (self.closed_dates or ()):
            return False, LABELS["closed_day"]
        blocked = self.blocked_dates.get(key) or {}
        if date_str in blocked:
            return False, blocked[date_str]
        if daypart in ("morning", "night"):
            for p in ((self.blocked_parts.get(key) or {}).get(date_str) or ()):
                if p.get("daypart") == daypart:
                    return False, p.get("reason") or LABELS["approved_time_off"]
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
        key = self.key(name)
        t = self.trainees.get(key)
        if t and manager_role_kind(t.get("target_role")) in ("owner", "manager", "lead", "trainee") \
                and self._training_on(t, date_str):
            # Training to manage: never the floor manager while it lasts
            # (schedule re-audit 10/4/26 RULES-2).
            return False
        if key in self.managers:
            return True
        return bool(date_str) and date_str in (self.acting_managers.get(key) or ())

    def family(self, role: str) -> str:
        """The role family a role belongs to — the restaurant's own map, else
        the name with its daypart words taken off (shift_quality.role_family)."""
        from shift_quality import role_family
        return role_family(role, self.role_families)

    def holds(self, name: str, role: str, date_str: str = None) -> bool:
        """Whether `name` may be scheduled in `role` (D-15): a role of its
        family is one of theirs (roster role, held roles, roles worked
        here), or the role they are training into, while training lasts.
        Somebody nothing is known about is not judged."""
        key = self.key(name)
        mine = self.known_roles.get(key)
        fam = self.family(role)
        if not mine or not fam or is_training_role(role):
            return True
        if fam in {self.family(r) for r in mine}:
            return True
        t = self.trainees.get(key) or {}
        return bool(t) and self.family(t.get("target_role")) == fam and self._training_on(t, date_str)

    @staticmethod
    def _training_on(t, date_str) -> bool:
        if not date_str:
            return True
        return (not t.get("from") or date_str >= t["from"]) and (not t.get("until") or date_str <= t["until"])

    def training_row(self, row: dict) -> bool:
        """A training shift (D-16): a training job code, or a trainee in the
        role they are learning while training lasts. It is never coverage —
        floors, the owner's rules and "somebody at close" do not count it —
        and no pass hands one out. shift_quality.is_training_row, the one
        test the scorer's floor count asks too (SQ-8)."""
        from shift_quality import is_training_row
        return is_training_row(row, self.trainees.get(self.key(row.get("employee"))), self.role_families)

    def fillable(self, name: str, date_str: str, daypart: str = None) -> tuple:
        """(ok, reason): whether a pass may CHOOSE `name` to fill a gap on
        `date_str` — over and above can_add, which asks whether the row is
        legal. Somebody who has not worked in weeks (dormant) or whose own
        note about that day nobody has confirmed (note_caution) is never
        picked by code; the owner can still write them in. A note that
        names only a lunch or a dinner ("no nights") leaves the other one
        to a fill that says which `daypart` it is filling."""
        key = self.key(name)
        if key in self.dormant:
            from time_utils import mdy
            return False, f"has not worked since {mdy(self.dormant[key]) if self.dormant[key] else 'they were added'}"
        caution = self.note_caution.get(key) or {}
        if caution:
            day = _weekday_of(date_str)
            if date_str in (caution.get("dates") or ()) or (day and day in (caution.get("days") or ())):
                parts = caution.get("parts") or set()
                if not (parts and daypart in ("morning", "night") and daypart not in parts):
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
        key = self.key(name)
        if not key:
            return False, "no name"
        d = row.get("date", "")
        # Any minute in a blocked half of the day is in it (RULES-11).
        from shift_quality import touched_dayparts as _touched
        for part in (_touched(row) or [daypart_of(row.get("shift_start", ""))]):
            ok, why = self.can_work(name, d, part)
            if not ok:
                return False, why
        ok, why = self.window_ok(name, d, row.get("shift_start", ""), row.get("shift_end", ""))
        if not ok:
            return False, why
        ok, why = self.cert_ok(name, row.get("role", ""), d)
        if not ok:
            return False, why
        # The hours the owner's rules give the role (PROMPT-1): no pass adds
        # a Wednesday-lunch bartender to an evening-only bar.
        ok, why = self.role_window_ok(row)
        if not ok:
            return False, why
        # A training shift is never coverage, so no pass creates one: the
        # owner places training beside a trainer (D-16).
        if self.training_row(row):
            return False, "a training shift — not coverage, and placed beside a trainer by the owner"
        # Their week under every spelling that is them, and the roster
        # person an open "same person?" question joins to them (E-25).
        group = self.sweep_key(name)
        mine = [r for r in (rows or ()) if r is not row and self.sweep_key(r.get("employee")) == group]
        worse = regressions(breach_profile(mine, self, person_only=True),
                            breach_profile(mine + [row], self, person_only=True), upto=TIER_PERSON)
        if worse:
            return False, worse[0]["label"]
        if overtime:
            # A salaried person's line is their weekly cap (E-17): code never
            # loads them past it, whatever their hours cost.
            b = self.bucket(d)
            carried = sum(float((per or {}).get(b, 0.0) or 0.0) for k, per in self.base_hours.items()
                          if k == key or self.linked.get(k, k) == group)
            total = (sum(row_hours(r) for r in mine if r.get("date") and self.bucket(r["date"]) == b)
                     + carried + row_hours(row))
            ot = overtime_line(self, name, line)
            if ot and total > ot + 0.05:
                if self.is_salaried(name):
                    return False, f"would take them to {total:g}h, past their {ot:g}h weekly cap"
                return False, f"would take them to {total:g}h, past their {ot:g}h overtime line"
        return True, ""

    def limit_maps(self) -> tuple:
        """(role_windows, role_caps) as schedule_requirements.
        shift_requirements reads them (PROMPT-1): keyed by every job code of
        a family and by the family, {weekday: {"earliest", "latest"}} and
        {weekday: {"morning" | "night" | "any": most}}. ({}, {}) when the
        owner's hours notes state neither."""
        if not self.role_windows and not self.role_caps:
            return {}, {}
        codes = {}
        for r in _all_roles(self):
            codes.setdefault(self.family(r), set()).add(r)
        windows, caps = {}, {}
        for fam, by_day in (self.role_windows or {}).items():
            spec = {d: {"earliest": w.get("earliest"), "latest": w.get("latest")} for d, w in by_day.items()}
            for k in {fam} | codes.get(fam, set()):
                windows[k] = spec
        for fam, items in (self.role_caps or {}).items():
            spec = {}
            for cap in items:
                for d in (cap.get("days") or DAYS):
                    part = cap.get("daypart") or "any"
                    cur = spec.setdefault(d, {})
                    cur[part] = min(int(cap["max"]), cur.get(part, int(cap["max"])))
            for d, parts in spec.items():
                if "any" in parts:
                    for p in ("morning", "night", "late"):
                        parts[p] = min(parts.get(p, parts["any"]), parts["any"])
            for k in {fam} | codes.get(fam, set()):
                caps[k] = spec
        return windows, caps

    def role_window_for(self, role, date_str) -> dict:
        """The owner's hours for `role` on `date_str`'s weekday
        ({"earliest", "latest", "source"}, minutes) — read from the RESTAURANT
        HOURS & SHIFT RULES (apply_hours_rules, PROMPT-1) — or {}."""
        if not self.role_windows:
            return {}
        try:
            day = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            return {}
        return (self.role_windows.get(self.family(role)) or {}).get(day) or {}

    def role_window_ok(self, row) -> tuple:
        """(ok, why): whether a row of a role starts and ends inside the
        hours the owner's rules give that role on that day."""
        w = self.role_window_for(row.get("role"), row.get("date"))
        if not w:
            return True, ""
        s_, e_ = start_minutes(row), end_minutes(row)
        if s_ is None or e_ is None:
            return True, ""
        src = f" (your hours & shift rules: “{str(w.get('source') or '')[:120]}”)" if w.get("source") else ""
        if w.get("earliest") is not None and s_ < w["earliest"]:
            return False, (f"{row.get('role')} starts {row.get('shift_start')} — no earlier than "
                           f"{_fmt_minutes(w['earliest'])}{src}")
        if w.get("latest") is not None and e_ > w["latest"]:
            return False, (f"{row.get('role')} ends {row.get('shift_end')} — done by "
                           f"{_fmt_minutes(w['latest'] % (24 * 60))}{src}")
        return True, ""

    def window_ok(self, name: str, date_str: str, start: str, end: str) -> tuple:
        """Whether a shift's times sit inside the person's window that day —
        and clear of any part of that date they have off (blocked_parts)."""
        key = self.key(name)
        for p in ((self.blocked_parts.get(key) or {}).get(date_str) or ()):
            if p.get("daypart"):
                continue                  # can_work reads a daypart off
            s_, e_ = parse_minutes(start), parse_minutes(end)
            if s_ is None or e_ is None:
                continue
            if e_ <= s_:
                e_ += 24 * 60
            if (p.get("from") is None or e_ > p["from"]) and (p.get("until") is None or s_ < p["until"]):
                return False, p.get("reason") or LABELS["approved_time_off"]
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

    def cert_ok(self, name: str, role: str, date_str: str = None) -> tuple:
        """Whether `name` holds every certificate `role` needs — on
        `date_str`, when given: a card that expires mid-week is held up to
        its expiry and not after (cert_expiry, RULES-14)."""
        need = self.role_requirements.get((role or "").strip().lower()) or set()
        if not need:
            return True, ""
        have = self.certifications.get(self.key(name)) or set()
        if date_str:
            ends = self.cert_expiry.get(self.key(name)) or {}
            have = {x for x in have if not (ends.get(x) and ends[x] < str(date_str)[:10])}
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
        if not rest_rule_applies(self, name):
            need = 0.0                      # an owner's or manager's own turnaround; overlaps still refused
        s, e = shift_span(candidate, self.tz)
        if not s:
            return True, ""
        group = self.sweep_key(name)
        # This week's rows (the candidate itself is among them), then the
        # tail — last week's, a sibling site's, a draft's: a tail row is
        # never the candidate, so one at the same start is an overlap.
        mine = [(r, False) for r in other_rows if self.sweep_key(r.get("employee")) == group]
        mine += [(r, True) for k, rs_ in self.base_rows.items() if k == self.key(name) or self.linked.get(k, k) == group
                 for r in (rs_ or [])]
        for r, tail in mine:
            if r is candidate or (not tail and r.get("date") == candidate.get("date")
                                  and r.get("shift_start") == candidate.get("shift_start")):
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


def _expiring_certs(restaurant_id, week_dates, db_path=None) -> dict:
    """{person name_key: {cert_key: expires_on}} for the certificates that
    run out during the week (after its first day, before its last):
    cert_ok holds a row to the card as it stands on the row's date
    (schedule re-audit 10/4/26 RULES-14 — a card expiring Wednesday
    counted all week)."""
    days = sorted(str(d)[:10] for d in (week_dates or []) if d)
    if not days:
        return {}
    import staff_knowledge
    kw = {"db_path": db_path} if db_path and db_path != DB_PATH else {}
    raw = staff_knowledge.cert_expiries(restaurant_id, days[-1], **kw) or {}
    out = {}
    for k, certs in raw.items():
        for cert, exp in (certs or {}).items():
            if exp >= days[0]:
                cur = out.setdefault(k, {})
                cur[cert_key(cert)] = max(cur.get(cert_key(cert), exp), exp)
    return out


def _load_json(raw, default):
    try:
        return json.loads(raw or "") or default
    except Exception:
        return default


# ── roles: families, who manages, who closes, who is training ───────────────
# (schedule audit 10/3/26 F1). The facts below are the owner's, stored per
# person in staff_settings and per restaurant on the restaurant row, and read
# into the Constraints by build_constraints; every rule, pass and screen
# reads them from there.

import re as _re_roles

# A salaried person's weekly cap when the owner set no limit of their own
# (E-12, E-17): seven 12-hour days (SALARIED_HOURS_CAP) let the manager gap
# filler load a salaried owner to 66h. restaurants.salaried_cap overrides.
# One number: the same default Constraints.salaried_limit falls back to.
SALARIED_CAP_DEFAULT = SALARIED_HOURS_CAP
SALARIED_CAP_BOUNDS = (20.0, 84.0)
# How far back a manager role someone worked still makes them a manager
# automatically (E-14: an hourly manager whose last punch was Bartender
# stopped being one for the week).
MANAGER_RECENT_WEEKS = 8

# The owner-operator titles count as owner (schedule re-audit 10/4/26
# RULES-13: "Managing Partner", "Operating Partner", "Franchisee" and a bare
# "Operator" read as nobody, so a week they ran was "no manager"). A bare
# "Partner" is left alone — some places call every employee one — and so is
# an "Operator" of a machine.
_OWNER_ROLE = _re_roles.compile(
    r"\b(?:co-?owners?|owners?|proprietors?|franchisees?|(?:managing|operating|general|founding|owner|"
    r"principal) partners?)\b|^\s*(?:owner[\s/-]*)?(?:franchise\s+)?operators?\s*$", _re_roles.I)
_MANAGER_WORD = _re_roles.compile(r"\b(?:managers?|mgrs?|supervisors?|supvs?)\b", _re_roles.I)
# Titles that name whoever runs the shift, whatever else the role says
# (P-7: "AGM", "MOD" and "Shift Lead" were missed).
_FLOOR_TITLES = _re_roles.compile(
    r"\b(?:gm|agm|mod|general manager|assistant (?:general )?manager|manager on duty|floor (?:lead|manager|supervisor)|"
    r"shift (?:lead|leader|manager|supervisor)|lead on duty|director of (?:restaurant )?operations|"
    r"operations director)\b", _re_roles.I)
# A manager title with a department in it is that department's manager, not
# somebody who runs the floor (P-7: "Kitchen Manager" and "Bar Manager"
# satisfied the floor-manager rule). The owner says yes per person
# (staff_settings.floor_manager); the confirmation list names them.
_DEPARTMENT_WORDS = _re_roles.compile(
    r"\b(?:kitchen|km|boh|back of house|bar|beverage|wine|catering|events?|office|marketing|culinary|pastry|prep|"
    r"dish|sous|chef|sales|bakery|banquets?|retail|purchasing|facilities|maintenance)\b", _re_roles.I)
# "MIT" is a Manager in Training (schedule re-audit 10/4/26 RULES-2); the
# pattern is shift_quality's, read by the scorer's floor test too (SQ-8).
from shift_quality import _TRAINING_ROLE  # noqa: E402
# The daypart words and the word split are shift_quality's — one reader of
# a job code's half of the day for the rules, the scorer, the solver, the
# prompt and the floors (schedule re-audit 10/4/26 RULES-3, SQ-1).
from shift_quality import _MORNING_WORDS, _NIGHT_WORDS, role_tokens as _role_words  # noqa: E402


def manager_role_kind(role):
    """"owner", "manager" (runs the floor), "lead" (a bare "Lead"),
    "department" (a manager of a department — not counted on its own),
    "trainee" (a "Manager in Training", "Manager Trainee", "MIT" — never
    the floor manager: their shift is a training shift, beside a manager,
    schedule re-audit 10/4/26 RULES-2) or None."""
    text = " ".join(str(role or "").split())
    if not text:
        return None
    if is_training_role(text):
        return "trainee" if (_OWNER_ROLE.search(text) or _FLOOR_TITLES.search(text)
                             or _MANAGER_WORD.search(text) or text.strip().lower() in ("mit", "mits")) else None
    if _OWNER_ROLE.search(text):
        return "owner"
    if _FLOOR_TITLES.search(text):
        return "manager"
    if _MANAGER_WORD.search(text):
        return "department" if _DEPARTMENT_WORDS.search(text) else "manager"
    words = [w for w in _role_words(text) if w not in _MORNING_WORDS | _NIGHT_WORDS]
    if words == ["lead"]:
        return "lead"
    return None


def is_owner_role(role) -> bool:
    return bool(_OWNER_ROLE.search(str(role or "")))


def is_training_role(role) -> bool:
    """A job code for training shifts ("Training", "Server Trainee"): never
    a role with its own headcount (D-16)."""
    return bool(_TRAINING_ROLE.search(str(role or "")))


def role_daypart(role):
    """'morning' for a job code naming the day half ("Server AM", "Lunch
    Bartender"), 'night' for the evening ("Barback PM"), else None —
    shift_quality.role_daypart, the one reader."""
    from shift_quality import role_daypart as _one
    return _one(role)


def role_families(restaurant) -> dict:
    """{role lower: family lower} — the owner's map of job codes to roles
    (restaurants.role_families_json, D-13). A role left out takes its name
    without the daypart words (shift_quality.role_family)."""
    out = {}
    raw = _load_json(getattr(restaurant, "role_families_json", None), {}) if restaurant is not None else {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        k, v = " ".join(str(k or "").lower().split()), " ".join(str(v or "").lower().split())
        if k and v:
            out[k] = v
    return out


def closer_roles(restaurant) -> list:
    """The roles the owner chose to have closers (restaurants
    .closer_roles_json, D-9) as stored; [] means every role somebody is
    marked to close for."""
    raw = _load_json(getattr(restaurant, "closer_roles_json", None), []) if restaurant is not None else []
    return [" ".join(str(x).split()) for x in (raw if isinstance(raw, list) else []) if str(x or "").strip()]


def salaried_cap_set(restaurant):
    """The weekly cap the owner set for a salaried person with no limit of
    their own (restaurants.salaried_cap, within SALARIED_CAP_BOUNDS), or
    None when they set none — Constraints.salaried_cap (RULES-5)."""
    try:
        v = float(getattr(restaurant, "salaried_cap", None))
    except (TypeError, ValueError):
        return None
    if v != v:
        return None
    lo, hi = SALARIED_CAP_BOUNDS
    return min(max(v, lo), hi)


def salaried_cap(restaurant) -> float:
    """The weekly cap for a salaried person with no limit of their own, as
    the settings screen shows it: the owner's, else SALARIED_CAP_DEFAULT."""
    try:
        v = float(getattr(restaurant, "salaried_cap", None))
    except (TypeError, ValueError):
        return SALARIED_CAP_DEFAULT
    lo, hi = SALARIED_CAP_BOUNDS
    return min(max(v, lo), hi) if v == v else SALARIED_CAP_DEFAULT


def _role_minute_map(raw) -> dict:
    out = {}
    for k, v in (raw.items() if isinstance(raw, dict) else []):
        k = " ".join(str(k or "").split())
        try:
            n = max(0, min(240, int(v)))
        except (TypeError, ValueError):
            continue
        if k:
            out[k] = n
    return out


def role_close_stays(restaurant) -> dict:
    """{role: N} — each role's ONE "stays until close + N minutes" setting
    (D-43). Two settings overlapped: role_close_min_json (the last of the
    role must stay until N after close) and role_close_buffer_json (it may
    run N past close); owners set one and expected the other. The stay is
    the must-stay value where the owner set one, else the allowance; both
    stored values are kept (role_close_conflicts names a role where they
    differ, until the owner saves the one setting, which writes both)."""
    may = _role_minute_map(_load_json(getattr(restaurant, "role_close_buffer_json", None), {}))
    must = _role_minute_map(_load_json(getattr(restaurant, "role_close_min_json", None), {}))
    out = {}
    for src in (may, must):
        for k, v in src.items():
            for old in [x for x in out if x.lower() == k.lower()]:
                del out[old]
            out[k] = v
    return out


def role_close_caps(restaurant) -> dict:
    """{role: N} the latest past close a role's shifts may run: the larger
    of its stay and its allowance, so a role told to stay is never clamped
    at close (models.get_role_close_buffers, the close-time clamp)."""
    may = _role_minute_map(_load_json(getattr(restaurant, "role_close_buffer_json", None), {}))
    must = _role_minute_map(_load_json(getattr(restaurant, "role_close_min_json", None), {}))
    out = {}
    for k, v in list(may.items()) + list(must.items()):
        old = next((x for x in out if x.lower() == k.lower()), None)
        if old is None:
            out[k] = v
        else:
            out[old] = max(out[old], v)
    return out


def role_close_conflicts(restaurant) -> list:
    """[{role, stays, may_run}] where the two stored values differ — said
    to the owner so the one setting is confirmed (D-43)."""
    may = {k.lower(): (k, v) for k, v in _role_minute_map(
        _load_json(getattr(restaurant, "role_close_buffer_json", None), {})).items()}
    must = _role_minute_map(_load_json(getattr(restaurant, "role_close_min_json", None), {}))
    out = []
    for k, v in must.items():
        other = may.get(k.lower())
        if other and other[1] != v:
            out.append({"role": k, "stays": v, "may_run": other[1]})
    return out


def role_minutes(mapping, role, families=None, default=0):
    """mapping[role] read the way a schedule names roles: exactly, then
    case-insensitively, then by role family ("Bartender": 30 holds for a
    "Bartender PM" row — D-13), the largest when a family has several."""
    if not mapping:
        return default
    if role in mapping:
        return mapping[role]
    low = " ".join(str(role or "").lower().split())
    for k, v in mapping.items():
        if " ".join(str(k).lower().split()) == low:
            return v
    from shift_quality import role_family
    fam = role_family(role, families)
    hits = [v for k, v in mapping.items() if fam and role_family(k, families) == fam]
    return max(hits) if hits else default


def manager_basis(name, role, settings=None, held=(), recent=(), certs=()) -> dict:
    """Whether a person counts as a manager on the floor, and why — the one
    answer for the rules and the owner's confirmation list (P-7, E-14,
    E-15): {"counts", "basis", "role", "why"}.

    The owner's word first (staff_settings.floor_manager: yes or no).
    Otherwise automatic: an owner or manager role on the roster, a role
    they hold (people.held_roles — a promotion), a manager role worked in
    the last MANAGER_RECENT_WEEKS weeks, or the floor manager certificate.
    A department manager ("Kitchen Manager") does not count on its own; it
    is named so the owner decides. `role` on the answer is the role a
    manager shift of theirs is written in."""
    st = settings or {}
    roster_role = " ".join(str(role or "").split())
    fm = st.get("floor_manager")
    if manager_role_kind(roster_role) == "trainee":
        # A manager in training is never the floor manager — not even on
        # the owner's yes, which is about the person once trained: they
        # count when their role in Team says they finished (RULES-2).
        return {"counts": False, "basis": "trainee", "role": roster_role,
                "why": f"{roster_role} is still training — a trainee runs the floor only beside a manager; "
                       "change their role in Team when they finish"}
    if fm is True:
        return {"counts": True, "basis": "set", "role": roster_role or "Manager",
                "why": "you set them as a floor manager"}
    if fm is False:
        return {"counts": False, "basis": "set_not", "role": roster_role,
                "why": "you set them as not a floor manager"}
    kind = manager_role_kind(roster_role)
    if kind in ("owner", "manager", "lead"):
        return {"counts": True, "basis": "role", "role": roster_role,
                "why": f"their role is {roster_role}"}
    for r in held or ():
        if manager_role_kind(r) in ("owner", "manager", "lead"):
            return {"counts": True, "basis": "held_role", "role": r, "why": f"they hold the {r} role"}
    for r in recent or ():
        if manager_role_kind(r) in ("owner", "manager", "lead"):
            return {"counts": True, "basis": "recent_role", "role": r,
                    "why": f"they worked as {r} in the last {MANAGER_RECENT_WEEKS} weeks"}
    if "manager" in {cert_key(x) for x in certs or ()}:
        return {"counts": True, "basis": "certificate", "role": roster_role or "Manager",
                "why": "they hold the floor manager certificate"}
    dept = next((r for r in [roster_role, *list(held or ()), *list(recent or ())]
                 if manager_role_kind(r) == "department"), None)
    if dept:
        return {"counts": False, "basis": "department", "role": dept,
                "why": f"{dept} manages a department, not the floor — set them as a floor manager if they run shifts"}
    return {"counts": False, "basis": None, "role": roster_role, "why": ""}


def role_for_daypart(name, daypart, roles, families=None):
    """The job code `name` means for `daypart`, among the restaurant's
    `roles`: `name` itself when it is one of them; else the one role of its
    family whose name says that half of the day ("servers at night" at a
    restaurant with "Server AM" and "Server PM" is "Server PM"); else the
    one role of the family with no daypart in its name; else None."""
    from shift_quality import role_family
    roles = [" ".join(str(r).split()) for r in roles or () if str(r or "").strip()]
    low = " ".join(str(name or "").lower().split())
    exact = next((r for r in roles if r.lower() == low), None)
    if exact:
        return exact
    fam = role_family(name, families)
    mine = sorted({r for r in roles if role_family(r, families) == fam}, key=str.lower)
    if not mine:
        return None
    if daypart:
        hit = [r for r in mine if role_daypart(r) == daypart]
        if len(hit) == 1:
            return hit[0]
    plain = [r for r in mine if role_daypart(r) is None]
    if len(plain) == 1:
        return plain[0]
    return mine[0] if len(mine) == 1 else None


def build_constraints(restaurant_id, week_dates, week_days, restaurant=None, db_path=DB_PATH) -> Constraints:
    """Everything the pipeline needs to know about who can work when,
    gathered once. Every source is optional; a missing one costs a rule,
    never the generation."""
    from models import get_restaurant, get_staff_availability, get_close_times, get_role_close_buffers, get_staff_notes
    restaurant = restaurant or get_restaurant(restaurant_id, db_path)
    c = Constraints(restaurant_id=restaurant_id, week_dates=list(week_dates or []), week_days=list(week_days or []))
    c.compliance = compliance(restaurant)
    try:
        c.compliance_set = compliance_set_by(restaurant)
    except Exception as exc:
        _input_problem(c, "compliance rules", exc)
    c.week_start_day = int(getattr(restaurant, "week_start_day", 0) or 0)
    c.tz = (getattr(restaurant, "timezone", None) or "").strip()
    c.jurisdiction = (getattr(restaurant, "jurisdiction", None) or "").strip().upper()
    c.section_cap = int(getattr(restaurant, "section_count", 0) or 0)
    c.role_floors = role_floors(restaurant)
    # The owner's role families and the salaried weekly cap (F1: D-13, E-12).
    c.role_families = role_families(restaurant)
    c.salaried_cap = salaried_cap_set(restaurant)
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
    # One person, one key (schedule audit 10/3/26 D-8, E-25): every spelling
    # people's identity knows for a roster person files their facts under
    # the roster's spelling, before any fact is read.
    _identity(c, restaurant_id, _people, db_path)
    try:
        c.salaried = _salaried_keys(c, restaurant, db_path)
    except Exception as exc:
        c.salaried = set()
        _input_problem(c, "salaried staff", exc)
    # Certificates past their expiry (staff_knowledge.staff_certs) are not
    # held: an expired food-handler card does not satisfy a role that needs
    # one (employee audit B7 handoff). Valid on the week's first day is the
    # test — cert_ok has no day of its own.
    expired = {}
    for k, v in (_expired_certs(restaurant_id, c.week_dates, db_path) or {}).items():
        expired.setdefault(c.key(k), set()).update(v)
    try:
        for k, v in (_expiring_certs(restaurant_id, c.week_dates, db_path) or {}).items():
            c.cert_expiry.setdefault(c.key(k), {}).update(v)
    except Exception as exc:
        _input_problem(c, "certificate expiry dates", exc)
    if c.stations and c.stations.get("skills"):
        # A station skill typed under another spelling is the person's own.
        skills = {}
        for person, have in c.stations["skills"].items():
            k = _file_under(c, "kitchen station skills", person)
            who = c.display.get(k, person)
            skills[who] = sorted(set(skills.get(who, [])) | set(have),
                                 key=lambda s_: (c.stations.get("stations") or []).index(s_)
                                 if s_ in (c.stations.get("stations") or []) else 99)
        c.stations = dict(c.stations, skills=skills)
    # Every role each person holds or has worked (F1 — E-14, D-15): who is
    # a manager and which roles are somebody's read all of them, not only
    # the roster's most recent one — filed under each person's one key
    # (D-8), so a role held under an old spelling is still theirs.
    held, worked = _roles_on_file(c, restaurant_id, db_path)
    held, worked = _by_person_key(c, held, keep=min), _by_person_key(c, worked, keep=max)
    # An Owner role is salaried-style unless the owner marked them paid by
    # the hour (E-12): no overtime line, the salaried weekly cap. Read before
    # each person's settings, so a salaried-style owner is never given the
    # restaurant's full-time minimum either (D-41: their pay does not follow
    # their hours).
    for e in _people:
        if e["active"] and not (e.get("settings") or {}).get("paid_hourly"):
            key = c.key(e["name"])
            mine = [e.get("role")] + sorted((held.get(key) or {}).keys())
            if any(is_owner_role(r) for r in mine) and not c.is_salaried(e["name"]):
                c.salaried.add(key)
                c.salaried_style.add(key)
    for e in _people:
        key = c.key(e["name"])
        if e["active"]:
            c.roster_names.append(e["name"])
            c.active.add(key)
        else:
            c.inactive.add(key)
        try:
            _person_settings(c, e, expired, _ss, held=held, worked=worked)
        except Exception as exc:
            _input_problem(c, "settings", exc, name=e.get("name"))
    # Closers, per role (D-9, L-9): each marked to close for their role, in
    # the roles the owner chose — until they choose, the roles their own
    # punches show on until close, so a cook marked to close (47 of 64 were,
    # at Simple EJ's) is never held to the bar's 2am.
    try:
        from models import get_leader_flags
        chosen, c.closer_roles_basis = closer_roles(restaurant), "yours"
        c.close_roles, c.early_close_roles = closing_split(c, _closing_history(restaurant_id, c))
        if not chosen:
            chosen = sorted(c.close_roles)
            c.closer_roles_basis = "history" if chosen else "all"
        _flags = get_leader_flags(restaurant_id, db_path) or {}
        # A closer flag under a spelling nobody on the roster goes by is
        # named (D-8); one under an alias is the person's (_closers_by_role
        # matches by c.key).
        for _n, _v in _flags.items():
            if _v:
                _file_under(c, "closers", _n)
        _closers_by_role(c, _people, _flags, chosen)
    except Exception as exc:
        _input_problem(c, "closers", exc)
    c.role_requirements = {str(k).strip().lower(): {cert_key(x) for x in (v or []) if str(x).strip()}
                           for k, v in (_load_json(getattr(restaurant, "role_requirements_json", None), {}) or {}).items() if k}
    foh = _load_json(getattr(restaurant, "foh_roles_json", None), [])
    c.foh_roles = {str(x).strip().lower() for x in foh if str(x).strip()} or {"server"}
    c.patio_roles = {str(x).strip().lower() for x in (_load_json(getattr(restaurant, "patio_roles_json", None), []) or []) if str(x).strip()}
    # Role settings hold for every job code of the role (D-13): the section
    # cap's default {"server"} matched nobody at a restaurant whose servers
    # are "Server AM" and "Server PM", and a certificate "Bartender" needs
    # was never asked of a "Bartender PM".
    c.foh_roles = _by_family(c, c.foh_roles)
    c.patio_roles = _by_family(c, c.patio_roles)
    for role, certs in list(c.role_requirements.items()):
        for r in _by_family(c, {role}):
            c.role_requirements[r] = set(c.role_requirements.get(r) or set()) | set(certs)
    # "Stays until close + N", one setting per role (D-43), by family: the
    # last of the bartenders stays, whichever bartender code they work.
    c.close_mins = {}
    for k, v in role_close_stays(restaurant).items():
        fam = c.family(k)
        if fam:
            c.close_mins[fam] = max(int(v), c.close_mins.get(fam, 0))

    _note_parts = []                      # (key, name, text, noted, expires): every note, for D-34
    # weekday availability + free-text notes. A weekday blocked for good is
    # a weekday rule; one blocked only between two dates ("Fridays until
    # 12/15", day_bounds — employee audit M5) blocks the dates of this week
    # inside them, and only those.
    try:
        from models import availability_day_bounds, availability_blocked_dates
        for a in get_staff_availability(restaurant_id, db_path) or []:
            if not (a.get("employee_name") or "").strip():
                continue
            key = _file_under(c, "availability", a.get("employee_name"))
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
                c.notes[key] = (c.notes.get(key, "") + " " + a["notes"].strip()).strip()
                _note_parts.append((key, a.get("employee_name"), a["notes"].strip(),
                                    str(a.get("updated_at") or "")[:10] or None, None))
    except Exception as exc:
        _input_problem(c, "availability", exc)
    try:
        for n in get_staff_notes(restaurant_id, db_path) or []:
            if not (n.get("employee_name") or "").strip():
                continue
            key = _file_under(c, "scheduling notes", n.get("employee_name"))
            if key and n.get("notes"):
                c.notes[key] = (c.notes.get(key, "") + " " + n["notes"]).strip()
                for part in (n.get("parts") or [{"text": n["notes"]}]):
                    if part.get("ended") or not str(part.get("text") or "").strip():
                        continue
                    _note_parts.append((key, n.get("employee_name"), str(part["text"]).strip(),
                                        part.get("noted"), part.get("expires")))
    except Exception as exc:
        _input_problem(c, "staff notes", exc)
    # Every note nobody has confirmed as a hold, read by the hold reader
    # (schedule audit 10/3/26 D-34): the days it names are kept clear by the
    # fill passes (Constraints.fillable) — "Ana can't close Fridays" was
    # told to the model and nothing stopped a backstop from putting her on
    # a Friday close. c.notes stays the model's text.
    try:
        _note_cautions(c, restaurant_id, _note_parts, db_path)
    except Exception as exc:
        _input_problem(c, "scheduling notes", exc)

    # time off: approved blocks, pending is a warning
    if c.week_dates:
        try:
            import time_off as _to
            # Part of a day off (D-39) blocks that part only; the rest of the
            # day stays workable.
            for name, by_date in (_to.approved_parts_in_window(restaurant_id, c.week_dates[0], c.week_dates[-1],
                                                               db_path=db_path) or {}).items():
                key = _file_under(c, "time off", name, "approved time off on " + ", ".join(
                    _mdy_safe(d) for d in sorted(by_date)))
                for d, parts in by_date.items():
                    for p in parts:
                        c.blocked_parts.setdefault(key, {}).setdefault(d, []).append(
                            dict(p, reason=f"{LABELS['approved_time_off']} ({p.get('words') or 'part of the day'})"))
                if key not in c.display and c.display:
                    c.input_problems.append({"source": "time off", "name": name,
                                             "error": "approved time off (" + ", ".join(_mdy_safe(d) for d in sorted(by_date))
                                                      + ") matches nobody on the roster — check the spelling"})
            for name, days in (_to.approved_in_window(restaurant_id, c.week_dates[0], c.week_dates[-1], db_path=db_path,
                                                      whole_days_only=True) or {}).items():
                key = _file_under(c, "time off", name, "approved time off " + ", ".join(_mdy_safe(d) for d in days))
                c.blocked_dates.setdefault(key, {}).update({d: LABELS["approved_time_off"] for d in days})
                if key not in c.display and c.display:
                    # A legal block typed under a name nobody on the roster
                    # goes by holds for nobody: the gate names it (D-8).
                    c.input_problems.append({"source": "time off", "name": name,
                                             "error": f"approved time off ({', '.join(_mdy_safe(d) for d in days)}) "
                                                      "matches nobody on the roster — check the spelling"})
            for name, days in (_pending_in_window(restaurant_id, c.week_dates[0], c.week_dates[-1], db_path) or {}).items():
                key = _file_under(c, "time off", name, "asked for " + ", ".join(_mdy_safe(d) for d in days) + " off")
                c.pending_off.setdefault(key, set()).update(days)
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
    # The RESTAURANT HOURS & SHIFT RULES the owner wrote (hours_notes): the
    # floors, the hours each role works, the ceilings and the stays after
    # close they state, held like any other (schedule re-audit 10/4/26
    # PROMPT-1) — after the Studio's rules, so a start the owner confirmed
    # there stands.
    try:
        apply_hours_rules(c, restaurant)
    except Exception as exc:
        _input_problem(c, "your hours & shift rules", exc)
    # A person's scheduling notes the owner confirmed as holds ("no Tuesdays
    # until 10/31", "out 12/20-12/28") are unavailability like any other
    # (person_note_holds, 10/2/26).
    try:
        import person_note_holds
        person_note_holds.apply_holds(c, restaurant_id, db_path=db_path)
    except Exception as exc:
        _input_problem(c, "scheduling note holds", exc)
    # A floor set on a role name the restaurant does not use as a job code
    # ("Server" where the codes are "Server AM" and "Server PM") is held on
    # the code that works that half of the day (D-13, D-14).
    try:
        _floors_on_job_codes(c)
    except Exception as exc:
        _input_problem(c, "role floors", exc)
    # Who has stopped working (schedule audit 10/3/26 E-3, D-17): no shift in
    # staff_settings.DORMANT_WEEKS weeks, not hand-added since, nothing on
    # file saying they are still here. Still on the roster — a row the owner
    # writes for them is legal — but never chosen by a fill (fillable), left
    # off the model's roster and the open-shift broadcast. Never dormant:
    # anyone on a published shift in those weeks (dormant_people), managers
    # of any kind — a floor manager, a department's, an owner, a lead, by
    # the roles they hold or have worked, or on a published week as one —
    # salaried people, and anyone with standing shifts, an acting-manager
    # date or a training: managers barely punch. The LAST step, so every
    # one of those facts is read first.
    try:
        import staff_settings as _ss
        exempt = (set(c.managers) | set(c.salaried) | set(c.standing_shifts or {})
                  | set(c.acting_managers or {}) | set(c.trainees or {}))
        exempt |= {k for k, roles in (c.known_roles or {}).items()
                   if any(manager_role_kind(r) for r in roles or ())}
        dormant = _ss.dormant_people(restaurant_id, people=_people, exempt=exempt, db_path=db_path)
        for e in _people:
            k = c.key(e["name"])
            if k in c.active and _ss.name_key(e["name"]) in dormant:
                c.dormant[k] = dormant[_ss.name_key(e["name"])]
    except Exception as exc:
        _input_problem(c, "who has stopped working", exc)
    return c


def _identity(c: "Constraints", restaurant_id, people_rows, db_path=None):
    """c.aliases, c.linked and c.display from the roster (people.
    identity_index). A failure costs the aliases, never the generation: the
    plain fold still keys everyone, and the review says so."""
    c.display = {}
    for e in people_rows or []:
        n = str(e.get("name") or "").strip()
        if n:
            c.display.setdefault(" ".join(n.split()).lower(), " ".join(n.split()))
    if not c.display:
        return
    try:
        import people
        idx = people.identity_index(restaurant_id, list(c.display.values()),
                                    db_path=None if db_path == DB_PATH else db_path)
        c.aliases = dict(idx.get("key_of") or {})
        c.linked = dict(idx.get("linked") or {})
    except Exception as exc:
        _input_problem(c, "who is who on the roster", exc)


def _file_under(c: "Constraints", source: str, name, detail: str = "") -> str:
    """The key a per-person fact is filed under (c.key), and — when the
    name matches nobody on the roster, active or deactivated — a line in
    c.unmatched for the review (D-8): a fact under a spelling nobody goes
    by applies to nobody, and the owner is told rather than left to find
    out on the floor."""
    key = c.key(name)
    if key and c.display and key not in c.display:
        shown = " ".join(str(name or "").split())
        if not any(u["source"] == source and u["name"] == shown for u in c.unmatched):
            entry = {"source": source, "name": shown,
                     "detail": f"{shown}: {detail or source} — matches nobody on the roster"}
            try:
                import people
                maybe = people.similar_on_roster(shown, list(c.display.values()))
            except Exception:
                maybe = []
            if maybe:
                entry["suggestion"] = maybe[0]
                entry["detail"] += f" (is it {maybe[0]}?)"
            c.unmatched.append(entry)
    return key


def _note_cautions(c: "Constraints", restaurant_id, note_parts, db_path=None):
    """c.note_caution from every note part not confirmed as a hold
    (person_note_holds.caution): {key: {"days", "dates", "parts", "text"}}.
    Two notes on one person keep the fills off both; a lunch-or-dinner
    allowance (parts) stands only when every note on that person names the
    same one — otherwise the whole day is kept clear."""
    if not note_parts:
        return
    import person_note_holds as _pnh
    try:
        from models import _restaurant_today
        today = _restaurant_today(restaurant_id)
    except Exception:
        today = None
    held = set()
    for h in _pnh.holds(restaurant_id, db_path=db_path, include_ended=True):
        held.add((c.key(h["employee_name"]), _pnh._key(h["part_text"])))
    for key, name, text, noted, expires in note_parts:
        if (key, _pnh._key(text)) in held:
            continue                      # confirmed: a hard hold (apply_holds), not a caution
        got = _pnh.caution(text, noted=noted, expires=expires, week_dates=c.week_dates, today=today)
        if not got:
            continue
        cur = c.note_caution.get(key)
        if cur is None:
            c.note_caution[key] = {"days": set(got["days"]), "dates": set(got["dates"]),
                                   "parts": set(got.get("parts") or ()), "text": text}
            continue
        same = cur.get("parts") and cur["parts"] == set(got.get("parts") or ())
        cur["days"] |= set(got["days"])
        cur["dates"] |= set(got["dates"])
        cur["parts"] = cur["parts"] if same else set()
        if text.lower() not in str(cur.get("text") or "").lower():
            cur["text"] = (str(cur.get("text") or "") + "; " + text).strip("; ")


def _salaried_keys(c: "Constraints", restaurant, db_path=None) -> set:
    """The keys of the salaried people (models.salaried_staff) through
    people's identity (D-7): each entry's own spelling, and the roster key
    of the one person any of its spellings means. A salaried name that
    matches nobody on the roster is named in c.unmatched — the model is
    never told about a salaried person the roster check would refuse."""
    from models import salaried_staff, salaried_name_key
    staff = salaried_staff(restaurant)
    if not staff:
        return set()
    out = set()
    try:
        import people
        spelled = people.spellings(c.restaurant_id, [s_["name"] for s_ in staff],
                                   db_path=None if db_path == DB_PATH else db_path)
    except Exception:
        spelled = {}
    for s_ in staff:
        own = salaried_name_key(s_["name"])
        out.add(own)
        keys = {c.key(k) for k in (spelled.get(s_["name"]) or {own})} | {c.key(s_["name"])}
        on = {k for k in keys if k in c.display}
        out |= on
        if not on:
            _file_under(c, "salaried staff", s_["name"], "salaried")
    return out


def _by_person_key(c: "Constraints", d: dict, keep=max) -> dict:
    """{person key: {role: date}} from a map kept by any spelling (held and
    worked roles — F1's _roles_on_file, by name_key): every spelling of one
    person filed under their one key (c.key, D-8), two dates for one role
    combined by `keep` (the earliest since a role was held, the latest it
    was worked)."""
    out = {}
    for k, roles in (d or {}).items():
        mine = out.setdefault(c.key(k), {})
        for role, when in (roles or {}).items():
            if role in mine and mine[role] and when:
                mine[role] = keep(mine[role], when)
            else:
                mine[role] = mine.get(role) or when
    return out


def _roles_on_file(c, restaurant_id, db_path):
    """({name_key: {role: since}}, {name_key: {role: last worked}}) — the
    roles people hold (people.held_roles, in force by the week's last day)
    and every role they have worked. Either unreadable costs only itself."""
    held, worked = {}, {}
    try:
        import people as _people_mod
        from datetime import date as _d
        last = _d.fromisoformat(max(c.week_dates)) if c.week_dates else None
        for r in _people_mod.held_roles(restaurant_id, db_path=None if db_path == DB_PATH else db_path, today=last):
            if (r.get("role") or "").strip():
                held.setdefault(r["key"], {})[" ".join(r["role"].split())] = r.get("since")
    except Exception as exc:
        _input_problem(c, "roles held", exc)
    try:
        import staff_settings as _ss
        worked = _ss.worked_roles(restaurant_id, db_path=db_path) or {}
    except Exception as exc:
        _input_problem(c, "roles worked", exc)
    return held, worked


def _all_roles(c) -> set:
    """Every role name in use here (lower): roster, held and worked roles,
    the owner's family map."""
    out = set(c.role_families) | set(c.role_names)
    for roles in c.known_roles.values():
        out |= set(roles)
    return out


def _by_family(c, roles) -> set:
    """`roles` (lower) and every role in use here of the same families."""
    fams = {c.family(r) for r in roles if r}
    return set(roles) | {r for r in _all_roles(c) if c.family(r) in fams}


def _closers_by_role(c, people, flags, chosen):
    """c.closers_by_role and c.keyholders from the closer flags (D-9): a
    person marked to close closes for the roles they were given
    (staff_settings.closes_for), else their own role's family; when the
    owner chose which roles have closers, only those. Only people on the
    roster count; a flag set through view-as does not (get_leader_flags,
    L-9)."""
    # Matched through people's identity (D-8): a flag kept under an alias
    # or an old spelling is the person's.
    flagged = {c.key(n) for n, v in (flags or {}).items() if v}
    keep = {c.family(x) for x in chosen or () if c.family(x)}
    for e in people:
        if not e.get("active") or c.key(e["name"]) not in flagged:
            continue
        key = c.key(e["name"])
        c.closer_flags.add(key)
        st = e.get("settings") or {}
        fams = {c.family(x) for x in (st.get("closes_for") or []) if c.family(x)} or \
            ({c.family(e.get("role"))} if c.family(e.get("role")) else set())
        for fam in fams:
            if not keep or fam in keep:
                c.closers_by_role.setdefault(fam, set()).add(key)
    c.keyholders = set().union(*c.closers_by_role.values()) if c.closers_by_role else set()


def chosen_closers(restaurant_id, restaurant=None, db_path=DB_PATH) -> dict:
    """{role family: {person key}} — the people the owner chose to close
    each role, read exactly as build_constraints reads them (the closer
    flags, in the roles the owner chose — until they choose, the roles the
    punches show on until close; _closers_by_role) without the rest of a
    week's constraints: for the readers that have none — the scheduling
    memory's learned closer and the rotation plan's closing queue (schedule
    re-audit 10/4/26 LEARN-10, PROMPT-2: both named people who are not
    chosen closers as the role's closers, beside the hard closer rule). {}
    when nobody is marked to close."""
    from models import get_restaurant, get_close_times, get_leader_flags
    restaurant = restaurant or get_restaurant(restaurant_id, db_path)
    flags = get_leader_flags(restaurant_id, db_path) or {}
    if not any(flags.values()):
        return {}
    c = Constraints(restaurant_id=restaurant_id, week_dates=[], week_days=[])
    c.role_families = role_families(restaurant)
    try:
        c.close_times = get_close_times(restaurant_id, db_path) or {}
    except Exception as exc:
        _input_problem(c, "open and close times", exc)
    import staff_settings as _ss
    people_ = list(_ss.roster(restaurant_id, db_path=db_path, include_inactive=True))
    _identity(c, restaurant_id, people_, db_path)
    chosen = closer_roles(restaurant)
    if not chosen:
        chosen = sorted(closing_families(c, _closing_history(restaurant_id, c)))
    _closers_by_role(c, people_, flags, chosen)
    return {fam: set(keys) for fam, keys in c.closers_by_role.items() if keys}


# A role "closes here" when its last person out reached the close (or, with
# no close on file, the day's last shift) — within the closer rule's own 15
# minutes — on at least half the days it worked in the last weeks of
# punches, and on enough days to say so.
CLOSING_ROLE_WEEKS = 8
CLOSING_ROLE_SHARE = 0.5
CLOSING_ROLE_MIN_DAYS = 3


def _closing_history(restaurant_id, c) -> list:
    """The last CLOSING_ROLE_WEEKS weeks of punches before the week."""
    try:
        import shift_facts
        from datetime import date as _d
        first = _d.fromisoformat(min(c.week_dates)) if c.week_dates else _d.today()
        return shift_facts.person_rows(restaurant_id, since=(first - timedelta(weeks=CLOSING_ROLE_WEEKS)).isoformat(),
                                       until=(first - timedelta(days=1)).isoformat()) or []
    except Exception as exc:
        _input_problem(c, "closing roles", exc)
        return []


def closing_families(c, rows) -> set:
    """The role families the restaurant's own punches show on until close
    (D-9): a closer rule's default reach until the owner chooses the roles."""
    return closing_split(c, rows)[0]


def closing_split(c, rows) -> tuple:
    """(closing, early): the role families the punches show on until close,
    and those seen on enough days to say they end before it (a kitchen that
    closes at 11pm when the bar closes at 2am) — closer_need holds only the
    first to the restaurant's close (RULES-6)."""
    by_day, fam_day = {}, {}
    for r in rows or []:
        d, fam, e = str(r.get("date") or "")[:10], c.family(r.get("role")), end_minutes(r)
        if not d or not fam or e is None or is_training_role(r.get("role")):
            continue
        by_day[d] = max(by_day.get(d, e), e)
        fam_day[(d, fam)] = max(fam_day.get((d, fam), e), e)
    worked, closed = {}, {}
    for (d, fam), e in fam_day.items():
        close = close_minutes(c, _weekday_of(d))
        last = close if close is not None else by_day[d]
        worked[fam] = worked.get(fam, 0) + 1
        if e >= last - 15:
            closed[fam] = closed.get(fam, 0) + 1
    seen = {f for f, n in worked.items() if n >= CLOSING_ROLE_MIN_DAYS}
    closing = {f for f in seen if closed.get(f, 0) >= CLOSING_ROLE_SHARE * worked[f]}
    return closing, seen - closing


def _floors_on_job_codes(c):
    """Re-key a floor named by a role family onto the job code that works
    each half of the day, merging with any floor already on that code (the
    larger wins). A floor whose name is a code, or a family with no single
    code for a half, stays as it is."""
    roles = sorted({c.role_names.get(r, r) for r in _all_roles(c)}, key=str.lower)
    if not roles or not c.role_floors:
        return
    known = {r.lower() for r in roles}
    out = {}
    sources = dict(c.rule_floor_sources or {})

    def _put(code, part, day, n, src_key):
        spec = out.setdefault(code, {"morning": 0, "night": 0, "days": {}})
        if day is None:
            spec[part] = max(int(spec.get(part) or 0), int(n))
        else:
            d = spec["days"].setdefault(day, {})
            d[part] = max(int(d.get(part) or 0), int(n))
        if src_key in sources and src_key[0] != code.lower():
            c.rule_floor_sources[(code.lower(), day, part)] = sources[src_key]

    for role, spec in (c.role_floors or {}).items():
        if role.strip().lower() in known:
            cur = out.setdefault(role, {"morning": 0, "night": 0, "days": {}})
            cur["morning"] = max(int(cur.get("morning") or 0), int(spec.get("morning") or 0))
            cur["night"] = max(int(cur.get("night") or 0), int(spec.get("night") or 0))
            for day, ds in (spec.get("days") or {}).items():
                for part, n in (ds or {}).items():
                    d = cur["days"].setdefault(day, {})
                    d[part] = max(int(d.get(part) or 0), int(n or 0))
            continue
        moved = False
        for part in ("morning", "night"):
            code = role_for_daypart(role, part, roles, c.role_families)
            if not code:
                continue
            code = next((r for r in (c.role_floors or {}) if r.strip().lower() == code.lower()), code)
            if spec.get(part):
                _put(code, part, None, spec[part], None)
            for day, ds in (spec.get("days") or {}).items():
                if part in (ds or {}):
                    _put(code, part, day, ds[part], (role.strip().lower(), day, part))
            moved = True
        if not moved:
            out[role] = spec
    c.role_floors = {k: v for k, v in out.items() if v.get("morning") or v.get("night") or v.get("days")}
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


# What each source the rules could not read means for the week, in the
# owner's words — one wording for the generation's review and the publish
# gate (schedule audit 10/3/26 P-1; client_api.publish_review reads it).
# A source not named here reads as "<source> couldn't be read".
INPUT_PROBLEM_WORDS = {
    "roster": "The team list couldn't be read, so nobody's own rules (hours, minors, availability, certificates) "
              "were checked",
    "availability": "Staff availability couldn't be read, so nobody's days off were checked",
    "time off": "Time off couldn't be read, so approved days off weren't checked",
    "open and close times": "Opening and closing times couldn't be read, so the close rules weren't checked",
    "closers": "Who closes couldn't be read, so the closer rule wasn't checked",
    "salaried staff": "The salaried staff list couldn't be read, so everyone was checked as hourly",
    "last week's shifts": "The week before's published shifts couldn't be read, so overtime and rest across the "
                          "two weeks weren't checked",
    "your staffing rules": "Your staffing rules couldn't be read, so their floors weren't checked",
    "schedule note rules": "The notes you confirmed as rules couldn't be read, so they weren't checked",
    "your hours & shift rules": "Your hours & shift rules couldn't be read, so their floors and arrival times "
                                "weren't checked",
    "scheduling note holds": "The staff notes you confirmed as holds couldn't be read, so they weren't checked",
    "staff notes": "Staff notes couldn't be read",
    "closed dates": "Your closed dates couldn't be read, so a closed day wasn't checked as closed",
    "kitchen stations": "Kitchen stations couldn't be read, so station coverage wasn't checked",
}


def input_problem_text(p) -> str:
    """One line for a setting or source the rules could not read
    (Constraints.input_problems): a person's settings name the person and
    where to fix them."""
    if p.get("name"):
        return (f"Settings for {p['name']} couldn't be read — fix them in Team; this week wasn't checked against "
                "their rules")
    src = str(p.get("source") or "a setting")
    return INPUT_PROBLEM_WORDS.get(src) or f"{src[:1].upper()}{src[1:]} couldn't be read, so the rules check ran without it"


# A standing shift tied to an event series (staff_settings when_event) is
# worked on each of its weekdays the series plays, and stays on until this
# long after kickoff when the game runs past the shift's end: an NFL game
# runs about three and a half hours, and the owner stays half an hour after.
EVENT_STAND_AFTER_KICKOFF_MIN = 4 * 60


def _event_standing(c: "Constraints", x: dict, role, name):
    """A standing shift that holds only when its event series plays that
    weekday ({"day": "Sunday", "when_event": "nfl-chicago-bears", ...}): one
    dated entry (from = until = the date) for each such date this week, its
    end held to the game's (owner, 10/5/26: "if the Bears play on a Sunday,
    schedule Erik 100% of the time, no exceptions; ONLY if they play"). A
    calendar that can't be read is a problem on the week, never a quiet
    week without them."""
    from datetime import date as _date
    day = str(x.get("day") or "").strip().capitalize()
    dates = [d for d in (c.week_dates or []) if DAYS[_date.fromisoformat(d).weekday()] == day]
    if not dates:
        return []
    try:
        from event_intel import store as _ev
        series = _ev.series_by_slug(str(x["when_event"]))
        if not series:
            raise LookupError(f"no event series '{x['when_event']}'")
        games = {}
        for ev in _ev.events_for([series["id"]], min(dates), max(dates)):
            if ev.get("event_date") in dates and str(ev.get("status") or "").lower() not in ("cancelled", "postponed"):
                games.setdefault(ev["event_date"], ev.get("kickoff_local"))
    except Exception as exc:
        _input_problem(c, "event standing shift", exc, name=name)
        return []
    out = []
    for d in sorted(games):
        start, end = parse_minutes(x.get("start") or ""), parse_minutes(x.get("end") or "")
        if start is None or end is None:
            continue
        if end <= start:
            end += 24 * 60
        kick = parse_minutes(str(games[d] or "")[:5]) if games[d] else None
        if kick is not None and kick + EVENT_STAND_AFTER_KICKOFF_MIN > end:
            end = kick + EVENT_STAND_AFTER_KICKOFF_MIN
        out.append({"day": day, "start": _fmt_minutes(start), "end": _fmt_minutes(end % (24 * 60)), "role": role,
                    "from": d, "until": d, "when_event": x["when_event"]})
    return out


def _person_settings(c: "Constraints", e: dict, expired: dict, _ss, held=None, worked=None):
    """One roster person's settings onto the Constraints (build_constraints'
    per-person step, each person on their own — see P-1 there). `held` and
    `worked` are every person's held and worked roles (_roles_on_file)."""
    # Filed under the person's one key (c.key, D-8): `held`, `worked` and
    # `expired` are kept by it too (build_constraints).
    key = c.key(e["name"])
    st = e.get("settings") or {}
    roster_role = " ".join(str(e.get("role") or "").split())
    mine_held = sorted((held or {}).get(key) or {})
    mine_worked = (worked or {}).get(key) or {}
    # Every role they can be scheduled in (D-15): the roster's, the ones
    # they hold (trained, promoted, the POS job list), the ones worked here.
    for r in [roster_role, *mine_held, *mine_worked]:
        if r:
            c.known_roles.setdefault(key, set()).add(r.lower())
            c.role_names.setdefault(r.lower(), r)
    if mine_held:
        c.held_roles[key] = {r.lower() for r in mine_held}
    if st.get("min_hours") is not None or st.get("max_hours") is not None:
        c.hours_limits[key] = (st.get("min_hours"), st.get("max_hours"))
    if st.get("employment_type"):
        c.employment[key] = st["employment_type"]
    # Full-time with no minimum of their own: the restaurant's full-time
    # line is their minimum (D-41). Never for a salaried person — their
    # pay does not follow their hours.
    ft = c.compliance.get("full_time_min_hours")
    if st.get("employment_type") == "full" and st.get("min_hours") is None and ft and key not in c.salaried:
        mx = st.get("max_hours")
        if mx is None or float(mx) >= float(ft):
            c.hours_limits[key] = (float(ft), mx)
            c.full_time_default.add(key)
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
        gone = expired.get(key, set())
        c.certifications[key] = {cert_key(x) for x in st["certifications"] if str(x).strip()
                                 and cert_key(x) not in gone}
        # A closer is a person the owner marked to close (stays until
        # close, the last of their role to leave), not a key holder
        # (owner, 10/2/26) — a manager or keyholder certificate no
        # longer makes anyone a closer; get_leader_flags below does.
    # Who runs the floor (P-7, E-14, E-15): the owner's yes or no, else
    # their role, a role they hold, a manager role worked in the last
    # weeks, or the floor manager certificate — never a food-safety
    # manager card, never a department manager's title on its own.
    since = None
    if c.week_dates:
        from datetime import date as _d
        since = (_d.fromisoformat(min(c.week_dates)) - timedelta(weeks=MANAGER_RECENT_WEEKS)).isoformat()
    recent = [r for r, last in sorted(mine_worked.items(), key=lambda kv: kv[1] or "", reverse=True)
              if not since or (last or "") >= since]
    basis = manager_basis(e["name"], roster_role, st, mine_held, recent, c.certifications.get(key) or ())
    if basis["basis"]:
        c.manager_basis[key] = basis
    if e["active"] and basis["counts"]:
        c.managers[key] = basis["role"] or roster_role or "Manager"
    if st.get("preferred_dayparts") or st.get("desired_hours"):
        c.preferred[e["name"]] = {"preferred_dayparts": list(st.get("preferred_dayparts") or []),
                                  "desired_hours": st.get("desired_hours")}
    if not e["active"]:
        return
    # Standing in as the manager on given dates (E-13).
    for rng in st.get("acting_manager") or []:
        f, u = str(rng.get("from") or ""), str(rng.get("until") or rng.get("from") or "")
        days = {d for d in c.week_dates if f <= d <= u}
        if days:
            c.acting_managers.setdefault(key, set()).update(days)
    # The shifts they always work (D-5), in their own role unless the
    # standing shift names one.
    if st.get("standing_shifts"):
        role_of = lambda x: (x.get("role") or (c.managers.get(key) if key in c.managers else None)  # noqa: E731
                             or roster_role)
        out = []
        for x in st["standing_shifts"]:
            if not isinstance(x, dict):
                continue
            if x.get("when_event"):
                out += _event_standing(c, x, role_of(x), e["name"])
                continue
            out.append(dict({"day": x.get("day"), "start": x.get("start"), "end": x.get("end"), "role": role_of(x)},
                            **{k: x[k] for k in ("from", "until") if x.get(k)}))
        if out:
            c.standing_shifts[key] = out
    # In training (D-16), while it lasts this week.
    t = st.get("trainee") or None
    if isinstance(t, dict) and t.get("target_role"):
        first = min(c.week_dates) if c.week_dates else None
        last = max(c.week_dates) if c.week_dates else None
        if (not first or not t.get("until") or t["until"] >= first) and (not last or not t.get("from") or t["from"] <= last):
            c.trainees[key] = {"target_role": t["target_role"], "trainer": t.get("trainer") or None,
                               "from": t.get("from") or None, "until": t.get("until") or None}


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
# Every account holder's rule constrains the draft — an owner-only one too
# (schedule audit 10/3/26 D-38: "never schedule Ana with Ben", kept private,
# reached neither the prompt nor the checks). An owner-only rule's WORDS
# never surface where the team reads: its floor reads like any floor, its
# check names no text, an unreadable one is kept off the shared review
# (owner_rules_unchecked_private), and a private pairing is scored and
# solved against but never printed (pairs_with_rules' "private").
#
# A rule about two people — "never schedule Ana with Ben", "keep Ana and Ben
# apart", "always pair Ana with Ben" — is a pairing (parse_pair_rule), the
# same "avoid"/"prefer" the owner's staff_pairs hold (owner_pairs).

_RULE_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
# ("day", "am" and "pm" are not here: "every day" is not lunch, and "I am"
# is not a daypart.)
_RULE_DAYPARTS = {"lunch": "morning", "brunch": "morning", "morning": "morning", "mornings": "morning",
                  "breakfast": "morning", "daytime": "morning",
                  "dinner": "night", "night": "night", "nights": "night", "evening": "night", "evenings": "night",
                  "close": "night", "closing": "night"}
_RULE_MAX = 10


_DAY_TOKENS = {
    **{d.lower(): (d,) for d in DAYS}, **{d.lower() + "s": (d,) for d in DAYS},
    "mon": ("Monday",), "tue": ("Tuesday",), "tues": ("Tuesday",), "wed": ("Wednesday",), "weds": ("Wednesday",),
    "thu": ("Thursday",), "thur": ("Thursday",), "thurs": ("Thursday",), "fri": ("Friday",),
    "sat": ("Saturday",), "sun": ("Sunday",),
    "weekend": ("Saturday", "Sunday"), "weekends": ("Saturday", "Sunday"),
    "weekday": tuple(DAYS[:5]), "weekdays": tuple(DAYS[:5]),
}
_RANGE_WORDS = frozenset({"-", "\u2013", "\u2014", "to", "through", "thru", "until", "till"})
_LIST_WORDS = frozenset({",", "and", "or", "&", "/", "on", "the", "for", "nor"})
# Words that take days OUT of a rule: "except Mondays", "every day but
# Sunday", "other than Tuesday" (RULES-8: they were read as the only days).
_EXCEPT_WORDS = (("other", "than"), ("apart", "from"), ("not", "on"), ("except",), ("excluding",), ("but",),
                 ("besides",))


def _rule_days_read(low: str) -> tuple:
    """(days, unclear): the weekdays a rule's words name — a tuple, or None
    for every day — and why they could not be read confidently (None when
    they could). "Mon-Fri" and "Monday through Friday" are ranges; days
    after "except", "but" or "other than" are taken out ("always 2 servers
    except Mondays" is every day but Monday). Each used to be read as the
    days it named: the exception became the only day and a range its two
    ends (schedule re-audit 10/4/26 RULES-8)."""
    import re as _re
    toks = _re.findall(r"[a-z]+|\d+|[-\u2013\u2014,&/]", low)
    incl, excl, i, unclear = [], [], 0, None

    def _day_run(j):
        """(days, next index) for the days from token j: a list, or a range."""
        got = []
        while j < len(toks):
            t = toks[j]
            if t in _DAY_TOKENS:
                if (j + 2 < len(toks) and toks[j + 1] in _RANGE_WORDS and toks[j + 2] in _DAY_TOKENS
                        and len(_DAY_TOKENS[t]) == 1 and len(_DAY_TOKENS[toks[j + 2]]) == 1):
                    a, b = DAYS.index(_DAY_TOKENS[t][0]), DAYS.index(_DAY_TOKENS[toks[j + 2]][0])
                    got += [DAYS[(a + k) % 7] for k in range((b - a) % 7 + 1)]
                    j += 3
                else:
                    got += list(_DAY_TOKENS[t])
                    j += 1
            elif t in _LIST_WORDS and got:
                j += 1
            else:
                break
        return got, j

    while i < len(toks):
        hit = next((w for w in _EXCEPT_WORDS if tuple(toks[i:i + len(w)]) == w), None)
        if hit:
            j = i + len(hit)
            while j < len(toks) and toks[j] in ("on", "the", "for", "not"):
                j += 1
            got, j2 = _day_run(j)
            if got:
                excl += got
                i = j2
                continue
            if (hit != ("but",) and hit != ("not", "on")) or \
                    (hit == ("but",) and j < len(toks) and toks[j] in ("never", "no", "except", "excluding")):
                # "except holidays", "other than the patio": not days, and
                # not to be guessed at.
                unclear = unclear or f"it makes an exception ({' '.join(hit)} …) that isn't days of the week"
            i += len(hit)
            continue
        if toks[i] in _DAY_TOKENS:
            got, i = _day_run(i)
            incl += got
            continue
        i += 1
    if excl:
        base = [d for d in DAYS if d in incl] if incl else list(DAYS)
        out = tuple(d for d in base if d not in set(excl))
        if not out:
            return None, "its days and its exceptions leave no day"
        return out, unclear
    out = tuple(d for d in DAYS if d in incl)
    return (out or None), unclear


def _rule_days(low: str):
    """The weekdays a rule's words name, or None for every day."""
    return _rule_days_read(low)[0]


def _rule_daypart(low: str):
    import re as _re
    for word, part in _RULE_DAYPARTS.items():
        if _re.search(r"\b" + word + r"\b", low):
            return part
    return None


def _rule_dayparts(low: str) -> set:
    """Every half of the day a rule's words name."""
    import re as _re
    return {part for word, part in _RULE_DAYPARTS.items() if _re.search(r"\b" + word + r"\b", low)}


_COUNT_WORDS = r"\d+|one|two|three|four|five|six|seven|eight|nine|ten"


def parse_owner_rule(text: str, roles, families=None) -> dict:
    """{"role", "min", "days", "daypart", "text"} for a staffing rule in one
    of the shapes above, naming one of `roles` (the restaurant's own role
    names, matched case-insensitively, singular or plural), or None — or
    {"role", "unclear": why, "text"} for one in those shapes that could not
    be read confidently (RULES-8), which the owner is asked about. `days`
    None is every trading day; `daypart` None is anywhere in the day — but a
    rule naming an AM or PM job code keeps to that code's half of the day.

    A rule naming a role FAMILY rather than a job code — "always two servers
    Saturday night" where the codes are "Server AM" and "Server PM" — never
    parsed, and fell to the unchecked list (schedule audit 10/3/26 D-14). It
    now matches the family (`families`: the owner's map, else the codes
    with their daypart words taken off), or a family's last word when only
    one family ends in it ("cooks" for "Line Cook"); such a rule carries
    "match": "family" and "family" (apply_owner_rules picks the job code)."""
    import re as _re
    from shift_quality import role_family
    low = " ".join(str(text or "").lower().replace("’", "'").split())
    if not low:
        return None
    names = sorted({" ".join(str(x).split()) for x in roles or () if str(x or "").strip()}, key=len, reverse=True)

    def _says(word):
        return _re.search(r"\b" + _re.escape(word) + r"(s|es)?\b", low)

    role, word, fam = None, None, None
    for r in names:
        if _says(r.lower()):
            role, word = r, r.lower()
            break
    if role is None:
        groups = {}
        for r in names:
            f = role_family(r, families)
            if f:
                groups.setdefault(f, []).append(r)
        heads = {}
        for f in groups:
            if len(f.split()) > 1:
                heads.setdefault(f.split()[-1], set()).add(f)
        for f in sorted(groups, key=len, reverse=True):
            if _says(f):
                fam, word = f, f
                break
        if fam is None:
            for w, fs in sorted(heads.items(), key=lambda kv: -len(kv[0])):
                if len(fs) == 1 and w not in groups and _says(w):
                    fam, word = next(iter(fs)), w
                    break
        if fam is None:
            return None
        role = _family_name(fam, groups[fam])
    rl = _re.escape(word) + r"(?:s|es)?"
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
    # A rule read as one floor must say one thing. Two counts ("2 at
    # dinner, 1 at lunch", "1 bartender, 2 on weekends"), both halves of
    # the day, or an exception that isn't days is not guessed at: the
    # first number and the first daypart became a hard lunch floor of 2
    # with no dinner floor at all (schedule re-audit 10/4/26 RULES-8). It
    # goes back to the owner as a rule Cavnar AI couldn't read.
    days, unclear = _rule_days_read(low)
    spans = []
    for r in names:
        for m_ in _re.finditer(r"\b" + _re.escape(r.lower()) + r"(s|es)?\b", low):
            spans.append((m_.start(), m_.end(), role_family(r, families)))
    spans = [x for x in spans if not any(y[0] <= x[0] and x[1] <= y[1] and (y[1] - y[0]) > (x[1] - x[0])
                                         for y in spans)]
    if len({f for _a, _b, f in spans}) > 1:
        unclear = "it names more than one role — write one rule for each"
    elif len(_re.findall(r"\b(?:" + _COUNT_WORDS + r")\b", low)) > 1:
        unclear = "it gives more than one number — write one rule for each"
    elif len(_rule_dayparts(low)) > 1:
        unclear = "it names both halves of the day — write one rule for lunch and one for dinner"
    if unclear:
        return {"role": role, "unclear": unclear, "text": " ".join(str(text).split())}
    out = {"role": role, "min": min(int(n), _RULE_MAX), "days": days,
           "daypart": _rule_daypart(low) or (role_daypart(role) if fam is None else None),
           "text": " ".join(str(text).split())}
    if fam is not None:
        out.update({"match": "family", "family": fam})
    return out


def _family_name(fam, codes) -> str:
    """A family as the restaurant spells it: a code of it without its
    daypart words ("Server" for "Server AM")."""
    for r in sorted(codes, key=len):
        words = [w for w in r.replace("-", " ").split() if w.strip(".,:;").lower() not in _MORNING_WORDS | _NIGHT_WORDS]
        if words:
            return " ".join(words)
    return str(fam).capitalize()


def rule_reads_as(rule) -> str:
    """How a parsed staffing rule is checked, in the owner's words — said
    back to them so a rule read wrongly is caught (D-14)."""
    who = rule.get("floor_role") or rule.get("role")
    days = rule.get("days")
    when = ("every trading day" if not days else
            "weekends" if tuple(days) == ("Saturday", "Sunday") else
            "weekdays" if tuple(days) == ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday") else
            "/".join(d[:3] for d in days))
    part = {"morning": " at lunch/day", "night": " at dinner/night"}.get(rule.get("daypart") or "", "")
    return f"at least {int(rule.get('min') or 1)} {who} on {when}{part}"


def _is_staffing_rule(fact) -> bool:
    mods = {m for m in str(fact.get("modules") or "").split(",") if m}
    return bool(mods & {"labor", "schedule"})


def apply_owner_rules(c: "Constraints", restaurant_id, db_path=None):
    """Read the owner's staffing rules into `c` (see above). Never raises
    into a generation: a rule that cannot be read costs a check, never the
    week."""
    import owner_memory
    import memory_context
    db = None if db_path == DB_PATH else db_path
    facts = owner_memory.facts_for(restaurant_id, viewer=None, surface="schedule",
                                   kinds=list(owner_memory.RULE_KINDS), db_path=db)
    # Which of them the whole team may read; the rest are owner-only (D-38).
    team = {(f.get("id"), f.get("from_location")) for f in owner_memory.facts_for(
        restaurant_id, viewer=memory_context.team_viewer("schedule"), surface="schedule",
        kinds=list(owner_memory.RULE_KINDS), db_path=db)}
    roles, names = set(), []
    try:
        import staff_settings as _ss
        _everyone = _ss.roster(restaurant_id, db_path=db_path)
        roles |= {str(e.get("role") or "").strip() for e in _everyone if e.get("role")}
        names = [e["name"] for e in _everyone]
    except Exception:
        pass
    roles |= {str(r).strip() for r in (c.role_floors or {})}
    # Every job code in use (held and worked roles too), so a rule names a
    # role family against all of its codes (D-14).
    roles |= {str(r).strip() for r in (c.role_names or {}).values()}
    roles.discard("")
    for f in facts:
        if not owner_memory.is_owner_rule(f):
            continue
        private = (f.get("id"), f.get("from_location")) not in team
        text = " ".join(str(f.get("fact") or "").split())
        pair = parse_pair_rule(text, names, aliases=c.aliases, display=c.display)
        if pair is not None:
            if pair.get("days"):
                # A pairing on some days only: the pairings have no day, so
                # it is named for the owner to check rather than held all
                # week or not at all.
                (c.owner_rules_unchecked_private if private else c.owner_rules_unchecked).append(text)
            else:
                c.owner_pairs.append(dict(pair, private=private))
            continue
        rule = parse_owner_rule(f.get("fact"), roles, c.role_families)
        if rule is None or rule.get("unclear"):
            # A rule that reads as staffing but not confidently is the
            # owner's to restate — never a guessed floor (RULES-8).
            if rule is not None or _is_staffing_rule(f):
                (c.owner_rules_unchecked_private if private else c.owner_rules_unchecked).append(text)
            continue
        rule["private"] = private
        c.owner_rules.append(rule)
        if not rule["daypart"]:
            rule["reads_as"] = rule_reads_as(rule)
            continue
        # A rule about a role family holds on the job code that works that
        # half of the day ("servers Saturday night" is Server PM); a family
        # with no single code for it is counted by family, per day.
        code = rule["role"] if rule.get("match", "role") == "role" else \
            role_for_daypart(rule["role"], rule["daypart"], roles, c.role_families)
        if not code:
            rule["by_family"] = True
            rule["reads_as"] = rule_reads_as(rule)
            continue
        rule["floor_role"] = code
        rule["reads_as"] = rule_reads_as(rule)
        # A daypart rule is a floor: the draft is built to it and checked
        # against it like any floor the owner set in the schedule settings.
        key = next((r for r in c.role_floors if r.strip().lower() == code.lower()), code)
        spec = c.role_floors.setdefault(key, {"morning": 0, "night": 0, "days": {}})
        for day in rule["days"] or DAYS:
            if floor_for(c.role_floors, key, day, rule["daypart"]) < rule["min"]:
                spec.setdefault("days", {}).setdefault(day, {})[rule["daypart"]] = rule["min"]
                if private:
                    # Held like any floor; its words are the owner's alone.
                    c.rule_floor_sources.pop((key.strip().lower(), day, rule["daypart"]), None)
                else:
                    c.rule_floor_sources[(key.strip().lower(), day, rule["daypart"])] = rule["text"]


# ── the RESTAURANT HOURS & SHIFT RULES, read into rules (PROMPT-1) ─────────
#
# The owner's hours notes (restaurants.hours_notes — "Bartenders: evening
# only, no earlier than 3:00pm", "Hosts: minimum 1 whenever the dining room
# is open", "Bussers cut 30 minutes before close") reached the model as free
# text ranked above SHIFT REQUIREMENTS, while that table — built from
# punches — said the owner's floors were already inside its numbers. They
# were not: nothing parsed the notes, the table asked for a Wednesday-lunch
# bartender the owner forbids and no host at Tuesday lunch, and nothing
# checked the draft against either (schedule re-audit 10/4/26 PROMPT-1).
#
# Each line is now read in the shapes owners write, every count through the
# owner-rule parser (parse_owner_rule: its roles and role families, days,
# ranges, exceptions and dayparts — one reader, not two):
#   * a count — "minimum 2", "3 Thu-Sat", "one from 10:00am", "a second
#     from 4:00pm", "Keep 2 servers through close" — is a floor for that
#     role, days and half of the day (both halves when it names neither:
#     "whenever the dining room is open"), raising role_floors: the
#     requirements table carries it, the floor fill builds to it and the
#     sweep holds the draft to it (coverage_floor, naming the line);
#   * "no earlier than 3:00pm", "evening only", "first arrives 2:00pm",
#     "done by 9pm", "cut 30 minutes before close" are the hours the role
#     works (role_windows): the table asks nothing of it outside them, no
#     pass adds a shift outside them (can_add), a row outside them is the
#     hard role_window breach, and the retime pass moves a row into them;
#   * "maximum 5 at once" is a ceiling (role_caps): the table never asks
#     for more, and a minute with more on is flagged (over_role_max);
#   * "stay 30 minutes after close", "through close", "close together" is
#     how long the role's last person stays (close_mins);
#   * "arrive 8:00am" is the role's start on that half of the day
#     (role_times, built to by apply_role_times);
#   * a manager's line is the manager rule, which the code already holds.
# What is left — "others stagger from 3:00pm", the shift lengths, "cut the
# rest about an hour after the dinner rush drops" — is not guessed at: it is
# named (hours_rules_unchecked), the prompt tells the model to follow it as
# written over the table, and the review lists it as not checked.

_HOURS_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "a second": 2, "second": 2,
                  "a third": 3, "third": 3}
_HOURS_COUNT = r"(\d+|one|two|three|four|five|six|a second|a third|second|third)"
# A number that is a time, a length or a duration is not a count.
_NOT_A_COUNT = re.compile(r"\s*(?::\d|h\b|hrs?\b|hours?\b|minutes?\b|mins?\b|am\b|pm\b|%|-\s*\d|\d|\.\d)")
_HOURS_TIME = r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)|noon|midnight)"
_COUNT_LEAD = r"(?:(?:minimum(?: of)?|min\.?|at least|no fewer than|not fewer than|keep|always)\s+)?"
_DAY_WORD = r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\.?"
_LEAD_DAYS = re.compile(r"^((?:" + _DAY_WORD + r"\s*(?:[-–&/,]|\bthrough\b|\bthru\b|\bto\b|\band\b)?\s*)+)")
# A clause none of the shapes reads is named as not checked when it says
# something that looks like a rule.
_HOURS_SIGNAL = re.compile(r"\d|\b(?:cut|stagger\w*|only|never|always|minimum|maximum|at least|at most|no more|"
                           r"no earlier|no later|first|last|close|closes|until|before|after)\b")
_HOURS_CUTOVER = 15 * 60          # the schedule's own 3pm split (daypart_of)


def _hours_minutes(text):
    t = str(text or "").strip().lower().replace(" ", "")
    if t == "noon":
        return 12 * 60
    if t == "midnight":
        return 24 * 60
    return parse_minutes(t)


def _rule_or_none(rule):
    return rule if rule and not rule.get("unclear") else None


def _hours_role(low, roles, families):
    """(rule, start): the first role a clause names, read by the owner-rule
    parser, and where its word starts — or (None, None)."""
    for m in re.finditer(r"\b[a-z]", low):
        k = m.start()
        # The role's words alone: a number or a second clause later in the
        # line is not this probe's to read.
        probe = " ".join(re.findall(r"[a-z]+", low[k:])[:4])
        rule = _rule_or_none(parse_owner_rule("at least 1 " + probe, roles, families))
        if not rule:
            continue
        words = {str(rule["role"]).lower()} | {w for w in str(rule["role"]).lower().split() if len(w) > 3}
        if rule.get("family"):
            words |= {rule["family"], rule["family"].split()[-1]}
        if any(low[k:].startswith(w) for w in words):
            return rule, k
    return None, None


def _days_of(text):
    """The weekdays `text` names, read as an owner rule's days are
    (_rule_days), or None for every day."""
    return _rule_days(" ".join(str(text or "").lower().split()))


def parse_hours_rules(text, roles, families=None) -> dict:
    """`text` (restaurants.hours_notes) read line by line against the
    restaurant's `roles` (see the block comment). Pure. {"floors": [owner
    rule dicts, "text" the line], "windows": [{"role", "family",
    "earliest"|"latest"|"before_close", "days", "text"}], "caps": [{"role",
    "family", "max", "days", "daypart", "text"}], "stays": [{"role",
    "family", "minutes", "text"}], "starts": [{"role", "family", "start",
    "daypart", "days", "text"}], "managers": [{"role", "text"}],
    "weekly_caps": [{"max", "text"}], "unchecked": [str]} — a line's text
    is "Role: clause" where the line has a head, else the line."""
    out = {"floors": [], "windows": [], "caps": [], "stays": [], "starts": [], "managers": [],
           "weekly_caps": [], "unchecked": []}

    def unchecked(words):
        words = " ".join(str(words).split()).strip(" -•*")
        if words and words not in out["unchecked"]:
            out["unchecked"].append(words)

    for raw in str(text or "").splitlines():
        line = " ".join(raw.split()).lstrip("-•* ").strip()
        if not line or re.match(r"^[^:]+:$", line):
            continue                                    # empty, or a heading ("STAFF ARRIVAL TIMES:")
        low_line = line.lower().replace("’", "'")
        hm = re.match(r"^([^:]*?[^\d\s:]):\s+(.+)$", line)
        head, body = (hm.group(1).strip(), hm.group(2)) if hm else ("", line)
        head_rule = _hours_role(head.lower(), roles, families)[0] if head else None
        head_days = None
        if head and not head_rule and not re.sub(_DAY_WORD + r"|[-–,&/\s]|\bthrough\b|\bthru\b|\bto\b|\band\b", "",
                                                 head.lower()):
            head_days = head.lower()                    # "Thu-Sat: 2 bartenders close together"
        if head and not head_rule and not head_days:
            head, body = "", line                       # "Nobody past 40 hours in a week: …" — one sentence
        if not head_rule and not _hours_role(low_line, roles, families)[0]:
            # About nobody's role: the weekly hours rule, the restaurant's own
            # hours (open_times / close_times hold those), or unread.
            m = re.search(r"\b(?:nobody|no one|no-one)\s+(?:works\s+)?(?:past|over|more than|above)\s+(\d+)\s*"
                          r"(?:hours?|h|hrs?)\b", low_line)
            if m:
                out["weekly_caps"].append({"max": int(m.group(1)), "text": line})
            elif not (re.search(r"\b(?:open|opens|close|closes)\b", low_line) and re.search(_HOURS_TIME, low_line)) \
                    and _HOURS_SIGNAL.search(low_line):
                unchecked(line)
            continue
        prev_rule, prev_part = head_rule, None
        for clause in [x.strip() for x in re.split(r";|,(?!\d)|\.\s+|\.$", body) if x and x.strip()]:
            low = clause.lower().replace("’", "'")
            # How a line part is named: "Role: clause" under a head; a
            # clause of a line with none is read as itself, and named with
            # its whole line when part of it is not read.
            label = f"{head}: {clause}" if head else clause
            whole = f"{head}: {clause}" if head else line
            lead = _LEAD_DAYS.match(low)
            lead_days = lead.group(1) if lead and _days_of(lead.group(1)) else ""
            rest = low[len(lead_days):].strip() if lead_days else low
            # A clause's own days ("Sun-Wed one is enough") replace the head's.
            days_words = lead_days or head_days or ""
            own, own_k = _hours_role(rest, roles, families)
            rule = own or prev_rule
            if rule is None:
                if _HOURS_SIGNAL.search(low):
                    unchecked(whole)
                continue
            if own is not None:
                prev_rule = own if not head_rule else prev_rule
            role, fam = rule["role"], rule.get("family")
            if manager_role_kind(role) in ("owner", "manager", "lead"):
                # Every manager line is the manager rule: a manager on the
                # floor every minute anyone is, the night one through close.
                out["managers"].append({"role": role, "text": label})
                continue
            read = False
            # 1. a count: a floor (said with "close", the last one stays to close)
            if own is not None:
                cm = re.search(r"(?:^|\s)" + _COUNT_LEAD + _HOURS_COUNT + r"\s+$", rest[:own_k])
                after = rest[own_k:]
            else:
                cm = re.match(r"^" + _COUNT_LEAD + _HOURS_COUNT + r"\b", rest)
                if cm is not None and _NOT_A_COUNT.match(rest[cm.end(1):]):
                    cm = None
                after = rest[cm.end():] if cm else ""
            if cm is not None and not re.search(r"\b(?:max(?:imum)?|at most|no more than)\b", rest[:cm.end()]):
                tok = cm.group(1)
                n = int(tok) if tok.isdigit() else _HOURS_NUMBERS.get(tok)
                when = re.findall(r"\b(?:from|after|starting|at)\s+" + _HOURS_TIME, after)
                words = re.sub(_HOURS_TIME, " ", re.sub(r"\b(?:from|after|starting|at)\s+" + _HOURS_TIME, " ", after))
                part = ""
                if when:
                    part = " at dinner" if (_hours_minutes(when[0]) or 0) >= _HOURS_CUTOVER else " at lunch"
                elif re.search(r"\bat open\b|\bwhen (?:we|the doors) open\b", after):
                    part = " at lunch"
                canon = (f"at least {n} " + (words if own is not None else f"{role.lower()} {words}")
                         + part + " " + days_words)
                if not part and not _rule_daypart(canon) and prev_part and own is None and not re.sub(
                        r"[^a-z]", "", re.sub(_DAY_WORD + r"|\band\b|\bthrough\b|\bthru\b|\bto\b|\bis\b|\benough\b|"
                                          r"\bon\b|\btoo\b", "", words)):
                    # "3 Thu-Sat" after "minimum 2 for dinner": the same half.
                    canon += " at dinner" if prev_part == "night" else " at lunch"
                fl = _rule_or_none(parse_owner_rule(canon, roles, families)) if n else None
                if fl is not None and fl.get("min") == min(int(n), _RULE_MAX):
                    out["floors"].append(dict(fl, text=label))
                    prev_part = fl.get("daypart")
                    read = True
                    if re.search(r"\b(?:through|until|till|to) close\b|\bclose together\b|\bcloses\b", after):
                        out["stays"].append({"role": role, "family": fam, "minutes": 0, "text": label})
            # 2. the hours the role works
            days = _days_of(days_words + " " + low)
            for pat, edge in ((r"\b(?:no earlier than|not before|never before|earliest(?: start)?(?: is| at)?|"
                               r"first (?:one )?(?:arrives?|in|on|starts?)(?: at)?)\s+" + _HOURS_TIME, "earliest"),
                              (r"\b(?:no later than|not after|done by|out by|off by|gone by|finish(?:es|ed)? by|"
                               r"leaves? by)\s+" + _HOURS_TIME, "latest")):
                for m in re.finditer(pat, low):
                    mins = _hours_minutes(m.group(1))
                    if mins is None:
                        continue
                    if edge == "latest" and mins < 5 * 60:
                        mins += 24 * 60                 # a 1am finish is that night's
                    out["windows"].append({"role": role, "family": fam, edge: mins, "days": days, "text": label})
                    read = True
            if re.search(r"\b(?:evening|evenings|night|nights|dinner)\s+only\b|\bonly\s+(?:at\s+|for\s+)?"
                         r"(?:night|nights|dinner|evenings?)\b", low):
                out["windows"].append({"role": role, "family": fam, "earliest": _HOURS_CUTOVER, "days": days,
                                       "text": label})
                read = True
            if re.search(r"\b(?:lunch|days|morning|mornings|daytime)\s+only\b|\bonly\s+(?:at\s+|for\s+)?"
                         r"(?:lunch|days|mornings?)\b", low):
                out["windows"].append({"role": role, "family": fam, "latest": _HOURS_CUTOVER, "days": days,
                                       "text": label})
                read = True
            m = re.search(r"\b(?:cut|out|leaves?|gone|off)\s+(\d+)\s*(?:minutes|mins?|min)\s+before\s+close\b", low)
            if m:
                out["windows"].append({"role": role, "family": fam, "before_close": int(m.group(1)), "days": days,
                                       "text": label})
                read = True
            # 3. a ceiling
            m = re.search(r"\b(?:maximum(?: of)?|max\.?|no more than|at most|never more than)\s+" + _HOURS_COUNT
                          + r"\b", low)
            if m and not _NOT_A_COUNT.match(low[m.end(1):]):
                n = int(m.group(1)) if m.group(1).isdigit() else _HOURS_NUMBERS.get(m.group(1))
                if n:
                    out["caps"].append({"role": role, "family": fam, "max": n, "days": days,
                                        "daypart": _rule_daypart(low), "text": label})
                    read = True
            # 4. how long the last of the role stays after close
            m = re.search(r"\bstays?\s+(\d+)\s*(minutes|mins?|min|hours?|hrs?)\s+(?:after|past)\s+close\b", low)
            if m:
                mins = int(m.group(1)) * (60 if m.group(2).startswith("h") else 1)
                out["stays"].append({"role": role, "family": fam, "minutes": mins, "text": label})
                read = True
            # 5. the role's start on that half of the day: "arrive 8:00am"
            m = re.search(r"\b(?:arrives?|starts?|comes? in)\s+(?:at\s+)?" + _HOURS_TIME, low)
            if m and not re.search(r"\bfirst\b", low[:m.start()]):
                mins = _hours_minutes(m.group(1))
                if mins is not None:
                    out["starts"].append({"role": role, "family": fam, "start": mins,
                                          "daypart": "night" if mins >= _HOURS_CUTOVER else "morning",
                                          "days": days, "text": label})
                    read = True
            if not read:
                unchecked(whole)
    return out


def _window_allows(w, part) -> bool:
    from schedule_requirements import window_allows
    return window_allows(w, part)


def _close_minutes_on(c, day):
    m = parse_minutes((c.close_times or {}).get(day, "") or "")
    if m is None:
        return None
    return m + 24 * 60 if m < 5 * 60 else m


def apply_hours_rules(c: "Constraints", restaurant, roles=None) -> dict:
    """Read the restaurant's RESTAURANT HOURS & SHIFT RULES into `c` (see
    the block comment above parse_hours_rules): role_windows first, then
    the floors where the role works, the ceilings, the stays after close,
    the starts (never over a start the owner confirmed in the Studio), and
    c.hours_rules / c.hours_rules_unchecked. Floors are only ever raised.
    Returns the parse."""
    text = getattr(restaurant, "hours_notes", None) if restaurant is not None else None
    if not text or not str(text).strip():
        return {}
    roles = set(roles or ()) | {str(r).strip() for r in (c.role_names or {}).values()} \
        | {str(r).strip() for r in (c.role_floors or {})}
    roles.discard("")
    parsed = parse_hours_rules(text, roles, c.role_families)
    reads, unread = [], list(parsed["unchecked"])

    def label(rule):
        return _family_label(c, c.family(rule.get("family") or rule["role"]))

    def days_word(days):
        return "" if not days else " on " + "/".join(d[:3] for d in days)

    for w in parsed["windows"]:
        fam = c.family(w.get("family") or w["role"])
        bits = []
        for day in (w.get("days") or DAYS):
            spec = c.role_windows.setdefault(fam, {}).setdefault(day, {"earliest": None, "latest": None,
                                                                       "source": w["text"]})
            if w.get("earliest") is not None:
                spec["earliest"] = max(spec["earliest"] or 0, int(w["earliest"]))
            latest = w.get("latest")
            if w.get("before_close") is not None:
                close = _close_minutes_on(c, day)
                if close is None:
                    continue
                latest = close - int(w["before_close"])
            if latest is not None:
                spec["latest"] = int(latest) if spec["latest"] is None else min(spec["latest"], int(latest))
            bits.append(day)
        if not bits:
            unread.append(w["text"])
            continue
        what = (f"starts no earlier than {_fmt_minutes(w['earliest'])}" if w.get("earliest") is not None else
                f"ends {int(w['before_close'])} min before close at the latest" if w.get("before_close") is not None
                else f"ends by {_fmt_minutes(int(w['latest']) % (24 * 60))}")
        reads.append({"text": w["text"], "kind": "role_window",
                      "reads_as": f"every {label(w)} shift {what}{days_word(w.get('days'))}"})
    for fl in parsed["floors"]:
        fam = c.family(fl.get("family") or fl["role"])
        key = next((r for r in c.role_floors if r.strip().lower() == fl["role"].strip().lower()), fl["role"])
        parts = [fl["daypart"]] if fl.get("daypart") else ["morning", "night"]
        held = set()
        for day in (fl.get("days") or DAYS):
            for part in parts:
                if not _trades_part(c, day, part):
                    continue
                if not _window_allows((c.role_windows.get(fam) or {}).get(day), part):
                    continue                            # an evening-only role has no lunch floor
                spec = c.role_floors.setdefault(key, {"morning": 0, "night": 0, "days": {}})
                if floor_for(c.role_floors, key, day, part) < fl["min"]:
                    spec.setdefault("days", {}).setdefault(day, {})[part] = fl["min"]
                    c.rule_floor_sources[(key.strip().lower(), day, part)] = fl["text"]
                held.add(part)
        if held:
            # Said as held: "whenever the bar is open" for an evening-only
            # bar is a dinner floor.
            r = dict(fl, daypart=next(iter(held)) if len(held) == 1 else None)
            reads.append({"text": fl["text"], "kind": "coverage_floor", "floor": True,
                          "reads_as": rule_reads_as(dict(r, role=label(fl)))
                          + (" at lunch/day and dinner/night" if len(held) > 1 else "")})
    for cap in parsed["caps"]:
        fam = c.family(cap.get("family") or cap["role"])
        c.role_caps.setdefault(fam, []).append({"max": int(cap["max"]), "days": cap.get("days"),
                                                "daypart": cap.get("daypart"), "source": cap["text"]})
        reads.append({"text": cap["text"], "kind": "over_role_max",
                      "reads_as": f"at most {int(cap['max'])} {label(cap)} on at once{days_word(cap.get('days'))}"})
    for st in parsed["stays"]:
        fam = c.family(st.get("family") or st["role"])
        c.close_mins[fam] = max(int(c.close_mins.get(fam, 0) or 0), int(st["minutes"]))
    for st in parsed["stays"]:
        fam = c.family(st.get("family") or st["role"])
        mins = int(c.close_mins.get(fam, 0) or 0)          # the longest stay the role is held to
        reads.append({"text": st["text"], "kind": "ends_before_role_close",
                      "reads_as": f"the last {label(st)} on until "
                                  + (f"{mins} min after close" if mins else "close")})
    for sr_ in parsed["starts"]:
        fam = c.family(sr_.get("family") or sr_["role"])
        codes = sorted({r for r in _all_roles(c) if c.family(r) == fam
                        and role_daypart(r) in (None, sr_["daypart"])}) or [sr_["role"].strip().lower()]
        for day in (sr_.get("days") or DAYS):
            if not _trades_part(c, day, sr_["daypart"]):
                continue
            for code in codes:
                spec = c.role_times.setdefault((code, day, sr_["daypart"]), {})
                if spec.get("start") is None:
                    spec["start"] = int(sr_["start"])
                    spec.setdefault("source", {})["start"] = sr_["text"]
        reads.append({"text": sr_["text"], "kind": "role_time",
                      "reads_as": f"every {label(sr_)} shift at "
                                  f"{'lunch/day' if sr_['daypart'] == 'morning' else 'dinner/night'} starts at "
                                  f"{_fmt_minutes(sr_['start'])}{days_word(sr_.get('days'))}"})
    for m in parsed["managers"]:
        reads.append({"text": m["text"], "kind": "no_manager",
                      "reads_as": "a manager on the floor every minute anyone is (the manager rule)"})
    for wk in parsed["weekly_caps"]:
        over = [n for n in (c.roster_names or []) if (c.max_hours(n) or 0) > wk["max"] + 0.05]
        if over:
            # The code lets some of them past it (a salaried cap, an owner's
            # own maximum): not a rule it holds as written.
            unread.append(wk["text"])
            continue
        reads.append({"text": wk["text"], "kind": "over_max_hours",
                      "reads_as": f"nobody past {wk['max']}h in a payroll week (each person's weekly maximum)"})
    seen, c.hours_rules = set(), []
    for r in reads:
        if (r["text"], r["reads_as"]) not in seen:
            seen.add((r["text"], r["reads_as"]))
            c.hours_rules.append(r)
    c.hours_rules_unchecked = list(dict.fromkeys(unread))
    return parsed


def _trades_part(c, day, part) -> bool:
    """Whether the restaurant serves `part` on `day` by its own hours
    (schedule_note_rules._trades — the one reading of it)."""
    try:
        import schedule_note_rules
        return schedule_note_rules._trades(c, day, part)
    except Exception:
        return True


def parse_pair_rule(text: str, names, aliases=None, display=None) -> dict:
    """{"kind": "avoid"|"prefer", "a", "b", "days", "text"} for a rule about
    two people on the roster ("never schedule Ana with Ben", "keep Ana and
    Ben apart", "Ana and Ben shouldn't be on together", "always pair Ana
    with Ben"), or None. Each person is named by a roster spelling, a
    spelling people's identity knows for them (`aliases`/`display`, from
    Constraints), or a first name only one person on the roster has. Two
    people named and a word that puts them on together (with, together,
    alongside, apart, separate, away from, same shift) are needed — "Ana and
    Ben never work Sundays" is about Sundays, not a pairing."""
    import re as _re
    orig = " " + " ".join(str(text or "").replace("’", "'").split()) + " "
    low = orig.lower()
    if not low.strip():
        return None
    if len(low) != len(orig):
        orig = low                                   # a character whose case changes its length
    spell = {}                                       # spelling -> roster display name
    firsts = {}
    for n in names or []:
        n = " ".join(str(n or "").split())
        if not n:
            continue
        spell[n.lower()] = n
        first = n.split()[0].lower().strip(".,")
        if len(first) >= 2:
            firsts.setdefault(first, set()).add(n)
    for k, rk in (aliases or {}).items():
        who = (display or {}).get(rk)
        if who and len(k) >= 3:
            spell.setdefault(k, who)
    first_only = set()
    for f, ns in firsts.items():
        if len(ns) == 1 and f not in spell:
            spell[f] = next(iter(ns))
            first_only.add(f)
    found = []                                       # (start, end, display)
    for sp in sorted(spell, key=len, reverse=True):
        for m in _re.finditer(r"(?<![a-z])" + _re.escape(sp) + r"(?![a-z])", low):
            if any(not (m.end() <= s0 or m.start() >= e0) for s0, e0, _n in found):
                continue
            if sp in first_only and not orig[m.start()].isupper():
                continue                             # "will", "may": a word, not Will or May
            found.append((m.start(), m.end(), spell[sp]))
    people_ = []
    for _s0, _e0, who in sorted(found):
        if who not in people_:
            people_.append(who)
    if len(people_) != 2:
        return None
    marked = low
    for s0, e0, who in sorted(found, reverse=True):
        marked = marked[:s0] + (" persona " if who == people_[0] else " personb ") + marked[e0:]
    together = _re.search(r"\b(with|together|alongside|apart|separate|separated|away from|same shift|on at once|"
                          r"split up)\b", marked)
    if not together:
        return None
    neg = _re.search(r"\b(never|don'?t|do not|not|no|shouldn'?t|should not|can'?t|cannot|won'?t|apart|separate|"
                     r"separated|away from|split up|avoid)\b", marked)
    pos = _re.search(r"\b(always|pair|together|best with|works? well|keep)\b", marked)
    if neg:
        kind = "avoid"
    elif pos:
        kind = "prefer"
    else:
        return None
    return {"kind": kind, "a": people_[0], "b": people_[1], "days": _rule_days(marked),
            "text": " ".join(str(text).split())}


def pairs_with_rules(pairs: dict, c: "Constraints") -> dict:
    """The owner's pairings (staff_settings.pair_sets: {"prefer", "avoid"}
    of frozensets of lowercase names) with the pairings their rules state
    (c.owner_pairs, D-38) added, and "private": the ones stated in an
    owner-only rule — scored and solved against, never printed where the
    team reads (the prompt's PAIRINGS block, the scorer's sentences)."""
    def _one(pair):
        # Each member under the roster's spelling of the person it means
        # (D-8): a pairing kept under "Mike" holds for the roster's "Michael".
        if c is None or not getattr(c, "display", None):
            return pair
        return frozenset(c.key(x) for x in pair)
    out = {k: {q for q in (_one(p) for p in ((pairs or {}).get(k) or ())) if len(q) == 2}
           for k in ("prefer", "avoid", "private")}
    for p in (getattr(c, "owner_pairs", None) or []) if c is not None else []:
        pair = frozenset((p["a"].lower(), p["b"].lower()))
        out.setdefault(p["kind"], set()).add(pair)
        if p.get("private"):
            out["private"].add(pair)
    if not out["private"]:
        out.pop("private")
    return out


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


# How far either side of the week the tail reaches: a close the night before
# or a Monday open after (rest), a run of days that started last week (days
# in a row), and every payroll week the generated week touches (a payroll
# week is 7 days, so it never reaches further than this).
TAIL_DAYS = 7

_LIVE_PUBLISHED = ("published_at IS NOT NULL AND superseded_by IS NULL AND NOT EXISTS (SELECT 1 FROM "
                   "schedule_history nw WHERE nw.restaurant_id=schedule_history.restaurant_id AND "
                   "nw.week_start=schedule_history.week_start AND nw.published_at IS NOT NULL AND "
                   "nw.id > schedule_history.id)")


def _published_tail(c: Constraints, restaurant_id, db_path):
    """The shifts around this week its rules must see — per person in
    c.base_rows (rest, overlap, days in a row) and per payroll week in
    c.base_hours (hours toward the ceiling) — over TAIL_DAYS either side:

      1. this site's live published weeks that overlap that window, every
         one of them (schedule audit 10/3/26 E-11: "the two newest by id"
         returned next week and this week once both were published, and
         last week — the one the rest rule, the run and Simple EJ's Wed-Sun
         payroll hours need — dropped out);
      2. for dates no published week covers, the draft in force of that
         week, each row labelled a draft (E-10: generating two weeks ahead
         double-booked hours a draft already gave);
      3. for past dates neither covers, the punches (shift_facts — D-22:
         with no published week, a Sunday close then a Monday open and a
         run begun last week were invisible), labelled punches;
      4. the organisation's other sites (E-26: org_sibling_ids — the
         organisation, not a matching owner email, so locations onboarded
         under their GMs' emails see each other), their live
         published weeks, each row matched to this site's person through
         people's identity at both sites and kept as a row of the tail: it
         is checked by overlap and rest like any other (D-40 — a lunch at
         one site no longer bars dinner at the other), and its hours count
         toward the payroll week.

    Every row carries this site's spelling of the person (c.key/display),
    and `_source` / `_site` / `_week` say where it came from."""
    if not c.week_dates:
        return
    from schedule_versions import rows_from_csv
    buckets = {c.bucket(d) for d in c.week_dates}
    first = datetime.strptime(min(c.week_dates), "%Y-%m-%d")
    last = datetime.strptime(max(c.week_dates), "%Y-%m-%d")
    lo = (first - timedelta(days=TAIL_DAYS)).strftime("%Y-%m-%d")
    hi = (last + timedelta(days=TAIL_DAYS)).strftime("%Y-%m-%d")
    week_set = set(c.week_dates)
    conn = get_conn(db_path)
    try:
        published = conn.execute(
            "SELECT id, week_start, week_end, schedule_csv FROM schedule_history WHERE restaurant_id=? AND "
            + _LIVE_PUBLISHED + " AND substr(week_end,1,10) >= ? AND substr(week_start,1,10) <= ? ORDER BY id DESC",
            (restaurant_id, lo, hi)).fetchall()
        drafts = conn.execute(
            "SELECT id, week_start, week_end, schedule_csv FROM schedule_history WHERE restaurant_id=? AND "
            "published_at IS NULL AND superseded_by IS NULL AND substr(week_end,1,10) >= ? "
            "AND substr(week_start,1,10) <= ? ORDER BY id DESC", (restaurant_id, lo, hi)).fetchall()
    finally:
        conn.close()

    def _dates(w):
        try:
            a = datetime.strptime(str(w["week_start"] or "")[:10], "%Y-%m-%d")
            b = datetime.strptime(str(w["week_end"] or w["week_start"] or "")[:10], "%Y-%m-%d")
        except ValueError:
            return set()
        return {(a + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((b - a).days + 1)}
    # The week being built is never its own tail: its stored copies (the
    # live published week, its draft) are what the caller's rows replace —
    # a rescore of an edit that removed Sunday would read the stored Sunday
    # back as "last week".
    published = [w for w in published if not (_dates(w) & week_set)]
    drafts = [w for w in drafts if not (_dates(w) & week_set)]
    covered = set()
    for w in published:
        covered |= _dates(w)
    seen = set()

    def _take(r, source, site=None, week=None, key=None):
        k = key or c.key(r.get("employee"))
        d = r.get("date") or ""
        if not k or not d or d in week_set:
            return
        sig = (site or "", k, d, r.get("shift_start"))
        if sig in seen:
            return
        seen.add(sig)
        row = dict(r, employee=c.display.get(k, r.get("employee")))
        if source != "published":
            row["_source"] = source
        if site:
            row["_site"] = site
        if week:
            row["_week"] = week
        if lo <= d <= hi:
            c.base_rows.setdefault(k, []).append(row)
        b = c.bucket(d)
        if b in buckets:
            c.base_hours.setdefault(k, {})[b] = c.base_hours.get(k, {}).get(b, 0.0) + row_hours(row)

    for w in published:
        for r in rows_from_csv(w["schedule_csv"]):
            _take(r, "published")
    try:
        from time_utils import restaurant_now_by_id
        today = restaurant_now_by_id(restaurant_id, naive=True).strftime("%Y-%m-%d")
    except Exception:
        today = datetime.now().strftime("%Y-%m-%d")
    # The draft in force of a week nothing published covers — its newest
    # unsent, unsuperseded generation — never this week's own draft, and
    # never a draft whose week has already begun unpublished: the restaurant
    # is working some other schedule, so its rows are nobody's week. Simple
    # EJ's draft of 10/5/26 (published by mistake, taken back) had Jose work
    # all seven days, and the next week's Monday became "8 days in a row"
    # (10/6/26). Its past days read the time clock instead (below).
    drafted = set()
    for w in drafts:
        ws = str(w["week_start"] or "")[:10]
        span = _dates(w)
        if ws and ws <= today:
            continue
        if ws in drafted or not (span - covered):
            continue
        drafted.add(ws)
        for r in rows_from_csv(w["schedule_csv"]):
            if (r.get("date") or "") not in covered:
                _take(r, "draft", week=ws)
        covered |= span
    # The time clock for past dates nothing else covers (D-22).
    gap_days = sorted(d for d in ((first - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(1, TAIL_DAYS + 1))
                      if d not in covered and d < today)
    if gap_days:
        try:
            import shift_facts
            for r in shift_facts.person_rows(restaurant_id, since=gap_days[0], until=gap_days[-1],
                                             db_path=None if db_path == DB_PATH else db_path):
                if (r.get("date") or "")[:10] in gap_days:
                    hours = r.get("actual_hours") if r.get("actual_hours") not in (None, "") else r.get("scheduled_hours")
                    _take(dict(r, date=str(r["date"])[:10], scheduled_hours=hours if hours is not None else ""),
                          "punches")
        except Exception as exc:
            _input_problem(c, "last week's punches", exc)
    # The organisation's other sites (E-26, D-40).
    for sib_id, label, sib_rows in _sibling_weeks(restaurant_id, lo, hi, db_path):
        names = sorted({(r.get("employee") or "").strip() for r in sib_rows} - {""})
        try:
            import people
            spelled = people.spellings(sib_id, names, db_path=None if db_path == DB_PATH else db_path)
        except Exception:
            spelled = {}
        for r in sib_rows:
            n = (r.get("employee") or "").strip()
            mine = {c.key(sp) for sp in (spelled.get(n) or {_fold_name(n)})} & set(c.display)
            if len(mine) > 1:
                continue                  # two people here answer to that spelling: never guessed
            k = next(iter(mine)) if mine else c.key(n)
            if (r.get("date") or "") in week_set:
                # On a date being built, it is still a row of the tail: kept
                # for overlap and rest, never a whole-date block (D-40).
                row = dict(r, employee=c.display.get(k, n), _site=label)
                sig = (label, k, row["date"], row.get("shift_start"))
                if sig in seen:
                    continue
                seen.add(sig)
                c.base_rows.setdefault(k, []).append(row)
                b = c.bucket(row["date"])
                if b in buckets:
                    c.base_hours.setdefault(k, {})[b] = c.base_hours.get(k, {}).get(b, 0.0) + row_hours(row)
            else:
                _take(r, "published", site=label, key=k)


def _fold_name(name) -> str:
    return " ".join(str(name or "").split()).lower()


def org_sibling_ids(restaurant_id, db_path=DB_PATH) -> list:
    """The other locations of this one's organisation (schedule audit
    10/3/26 E-26): every location sharing its organization_id — the
    organisation, one account's, is the owner check, so locations onboarded
    under their GMs' emails are siblings once linked to it — else, for a
    location linked to none, the same group under the same owner email (the
    old rule; a group name alone is never enough: two unrelated "Syrup"s).
    Read from the column itself: the Restaurant object carries no
    organization_id."""
    conn = get_conn(db_path)
    try:
        me = conn.execute("SELECT organization_id, location_group, owner_email FROM restaurants WHERE id=?",
                          (restaurant_id,)).fetchone()
        if not me:
            return []
        if me["organization_id"]:
            rows = conn.execute("SELECT id FROM restaurants WHERE organization_id=? AND id<>?",
                                (me["organization_id"], restaurant_id)).fetchall()
        elif (me["location_group"] or "").strip():
            rows = conn.execute("SELECT id FROM restaurants WHERE TRIM(location_group)=? "
                                "AND LOWER(TRIM(COALESCE(owner_email,'')))=? AND id<>?",
                                (me["location_group"].strip(), (me["owner_email"] or "").strip().lower(),
                                 restaurant_id)).fetchall()
        else:
            rows = []
    finally:
        conn.close()
    return sorted(r["id"] for r in rows)


def _sibling_weeks(restaurant_id, lo, hi, db_path):
    """[(site id, label, rows)] — the live published rows in [lo, hi] at
    every other location of this one's organisation (org_sibling_ids —
    E-26), one entry per site."""
    try:
        ids = org_sibling_ids(restaurant_id, db_path)
    except Exception:
        ids = []
    if not ids:
        return []
    from schedule_versions import rows_from_csv
    out = []
    conn = get_conn(db_path)
    try:
        for sid in ids:
            meta = conn.execute("SELECT COALESCE(location_name, name) AS label FROM restaurants WHERE id=?",
                                (sid,)).fetchone()
            if not meta:
                continue
            rows = []
            for w in conn.execute("SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND "
                                  + _LIVE_PUBLISHED + " AND substr(week_end,1,10) >= ? AND substr(week_start,1,10) <= ? "
                                  "ORDER BY id DESC", (sid, lo, hi)).fetchall():
                rows += [r for r in rows_from_csv(w["schedule_csv"]) if lo <= (r.get("date") or "") <= hi]
            if rows:
                out.append((sid, meta["label"], rows))
    finally:
        conn.close()
    return out


# ── the violation sweep ────────────────────────────────────────────────────

def violations(rows: list, c: Constraints, person_only: bool = False, day_only: bool = False,
               context=()) -> list:
    """Every rule the finished week breaks, one entry per breach, each
    naming the person, the date, the rule and whether it is hard (the row
    cannot stand) or soft (worth a look). `person_only` keeps to the rules
    about one person's own rows (can_add sweeps a single person's week with
    it: the day-level rules — floors, a closer, a manager — need everyone);
    `day_only` to the rules about who is on a day (IncrementalSweep sweeps
    one date's rows with it; `context` is then the neighbouring dates'
    rows a manager's minutes may reach across midnight from, RULES-10)."""
    out = []
    by_person = {}
    seen_slots = set()
    from shift_quality import touched_dayparts as _touched
    for i, r in enumerate([] if day_only else (rows or [])):
        name = (r.get("employee") or "").strip()
        # One person, one key: every spelling that is them (people's
        # identity), and a roster person an open "same person?" question
        # joins to them, share one week here — overlaps, rest, hours, days
        # in a row (schedule audit 10/3/26 D-8, E-25).
        key = c.key(name)
        if not key:
            continue
        by_person.setdefault(c.sweep_key(name), []).append((i, r))
        # Availability is judged on every daypart the shift reaches into,
        # not its start (a "mornings only" person on 2:30-11:30pm was never
        # flagged), by any minute of it, not an hour (RULES-11).
        ok, why = True, ""
        for part in _touched(r):
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
        slot = (c.sweep_key(name), r.get("date"), r.get("shift_start"))
        if slot in seen_slots:
            out.append(_v("double_booked", i, r, LABELS["double_booked"]))
        seen_slots.add(slot)
        hrs = row_hours(r)
        mx = c.compliance.get("max_shift_hours")
        # The shift-length maximum is for the team, never an owner or a
        # manager: how long they stay is their own call (Simple EJ's,
        # 10/6/26 - Erik and Jim choose their 13-hour busy days, and every
        # restaurant's are different). max_shift_applies is the one answer.
        if mx and hrs > float(mx) + 0.01 and max_shift_applies(c, key):
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
            # Part of a day off is time off, or a held note — not a window.
            kind = ("approved_time_off" if str(why).startswith(LABELS["approved_time_off"]) else
                    "note_unavailable" if str(why).startswith("your note:") else "outside_window")
            out.append(_v(kind, i, r, why))
        ok, why = c.cert_ok(name, r.get("role", ""), r.get("date"))
        if not ok:
            out.append(_v("missing_cert", i, r, why))
        # The hours the owner's RESTAURANT HOURS & SHIFT RULES give the role
        # ("Bartenders: evening only, no earlier than 3:00pm" — PROMPT-1).
        if c.role_windows:
            ok, why = c.role_window_ok(r)
            if not ok:
                w = c.role_window_for(r.get("role"), r.get("date"))
                s_, e_ = start_minutes(r), end_minutes(r)
                off = max(0, (w.get("earliest") or 0) - (s_ or 0)) if w.get("earliest") is not None else 0
                off += max(0, (e_ or 0) - w["latest"]) if w.get("latest") is not None else 0
                out.append(_v("role_window", i, r, why, severity=round(off / 60.0, 2)))
        # Past close by more than the role may stay: the one cap the job's
        # parser holds every model row to (_enforce_close_time), judged here
        # so a pass that runs a close past it is seen (re-audit 10/4/26
        # PIPE-10). Soft: an owner may keep somebody on by hand.
        cap = close_cap_minutes(c, r)
        end_m = end_minutes(r) if cap is not None else None
        if cap is not None and end_m is not None and end_m > cap:
            out.append(_v("past_close", i, r,
                          f"ends {r.get('shift_end')}, past {_fmt_minutes(cap % (24 * 60))} — the most after close "
                          f"you allow {r.get('role') or 'the role'}",
                          severity=round((end_m - cap) / 60.0, 2), floor_role=c.family(r.get("role"))))
        # A role written for somebody who holds no role of its family — the
        # model's guess treated as theirs (schedule audit 10/3/26 D-15).
        if not c.holds(name, r.get("role", ""), r.get("date")):
            theirs = sorted({c.role_names.get(x, x) for x in (c.known_roles.get(key) or ())}, key=str.lower)
            out.append(_v("role_not_held", i, r,
                          f"{name} doesn't hold {r.get('role')} (works {', '.join(theirs[:4])}) — record it in Team "
                          "if they're trained for it"))

    # a role that stays until N minutes after close: the last of that role
    # each night must end no earlier ("bartenders stay an hour after close"),
    # whichever of the role's job codes they work (D-13, D-43). A role with
    # closers is held to it by the closer rule instead (_closer_breaches).
    if c.close_mins and c.close_times and not person_only:
        by_date_role = {}
        for i, r in enumerate(rows or []):
            fam = c.family(r.get("role"))
            if fam in c.close_mins and r.get("date") and not c.training_row(r):
                by_date_role.setdefault((r["date"], fam), []).append((i, r))
        for (d, fam), items in by_date_role.items():
            try:
                day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
            except (ValueError, TypeError):
                continue
            close_m = close_minutes(c, day)
            if close_m is None or _closer_rule_runs(c, fam, d):
                continue
            need = close_m + int(c.close_mins[fam])
            ends = [(end_minutes(r), i, r) for i, r in items]
            ends = [(e, i, r) for e, i, r in ends if e is not None]
            if not ends:
                continue
            e_last, i_last, r_last = max(ends, key=lambda t: t[0])
            if e_last < need - 15:
                out.append(_v("ends_before_role_close", i_last, r_last,
                              f"last {r_last.get('role')} ends {r_last.get('shift_end')}, the rule is until {_fmt_minutes(need % (24 * 60))}",
                              floor_role=fam, day_level=True))

    # a start or end the owner made a rule for a role on a daypart (schedule
    # audit 10/3/26 L-33): soft — the draft is built to it
    # (apply_role_times); a row off it is worth a look, never a blocker.
    if getattr(c, "role_times", None) and not person_only:
        out.extend(_role_time_violations(rows, c))

    # coverage the owner set and the defaults every trading day needs
    # (NS5 M8): the role floors are hard, a keyholder stays until close
    # whenever the roster has one, and somebody is on at close.
    if not person_only:
        out.extend(_coverage_violations(rows, c))
        # a manager on the floor every minute anyone is (owner, 10/2/26)
        out.extend(_manager_violations(rows, c, context=context))

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
            if not any(c.key(r.get("employee")) in c.keyholders for _, r in items):
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
                s_, e_ = start_minutes(r), end_minutes(r)
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

    # The most of a role on at once by the owner's RESTAURANT HOURS & SHIFT
    # RULES ("Servers: maximum 5 at once" — PROMPT-1), per date and family,
    # in the order of each one's first row (IncrementalSweep phase 7.5).
    if getattr(c, "role_caps", None) and not person_only:
        out.extend(_role_cap_violations(rows, c))

    # per-person rules: overlap, rest, hours, days off
    for key, items in by_person.items():
        items.sort(key=lambda ir: (ir[1].get("date", ""), start_minutes(ir[1]) or 0))
        # The keys that are this person: their own, and any roster person an
        # open "same person?" question joins to them (E-25).
        members = {c.key(r.get("employee")) for _, r in items} | {key}
        if c.linked:
            members |= {k for k, g in c.linked.items() if g == key}
        tail_rows = [r for m in sorted(members) for r in (c.base_rows.get(m) or [])]
        # Read for the group: a minor among the names an open question
        # joins holds the whole group to a minor's limits.
        minor_key = next((m for m in sorted(members) if m in c.minors), None)
        # hours by payroll bucket, including what is already published
        per_bucket = {}
        for m in members:
            for b, h in (c.base_hours.get(m) or {}).items():
                per_bucket[b] = per_bucket.get(b, 0.0) + h
        for i, r in items:
            b = c.bucket(r.get("date", "")) if r.get("date") else ""
            per_bucket[b] = per_bucket.get(b, 0.0) + row_hours(r)
        name = items[0][1].get("employee")
        # Two names an open question joins are held to the stricter limit.
        mx = min((c.max_hours(n) for n in {(r.get("employee") or "").strip() for _, r in items}),
                 default=c.max_hours(name))
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
                said = (f"{this_week:g}h this week — full-time, at least {mn:g}h unless you set their own minimum"
                        if c.key(name) in c.full_time_default else f"{this_week:g}h this week, wants at least {mn:g}h")
                out.append(_v("under_min_hours", items[0][0], items[0][1], said,
                              severity=round(mn - this_week, 2)))
        # The day's hours, every leg of it (schedule audit 10/3/26 E-6): a
        # 16-17 host on 10am-3pm and 4-9pm works ten hours, and a server on
        # 6h + 6h in a daily-overtime state works twelve — checked one row
        # at a time, neither was flagged, while the price summed the day.
        # Counted in time order, so the leg that crosses the cap carries the
        # flag (moving or cutting it fixes the day); each flag's severity is
        # the day's excess so far, and the breach is the day's worst. Two
        # names an open "same person?" question joins share one day (E-25).
        dot = None if c.is_salaried(name) else c.compliance.get("daily_ot_hours")
        days = {}
        for i, r in items:
            days.setdefault(r.get("date") or "", []).append((i, r))
        for d, legs in days.items():
            mcap, mtext = minor_daily_cap(c, minor_key, d) if minor_key else (None, None)
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
        if minor_key:
            band = c.minor_bands.get(minor_key)
            br = minor_rules(band, c.jurisdiction)
            if not band:
                out.append(_v("minor_age_unknown", items[0][0], items[0][1],
                              f"{name} is marked a minor with no age band — set 14-15 or 16-17 so the age limits are checked"))
            if br.get("max_weekly_school_week"):
                week_tot = {}
                for r in tail_rows:
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
        tail = [(None, r, *shift_span(r, c.tz)) for r in tail_rows]
        tail = [(i, r, s, e) for i, r, s, e in tail if s]
        need = float(c.compliance.get("min_rest_hours") or 0)
        # Owners and managers keep their own turnarounds (rest_rule_applies);
        # overlaps still count for everyone.
        if need and all(not rest_rule_applies(c, m) for m in members):
            need = 0.0
        allspans = sorted(spans + tail, key=lambda t: (t[2], t[3]))
        # Each span against the one with the latest end so far: an overlap
        # inside a long shift, and a tail row AFTER a row of this week (next
        # week's published Monday open, a sibling's dinner after lunch here),
        # were never compared — only the later-starting row of a pair was
        # checked, and only when it was this week's. A breach is charged to
        # the row of this week in the pair.
        reach = None
        for cur in allspans:
            i_cur, r_cur, s_cur, e_cur = cur
            if reach is not None and not (reach[0] is None and i_cur is None):
                i_p, r_p, _s_p, e_p = reach
                own_i, own_r, other = (i_cur, r_cur, r_p) if i_cur is not None else (i_p, r_p, r_cur)
                if s_cur < e_p:
                    # A tail row (another site's, a draft's) at the same start
                    # is still another shift; only this week's own duplicate
                    # is the double booking flagged above.
                    if (i_p is None or i_cur is None or r_cur.get("shift_start") != r_p.get("shift_start")
                            or r_cur.get("date") != r_p.get("date")):
                        out.append(_v("overlap", own_i, own_r, f"overlaps {_whose(other, own_r)}"
                                      f"{other.get('shift_start')}–{other.get('shift_end')} on "
                                      f"{_mdy_safe(other.get('date'))}{_tail_note(other)}{_maybe_same(other, own_r)}"))
                else:
                    gap = (s_cur - e_p).total_seconds() / 3600
                    # Same date = a double shift, not a rest breach (see rest_ok).
                    if need and gap < need - 0.01 and r_cur.get("date") != r_p.get("date"):
                        said = ("since " + _whose(other, own_r, "previous shift") if own_r is r_cur
                                else "before " + _whose(other, own_r, "next shift"))
                        out.append(_v("rest_gap", own_i, own_r, f"{gap:.1f}h {said}{_tail_note(other)}, "
                                                                 f"the rule is {need:g}h{_maybe_same(other, own_r)}",
                                      severity=round(need - gap, 2)))
            if reach is None or e_cur > reach[3]:
                reach = cur
        # too many days in a row — the published tail counts, so a Saturday
        # and Sunday already sent plus Monday to Friday here reads as seven.
        # Every run past the rule that this week's rows are part of counts:
        # the severity is the days past it summed over them, so splitting a
        # nine-day run into two of four is progress the repair can see
        # (schedule audit 10/3/26 P-30), and the flag names the longest.
        max_run = c.compliance.get("max_consecutive_days")
        if max_run:
            worked_dates = {r.get("date") for _, r in items if r.get("date")}
            worked_dates |= {r.get("date") for r in tail_rows if r.get("date")}
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


def _mdy_safe(iso) -> str:
    """An owner-facing date (M/D/YY), or the text as given when it is not one."""
    try:
        from time_utils import mdy
        return mdy(iso) or str(iso or "")
    except Exception:
        return str(iso or "")


def _whose(prev: dict, cur: dict, what: str = "") -> str:
    """"their " — or "Kim T.'s " when the other row is under a name an open
    "same person?" question joins to this one (E-25)."""
    a, b = (prev.get("employee") or "").strip(), (cur.get("employee") or "").strip()
    if a and b and a.lower() != b.lower():
        return f"{a}'s {what + ' ' if what else ''}"
    return f"their {what + ' ' if what else ''}"


def _maybe_same(prev: dict, cur: dict) -> str:
    """Why another name's shift counts against this one (E-25), or ""."""
    a, b = (prev.get("employee") or "").strip(), (cur.get("employee") or "").strip()
    if a and b and a.lower() != b.lower():
        return f" — {a} may be the same person as {b}: answer it on the Team page"
    return ""


def _tail_note(r: dict) -> str:
    """Where a row of the tail comes from, when it is not this site's
    published week: another location, an unsent draft, the time clock."""
    bits = []
    if r.get("_site"):
        bits.append(f" at {r['_site']}")
    src = r.get("_source")
    if src == "draft":
        bits.append(" (in the unsent draft of that week)")
    elif src == "punches":
        bits.append(" (clocked, from punches)")
    return "".join(bits)


def close_minutes(c: Constraints, day: str):
    """The close on `day` as minutes past that day's midnight — a close in
    the small hours ("12:00am", "1:30am") is the NEXT morning, past 24h. It
    was read as minute 0, so a midnight close never flagged a bartender
    leaving at 9pm under "stays an hour after close" (NS5 L14)."""
    m = parse_minutes((c.close_times or {}).get(day, ""))
    if m is None:
        return None
    return m + 24 * 60 if m < _OVERNIGHT_LATEST_BEFORE else m


def close_cap_minutes(c, row):
    """The latest a row may end, as minutes past its date's midnight: that
    day's close plus the most its role may stay after it
    (Constraints.role_buffers, role_close_caps — the cap the job's parser
    holds every model row to), or None when that day has no close set. A
    manager on that date may stay as long as any role does — the manager
    plan keeps one on until the last role leaves (schedule_skeleton) —
    (re-audit 10/4/26 PIPE-10)."""
    if not getattr(c, "close_times", None):
        return None
    day = _weekday_of(row.get("date") or "")
    close_m = close_minutes(c, day) if day else None
    if close_m is None:
        return None
    # The larger of the role's allowance and its stay (role_close_caps): a
    # role told to stay is never past close by staying.
    buffers = getattr(c, "role_buffers", None) or {}
    stays = getattr(c, "close_mins", None) or {}
    role = (row.get("role") or "").strip()
    stay = max(int(role_minutes(buffers, role, getattr(c, "role_families", None)) or 0),
               int(stays.get(c.family(role)) or 0))
    name = (row.get("employee") or "").strip()
    if name and c.manages(name, row.get("date")):
        stay = max([stay] + [int(v or 0) for v in list(buffers.values()) + list(stays.values())])
    return close_m + stay


# The mark the job's final sweep writes on a row it still faults ("— NEEDS
# REVIEW: why"). One pattern for every path that takes it off (re-audit
# 10/4/26 PIPE-6): a redo used to carry a stale one on a kept row.
REVIEW_MARK = _re_roles.compile(r"\s*(?:—\s*)?NEEDS REVIEW\b.*$", _re_roles.S)


def strip_review_mark(notes) -> str:
    """`notes` without a NEEDS REVIEW mark."""
    return REVIEW_MARK.sub("", str(notes or "")).strip()


def end_minutes(row):
    """A row's end as minutes past its own date's midnight: an end at or
    before its start crosses midnight, and a row starting in the small hours
    of its night ends in them too (E-32, _night_offset)."""
    s, e = parse_minutes(row.get("shift_start", "")), parse_minutes(row.get("shift_end", ""))
    if e is None:
        return None
    night = _night_offset(s, e)
    if s is not None and e <= s:
        e += 24 * 60
    return e + night


def role_time_for(c, row) -> dict:
    """The owner's start/end rule for this row's role on its weekday and
    daypart (Constraints.role_times, L-33), or {}."""
    times = getattr(c, "role_times", None) or {}
    if not times:
        return {}
    role = (row.get("role") or "").strip().lower()
    return times.get((role, _weekday_of(row.get("date") or ""), daypart_of(row.get("shift_start", "")))) or {}


def _role_cap_violations(rows: list, c: Constraints) -> list:
    """over_role_max: a date with more of a capped role family on at once
    than the owner's ceiling (Constraints.role_caps), pinned to the latest
    starter of the busiest moment. Soft and day-level."""
    from shift_quality import present_dayparts as _present
    groups = {}
    for i, r in enumerate(rows or []):
        if not r.get("date") or not (r.get("employee") or "").strip() or c.training_row(r):
            continue
        fam = c.family(r.get("role"))
        if fam in c.role_caps:
            groups.setdefault((r["date"], fam), []).append((i, r))
    out = []
    for (d, fam), items in groups.items():
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue
        for cap in c.role_caps.get(fam) or []:
            if cap.get("days") and day not in cap["days"]:
                continue
            mine = [(i, r) for i, r in items if not cap.get("daypart") or cap["daypart"] in _present(r)]
            events = []
            for i, r in mine:
                s_, e_ = start_minutes(r), end_minutes(r)
                if s_ is not None and e_ is not None and e_ > s_:
                    events += [(s_, 1, i), (e_, -1, i)]
            events.sort(key=lambda ev: (ev[0], ev[1]))
            running, peak, active, worst = 0, 0, [], []
            for _t, delta, i in events:
                active = active + [i] if delta > 0 else [x for x in active if x != i]
                running += delta
                if running > peak:
                    peak, worst = running, list(active)
            if peak > int(cap["max"]) and worst:
                latest = max(worst, key=lambda i: start_minutes(rows[i]) or 0)
                out.append(_v("over_role_max", latest, rows[latest],
                              f"{peak} {_family_label(c, fam)} on at once — your rule is at most {int(cap['max'])}"
                              + (f" (“{str(cap.get('source'))[:120]}”)" if cap.get("source") else ""),
                              floor_role=fam, day_level=True, severity=float(peak - int(cap["max"]))))
                break
    return out


def _role_time_violations(rows: list, c: Constraints) -> list:
    """A row of a role whose start or end is off the owner's time rule for
    that weekday and daypart (schedule audit 10/3/26 L-33), naming the rule."""
    out = []
    for i, r in enumerate(rows or []):
        if not (r.get("employee") or "").strip():
            continue
        spec = role_time_for(c, r)
        for edge, col in (("start", "shift_start"), ("end", "shift_end")):
            want, have = spec.get(edge), parse_minutes(r.get(col, ""))
            if want is None or have is None or have % (24 * 60) == want % (24 * 60):
                continue
            src = (spec.get("source") or {}).get(edge)
            out.append(_v("role_time", i, r,
                          f"{r.get('role')} {edge}s {r.get(col)} here — your rule is {_fmt_minutes(want)}"
                          + (f" (“{str(src)[:120]}”)" if src else ""),
                          edge=edge, severity=round(abs((have - want) % (24 * 60)) / 60.0, 2)))
    return out


# A rule never makes a shift shorter than this (a 4:30pm start on a shift
# the draft ended at 5pm is the draft's problem to fix, not a 30-minute row).
ROLE_TIME_MIN_MINUTES = 120


def apply_role_times(rows: list, c: Constraints, editable=None) -> dict:
    """Retime each row of a role to the owner's start/end rule for its
    weekday and daypart (schedule audit 10/3/26 L-33 — a retime the manager
    kept making, made a rule) where that is legal: never a row the owner or
    the manager plan pinned (`_pinned`), never a date outside `editable` (a
    partial redo keeps the owner's days), only when the person can work the
    new times (Constraints.can_add) and the change makes no tier above
    quality worse (regressions up to TIER_BUDGET, soft breaches included —
    a manager's coverage, a floor, overtime and the budget all outrank a
    start time). {rows, retimed: [{index, employee, date, from, to,
    reason}], left: [{index, employee, date, reason}]}."""
    out_rows = [dict(r) for r in rows or []]
    retimed, left = [], []
    if not getattr(c, "role_times", None) and not getattr(c, "role_windows", None):
        return {"rows": out_rows, "retimed": retimed, "left": left}
    sweep = IncrementalSweep(c)          # each retime re-sweeps its person and date only (P-37)
    for i in range(len(out_rows)):
        r = out_rows[i]
        if not (r.get("employee") or "").strip() or r.get("_pinned"):
            continue
        if editable is not None and r.get("date") not in editable:
            continue
        spec = role_time_for(c, r)
        # The hours the owner's RESTAURANT HOURS & SHIFT RULES give the
        # role (PROMPT-1): a row starting before them starts when they do,
        # one ending after them ends then.
        win = c.role_window_for(r.get("role"), r.get("date")) if getattr(c, "role_windows", None) else {}
        if not spec and not win:
            continue
        s0, e0 = parse_minutes(r.get("shift_start", "")), parse_minutes(r.get("shift_end", ""))
        if s0 is None or e0 is None:
            continue
        s1 = spec["start"] if spec.get("start") is not None else s0
        e1 = spec["end"] if spec.get("end") is not None else e0
        why_win = None
        if win:
            probe = dict(r, shift_start=_fmt_minutes(s1), shift_end=_fmt_minutes(e1 % (24 * 60)))
            sa, ea = start_minutes(probe), end_minutes(probe)
            if sa is not None and ea is not None:
                if win.get("earliest") is not None and sa < win["earliest"]:
                    sa, why_win = win["earliest"], win.get("source")
                if win.get("latest") is not None and ea > win["latest"]:
                    ea, why_win = win["latest"], win.get("source")
                s1, e1 = sa % (24 * 60), ea % (24 * 60)
        if s1 % (24 * 60) == s0 % (24 * 60) and e1 % (24 * 60) == e0 % (24 * 60):
            continue
        span = (e1 - s1) % (24 * 60)
        if span < ROLE_TIME_MIN_MINUTES:
            left.append({"index": i, "employee": r.get("employee"), "date": r.get("date"),
                         "reason": f"the rule would leave a {span / 60:g}h shift"})
            continue
        new = dict(r, shift_start=_fmt_minutes(s1), shift_end=_fmt_minutes(e1 % (24 * 60)),
                   scheduled_hours=f"{span / 60:g}")
        trial = out_rows[:i] + [new] + out_rows[i + 1:]
        ok, why = c.can_add(new, trial)
        if ok:
            worse = regressions(sweep.profile(out_rows), sweep.profile(trial), upto=TIER_BUDGET,
                                hard_only=False)
            if worse:
                ok, why = False, worse[0]["label"]
        if not ok:
            left.append({"index": i, "employee": r.get("employee"), "date": r.get("date"), "reason": why})
            continue
        src = why_win or (spec.get("source") or {}).get("start" if spec.get("start") is not None else "end")
        retimed.append({"index": i, "employee": r.get("employee"), "date": r.get("date"),
                        "from": hours_label(r),
                        "to": f"{new['shift_start']}–{new['shift_end']}",
                        "reason": "your rule" + (f": “{str(src)[:120]}”" if src else "")})
        out_rows = trial
    return {"rows": out_rows, "retimed": retimed, "left": left}


def _coverage_violations(rows: list, c: Constraints) -> list:
    """coverage_floor, keyholder_until_close and nobody_at_close — rules
    about who is on a day, not about one person's row. Each is pinned to a
    row of that day (the review and the publish gate name a row) and marked
    day_level, and none is something a different person on that row would
    fix, so the fix pass leaves them for the owner. Only days with shifts on
    them are judged: a day with nobody at all is visibly empty, and a closed
    day has none. A training shift is nobody's coverage (D-16): it counts
    toward no floor, no rule and not toward somebody at close."""
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
        # Who is really on: a training shift is nobody's coverage (D-16), and
        # neither is a row its person cannot work that day (time off, off the
        # roster) — the scorer leaves those out too (schedule re-audit
        # 10/4/26 SQ-8).
        counted = [(i, r) for i, r in items if _on_the_floor(c, r)]
        out.extend(_floor_breaches(c, d, day, items, counted))
        # the owner's rules checked per day: those that name no daypart (the
        # role on the day at all) and a daypart rule about a role whose job
        # codes have no single code for that half (counted by family there)
        for rule in (c.owner_rules or []):
            if (rule.get("daypart") and not rule.get("by_family")) or (rule.get("days") and day not in rule["days"]):
                continue
            role_low = rule["role"].strip().lower()
            if rule.get("match", "role") == "role":
                mine = [(i, r) for i, r in counted if (r.get("role") or "").strip().lower() == role_low]
            else:
                mine = [(i, r) for i, r in counted if c.family(r.get("role")) == rule.get("family")]
            part = rule.get("daypart")
            if part:
                # A daypart rule counted by family is a floor over that
                # daypart's service: the one floor test (SQ-8).
                held = _floor_test(c, day, [r for _i, r in mine], rule.get("family") or role_low, rule["min"], part)
                n_on = rule["min"] if held["held"] else int(held["on_at_worst"] or 0)
            else:
                n_on = len({c.key(r.get("employee")) for _i, r in mine})
            if n_on < rule["min"]:
                i0, r0 = (mine or counted or items)[0]
                # An owner-only rule's words stay the owner's (D-38).
                said = ("below a staffing rule you set" if rule.get("private")
                        else f"your rule: “{rule['text'][:120]}”")
                out.append(_v("owner_rule", i0, r0,
                              f"{n_on} {rule['role']} on {day}"
                              + (f" at {'lunch/day' if part == 'morning' else 'dinner/night'}" if part else "")
                              + f" — {said}",
                              floor_role=rule.get("family") or role_low, severity=rule["min"] - n_on,
                              day_level=True))
        ends_all = [(end_minutes(r), i, r) for i, r in items]
        ends_all = [(e, i, r) for e, i, r in ends_all if e is not None]
        if not ends_all:
            continue
        ends = [(e, i, r) for e, i, r in ends_all if not c.training_row(r)]
        close_m = close_minutes(c, day)
        e_last, i_last, r_last = max(ends or ends_all, key=lambda t: t[0])
        if close_m is not None and (not ends or e_last < close_m - 15):
            out.append(_v("nobody_at_close", i_last, r_last,
                          (f"the last shift {day} ends {r_last.get('shift_end')}, you close at "
                           f"{_fmt_minutes(close_m % (24 * 60))}") if ends else
                          f"nobody but a trainee is on {day} at close ({_fmt_minutes(close_m % (24 * 60))})",
                          severity=close_m - e_last if ends else close_m, day_level=True))
        # Each role with closers (D-9): its last person out is one of its
        # closers and stays until close plus the role's minutes.
        if c.compliance.get("keyholder_until_close", True) and c.closers_by_role:
            out.extend(_closer_breaches(c, d, day, items))
        # A trainee beside nobody who can train them (D-16).
        out.extend(_trainee_breaches(c, day, items))
    return out


def _on_the_floor(c, r) -> bool:
    """Whether a row puts its person on the floor for coverage: not a
    training shift, and nothing about the row itself says it will not stand
    — the day or a daypart it reaches into off, outside their window, a
    certificate the role needs missing that date. The scorer leaves the
    same rows out (the sweep's no-show rows, `flagged`), so the two count
    one crew (SQ-8)."""
    if c.training_row(r):
        return False
    name, d = r.get("employee"), r.get("date") or ""
    from shift_quality import touched_dayparts
    if not all(c.can_work(name, d, p)[0] for p in touched_dayparts(r)):
        return False
    if not c.window_ok(name, d, r.get("shift_start", ""), r.get("shift_end", ""))[0]:
        return False
    return c.cert_ok(name, r.get("role", ""), d)[0]


def _floor_test(c, day, rows, role, need, part) -> dict:
    """shift_quality.floor_shortfall for one date's `rows` — the ONE floor
    test, the scorer's coverage by the hour included: `need` people of
    `role`'s family (its AM/PM job codes one role) on at every half hour of
    the daypart's core service, clipped to the day's hours (schedule
    re-audit 10/4/26 SQ-8, RULES-12). The sweep used to count anybody whose
    row overlapped the core window by an hour, by the exact job code: a
    5-8pm and a 7-11pm server passed a dinner floor of 2 that the score
    capped at 33, and two "Bartender PM" bartenders read as 0 against a
    "Bartender" floor."""
    from shift_quality import floor_shortfall
    open_m = parse_minutes((c.open_times or {}).get(day, "") or "")
    close_m = parse_minutes((c.close_times or {}).get(day, "") or "")
    return floor_shortfall(rows, role, need, part, open_m, close_m, families=c.role_families)


def _floor_breaches(c, d, day, items, counted) -> list:
    """coverage_floor for one trading day: each role family's floors for a
    daypart (its job codes' floors added up, as the scorer adds them) held
    by floor_shortfall. Severity is the people missing summed over the short
    half hours, so a change that leaves the floor short for longer, or by
    more, reads as worse and one that closes part of it as better."""
    out = []
    by_fam = {}
    for role in sorted(c.role_floors or {}, key=str.lower):
        for part in ("morning", "night"):
            need = floor_for(c.role_floors, role, day, part)
            if need <= 0:
                continue
            fam = c.family(role)
            entry = by_fam.setdefault((fam, part), [0, role])
            entry[0] += need
    if not by_fam:
        return out
    rows = [r for _i, r in counted]
    for (fam, part), (need, role) in sorted(by_fam.items()):
        held = _floor_test(c, day, rows, role, need, part)
        if held["held"]:
            continue
        role_low = role.strip().lower()
        mine = [(i, r) for i, r in counted if c.family(r.get("role")) == fam]
        i0, r0 = (mine or counted or items)[0]
        word = "lunch/day" if part == "morning" else "dinner/night"
        src = (c.rule_floor_sources or {}).get((role_low, day, part))
        mins = int(held["short_minutes"])
        dur = f"{mins // 60}h" + (f" {mins % 60}m" if mins % 60 else "") if mins >= 60 else f"{mins}m"
        out.append(_v("coverage_floor", i0, r0,
                      f"{held['on_at_worst']} {role} on at {_fmt_minutes(held['worst_at'] % (24 * 60))} {day} "
                      f"({word}, short for {dur}), your floor is {need}"
                      + (f" (your rule: \u201c{src[:120]}\u201d)" if src else ""),
                      floor_role=role_low, daypart=part, severity=float(held.get("missing") or 1),
                      worst_at=held["worst_at"], minutes=mins, day_level=True))
    return out


def _family_label(c, fam) -> str:
    """How a role family reads to the owner: its job code without the
    daypart words, as the restaurant spells it ("Server" for Server AM/PM)."""
    from collections import Counter
    names = Counter()
    for low, disp in (c.role_names or {}).items():
        if c.family(low) == fam:
            words = [w for w in str(disp).replace("-", " ").split()
                     if w.strip(".,:;").lower() not in _MORNING_WORDS | _NIGHT_WORDS]
            if words:
                names[" ".join(words)] += 1
    return names.most_common(1)[0][0] if names else str(fam or "").capitalize()


def _display_name(c, key) -> str:
    """How the roster spells the person filed under `key` (c.key)."""
    return (c.display or {}).get(key) or next((n for n in (c.roster_names or []) if c.key(n) == key), key)


def _closer_rule_runs(c, fam, d) -> bool:
    """Whether the closer rule judges `fam` on date `d`: the role has
    closers and at least one of them can work that day."""
    if not c.compliance.get("keyholder_until_close", True):
        return False
    closers = (c.closers_by_role or {}).get(fam) or ()
    return any(c.can_work(_display_name(c, k), d)[0] for k in closers)


def closer_need(c, fam, day, role_last_end) -> tuple:
    """(minute, until_close) the closer of role family `fam` stays until on
    `day` — each role against its own close (schedule re-audit 10/4/26
    RULES-6). Every role used to be held to the restaurant's close, or with
    no close on file to the day's last shift of ANY role, so a cook closer
    was kept on to the bar's 2am and a server to the dishwasher's 1am. In
    order:
      1. the owner's end-time rule for the role's night (role_times — "line
         cooks end at 11pm Fridays"): that is the role's close;
      2. a "stays until close + N" setting (close_mins): close plus N;
      3. a role its own punches show ending before the close
         (early_close_roles): the last of the role out — the closer is the
         last of their role to leave (owner, 10/2/26);
      4. the restaurant's close, when it is on file;
      5. else the role's own last end.
    The sweep, close_out_gaps, the solver and the prompt all ask this."""
    for (role, wd, part), spec in (getattr(c, "role_times", None) or {}).items():
        if wd == day and part == "night" and spec.get("end") is not None and c.family(role) == fam:
            end = int(spec["end"])
            return (end + 24 * 60 if end < _OVERNIGHT_LATEST_BEFORE else end), True
    close_m = close_minutes(c, day)
    if close_m is not None and fam in (c.close_mins or {}):
        return close_m + int((c.close_mins or {}).get(fam, 0) or 0), True
    if close_m is not None and fam not in (getattr(c, "early_close_roles", None) or set()):
        return close_m, True
    return role_last_end, False


# A role with no closer on at all reads as at least this bad, whatever the
# hours: a closer on, even one leaving early, is always better than none.
CLOSER_MISSING_SEVERITY = 24 * 60


def _closer_breaches(c, d, day, items) -> list:
    """The closer rule for one trading day (schedule audit 10/3/26 D-9): for
    each role with closers the owner chose, the last of that role to leave
    is one of its closers, and a role that stays to the close keeps its
    closer on until close plus the role's minutes (closer_need). It used
    to be one roster-wide check — a dishwasher staying late satisfied it
    while the bar closed without a closer, and 47 of 64 people at Simple
    EJ's were marked. A role that does not work that day is not judged; a
    day none of its closers can work is a soft notice, not a hard breach
    nobody can fix.

    Severity is about the close and nothing else (RULES-7): with a closer
    on, how far before the target they leave or how long a teammate
    outlasts them; with none, CLOSER_MISSING_SEVERITY plus how far before
    the target the role's last person leaves. It was measured from the
    role's EARLIEST start, so adding a lunch server read as making the
    dinner closer breach worse and the floor fill was refused."""
    out = []
    for fam, closers in sorted((c.closers_by_role or {}).items()):
        mine = [(end_minutes(r), i, r) for i, r in items
                if c.family(r.get("role")) == fam and not c.training_row(r)]
        mine = [(e, i, r) for e, i, r in mine if e is not None]
        if not mine:
            continue
        e_last, i_last, r_last = max(mine, key=lambda t: t[0])
        # Worded only when there is something to say: the sweep runs on
        # every trial change a pass makes.
        label = lambda: _family_label(c, fam)  # noqa: E731
        names = lambda: ", ".join(sorted(_display_name(c, k) for k in closers)[:6])  # noqa: E731
        if not _closer_rule_runs(c, fam, d):
            out.append(_v("closer_unavailable", i_last, r_last,
                          f"none of your {label()} closers ({names()}) can work {day} — the last {label()} out closes",
                          close_role=fam, day_level=True))
            continue
        need, until_close = closer_need(c, fam, day, e_last)
        on = [(e, i, r) for e, i, r in mine if c.key(r.get("employee")) in closers]
        best = max((e for e, _i, _r in on), default=None)
        if best is not None and best >= need - 15 and e_last <= best + 15:
            continue
        label, names = label(), names()
        if best is None:
            sev = CLOSER_MISSING_SEVERITY + max(need - e_last, 0)
        else:
            sev = max(need - best, e_last - best, 1)
        until = f"close ({_fmt_minutes(need % (24 * 60))})"
        if best is None:
            detail = (f"no {label} closer on {day} — {r_last.get('employee')} is the last {label} out; "
                      f"your {label} closers: {names}")
        elif until_close and best < need - 15:
            who = next(r for e, _i, r in on if e == best).get("employee")
            detail = f"{who}, the {label} closer {day}, leaves before {until}"
        else:
            detail = (f"{r_last.get('employee')} is the last {label} out {day}, after the closer — "
                      f"the last {label} to leave is one of your closers ({names})")
        out.append(_v("keyholder_until_close", i_last, r_last, detail, close_role=fam, severity=sev,
                      day_level=True))
    return out


def _trainee_breaches(c, day, items) -> list:
    """A trainee's training shift with their trainer — or, with none named,
    somebody who holds the role — on for at least half of it (D-16)."""
    out = []
    if not c.trainees:
        return out
    for i, r in items:
        key = c.key(r.get("employee"))
        t = c.trainees.get(key)
        if not t or not c.training_row(r) or is_training_role(r.get("role")):
            continue
        sp = _span(r)
        if not sp:
            continue
        trainer = c.key(t.get("trainer")) if (t.get("trainer") or "").strip() else ""
        fam = c.family(t.get("target_role"))
        mentors = [_span(x) for _j, x in items if x is not r and (
            (c.key(x.get("employee")) == trainer) if trainer else
            (c.family(x.get("role")) == fam and not c.training_row(x)))]
        covered = sum(max(0, min(sp[1], m[1]) - max(sp[0], m[0])) for m in _merge([m for m in mentors if m]))
        if covered * 2 >= sp[1] - sp[0]:
            continue
        who = _display_name(c, trainer) if trainer else None
        out.append(_v("trainee_unpaired", i, r,
                      f"{r.get('employee')} is training as {t.get('target_role')} {day} "
                      + (f"and their trainer {who} isn't on with them" if who
                         else f"with no {_family_label(c, fam)} beside them to train them")))
    return out


# ── a manager on the floor every minute anyone is (owner, 10/2/26) ─────────
#
# "There can be ZERO shifts without ONE manager. Always." — Erik's first
# generated week had no manager and neither owner on any day. A manager is
# whoever the owner says runs the floor (staff_settings.floor_manager), else
# automatically a roster person whose role, held role or recent role is a
# floor manager or owner title, or who holds the floor manager certificate
# (manager_basis); the person counts, whatever role their row is in. A
# minute with somebody on and no manager on is a hard breach: it holds the
# publish, and every pass that changes rows is refused one that makes a new
# one. cover_manager_gaps is the deterministic backstop that fills them.


def rest_rule_applies(c, name_or_key) -> bool:
    """Whether the rest rule (min_rest_hours between one shift's end and the
    next start) holds this person: everyone but an owner or a manager, the
    same people max_shift_applies sets free (Simple EJ's, 10/6/26 - Andrew
    Marola closes Saturday and opens Sunday by choice). An overlap is still
    an overlap for everyone."""
    return c.key(name_or_key) not in (c.managers or {})


def max_shift_applies(c, name_or_key) -> bool:
    """Whether the shift-length maximum holds this person: everyone but an
    owner or a manager (Constraints.managers - a manager or owner role, or
    the floor-manager certificate). Someone only standing in as a manager
    on a date is still held to it."""
    return c.key(name_or_key) not in (c.managers or {})


def is_manager_role(role) -> bool:
    """A role that names whoever runs the floor (manager_role_kind): an
    owner, a manager or supervisor of no single department, a GM, AGM, MOD
    or shift lead. A pattern over the role name used to decide it — "Kitchen
    Manager" ran the floor and "AGM", "MOD" and "Shift Lead" did not
    (schedule audit 10/3/26 P-7); the owner's yes or no now wins over it."""
    return manager_role_kind(role) in ("owner", "manager", "lead")


NO_ONE = ""   # a fix record's "from" when the shift was added, not moved


def hours_label(r) -> str:
    """A row's hours as a fix record names them: "4:00pm–11:00pm"."""
    return f"{r.get('shift_start')}–{r.get('shift_end')}"


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


def open_hours(c: "Constraints", day: str):
    """(open, close) minutes past `day`'s midnight the restaurant is open,
    from the hours on file — a close in the small hours is the next
    morning's. Either end is None when it is not on file."""
    o = parse_minutes((c.open_times or {}).get(day, "") or "")
    cl = close_minutes(c, day)
    if o is not None and cl is not None and cl <= o:
        cl += 24 * 60
    return o, cl


def _day_offset(d: str, base: str):
    """Whole days from date `base` to date `d` (None when unreadable)."""
    try:
        return (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(base, "%Y-%m-%d")).days
    except (ValueError, TypeError):
        return None


def counts_as_manager(c: "Constraints", row: dict) -> bool:
    """Whether `row` puts a manager on the floor: its person manages that
    date (Constraints.manages), can work it, and it is not a training shift
    — a manager in training is never the floor manager, alone or not
    (schedule re-audit 10/4/26 RULES-2)."""
    d = row.get("date") or ""
    name = row.get("employee")
    return bool(c.manages(name, d) and not c.training_row(row) and c.can_work(name, d)[0])


def manager_gaps(rows: list, c: "Constraints", dates=None, context=()) -> dict:
    """{date: [(start_min, end_min, row_index)]} — each stretch the
    restaurant is open or somebody is on and no manager is, pinned to the
    first row on at its start (else the day's first row). Empty when the
    roster has no manager (no_manager_roster says so instead).

    Judged against the day's open hours as well as its rows: a Monday open
    at 11am whose first shift starts at 12:30pm was 90 minutes open with
    nobody on and no manager, and nothing said so (schedule re-audit 10/4/26
    RULES-1). And on one timeline across the week: a Saturday-dated
    manager's 10pm-6am covers the small hours of Sunday, and a Sunday 6am
    manager covers a Saturday-dated porter's last hour — per date they read
    as false gaps at the date boundary (RULES-10). `context` is rows of the
    neighbouring dates that may cover these dates' minutes but are not
    judged here (IncrementalSweep's one-date sweep passes them)."""
    if not c.managers and not c.acting_managers:
        return {}
    by_date, cover = {}, []
    for i, r in enumerate(rows or []):
        d = r.get("date")
        if not d or not (r.get("employee") or "").strip() or d in (c.closed_dates or set()):
            continue
        sp = _span(r)
        if not sp:
            continue
        if counts_as_manager(c, r):
            cover.append((d, sp))
        if dates is not None and d not in dates:
            continue
        by_date.setdefault(d, []).append((sp, i, r))
    for r in context or ():
        d = r.get("date")
        sp = _span(r) if d and (r.get("employee") or "").strip() and d not in (c.closed_dates or set()) else None
        if sp and counts_as_manager(c, r):
            cover.append((d, sp))
    out = {}
    for d, items in sorted(by_date.items()):
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day = ""
        need = [sp for sp, _i, _r in items]
        o, cl = open_hours(c, day) if day else (None, None)
        if o is not None or cl is not None:
            lo = o if o is not None else min(s for s, _e in need)
            hi = cl if cl is not None else max(e for _s, e in need)
            if hi > lo:
                need.append((lo, hi))
        staffed = _merge(need)
        mgr = []
        for md, (s, e) in cover:
            off = _day_offset(md, d)
            if off is not None and -1 <= off <= 1:
                mgr.append((s + off * 24 * 60, e + off * 24 * 60))
        gaps = [(s, e) for s, e in _uncovered(staffed, _merge(mgr)) if e - s > 0]
        if not gaps:
            continue
        pinned = []
        for gs, ge in gaps:
            on = sorted((sp[0], i) for sp, i, _r in items if sp[0] <= gs < sp[1])
            pinned.append((gs, ge, on[0][1] if on else items[0][1]))
        out[d] = pinned
    return out


def _manager_violations(rows: list, c: "Constraints", context=()) -> list:
    """no_manager per unmanaged stretch, and no_manager_roster when nobody on
    the roster manages — soft to the repair passes (no change to the week
    can mend the roster), but a blocker at publish (ROSTER_BLOCKERS,
    schedule re-audit 10/4/26 RULES-13). Each no_manager is about its DAY, not the
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
                          "nobody on the roster counts as a floor manager, so no shift has a manager on — name who "
                          "runs the floor in Team (Floor manager: yes) before this week goes out"))
        if not only:
            return out
    for d, gaps in manager_gaps(rows, c, dates=only, context=context).items():
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            day = d
        for gs, ge, idx in gaps:
            nobody = not any(sp and sp[0] < ge and gs < sp[1] for sp in
                             (_span(r) for r in rows if r.get("date") == d and (r.get("employee") or "").strip()))
            out.append(_v("no_manager", idx, rows[idx],
                          f"no manager on {day} from {_fmt_minutes(gs % (24 * 60))} to {_fmt_minutes(ge % (24 * 60))}"
                          + (" — you're open and nobody is on" if nobody else ""),
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
                                    "long_run", "minor_early", "minor_week_hours", "role_not_held"})
COVERAGE_KINDS = frozenset({"coverage_floor", "keyholder_until_close", "nobody_at_close", "no_manager_on_duty",
                            "owner_rule", "ends_before_role_close"})
BREACH_TIER = {**{k: TIER_PERSON for k in PERSON_KINDS},
               "no_manager": TIER_MANAGER,
               **{k: TIER_COVERAGE for k in COVERAGE_KINDS},
               "daily_ot": TIER_OVERTIME,
               "payroll_tail_full": TIER_OVERTIME,
               "under_min_hours": TIER_MIN_HOURS,
               # Soft (an owner may keep somebody on by hand), but ranked with
               # the close rules, so no pass below them runs a shift past the
               # owner's after-close allowance (re-audit 10/4/26 PIPE-10).
               "past_close": TIER_COVERAGE,
               # the owner's start/end rule for a role (L-33): below every
               # tier that keeps people legal, managed, covered and paid right
               "role_time": TIER_QUALITY,
               # the hours a role works and the most of it on at once, by the
               # owner's RESTAURANT HOURS & SHIFT RULES (PROMPT-1): the
               # owner's staffing rules, ranked with the floors
               "role_window": TIER_COVERAGE, "over_role_max": TIER_COVERAGE}
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
    if kind in ("owner_rule", "ends_before_role_close", "past_close"):
        # past_close is about a role's stay that night, not who works it: a
        # swap of people is no change, a later end is worse (PIPE-10).
        return (kind, d, v.get("floor_role") or (v.get("role") or "").strip().lower())
    if kind == "over_role_max":
        return (kind, d, v.get("floor_role") or "")
    if kind in ("nobody_at_close", "keyholder_until_close", "over_section_cap", "closer_unavailable"):
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


# ── the sweep, kept by person and by date (schedule audit 10/3/26 P-37) ────
#
# Every trial change a pass weighs used to copy the week and run the whole
# rule sweep over it: up to 300 sweeps in the overtime rebalance, 120 a run
# in the manager filler, 192 in the fix pass, one per candidate in the
# optimizer and the solver's judge — about a thousand whole-week sweeps a
# generation, doubled when the quality gate fired. A change touches one or
# two people on one or two dates. Every rule is about one person's own rows
# (their hours, rest, minors, availability) or about one date's rows (the
# floors, the closers, the manager every minute, the section count), so a
# trial only re-sweeps the people and the dates whose rows moved; the rest
# is the last sweep's answer. What comes back is exactly violations(rows, c)
# — the same entries in the same order — so nothing that reads the sweep can
# tell the difference (tests/test_sched_fix_d2_sweep.py holds the two equal
# over randomised weeks).

# The person-level kinds violations() emits in its per-person block, after
# every day-level rule; every other person-level kind is a row's own check.
_PERSON_WEEK_KINDS = frozenset({"over_max_hours", "payroll_tail_full", "under_min_hours", "minor_hours",
                                "daily_ot", "minor_age_unknown", "minor_week_hours", "overlap", "rest_gap",
                                "long_run", "days_off"})
# violations()' order of the day-level blocks.
_DAY_PHASE = {"ends_before_role_close": 2, "role_time": 3, "coverage_floor": 4, "owner_rule": 4,
              "nobody_at_close": 4, "keyholder_until_close": 4, "closer_unavailable": 4,
              "trainee_unpaired": 4, "no_manager": 5, "no_manager_on_duty": 6, "over_section_cap": 7,
              "over_role_max": 7.5}
# The two about the whole week, not a person or a date.
_WEEK_KINDS = frozenset({"no_manager_roster", "manager_rule_unusable"})
SWEEP_CACHE_LIMIT = 20000


def _neighbour_dates(d: str) -> tuple:
    """The dates either side of `d` (ISO)."""
    try:
        x = datetime.strptime(d, "%Y-%m-%d")
    except (ValueError, TypeError):
        return ()
    return ((x - timedelta(days=1)).strftime("%Y-%m-%d"), (x + timedelta(days=1)).strftime("%Y-%m-%d"))


def _sweep_sig(r) -> tuple:
    """What the sweep reads of one row."""
    out = []
    for k in ("employee", "date", "day", "role", "shift_start", "shift_end", "scheduled_hours"):
        v = r.get(k)
        try:
            hash(v)
        except TypeError:
            v = str(v)
        out.append(v)
    return tuple(out)


class IncrementalSweep:
    """violations(rows, c) for one week's trials, re-sweeping only the people
    and the dates whose rows differ from a sweep already run (P-37). Build
    one per pass (or per week being edited) over its Constraints; reuse it
    for every trial. `violations(rows)` and `profile(rows)` are the sweep's
    and breach_profile's answers for `rows`."""

    def __init__(self, c: "Constraints"):
        self.c = c
        self._person = {}
        self._date = {}
        self.swept_people = self.swept_dates = 0

    def _cache(self, store, key, compute):
        hit = store.get(key)
        if hit is None:
            if len(store) >= SWEEP_CACHE_LIMIT:
                store.clear()
            hit = store[key] = compute()
        return hit

    def violations(self, rows: list) -> list:
        c = self.c
        rows = rows or []
        by_person, by_date = {}, {}
        for i, r in enumerate(rows):
            # People grouped exactly as violations() groups them: one person,
            # one key, and a roster person an open "same person?" question
            # joins read as one (Constraints.key / sweep_key — D-8, E-25).
            name = (r.get("employee") or "").strip()
            if c.key(name):
                by_person.setdefault(c.sweep_key(name), []).append((i, r))
            if r.get("date"):
                by_date.setdefault(r["date"], []).append((i, r))
        out = []
        for pos, (key, items) in enumerate(by_person.items()):
            sig = tuple(_sweep_sig(r) for _i, r in items)

            def _person(items=items):
                self.swept_people += 1
                return violations([r for _i, r in items], c, person_only=True)
            for seq, v in enumerate(self._cache(self._person, (key, sig), _person)):
                gi = items[v["index"]][0]
                out.append(((8, pos, seq) if v["kind"] in _PERSON_WEEK_KINDS else (1, gi, seq), dict(v, index=gi)))
        firsts = {}
        for d, items in by_date.items():
            # A manager on the dates either side reaches across midnight
            # (manager_gaps' one timeline, RULES-10): their rows are part of
            # what this date's answer depends on.
            ctx = [r for nd in _neighbour_dates(d) for _i, r in by_date.get(nd, ())
                   if counts_as_manager(c, r)] if (c.managers or c.acting_managers) else []
            sig = (tuple(_sweep_sig(r) for _i, r in items), tuple(_sweep_sig(r) for r in ctx))

            def _day(items=items, ctx=ctx):
                self.swept_dates += 1
                return [v for v in violations([r for _i, r in items], c, day_only=True, context=ctx)
                        if v["kind"] not in _WEEK_KINDS]
            for seq, v in enumerate(self._cache(self._date, (d, sig), _day)):
                gi = items[v["index"]][0]
                phase = _DAY_PHASE.get(v["kind"], 9)
                if phase == 2:
                    sk = (2, self._first(firsts, items, ("close", v.get("floor_role")),
                                         lambda r, fam=v.get("floor_role"): c.family(r.get("role")) == fam
                                         and not c.training_row(r)))
                elif phase == 3:
                    sk = (3, gi, seq)
                elif phase == 7:
                    counted = {c.family(x) for x in (c.foh_roles or ()) if str(x).strip()} or {"server"}
                    sk = (7, self._first(firsts, items, ("cap",), lambda r: c.family(r.get("role")) in counted))
                elif phase == 7.5:
                    fam = v.get("floor_role")
                    sk = (7.5, self._first(firsts, items, ("rolemax", fam),
                                           lambda r, fam=fam: c.family(r.get("role")) == fam
                                           and (r.get("employee") or "").strip() and not c.training_row(r)))
                else:
                    sk = (phase, 1, d, seq)
                out.append((sk, dict(v, index=gi)))
        # The week's own: nobody on the roster manages (pinned to the first
        # row somebody is on), and the retired per-daypart switch with nobody
        # to satisfy it (pinned to the first row).
        staffed = next(((i, r) for i, r in enumerate(rows) if r.get("date") and (r.get("employee") or "").strip()
                        and r.get("date") not in (c.closed_dates or set())), None)
        if staffed is not None:
            for v in _manager_violations([staffed[1]], c):
                if v["kind"] == "no_manager_roster":
                    out.append(((5, 0), dict(v, index=staffed[0])))
        if c.compliance.get("manager_on_duty") and not c.keyholders and rows:
            out.append(((6, 0), _v("manager_rule_unusable", 0, rows[0], LABELS["manager_rule_unusable"])))
        out.sort(key=lambda t: t[0])
        return [v for _k, v in out]

    @staticmethod
    def _first(memo, items, tag, test) -> int:
        """The first row index on this date passing `test` — where
        violations()' block met that date first."""
        key = (items[0][1].get("date"),) + tuple(tag)
        if key not in memo:
            memo[key] = next((i for i, r in items if test(r)), items[0][0])
        return memo[key]

    def profile(self, rows: list, viols: list = None) -> dict:
        """breach_profile(rows, c) through the kept sweep."""
        return breach_profile(rows, self.c, viols=viols if viols is not None else self.violations(rows))


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


def rule_tag(kind) -> str:
    """"[HARD]" for a breach kind the sweep holds hard (HARD), "[SOFT]" for
    any other — the same sets violations() marks each breach by, so a rule
    line can never call itself something the checker does not hold it to
    (schedule audit 10/3/26 PR-5: every rule used to be a flat "- …" line
    under one header that said each was "verified … a breach is flagged",
    daily overtime beside the longest shift). A prompt rule with no breach
    kind of its own (a station the backstop fills) is [SOFT]: nothing about
    it ever blocks a week."""
    return "[HARD]" if kind in HARD else "[SOFT]"


def _rule(kind, text, why=None) -> str:
    """One rule line: its tag, the rule, and for a hard rule why it is hard
    (PR-27: reasons let the model carry a rule to a case it does not name)."""
    return f"- {rule_tag(kind)} {text}" + (f" — {why}." if why else "")


def _iso_day(d) -> str:
    """"Wed 2026-10-07": the one date the model reads (PR-20). An owner's
    M/D/YY (a trainee's "until 12/1/26") reads the same way."""
    text = str(d or "").strip()
    for fmt, part in (("%Y-%m-%d", text[:10]), ("%m/%d/%y", text), ("%m/%d/%Y", text)):
        try:
            x = datetime.strptime(part, fmt)
        except ValueError:
            continue
        return f"{x.strftime('%a')} {x.strftime('%Y-%m-%d')}"
    return text


def _iso_days(dates) -> str:
    named = [_iso_day(d) for d in sorted({str(d)[:10] for d in (dates or []) if d})]
    return named[0] if len(named) == 1 else (", ".join(named[:-1]) + " and " + named[-1] if named else "")


def _acting_prompt_lines(c) -> list:
    """Who stands in as the manager, on which dates (E-13)."""
    bits = []
    for key, days in sorted(c.acting_managers.items()):
        if days:
            bits.append(f"{_display_name(c, key)} on " + ", ".join(_iso_day(d) for d in sorted(days)))
    return [_rule("no_manager", "Standing in as the manager (counts as the manager on the floor those days only): "
                  + "; ".join(bits) + ".")] if bits else []


def _role_window_text(c, fam, by_day) -> str:
    """"Bartender: from 3:00pm" / "Busser: until 9:30pm Sun-Wed, until
    11:30pm Thu-Sat" — one family's hours, days with the same hours named
    together."""
    groups = {}
    for day in DAYS:
        w = by_day.get(day)
        if not w:
            continue
        bits = []
        if w.get("earliest") is not None:
            bits.append(f"from {_fmt_minutes(w['earliest'])}")
        if w.get("latest") is not None:
            bits.append(f"until {_fmt_minutes(w['latest'] % (24 * 60))}")
        if bits:
            groups.setdefault(" ".join(bits), []).append(day)
    parts = [k + ("" if len(ds) == 7 else " " + "/".join(d[:3] for d in ds)) for k, ds in groups.items()]
    return f"{_family_label(c, fam)}: " + ", ".join(parts)


def _closer_prompt_lines(c) -> list:
    """The closers, by role (D-9): who may close each role, and what
    closing means."""
    bits = []
    for fam, keys in sorted(c.closers_by_role.items()):
        mins = int((c.close_mins or {}).get(fam, 0) or 0)
        # Each role's own close (closer_need, RULES-6): a role that stays to
        # the restaurant's close is told so; the rest close when their role
        # is done — a kitchen closer is not kept on to the bar's close.
        stays = bool(c.close_times) and fam not in (c.early_close_roles or set())
        bits.append(f"{_family_label(c, fam)}: " + ", ".join(sorted(_display_name(c, k) for k in keys))
                    + (f" (on until {mins} min after close)" if stays and mins else
                       " (on until close)" if stays else " (the last of the role to leave)"))
    return [_rule("keyholder_until_close",
                  "Closers, by role — every open day the last person of each of these roles to leave is one of its "
                  "closers: " + "; ".join(bits) + ". A closer is somebody chosen to close for their "
                  "role, not somebody with keys; other roles have no closer rule",
                  why="the owner chose who closes each role")] if bits else []


def _trainee_prompt_lines(c) -> list:
    """Who is training, for what, with whom (D-16)."""
    out = []
    for key, t in sorted(c.trainees.items()):
        who = _display_name(c, key)
        trainer = _display_name(c, c.key(t["trainer"])) if (t.get("trainer") or "").strip() else None
        out.append(f"  {who}: training as {t['target_role']}" + (f" until {_iso_day(t['until'])}" if t.get("until") else "")
                   + (f" with {trainer}" if trainer else "") + f" — schedule their {t['target_role']} shifts only "
                   f"alongside {trainer or 'a strong ' + t['target_role']}; a training shift does not count toward "
                   f"{t['target_role']} headcount or any floor.")
    return [_rule("trainee_unpaired", "TRAINEES:")] + out if out else []


def _minor_band_lines(c) -> list:
    """The limits the code checks for each age band on the roster (NS5 H4),
    once per band — who is in which band is in each person's ROSTER line."""
    out = []
    comp = c.compliance
    bands = sorted({c.minor_bands.get(c.key(n)) or "" for n in (c.roster_names or []) if c.key(n) in c.minors})
    for band in bands:
        br = minor_rules(band or None, c.jurisdiction)
        if br.get("earliest_start"):
            out.append(f"  Minors {band}: start no earlier than {br['earliest_start']}; finish by "
                       f"{br['latest_end_school']} ({br['latest_end_summer']} June 1 to Labor Day); at most "
                       f"{br['max_daily_school_day']:g}h on a school day and {br['max_weekly_school_week']:g}h in a "
                       f"school week, {br['max_daily_other_day']:g}h a day and {br['max_weekly_other_week']:g}h a week "
                       f"otherwise. Treat every weekday outside June 1 to Labor Day as a school day.")
        else:
            out.append(f"  Minors{(' ' + band) if band else ''}: finish by {comp.get('minor_latest_end')}, at most "
                       f"{float(comp.get('minor_max_daily_hours') or 8):g} hours a day.")
    return out


def _whose_limit(c, key, owner_words, default_words) -> str:
    """Why a limit holds, said as whose it is (PROMPT-10): the owner's only
    when the owner set it; a jurisdiction pack's starting value; else the
    code's default — never an invented owner rule."""
    by = getattr(c, "compliance_set", None)
    if by is None or by.get(key) == "owner":
        return owner_words
    if by.get(key) == "pack":
        return "the starting value from the restaurant's jurisdiction pack"
    return default_words


def prompt_block(c: Constraints, manager_plan: dict = None) -> str:
    """The rules the week is checked against, as the model reads them —
    the same facts the code checks afterwards, each line marked [HARD] or
    [SOFT] from the sweep's own sets (rule_tag, PR-5), a hard rule with why.
    Each person's own limits, windows, time off, certificates and band are
    their ROSTER line (labor, from person_facts — PR-33); this block says
    the rule once. `manager_plan` (schedule_skeleton.plan_manager_coverage)
    — when the managers' shifts were planned, the manager line points at
    them."""
    from labor import OVERTIME_THRESHOLD_HOURS as _OT
    comp = c.compliance
    lines = []
    if c.managers:
        _mg = ", ".join(sorted(f"{n} ({c.managers.get(c.key(n)) or 'manager'})" for n in (c.roster_names or [])
                               if c.key(n) in c.managers))
        # The same rule PRIORITIES 1a states, in the same rank: it used to
        # call itself "above every other rule" — above the availability and
        # time off PRIORITIES puts first (schedule audit 10/3/26 PR-1).
        lines.append(_rule("no_manager",
                           "NON-NEGOTIABLE — PRIORITIES 1a, the owner's highest staffing rule: at every minute anybody "
                           f"is on the schedule, at least ONE manager is on too. The managers: {_mg}."
                           + (" Somebody standing in as the manager counts on their dates (below)." if c.acting_managers else "")
                           + " It gives way only to a manager's own availability, time off and legal limits. Every "
                             "trading day has a manager from the first shift's start to the last shift's end — overlap "
                             "them so there is never a gap, not even a few minutes."
                           + (" Their shifts are already planned: MANAGER COVERAGE, at the top, is fixed."
                              if (manager_plan or {}).get("rows") else ""),
                           why="the owner's non-negotiable: somebody is always in charge of the floor"))
    if c.acting_managers:
        lines.extend(_acting_prompt_lines(c))
    lines.append(_rule("unavailable_day", "Availability: nobody works outside their AVAILABLE column — a weekday or "
                       "daypart they can't work, outside their hours that day, or on approved time off",
                       why="the person cannot be there"))
    lines.append(_rule("pending_time_off", "A day somebody has asked off that the owner has not decided (\"asked off\" "
                       "in AVAILABLE): avoid it if the day can be covered."))
    lines.append(_rule("role_not_held", "Roles: only a role in the person's CAN WORK",
                       why="anything else is a role nobody has trained them for"))
    lines.append(_rule("overlap", "No two shifts of one person overlap; two on one date (lunch, then dinner) are a "
                       "double, which is allowed"))
    if comp.get("min_rest_hours"):
        lines.append(_rule("rest_gap", f"At least {float(comp['min_rest_hours']):g} hours between the end of one shift and "
                           "the same person's next start — no closing then opening (except an owner or a manager, "
                           "who keeps their own turnaround)",
                           why="a short turnaround can be the owner's legal exposure under rest rules, and it puts an "
                               "opener on the floor without sleep"))
    if comp.get("max_shift_hours"):
        lines.append(_rule("shift_too_long", f"No shift longer than {float(comp['max_shift_hours']):g} hours, "
                           "except an owner's or a manager's: how long they stay is their own call"))
    if comp.get("min_shift_hours"):
        lines.append(_rule("shift_too_short", f"No shift shorter than {float(comp['min_shift_hours']):g} hours."))
    # One weekly limit per person — their MAX, which is the code's
    # max_hours (P-12, D-18): the ceiling, or their own maximum when the
    # owner set it — above the overtime line a ceiling, not their overtime
    # line (schedule re-audit 10/4/26 RULES-9). The literal 40 the prompt
    # used to state beside a person's own 45h is gone (PR-3); their ROSTER
    # line says "OT 40h" beside the maximum.
    _ceiling = float(comp.get("weekly_hours_ceiling") or DEFAULTS["weekly_hours_ceiling"])
    lines.append(_rule("over_max_hours",
                       "Nobody past their weekly maximum in a payroll week"
                       + (" (it starts on " + DAYS[c.week_start_day] + ")" if c.week_start_day else "")
                       + f": MAX in their ROSTER line — the {_ceiling:g}h ceiling, or their own maximum where the owner "
                         f"set one (above {float(_OT):g}h it is a ceiling, not their overtime line: past "
                         f"{float(_OT):g}h only when no teammate in the role has room). Hours already "
                         "published in the same payroll week count toward it",
                       why=_whose_limit(c, "weekly_hours_ceiling", "the most hours the owner allows anyone",
                                        "the default ceiling: the owner has set none")))
    # A payroll week that runs on into next week's days (E-10): next week's
    # draft schedules them inside the same overtime line.
    _tails = [c.bucket_tail(b) for b in sorted({c.bucket(d) for d in (c.week_dates or [])})]
    _tail = next((t for t in _tails if len(t) >= TAIL_RESERVE_MIN_DAYS), None)
    if _tail:
        _from = DAYS[c.week_start_day] if c.week_start_day else "Monday"
        lines.append(_rule("payroll_tail_full",
                           f"The payroll week that starts {_from} runs on into next week ({_iso_days(_tail)}), which "
                           f"next week's schedule fills from the same overtime line: leave each hourly person about "
                           f"{len(_tail)}/7 of their line free for those days (about {float(_OT) * len(_tail) / 7:.0f}h "
                           f"of {float(_OT):g}h)."))
    if comp.get("daily_ot_hours"):
        lines.append(_rule("daily_ot", f"Daily overtime starts at {float(comp['daily_ot_hours']):g} hours — avoid it."))
    if comp.get("meal_break_after_hours"):
        lines.append(_rule("meal_break", f"A shift over {float(comp['meal_break_after_hours']):g} hours includes a meal "
                           "break; leave room for it."))
    if comp.get("max_consecutive_days"):
        lines.append(_rule("long_run", f"Nobody works more than {int(comp['max_consecutive_days'])} days in a row, "
                           "counting days already published last week",
                           why=_whose_limit(c, "max_consecutive_days", "the owner's limit on days in a row",
                                            "the default limit on days in a row: the owner has set none")))
    if c.minors:
        lines.append(_rule("minor_late", "MINORS (marked minor in the ROSTER) — the limits the code checks, as "
                           "starting values, not legal advice", why="child-labor law is the owner's legal exposure"))
        lines.extend(_minor_band_lines(c))
    if c.salaried:
        # Each one's weekly cap — their own, the restaurant's, or the
        # default — is their MAX in the ROSTER (E-17).
        lines.append(_rule("over_max_hours", "Salaried people (marked salaried in the ROSTER, the same pay whatever "
                           "the hours): at most their weekly cap; no overtime and no hourly ceiling for them, and their "
                           "hours are not spent from the hourly hours budget. Never move an hourly person's shift onto "
                           "them to save overtime"))
    if c.role_requirements:
        lines.append(_rule("missing_cert",
                           "Certifications by role: " + "; ".join(f"{r} needs {', '.join(sorted(v))}"
                                                                  for r, v in sorted(c.role_requirements.items()))
                           + " — only schedule people who hold them (CAN WORK leaves the role out for anyone who "
                             "doesn't)", why="the owner requires it to work the role"))
    if comp.get("keyholder_until_close", True) and c.closers_by_role:
        lines.extend(_closer_prompt_lines(c))
    lines.append(_rule("nobody_at_close", "Somebody is on until close every trading day."))
    if c.close_mins and c.close_times:
        lines.append(_rule("ends_before_role_close", "Stays after close: "
                           + "; ".join(f"the last {_family_label(c, fam)} until {m} min after close"
                                       for fam, m in sorted(c.close_mins.items()) if m) + "."))
    if c.trainees:
        lines.extend(_trainee_prompt_lines(c))
    if c.role_floors:
        lines.append(_rule("coverage_floor", "The staffing floors below are hard: a day under a floor is flagged to the "
                           "owner", why="the fewest people the owner says a shift can run with"))
    # The hours each role works and the most of it on at once, by the
    # owner's RESTAURANT HOURS & SHIFT RULES (PROMPT-1).
    if getattr(c, "role_windows", None):
        lines.append(_rule("role_window", "Hours by role — no shift of the role starts before or ends after them: "
                           + "; ".join(_role_window_text(c, fam, by_day)
                                       for fam, by_day in sorted(c.role_windows.items())),
                           why="the owner's RESTAURANT HOURS & SHIFT RULES"))
    if getattr(c, "role_caps", None):
        bits = []
        for fam, items in sorted(c.role_caps.items()):
            for cap in items:
                when = ("" if not cap.get("days") else " on " + "/".join(d[:3] for d in cap["days"])) + \
                    {"morning": " at lunch/day", "night": " at dinner/night"}.get(cap.get("daypart") or "", "")
                bits.append(f"{_family_label(c, fam)} at most {int(cap['max'])} on at once{when}")
        lines.append(_rule("over_role_max", "Most of a role on at once: " + "; ".join(bits) + "."))
    lines.append(_rule("under_min_hours", "Each person's minimum hours (MIN in the ROSTER; a full-timer's is the "
                       "full-time line): fill from the people below theirs first."))
    if c.stations:
        import kitchen_stations as _ks
        lines.extend(("- [SOFT] " + ln[2:]) if ln.startswith("- ") else ("- [SOFT] " + ln)
                     for ln in _ks.prompt_lines(c.stations, people=False))
    block = ("\n\nRULES THE SCHEDULE IS CHECKED AGAINST — the code checks every one of these after you write the "
             "week; each is [HARD] or [SOFT] as the standing instructions say:\n" + "\n".join(lines))
    pack = comp.get("_pack") or {}
    if pack.get("applied"):
        # Starting values from a pack, never "the law": the Illinois pack's
        # 10-hour rest is the Chicago ordinance's, not the state's (NS5 L13).
        block += (f"\n  Starting values from the {pack.get('label')} pack (set in Cavnar AI, not a statement of the "
                  "law): " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in pack["applied"].items()) + ".")
    # Each role's floor per weekday, the days with the same floor said
    # together ("1 lunch/day, 1 dinner/night on Mon/Tue/Wed/Fri/Sun; 1
    # lunch/day, 2 dinner/night on Thu/Sat") — the hours notes' floors are
    # set per day, and seven "Monday: 1 morning, 1 night" groups said one
    # thing (schedule re-audit 10/4/26 PROMPT-1).
    floors = []
    for role, spec in (c.role_floors or {}).items():
        groups = {}
        for d in DAYS:
            n_m, n_n = floor_for(c.role_floors, role, d, "morning"), floor_for(c.role_floors, role, d, "night")
            if n_m or n_n:
                groups.setdefault((n_m, n_n), []).append(d)
        if not groups:
            continue
        bits = []
        for (n_m, n_n), ds in sorted(groups.items(), key=lambda kv: (-len(kv[1]), DAYS.index(kv[1][0]))):
            what = ", ".join(x for x in ((f"{n_m} lunch/day" if n_m else ""), (f"{n_n} dinner/night" if n_n else ""))
                             if x)
            bits.append(f"{what} " + ("every day" if len(ds) == 7 else "on " + "/".join(d[:3] for d in ds)))
        floors.append(f"  {role}: at least " + "; ".join(bits))
    if floors:
        block += (f"\n\nSTAFFING FLOORS BY ROLE AND DAYPART {rule_tag('coverage_floor')} (never below these, whatever "
                  "the hours ceiling says):\n" + "\n".join(floors))
    # The owner's start and end rules (schedule audit 10/3/26 L-33): the
    # code retimes the draft to them afterwards, so the model is told them.
    times = []
    grouped = {}
    for (role, day, part), spec in sorted((getattr(c, "role_times", None) or {}).items(),
                                          key=lambda kv: (kv[0][0], DAYS.index(kv[0][1]) if kv[0][1] in DAYS else 7,
                                                          kv[0][2])):
        bits = [f"{edge} at {_fmt_minutes(spec[edge])}" for edge in ("start", "end") if spec.get(edge) is not None]
        if bits:
            grouped.setdefault((role, part, " and ".join(bits)), []).append(day)
    for (role, part, bits), ds in grouped.items():
        # The days with the same rule said once (PROMPT-1).
        when = "every day" if len(ds) == 7 else "on " + "/".join(d[:3] for d in ds)
        times.append(f"  {role.title()} {when} at {'lunch/day' if part == 'morning' else 'dinner/night'}: {bits}")
    if times:
        block += (f"\n\nSTARTS AND ENDS BY ROLE {rule_tag('role_time')} (the owner's rules — every shift of that role "
                  "on that daypart):\n" + "\n".join(times))
    return block


# ── each person's facts, for the ROSTER (schedule audit 10/3/26 PR-33) ─────
#
# What the checker holds each person to, as plain data — the one source of
# the ROSTER table's availability and hours columns (labor._roster_people
# adds the scores, experience, usual pattern, wants and reliability). The
# same facts used to reach the model in five blocks (PER-PERSON LIMITS,
# EMPLOYEE AVAILABILITY, the salaried line, the minors lines, the closers
# line), with "Xh already published in this payroll week" summed over every
# payroll week the schedule week touches (E-9, D-19).

def person_facts(c: Constraints, names=None) -> dict:
    """{display name: facts} for `names` (default the roster): manager role,
    acting dates, the week's limits (min, the code's max, the overtime
    line, salaried cap, full-time), hours already published per payroll
    week this week shares (with the dates here and the room left before
    overtime), availability (weekdays off, daypart-only days, windows,
    approved time off, days held off by a note, days asked off), minor band,
    certificates, the roles they can be scheduled in by the code's own test
    (`holds` families, a certificate their role needs left out), closer
    families, training. Plain data: lists and numbers, ISO dates."""
    from labor import OVERTIME_THRESHOLD_HOURS as _OT
    out = {}
    buckets = {}
    for d in c.week_dates or []:
        buckets.setdefault(c.bucket(d), []).append(d)
    fams_by_person = {}
    for fam, keys in (c.closers_by_role or {}).items():
        for k in keys or ():
            fams_by_person.setdefault(k, []).append(_family_label(c, fam))
    for name in (names if names is not None else (c.roster_names or [])):
        name = str(name or "").strip()
        if not name:
            continue
        key = c.key(name)
        lim = c.hours_limits.get(key) or (None, None)
        salaried = c.is_salaried(name)
        f = {"manager": c.managers.get(key), "acting": sorted(c.acting_managers.get(key) or ()),
             "salaried": salaried, "cap": float(c.salaried_limit(name)) if salaried else None,
             "min": float(lim[0]) if lim[0] else None,
             "max": float(c.max_hours(name)) if c.max_hours(name) else None, "own_max": bool(lim[1]),
             "ceiling": float(c.compliance.get("weekly_hours_ceiling") or DEFAULTS["weekly_hours_ceiling"]),
             "ot": None if salaried else float(overtime_line(c, name) or _OT),
             "employment": c.employment.get(key), "full_time_default": key in c.full_time_default,
             "minor": (c.minor_bands.get(key) or "minor") if key in c.minors else None}
        carried = []
        base = c.base_hours.get(key) or {}
        for b, here in sorted(buckets.items()):
            h = float(base.get(b) or 0)
            if not b or h <= 0:
                continue
            try:
                start = datetime.strptime(str(b)[:10], "%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            end = (start + timedelta(days=6)).strftime("%Y-%m-%d")
            line = f["max"] if salaried else f["ot"]
            carried.append({"bucket": b, "start": start.strftime("%Y-%m-%d"), "end": end, "hours": round(h, 1),
                            "dates_here": sorted(here), "room": round(max(0.0, (line or 0) - h), 1) if line else None,
                            "tail": c.bucket_tail(b)})
        f["carried"] = carried
        dp = c.daypart_avail.get(key) or {}
        f["off_days"] = [d for d in DAYS if d in (c.unavailable_days.get(key) or set()) or dp.get(d) == "off"]
        f["daypart_only"] = {d: dp[d] for d in DAYS if dp.get(d) in ("morning", "night") and d not in f["off_days"]}
        f["windows"] = {d: list(w) for d, w in (c.time_windows.get(key) or {}).items() if d not in f["off_days"]}
        time_off, note_off, other_off = [], [], []
        for d, why in sorted((c.blocked_dates.get(key) or {}).items()):
            if d not in (c.week_dates or [d]):
                continue
            why = str(why or "")
            if why.startswith(LABELS["approved_time_off"]):
                time_off.append(d)
            elif "note" in why.lower():
                note_off.append(d)
            else:
                other_off.append(d)
        f["time_off"], f["note_off"], f["unavailable_dates"] = time_off, note_off, other_off
        f["asked_off"] = sorted(d for d in (c.pending_off.get(key) or ()) if d in (c.week_dates or [d]))
        parts = []
        for d, ps in sorted((c.blocked_parts.get(key) or {}).items()):
            for p in ps or []:
                if p.get("daypart"):
                    off = "no lunch/day shift" if p["daypart"] == "morning" else "no dinner/night shift"
                else:
                    off = (f"off from {_fmt_minutes(p['from'] % 1440)}" if p.get("from") is not None else "off")
                    off += (f" until {_fmt_minutes(p['until'] % 1440)}" if p.get("until") is not None else " to close")
                parts.append({"date": d, "words": off})
        f["parts"] = parts
        f["certs"] = sorted(c.certifications.get(key) or ())
        roles, lacking = [], {}
        for r in sorted(c.known_roles.get(key) or (), key=str.lower):
            need = c.role_requirements.get(r) or set()
            missing = sorted(need - set(c.certifications.get(key) or ()))
            if missing:
                lacking[c.role_names.get(r, r)] = missing
                continue
            roles.append(c.role_names.get(r, r))
        f["roles"], f["roles_lacking_cert"] = roles, lacking
        f["closes_for"] = sorted(fams_by_person.get(key) or [])
        t = c.trainees.get(key)
        f["trainee"] = ({"target_role": t.get("target_role"), "until": t.get("until"),
                         "trainer": _display_name(c, t["trainer"].strip().lower()) if t.get("trainer") else None}
                        if t else None)
        skills = (c.stations or {}).get("skills") or {}
        f["stations"] = list(skills.get(name) or skills.get(key) or [])
        # The roles they hold beyond their roster role (people.held_roles —
        # trained or promoted, D-15): part of CAN WORK (PR-21).
        f["held"] = sorted(c.role_names.get(r, r) for r in (c.held_roles.get(key) or ()))
        out[name] = f
    return out


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
    """The most hours in a payroll week code gives a person: the legal
    overtime line (40h, labor.OVERTIME_THRESHOLD_HOURS, or the line passed
    in), or their own maximum when the owner set it LOWER. An owner's
    maximum above the line is a ceiling (max_hours), not a line: a cook
    allowed 45h was drafted to 45h and the rebalance left the five hours of
    overtime while a same-role teammate had room (schedule re-audit 10/4/26
    RULES-9; owner, 10/2/26 — a draft never creates overtime a teammate
    could take). The rebalance moves what is past the line wherever a
    teammate can take it, and keeps it, up to the maximum, only where
    nobody can. A salaried person owes no overtime: their line is the
    salaried cap (Constraints.salaried_limit, E-17)."""
    from labor import OVERTIME_THRESHOLD_HOURS
    if c.is_salaried(name):
        return c.salaried_limit(name)
    ot = float(line or OVERTIME_THRESHOLD_HOURS)
    mx = c.max_hours(name)
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

    def __init__(self, rows, c, upto=TIER_COVERAGE, soft_upto=None, max_sweeps=OT_MAX_SWEEPS, ignore=(),
                 tier=None):
        self.c = c
        # The rank the pass serves (its engine stage's): the soft rules of
        # every tier above it — the owner's standing rule, the stay after
        # close — are held as the engine holds the stage (_judge), so a move
        # that breaks one is refused here, not the whole stage after
        # (schedule re-audit 10/4/26 RULES-4).
        self.tier = tier
        self.rows = [dict(r) for r in (rows or [])]
        # Each trial re-sweeps only the people and dates it moved (P-37).
        self.sweep = IncrementalSweep(c)
        self.viols = self.sweep.violations(self.rows)
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
        viols = self.sweep.violations(trial)
        after = breach_profile(trial, self.c, viols=viols)
        upto = self.upto if upto is None else upto
        soft = self.soft_upto if soft_upto == "same" else soft_upto
        worse = regressions(self.prof, after, upto=upto, hard_only=True)
        if self.tier is not None and self.tier > TIER_PERSON:
            worse += [w for w in regressions(self.prof, after, upto=self.tier - 1, hard_only=False)
                      if w["id"][0] not in HARD]
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
                  ignore=("payroll_tail_full",), tier=TIER_OVERTIME)
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
        if not e.get("salaried"):
            return "no overtime"
        # "you set" only for a limit the owner set (RULES-5).
        return (f"inside the {e['limit']:g}h you set for them" if c.salaried_cap_set(name_of(e["low"]))
                else f"inside {e['limit']:g}h")

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
    """The deterministic backstop for the closer rule, per role (schedule
    audit 10/3/26 D-9): on a trading day where a role with closers has no
    closer staying until close plus the role's minutes, or a teammate
    outlasting its closer, a closer of that role already working that night
    runs on to close (and past the last of the role to leave). The model
    reads "Close: 12:00am Thu-Sat" and still ends the night at 11:30pm.

    Each extension is asked of the person first (Constraints.can_add: rest,
    the shift-length rule, a minor's limits, the hours maximum, the overtime
    line) and kept only when the whole week's breach profile shows nothing
    new or worse at or above the coverage tier (regressions — it used to
    compare (row index, kind) sets, blind to a new breach pinned to an
    existing row) and the role's closer breach got better. A pinned row
    ("_pinned") is never re-timed; a day shift is not stretched into the
    night. Returns {rows, extended: [{index, employee, from, to, day, kind,
    reason}]}."""
    rows = [dict(r) for r in rows]
    extended = []
    if not c.closers_by_role or not c.compliance.get("keyholder_until_close", True):
        return {"rows": rows, "extended": extended}
    # Each trial re-sweeps only the person and the date it moved (P-37).
    sweep = IncrementalSweep(c)
    before = sweep.profile(rows)
    sweeps = 0
    for d in sorted({r.get("date") for r in rows if r.get("date")}):
        if d in (c.closed_dates or set()) or (editable is not None and d not in editable) or sweeps >= max_sweeps:
            continue
        try:
            day = datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except (ValueError, TypeError):
            continue
        for fam in sorted(c.closers_by_role):
            bid = ("keyholder_until_close", d, fam)
            if bid not in before["by_id"] or sweeps >= max_sweeps:
                continue
            items = [(i, r) for i, r in enumerate(rows) if r.get("date") == d and (r.get("employee") or "").strip()]
            mine = [(end_minutes(r), i, r) for i, r in items
                    if c.family(r.get("role")) == fam and not c.training_row(r) and end_minutes(r) is not None]
            if not mine:
                continue
            last_e = max(e for e, _i, _r in mine)
            # The role's own target (closer_need, RULES-6): the close for a
            # role that stays to it, else the last of the role out.
            need, _until = closer_need(c, fam, day, last_e)
            target = max(need, last_e)
            closers = c.closers_by_role.get(fam) or set()
            cands = sorted(((e, i, r) for e, i, r in mine if c.key(r.get("employee")) in closers
                            and not r.get("_pinned") and e >= target - 3 * 60 and e < target - 0.5),
                           key=lambda t: -t[0])
            for e, i, r in cands:
                if sweeps >= max_sweeps:
                    break
                s0 = parse_minutes(r.get("shift_start", ""))
                if s0 is None:
                    continue
                new = dict(r, shift_end=_fmt_minutes(target % (24 * 60)),
                           scheduled_hours=str(round((target - s0) / 60.0, 2)).rstrip("0").rstrip("."))
                ok, _why = c.can_add(new, [x for j, x in enumerate(rows) if j != i], line=line)
                sweeps += 1
                if not ok:
                    continue
                trial = [dict(x) for x in rows]
                trial[i] = new
                after = sweep.profile(trial)
                if regressions(before, after, upto=TIER_COVERAGE) or \
                        after["by_id"].get(bid, 0.0) >= before["by_id"].get(bid, 0.0):
                    continue
                name = (r.get("employee") or "").strip()
                extended.append({"index": i, "employee": name, "day": day, "from": r.get("shift_end"),
                                 "to": new["shift_end"], "kind": "close",
                                 "reason": f"Kept {name} on until close ({_fmt_minutes(target % (24 * 60))}) on {day} — "
                                           f"the last {_family_label(c, fam)} out is one of your closers."})
                rows, before = trial, after
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
    rep = _Repair(rows, c, upto=TIER_COVERAGE, soft_upto=TIER_OVERTIME, max_sweeps=max_sweeps,
                  tier=TIER_PERSON)
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
                    fixes.append({"index": i, "from": name + " " + hours_label(r),
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
                fixes.append({"index": i, "from": name + " " + hours_label(r),
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
    rep = _Repair(rows, c, upto=TIER_COVERAGE, soft_upto=TIER_OVERTIME, max_sweeps=max_sweeps,
                  tier=TIER_MANAGER)
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
                                 "from": hours_label(was), "to": now, "kind": "manager",
                                 "reason": f"No manager was on {day} {span_txt}: {nm} stays on to cover it{ot}."})
            else:
                added.append({"index": idx, "employee": nm, "day": day, "date": d, "from": NO_ONE, "to": now,
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
    rep = _Repair(rows, c, upto=TIER_COVERAGE, soft_upto=TIER_OVERTIME, max_sweeps=max_sweeps,
                  tier=TIER_MIN_HOURS)
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
