"""
staff_settings.py — who is on the roster and the scheduling facts about them.

The roster used to be whoever appeared in the shift history: a new hire with
no shift could not be scheduled and a server who left in March was offered to
the model every week, because only hand-typed names could be removed. This
module owns one roster (shift history ∪ hand-added names − deactivated) and
the per-person facts a schedule has to respect: full or part time, an hours
envelope, which dayparts they can work on each day, and whether they are a
minor. Pairings ("works well with", "keep apart") live here too.

Everything is keyed by employee name, like every other staff table — POS
data has no stable id.
"""
import json
import re

from models import get_conn, DB_PATH

EMPLOYMENT_TYPES = ("full", "part")
DAYPART_CHOICES = ("any", "morning", "night", "off")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
PAIR_KINDS = ("prefer", "avoid")


class StaffSettingsError(ValueError):
    pass


# ── settings ───────────────────────────────────────────────────────────────

CERTIFICATIONS = ("alcohol", "food_handler", "manager", "keyholder", "trainer", "allergen", "first_aid",
                  "food_protection_manager")
# How each certificate reads to the owner. "manager" used to read as plain
# "Manager" beside "Food handler", so a line cook holding a Certified Food
# Protection Manager card (required on site in Chicago) was given it — and
# the certificate makes its holder a manager the every-minute rule counts
# (schedule audit 10/3/26 E-15). It means a floor manager, somebody who can
# run the shift; the food-safety card is its own certificate and never
# makes anybody a manager. The stored keys stay what they were.
CERTIFICATION_LABELS = {
    "alcohol": "Alcohol", "food_handler": "Food handler", "manager": "Floor manager (can run the shift)",
    "keyholder": "Keyholder", "trainer": "Trainer", "allergen": "Allergen", "first_aid": "First aid",
    "food_protection_manager": "Food protection manager (food safety)",
}
# The certificate that makes its holder a floor manager (schedule_rules
# build_constraints); FOOD_SAFETY_CERT never does.
FLOOR_MANAGER_CERT = "manager"
FOOD_SAFETY_CERT = "food_protection_manager"

# Caps on the per-person lists below, so a client loop cannot store
# thousands of entries on one person.
MAX_STANDING_SHIFTS = 21
MAX_ACTING_RANGES = 20
MAX_CLOSES_FOR = 6
STANDING_SHIFT_MAX_HOURS = 16


def _json_field(r, key, default):
    try:
        return json.loads(r[key] or "null") or default
    except Exception:
        return default


def _row(r):
    try:
        avail = json.loads(r["daypart_availability"] or "{}") or {}
    except Exception:
        avail = {}
    keys = r.keys()
    windows = _json_field(r, "time_windows", {}) if "time_windows" in keys else {}
    certs = _json_field(r, "certifications", []) if "certifications" in keys else []
    prefs = _json_field(r, "preferred_dayparts", []) if "preferred_dayparts" in keys else []
    return {
        "time_windows": {d: w for d, w in (windows or {}).items() if d in DAYS and isinstance(w, dict)},
        "certifications": [c for c in (certs or []) if isinstance(c, str)],
        "preferred_dayparts": [p for p in (prefs or []) if p in ("morning", "night")],
        "desired_hours": (r["desired_hours"] if "desired_hours" in keys else None),
        # The owner's word that this person knows the job. Shift history is
        # a rolling upload window, so a ten-year server can read as "new"
        # for as long as the window is short (experience_balance).
        "experienced": bool(r["experienced"]) if "experienced" in keys and r["experienced"] is not None else False,
        "employee_name": r["employee_name"],
        "active": bool(r["active"]) if r["active"] is not None else True,
        "employment_type": r["employment_type"],
        "min_hours": r["min_hours"],
        "max_hours": r["max_hours"],
        "daypart_availability": {d: v for d, v in avail.items() if d in DAYS and v in DAYPART_CHOICES},
        "is_minor": bool(r["is_minor"]) or bool(r["minor_age_band"] if "minor_age_band" in keys else None),
        # Which minor rule table applies (schedule_rules.MINOR_BANDS); None
        # for an adult, or a minor whose age band was never set.
        "minor_age_band": (r["minor_age_band"] if "minor_age_band" in keys else None) or None,
        # Who runs the floor (schedule audit 10/3/26 P-7, E-14): True or
        # False as the owner set it; None is automatic (their role, a held
        # role or a manager role worked in the last eight weeks, or the
        # floor manager certificate — schedule_rules.manager_basis).
        "floor_manager": (None if "floor_manager" not in keys or r["floor_manager"] is None
                          else bool(r["floor_manager"])),
        # An Owner-role person paid by the hour: held to the overtime line
        # like anyone hourly. Unset, an Owner role is salaried-style (E-12).
        "paid_hourly": bool(r["paid_hourly"]) if "paid_hourly" in keys and r["paid_hourly"] is not None else False,
        # Dates they stand in as the manager on duty (E-13): [{from, until, note}].
        "acting_manager": _json_list(r, "acting_manager", keys),
        # The days and hours they always work (D-5): [{day, start, end, role}].
        "standing_shifts": _json_list(r, "standing_shifts", keys),
        # In training (D-16): {target_role, trainer, from, until} or None.
        "trainee": (_json_field(r, "trainee", None) if "trainee" in keys else None) or None,
        # The roles they close for when marked to close (D-9); empty is
        # their own role.
        "closes_for": [x for x in _json_list(r, "closes_for", keys) if isinstance(x, str)],
        "updated_by": r["updated_by"],
        "updated_at": r["updated_at"],
    }


def _json_list(r, key, keys) -> list:
    if key not in keys:
        return []
    v = _json_field(r, key, [])
    return list(v) if isinstance(v, list) else []


def name_key(name) -> str:
    """One person, however the name was typed: 'Maria G.', 'maria g.' and
    'MARIA G. ' are the same key (MOD-EMP-2). Every staff table is keyed by a
    free-text name, so this is what makes a name mean one person."""
    return " ".join(str(name or "").split()).casefold()


def clean_phone(raw):
    """(phone or "" , error or None). A staff phone number is digits with
    the usual punctuation, 7-15 digits; anything else — "<script>" included —
    is refused rather than stored and later texted (MOD-EMP-4)."""
    import re
    value = " ".join(str(raw or "").split())
    if not value:
        return "", None
    if not re.fullmatch(r"[0-9+()\-. ]+", value) or not 7 <= len(re.sub(r"\D", "", value)) <= 15:
        return None, "That doesn't look like a phone number."
    return value, None


def for_name(restaurant_id, name, db_path=DB_PATH) -> dict:
    """This person's settings whatever case their name is in, or {}."""
    key = name_key(name)
    for n, st in get_all(restaurant_id, db_path=db_path).items():
        if name_key(n) == key:
            return st
    return {}


def get_all(restaurant_id, db_path=DB_PATH) -> dict:
    """{employee_name: settings} for everyone with a row."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_settings WHERE restaurant_id=?", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return {r["employee_name"]: _row(r) for r in rows}


def _clean_windows(raw):
    from schedule_rules import parse_minutes, _OVERNIGHT_LATEST_BEFORE
    if not isinstance(raw, dict):
        raise StaffSettingsError("time windows are a map of weekday to {earliest, latest}")
    out = {}
    for d, w in raw.items():
        d = str(d).strip().capitalize()
        if d not in DAYS or not isinstance(w, dict):
            continue
        lo, hi = (w.get("earliest") or "").strip(), (w.get("latest") or "").strip()
        if lo and parse_minutes(lo) is None:
            raise StaffSettingsError(f"{d}: '{lo}' is not a time")
        if hi and parse_minutes(hi) is None:
            raise StaffSettingsError(f"{d}: '{hi}' is not a time")
        # A latest before the earliest is a window past midnight ("5pm to
        # 1am" — SCHED-13, read that way by schedule_rules.window_allows),
        # as long as it ends in the small hours; "9pm to 10am" is a typo.
        if lo and hi and parse_minutes(lo) >= parse_minutes(hi) and \
                not (parse_minutes(hi) < _OVERNIGHT_LATEST_BEFORE < parse_minutes(lo)):
            raise StaffSettingsError(f"{d}: the window ends before it starts")
        if lo or hi:
            out[d] = {"earliest": lo or None, "latest": hi or None}
            # Dates the window holds between ("not before 5pm on Tuesdays
            # until 12/15/26" — employee audit M5); read per week by
            # schedule_rules.window_holds.
            f, u = _clean_bound(w.get("from"), d), _clean_bound(w.get("until"), d)
            if f and u and f > u:
                raise StaffSettingsError(f"{d}: the window ends before it starts")
            if f:
                out[d]["from"] = f
            if u:
                out[d]["until"] = u
    return out


def _keep_window_dates(raw, cleaned, stored) -> dict:
    """An editor that knows nothing of a window's dates (the owner's iOS
    roster sends {earliest, latest} only) must not turn "until 12/15" into
    for good: a day sent with the same times and no from/until keys keeps
    the dates on file. Sending the keys, even empty, sets them."""
    out = {}
    for d, w in cleaned.items():
        sent = next((v for k, v in (raw or {}).items() if str(k).strip().capitalize() == d), {}) or {}
        old = stored.get(d) or {}
        if "from" not in sent and "until" not in sent and \
                (old.get("earliest"), old.get("latest")) == (w.get("earliest"), w.get("latest")):
            w = dict(w, **{k: old[k] for k in ("from", "until") if old.get(k)})
        out[d] = w
    return out


def _clean_bound(raw, day) -> str:
    """An iso date ("2026-12-15") or None; anything else is refused."""
    from datetime import datetime
    v = str(raw or "").strip()[:10]
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise StaffSettingsError(f"{day}: '{raw}' is not a date (YYYY-MM-DD)")


def _flag(v) -> bool:
    """bool("false") is True; a client sending strings would have switched
    a flag ON by asking for off."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


# ── the owner's per-person scheduling facts (schedule audit 10/3/26 F1) ────
#
# Who runs the floor, who stands in as the manager on given dates, the days
# and hours someone always works, who is in training and for what, which
# roles a closer closes for. Each is validated here, stored on the
# person's staff_settings row, recorded in the change history like every
# other roster field, and read into the rules by
# schedule_rules.build_constraints.

AUTO = "auto"           # floor_manager: back to automatic


def _clean_floor_manager(v):
    """1, 0, or AUTO (clear: automatic) for what a client sent."""
    if isinstance(v, bool):
        return 1 if v else 0
    s = str(v).strip().lower()
    if s in ("", "auto", "automatic", "default", "null", "none"):
        return AUTO
    if s in ("1", "true", "yes", "on"):
        return 1
    if s in ("0", "false", "no", "off"):
        return 0
    raise StaffSettingsError("floor manager is yes, no or automatic")


def _clean_date(raw, what) -> str:
    """An ISO date from ISO or M/D/YY input; raises when it is not a date."""
    from models import _iso_or_none
    iso = _iso_or_none(raw)
    if not iso:
        raise StaffSettingsError(f"{what}: '{raw}' isn't a date — use M/D/YY")
    return iso


def _clean_acting(raw) -> list:
    """[{from, until, note}] — the date ranges somebody stands in as the
    manager on duty, sorted, each at most a year long."""
    from datetime import date as _d
    if not isinstance(raw, list):
        raise StaffSettingsError("acting manager dates are a list of {from, until}")
    if len(raw) > MAX_ACTING_RANGES:
        raise StaffSettingsError(f"at most {MAX_ACTING_RANGES} acting-manager date ranges")
    out = []
    for e in raw:
        if not isinstance(e, dict):
            raise StaffSettingsError("acting manager dates are a list of {from, until}")
        f = _clean_date(e.get("from"), "acting manager from")
        u = _clean_date(e.get("until") or e.get("from"), "acting manager until")
        if f > u:
            raise StaffSettingsError("acting manager: the end date is before the start date")
        if (_d.fromisoformat(u) - _d.fromisoformat(f)).days > 366:
            raise StaffSettingsError("acting manager: a date range is at most a year")
        entry = {"from": f, "until": u}
        note = " ".join(str(e.get("note") or "").split())[:120]
        if note:
            entry["note"] = note
        if entry not in out:
            out.append(entry)
    return sorted(out, key=lambda x: (x["from"], x["until"]))


def _clean_standing(raw) -> list:
    """[{day, start, end, role}] — the shifts somebody always works, one
    weekday each (D-5: the owners' and managers' real floor days). Times in
    the house form ("10:00am"); an end at or before the start crosses
    midnight; two on the same day may not overlap."""
    from schedule_rules import parse_minutes, _fmt_minutes
    if not isinstance(raw, list):
        raise StaffSettingsError("standing shifts are a list of {day, start, end}")
    if len(raw) > MAX_STANDING_SHIFTS:
        raise StaffSettingsError(f"at most {MAX_STANDING_SHIFTS} standing shifts")
    out, spans = [], {}
    for e in raw:
        if not isinstance(e, dict):
            raise StaffSettingsError("standing shifts are a list of {day, start, end}")
        day = str(e.get("day") or "").strip().capitalize()
        if day not in DAYS:
            raise StaffSettingsError(f"'{e.get('day')}' is not a weekday")
        s, t = parse_minutes(str(e.get("start") or "")), parse_minutes(str(e.get("end") or ""))
        if s is None or t is None:
            raise StaffSettingsError(f"{day}: give the start and end as times, like 10:00am and 6:00pm")
        end = t if t > s else t + 24 * 60
        if not 60 <= end - s <= STANDING_SHIFT_MAX_HOURS * 60:
            raise StaffSettingsError(f"{day}: a standing shift runs 1 to {STANDING_SHIFT_MAX_HOURS} hours")
        for os_, oe in spans.get(day, ()):
            if s < oe and os_ < end:
                raise StaffSettingsError(f"{day}: two standing shifts overlap")
        spans.setdefault(day, []).append((s, end))
        role = " ".join(str(e.get("role") or "").split())[:60]
        entry = {"day": day, "start": _fmt_minutes(s), "end": _fmt_minutes(t), "role": role or None}
        # Optional dates it holds between ("Tuesdays until 12/15/26").
        f = _clean_date(e.get("from"), f"{day} from") if e.get("from") else None
        u = _clean_date(e.get("until"), f"{day} until") if e.get("until") else None
        if f and u and f > u:
            raise StaffSettingsError(f"{day}: the standing shift ends before it starts")
        if f:
            entry["from"] = f
        if u:
            entry["until"] = u
        # Only on the days an event series plays (owner, 10/5/26: Erik works
        # every Sunday the Bears play, and only those): its slug, resolved
        # against the event calendar week by week (schedule_rules
        # ._event_standing).
        if e.get("when_event"):
            slug = str(e.get("when_event")).strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,79}", slug):
                raise StaffSettingsError(f"{day}: '{e.get('when_event')}' is not an event calendar")
            entry["when_event"] = slug
        out.append(entry)
    return sorted(out, key=lambda x: (DAYS.index(x["day"]), parse_minutes(x["start"]) or 0))


def _clean_trainee(raw, today=None):
    """{target_role, trainer, from, until} or None (not in training). The
    role they are learning and the date training ends are required: a
    trainee with no end stayed one for good, and the headcount never came
    back (D-16)."""
    if raw in (None, False, "", {}, []):
        return None
    if not isinstance(raw, dict):
        raise StaffSettingsError("training is {target_role, trainer, until}")
    role = " ".join(str(raw.get("target_role") or raw.get("role") or "").split())[:60]
    if not role:
        raise StaffSettingsError("training: name the role they're learning")
    until = _clean_date(raw.get("until"), "training ends")
    if today is not None and until < today.isoformat():
        raise StaffSettingsError("training: that end date has passed")
    out = {"target_role": role, "until": until}
    if raw.get("from"):
        f = _clean_date(raw.get("from"), "training starts")
        if f > until:
            raise StaffSettingsError("training: the end date is before the start date")
        out["from"] = f
    trainer = " ".join(str(raw.get("trainer") or "").split())[:120]
    if trainer:
        out["trainer"] = trainer
    return out


def _clean_closes_for(raw) -> list:
    """The roles a closer closes for ("Bartender", "Server"); empty means
    their own role (D-9)."""
    if not isinstance(raw, list):
        raise StaffSettingsError("closes for is a list of roles")
    out = []
    for x in raw:
        r = " ".join(str(x or "").split())[:60]
        if r and r.casefold() not in {o.casefold() for o in out}:
            out.append(r)
    if len(out) > MAX_CLOSES_FOR:
        raise StaffSettingsError(f"a closer closes for at most {MAX_CLOSES_FOR} roles")
    return out


def upsert(restaurant_id, employee_name, active=None, employment_type=None, min_hours=None,
           max_hours=None, daypart_availability=None, is_minor=None, updated_by=None,
           time_windows=None, certifications=None, preferred_dayparts=None, desired_hours=None,
           experienced=None, minor_age_band=None, floor_manager=None, paid_hourly=None,
           acting_manager=None, standing_shifts=None, trainee=None, closes_for=None,
           db_path=DB_PATH) -> dict:
    """Set any subset of one person's facts. Unset arguments keep their
    stored value; the caller passes only what changed.

    `minor_age_band` is "14-15", "16-17" or "" (clear). Setting a band marks
    the person a minor; switching is_minor off clears the band (NS5 H4).

    The owner's scheduling facts (schedule audit 10/3/26 F1): `floor_manager`
    True / False / "auto"; `paid_hourly` (an Owner role held to overtime);
    `acting_manager` [{from, until, note}]; `standing_shifts` [{day, start,
    end, role}]; `trainee` {target_role, trainer, from, until} or {} to end
    it; `closes_for` [role]. Each replaces what is stored."""
    if employee_name is not None and not isinstance(employee_name, str):
        raise StaffSettingsError("an employee name is required")
    name = (employee_name or "").strip()[:120]
    if not name:
        raise StaffSettingsError("an employee name is required")
    if employment_type is not None and employment_type not in EMPLOYMENT_TYPES + ("",):
        raise StaffSettingsError("employment type is full or part")
    for label, v in (("min_hours", min_hours), ("max_hours", max_hours)):
        if v not in (None, ""):
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise StaffSettingsError(f"{label.replace('_', ' ')} must be a number")
            if v < 0 or v > 80:
                raise StaffSettingsError(f"{label.replace('_', ' ')} must be between 0 and 80")
    if daypart_availability is not None:
        if not isinstance(daypart_availability, dict):
            raise StaffSettingsError("daypart availability is a map of weekday to any/morning/night/off")
        cleaned = {}
        for d, v in daypart_availability.items():
            d = str(d).strip().capitalize()
            v = str(v or "any").strip().lower()
            if d in DAYS and v in DAYPART_CHOICES:
                cleaned[d] = v
        if len([d for d, v in cleaned.items() if v == "off"]) == 7:
            raise StaffSettingsError("every day is off — leave at least one day they can work")
        daypart_availability = cleaned

    if minor_age_band is not None:
        from schedule_rules import MINOR_BANDS
        minor_age_band = str(minor_age_band or "").strip().replace("–", "-").replace(" ", "")
        if minor_age_band and minor_age_band not in MINOR_BANDS:
            raise StaffSettingsError("age band is 14-15 or 16-17")
        if minor_age_band and is_minor is None:
            is_minor = True
    if is_minor is not None and not _flag(is_minor) and minor_age_band is None:
        minor_age_band = ""
    raw_windows = time_windows
    if time_windows is not None:
        time_windows = _clean_windows(time_windows)
    if certifications is not None:
        if not isinstance(certifications, list):
            raise StaffSettingsError("certifications is a list")
        certifications = sorted({str(c).strip().lower()[:40] for c in certifications if str(c).strip()})
    if preferred_dayparts is not None:
        if not isinstance(preferred_dayparts, list):
            raise StaffSettingsError("preferred dayparts is a list of morning/night")
        preferred_dayparts = [p for p in ("morning", "night") if p in {str(x).strip().lower() for x in preferred_dayparts}]
    if desired_hours not in (None, ""):
        try:
            desired_hours = float(desired_hours)
        except (TypeError, ValueError):
            raise StaffSettingsError("desired hours must be a number")
        if desired_hours < 0 or desired_hours > 80:
            raise StaffSettingsError("desired hours must be between 0 and 80")
    if floor_manager is not None:
        floor_manager = _clean_floor_manager(floor_manager)
    if acting_manager is not None:
        acting_manager = _clean_acting(acting_manager)
    if standing_shifts is not None:
        standing_shifts = _clean_standing(standing_shifts)
    if trainee is not None:
        trainee = _clean_trainee(trainee, today=_today(restaurant_id)) or ""
    if closes_for is not None:
        closes_for = _clean_closes_for(closes_for)
    conn = get_conn(db_path)
    try:
        # The row this person already has, whatever case it was saved in:
        # "maria g." used to start a second row beside "Maria G." (MOD-EMP-2).
        for r in conn.execute("SELECT employee_name FROM staff_settings WHERE restaurant_id=?", (restaurant_id,)).fetchall():
            if name_key(r["employee_name"]) == name_key(name):
                name = r["employee_name"]
                break
        cur = conn.execute("SELECT * FROM staff_settings WHERE restaurant_id=? AND employee_name=?",
                           (restaurant_id, name)).fetchone()
        current = _row(cur) if cur else {"active": True, "employment_type": None, "min_hours": None,
                                         "max_hours": None, "daypart_availability": {}, "is_minor": False,
                                         "time_windows": {}, "certifications": [], "preferred_dayparts": [],
                                         "desired_hours": None, "experienced": False, "minor_age_band": None,
                                         "floor_manager": None, "paid_hourly": False, "acting_manager": [],
                                         "standing_shifts": [], "trainee": None, "closes_for": []}
        if time_windows is not None:
            time_windows = _keep_window_dates(raw_windows, time_windows, current.get("time_windows") or {})
        new = {
            "active": int(_flag(active)) if active is not None else int(current["active"]),
            "employment_type": (employment_type or None) if employment_type is not None else current["employment_type"],
            "min_hours": (float(min_hours) if min_hours not in (None, "") else None) if min_hours is not None else current["min_hours"],
            "max_hours": (float(max_hours) if max_hours not in (None, "") else None) if max_hours is not None else current["max_hours"],
            "daypart_availability": daypart_availability if daypart_availability is not None else current["daypart_availability"],
            "is_minor": int(_flag(is_minor)) if is_minor is not None else int(current["is_minor"]),
            "time_windows": time_windows if time_windows is not None else current.get("time_windows") or {},
            "certifications": certifications if certifications is not None else current.get("certifications") or [],
            "preferred_dayparts": preferred_dayparts if preferred_dayparts is not None else current.get("preferred_dayparts") or [],
            "desired_hours": ((desired_hours if desired_hours != "" else None) if desired_hours is not None else current.get("desired_hours")),
            "experienced": int(_flag(experienced)) if experienced is not None else int(bool(current.get("experienced"))),
            "minor_age_band": (minor_age_band or None) if minor_age_band is not None else current.get("minor_age_band"),
            "floor_manager": ((None if floor_manager == AUTO else floor_manager) if floor_manager is not None
                              else (None if current.get("floor_manager") is None else int(current["floor_manager"]))),
            "paid_hourly": int(_flag(paid_hourly)) if paid_hourly is not None else int(bool(current.get("paid_hourly"))),
            "acting_manager": acting_manager if acting_manager is not None else current.get("acting_manager") or [],
            "standing_shifts": standing_shifts if standing_shifts is not None else current.get("standing_shifts") or [],
            "trainee": (trainee or None) if trainee is not None else current.get("trainee"),
            "closes_for": closes_for if closes_for is not None else current.get("closes_for") or [],
        }
        if new["min_hours"] is not None and new["max_hours"] is not None and new["min_hours"] > new["max_hours"]:
            raise StaffSettingsError("minimum hours cannot exceed maximum hours")
        # An existing row is updated in the columns this call set and no
        # others: writing back every column it had read meant two managers
        # editing different fields of one person at once lost one edit
        # (MOD-EMP-9).
        given = {"active": active, "employment_type": employment_type, "min_hours": min_hours,
                 "max_hours": max_hours, "daypart_availability": daypart_availability, "is_minor": is_minor,
                 "time_windows": time_windows, "certifications": certifications,
                 "preferred_dayparts": preferred_dayparts, "desired_hours": desired_hours,
                 "experienced": experienced, "minor_age_band": minor_age_band,
                 "floor_manager": floor_manager, "paid_hourly": paid_hourly, "acting_manager": acting_manager,
                 "standing_shifts": standing_shifts, "trainee": trainee, "closes_for": closes_for}
        sets = [f"{col}=excluded.{col}" for col, v in given.items() if v is not None]
        sets += ["updated_by=excluded.updated_by", "updated_at=excluded.updated_at"]
        conn.execute("""INSERT INTO staff_settings (restaurant_id, employee_name, active, employment_type,
                            min_hours, max_hours, daypart_availability, is_minor, time_windows, certifications,
                            preferred_dayparts, desired_hours, experienced, minor_age_band, floor_manager,
                            paid_hourly, acting_manager, standing_shifts, trainee, closes_for, updated_by, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                        ON CONFLICT(restaurant_id, employee_name) DO UPDATE SET """ + ", ".join(sets),
                     (restaurant_id, name, new["active"], new["employment_type"], new["min_hours"],
                      new["max_hours"], json.dumps(new["daypart_availability"]), new["is_minor"],
                      json.dumps(new["time_windows"]), json.dumps(new["certifications"]),
                      json.dumps(new["preferred_dayparts"]), new["desired_hours"], new["experienced"],
                      new["minor_age_band"], new["floor_manager"], new["paid_hourly"],
                      json.dumps(new["acting_manager"]), json.dumps(new["standing_shifts"]),
                      json.dumps(new["trainee"]) if new["trainee"] else None, json.dumps(new["closes_for"]),
                      (updated_by or "").strip()[:120] or None))
        if time_windows is not None and time_windows != (current.get("time_windows") or {}):
            # The staff app edits these windows on its availability screen
            # and saves against that row's version (save_own_availability).
            from models import touch_staff_availability
            touch_staff_availability(conn, restaurant_id, name)
        conn.commit()
        row = conn.execute("SELECT * FROM staff_settings WHERE restaurant_id=? AND employee_name=?",
                           (restaurant_id, name)).fetchone()
    finally:
        conn.close()
    if active is not None:
        _sync_portal_access(restaurant_id, name, _flag(active), db_path)
    _log_roster_changes(restaurant_id, name, current, new, given, db_path=db_path)
    return _row(row)


# How each staff_settings field is named in the change history.
_ROSTER_FIELDS = {"employment_type": "employment type", "min_hours": "minimum hours", "max_hours": "maximum hours",
                  "daypart_availability": "availability", "is_minor": "minor", "time_windows": "time windows",
                  "certifications": "certifications", "preferred_dayparts": "preferred dayparts",
                  "desired_hours": "desired hours", "experienced": "experienced", "minor_age_band": "minor age band",
                  "floor_manager": "floor manager", "paid_hourly": "paid hourly",
                  "acting_manager": "acting manager dates", "standing_shifts": "standing shifts",
                  "trainee": "training", "closes_for": "closes for"}


def _log_roster_changes(restaurant_id, name, current, new, given, db_path=DB_PATH):
    """Every field this save changed, into the change history with
    subject= the person (memory audit 9/29/26, change_log — M7's owed
    roster caller): taking someone off the roster is "left" (roster_leave),
    putting them back "added" (roster_add), anything else a roster change.
    Whose change it is comes from the request or an attributed() block
    (change_log.actor_context). Never raises."""
    try:
        import change_log
        db = None if db_path == DB_PATH else db_path
        for field, v in given.items():
            if v is None:
                continue
            before, after = current.get(field), new.get(field)
            if field == "active":
                was, now = bool(before), bool(after)
                if was != now:
                    change_log.record(restaurant_id, "roster", "left" if was else "added", was, now, subject=name,
                                      db_path=db)
                continue
            if field in ("is_minor", "experienced", "paid_hourly"):
                before, after = bool(before), bool(after)
            if field == "floor_manager":
                words = {None: "automatic", True: "yes", False: "no"}
                before = words[None if before is None else bool(before)]
                after = words[None if after is None else bool(after)]
            change_log.record(restaurant_id, "roster", _ROSTER_FIELDS.get(field, field), before, after, subject=name,
                              db_path=db)
    except Exception as e:
        print(f"[staff_settings] change history not recorded rid={restaurant_id}: {e!r}")


def _sync_portal_access(restaurant_id, name, active, db_path=DB_PATH):
    """Roster deactivation is the one switch: it also deactivates the
    person's staff-portal membership (ending their sessions and PIN sign-in)
    and expires their schedule share links; reactivating restores the
    membership. The two were unrelated switches, so someone taken off the
    roster kept reading the schedule (MOD-EMP-3 / DATA-59).

    Reactivating restores what the roster switch took away, and nothing
    else (employee audit C9 / LG-05): never a login an owner unlinked as
    claimed by the wrong person, nor one its holder deleted (unlinked_at /
    deleted_at), and at most one employee login for the name — the most
    recently claimed — and none if the name already has an active one. It
    used to reactivate every row carrying the name, an impostor's included,
    with its old PIN.

    A failure here leaves a person off the roster still signed in, so it is
    reported through ops.capture like the session revoke it calls (SEC-15)."""
    key = name_key(name)
    try:
        import auth
        if not active:
            auth.expire_share_links(restaurant_id, name, db_path=db_path)
        conn = get_conn(db_path)
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, employee_name, role, is_active, unlinked_at, deleted_at, claimed_at, updated_at "
                "FROM memberships WHERE restaurant_id=? AND employee_name IS NOT NULL",
                (restaurant_id,)).fetchall() if name_key(r["employee_name"]) == key]
        finally:
            conn.close()
        if not active:
            targets = [r["id"] for r in rows if r["is_active"]]
        else:
            ended = lambda r: r["unlinked_at"] or r["deleted_at"]
            targets = [r["id"] for r in rows if not r["is_active"] and not ended(r) and r["role"] != "employee"]
            if not any(r["is_active"] and r["role"] == "employee" for r in rows):
                back = sorted((r for r in rows if not r["is_active"] and not ended(r) and r["role"] == "employee"),
                              key=lambda r: (r["claimed_at"] or "", r["updated_at"] or "", r["id"]), reverse=True)
                targets += [r["id"] for r in back[:1]]
        for mid in targets:
            try:
                auth.set_membership_active(mid, restaurant_id, active, db_path=db_path)
            except auth.NameTakenError as e:
                print(f"[staff_settings] membership {mid} left inactive rid={restaurant_id}: {e}")
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="staff_portal_access_sync",
                        context=f"rid={restaurant_id} active={active}",
                        db_path=None if db_path == DB_PATH else db_path, restaurant_id=restaurant_id)
        except Exception:
            print(f"[staff_settings] portal access sync failed rid={restaurant_id}: {e!r}")


# ── the roster ─────────────────────────────────────────────────────────────

# A person's roster role (schedule audit 10/3/26 D-17): the role they worked
# most over their last ROLE_WEEKS weeks of shifts — "most recent role wins"
# turned a bartender into a Host for one host pickup, and with the role went
# their manager status, leader rules and section caps.
ROLE_WEEKS = 8


def _person_index(restaurant_id, db_path=DB_PATH):
    """{spelling name_key: (person name_key, display name)} for every
    spelling people's identity knows here (people.identity), or {} when the
    identity tables can't be read — the roster then keys by spelling, as it
    always did."""
    try:
        import people as _people
        conn = _people._conn(None if db_path == DB_PATH else db_path)
        try:
            idx = _people._Index(conn, restaurant_id)
        finally:
            conn.close()
    except Exception:
        return {}
    out = {}
    for k in list(idx.by_key):
        live = idx.for_key(k)
        if len(live) == 1:
            p = idx.people[next(iter(live))]
            out[k] = (p["name_key"], p["display_name"])
    return out


def _main_role(worked, last) -> str:
    """The role worked most in the ROLE_WEEKS weeks up to `last` (their
    newest shift); a tie goes to the role worked most recently."""
    from datetime import date, timedelta
    try:
        since = (date.fromisoformat(str(last)[:10]) - timedelta(weeks=ROLE_WEEKS)).isoformat()
    except (TypeError, ValueError):
        since = ""
    tally = {}
    for d, role in worked:
        if role and (not since or d > since):
            n, newest = tally.get(role, (0, ""))
            tally[role] = (n + 1, max(newest, d))
    if not tally:
        newest = max(worked, default=("", None))
        return newest[1]
    return max(tally.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[0]))[0]


def roster(restaurant_id, db_path=DB_PATH, include_inactive=False) -> list:
    """[{name, role, shifts, last_worked, is_manual, added_at, active,
    settings, recent_roles}].

    Shift history ∪ hand-added names, each person once — however the POS,
    the hand-added list or a settings row spells them (people's identity,
    schedule audit 10/3/26 D-8); the role is the one they worked most over
    their last ROLE_WEEKS weeks (D-17), unless the owner recorded a primary
    role; `recent_roles` every role in those weeks, most worked first; a
    deactivated person is left out unless asked for. This is the one list
    the generator, the roster check, the replacement pickers and the team
    screen should all read.
    """
    from models import get_manual_team_members, _cached_shifts
    # Keyed by the person: one entry however their name was typed (MOD-EMP-2
    # folded case and spacing; people's identity folds an alias, an old
    # spelling, a POS rename). The display name is the person's own
    # (people.display_name — the spelling every store is kept under), else
    # the spelling on their most recent shift.
    ident = _person_index(restaurant_id, db_path)

    def _who(n):
        hit = ident.get(name_key(n))
        return hit if hit else (name_key(n), None)
    seen, worked = {}, {}
    try:
        for sh in _cached_shifts(restaurant_id):
            n = " ".join(str(sh.get("employee") or "").split())
            if not n:
                continue
            k, display = _who(n)
            e = seen.setdefault(k, {"name": display or n, "role": None, "shifts": 0, "last_worked": "",
                                    "is_manual": False, "added_at": None})
            e["shifts"] += 1
            d = str(sh.get("date") or "")[:10]
            role = (sh.get("role") or "").strip()
            worked.setdefault(k, []).append((d, role))
            if d >= e["last_worked"]:
                e["last_worked"] = d
                if not display:
                    e["name"] = n
    except Exception:
        pass
    for k, e in seen.items():
        e["role"] = _main_role(worked.get(k) or [], e["last_worked"])
        since = ""
        try:
            from datetime import date as _d, timedelta as _td
            since = (_d.fromisoformat(e["last_worked"]) - _td(weeks=ROLE_WEEKS)).isoformat()
        except (TypeError, ValueError):
            pass
        counts = {}
        for d, role in worked.get(k) or []:
            if role and d > since:
                counts[role] = counts.get(role, 0) + 1
        e["recent_roles"] = [r for r, _n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    try:
        from models import manual_team_added
        added = manual_team_added(restaurant_id, db_path=db_path)
    except Exception:
        added = {}
    try:
        for m in get_manual_team_members(restaurant_id, db_path=db_path):
            n = " ".join(str(m.get("name") or "").split())
            if not n:
                continue
            k, display = _who(n)
            if k not in seen:
                seen[k] = {"name": display or n, "role": m.get("role"), "shifts": 0, "last_worked": "",
                           "is_manual": True, "added_at": added.get(m.get("name")), "recent_roles": []}
            else:
                seen[k]["added_at"] = added.get(m.get("name"))
                if m.get("role") and not seen[k]["role"]:
                    seen[k]["role"] = m["role"]
    except Exception:
        pass
    # A settings row kept under another spelling of the person is theirs; the
    # row under their own spelling wins when there are two.
    settings = {}
    for n, st in get_all(restaurant_id, db_path=db_path).items():
        k, _display = _who(n)
        if k not in settings or name_key(n) == k:
            settings[k] = st
    # A promotion the owner recorded (people.add_role, primary) is the
    # person's role from its date — before, a role came only from the shifts
    # someone had already worked (memory audit 9/29/26, uncaptured).
    try:
        import people as _people
        primary = {_who(r["name"])[0]: r["role"] for r in _people.held_roles(restaurant_id) if r["primary"]}
    except Exception:
        primary = {}
    out = []
    for k, e in seen.items():
        st = settings.get(k) or {}
        active = st.get("active", True)
        if not active and not include_inactive:
            continue
        if primary.get(k):
            e = {**e, "role": primary[k]}
        out.append({**e, "active": bool(active), "settings": st})
    out.sort(key=lambda e: (not e["active"], e["name"].lower()))
    return out


# ── who stopped working (schedule audit 10/3/26 E-3, D-17) ──────────────────
#
# Everyone in three years of shift history stayed on the roster until the
# owner deactivated them, and every fill-in pass sorted candidates by fewest
# hours — so the server who left in June was the first one picked, and the
# open-shift broadcast pinged her. A person with no shift in DORMANT_WEEKS
# weeks is DORMANT: still on the roster (a row the owner writes for them is
# legal), never chosen by code, left off the model's roster, not told about
# open shifts, and offered to the owner as "not worked since M/D/YY —
# deactivate?". Never dormant: somebody hand-added after their last shift
# (or never on a shift — a new hire), anybody the owner or they themselves
# touched lately (availability, settings, time off ahead, a published shift
# ahead), and whoever the caller exempts — salaried people and managers,
# who barely punch (Erik 1 punch, Jim 0 at Simple EJ's).
DORMANT_WEEKS = 6


def dormant_people(restaurant_id, people=None, exempt=(), today=None, db_path=DB_PATH) -> dict:
    """{name_key: last worked iso} for the roster people (`people`, a
    roster(include_inactive=True) list; read when None) who are dormant.
    `exempt`: name_keys never dormant (the caller's managers, salaried
    people, standing shifts). The weeks are counted back from the newest
    shift on file when the history stops short of today — a restaurant
    whose uploads stopped is not a restaurant whose staff all quit."""
    from datetime import date as _date, timedelta as _td
    people = list(people if people is not None else roster(restaurant_id, db_path=db_path, include_inactive=True))
    today = today or _today(restaurant_id)
    edge = max((e.get("last_worked") or "" for e in people), default="")
    ref = min(today.isoformat(), edge) if edge else today.isoformat()
    try:
        cutoff = (_date.fromisoformat(ref) - _td(weeks=DORMANT_WEEKS)).isoformat()
    except ValueError:
        return {}
    exempt = {name_key(x) for x in (exempt or ())}
    lately = _touched_since(restaurant_id, cutoff, today, db_path)
    try:
        from models import salaried_keys as _sal_keys, get_restaurant as _gr
        exempt |= {name_key(k) for k in _sal_keys(_gr(restaurant_id, db_path) if db_path != DB_PATH
                                                  else _gr(restaurant_id), db_path=db_path)}
    except Exception:
        pass
    out = {}
    for e in people:
        k = name_key(e.get("name"))
        last = e.get("last_worked") or ""
        if not k or not e.get("active", True) or k in exempt:
            continue
        if not last or last >= cutoff:
            continue                      # never on a shift (hand-added), or worked lately
        added = str(e.get("added_at") or "")[:10]
        if added and added >= last:
            continue                      # hand-added (back) after their last shift
        st = e.get("settings") or {}
        if str(st.get("updated_at") or "")[:10] >= cutoff or st.get("floor_manager") is True \
                or st.get("standing_shifts") or "manager" in (st.get("certifications") or []):
            continue
        if k in lately:
            continue
        # A manager of any kind — one who runs the floor, a department's, an
        # owner, a lead — barely punches: their role is enough (schedule fix
        # round 10/3/26, M). Not only the floor kind is_manager_role counts.
        try:
            from schedule_rules import manager_role_kind
            if any(manager_role_kind(r) for r in [e.get("role")] + list(e.get("recent_roles") or [])):
                continue
        except Exception:
            pass
        out[k] = last
    return out


def dormancy_fields(entry, dormant: dict) -> dict:
    """{dormant, last_worked_label, dormant_text} for one roster entry —
    what the team page shows beside a person who has stopped working:
    "Not worked since 8/14/26 — deactivate?" (E-3). Marking them still here
    (any settings save, e.g. active=true) keeps them for DORMANT_WEEKS more."""
    from time_utils import mdy
    last = entry.get("last_worked") or ""
    gone = name_key(entry.get("name")) in (dormant or {})
    return {"dormant": gone, "last_worked_label": mdy(last) if last else None,
            "dormant_text": f"Not worked since {mdy(last)} — deactivate?" if gone and last else None}


# How far back (before the dormancy cutoff) a manager's published shifts
# keep them on the team: managers barely punch, so the published weeks are
# their record — a year of them.
DORMANT_MANAGER_PUBLISHED_WEEKS = 52


def _touched_since(restaurant_id, cutoff, today, db_path=DB_PATH) -> set:
    """name_keys with something on file that says they are still here: an
    availability row updated since `cutoff`, time off still ahead, a shift
    on a live published week from `cutoff` on, or — managers barely punch —
    a manager's shift (any manager role) on a live published week up to
    DORMANT_MANAGER_PUBLISHED_WEEKS weeks before `cutoff`."""
    out = set()
    conn = get_conn(db_path)
    try:
        try:
            for r in conn.execute("SELECT employee_name FROM staff_availability WHERE restaurant_id=? "
                                  "AND substr(updated_at,1,10) >= ?", (restaurant_id, cutoff)).fetchall():
                out.add(name_key(r["employee_name"]))
        except Exception:
            pass
        try:
            for r in conn.execute("SELECT employee_name FROM staff_time_off WHERE restaurant_id=? AND status IN "
                                  "('pending','approved') AND end_date >= ?",
                                  (restaurant_id, today.isoformat())).fetchall():
                out.add(name_key(r["employee_name"]))
        except Exception:
            pass
        try:
            from datetime import date as _date, timedelta as _td
            mgr_cutoff = (_date.fromisoformat(cutoff) - _td(weeks=DORMANT_MANAGER_PUBLISHED_WEEKS)).isoformat()
        except (TypeError, ValueError):
            mgr_cutoff = cutoff
        try:
            weeks = conn.execute(
                "SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL "
                "AND superseded_by IS NULL AND substr(week_end,1,10) >= ?", (restaurant_id, mgr_cutoff)).fetchall()
        except Exception:
            weeks = []
    finally:
        conn.close()
    if weeks:
        from schedule_versions import rows_from_csv
        try:
            from schedule_rules import manager_role_kind
        except Exception:
            manager_role_kind = None
        for w in weeks:
            for r in rows_from_csv(w["schedule_csv"]):
                d = r.get("date") or ""
                if d >= cutoff or (d >= mgr_cutoff and manager_role_kind and manager_role_kind(r.get("role"))):
                    out.add(name_key(r.get("employee")))
    try:
        ident = _person_index(restaurant_id, db_path)
        out |= {ident[k][0] for k in list(out) if k in ident}
    except Exception:
        pass
    return out


def roles_for(restaurant_id, name, db_path=DB_PATH) -> set:
    """Every role (lowercase) this person has worked or was added under —
    not just the most recent one roster() keeps. Empty when unknown."""
    from models import get_manual_team_members, _cached_shifts
    low = (name or "").strip().lower()
    out = set()
    try:
        for sh in _cached_shifts(restaurant_id) or []:
            if (sh.get("employee") or "").strip().lower() == low and (sh.get("role") or "").strip():
                out.add(sh["role"].strip().lower())
    except Exception:
        pass
    try:
        for m in get_manual_team_members(restaurant_id, db_path=db_path):
            if (m.get("name") or "").strip().lower() == low and (m.get("role") or "").strip():
                out.add(m["role"].strip().lower())
    except Exception:
        pass
    # Roles they were trained for or promoted into (people.person_roles):
    # a server trained on bar is a bartender candidate before her first
    # bar shift (memory audit 9/29/26, uncaptured).
    try:
        import people as _people
        out |= {r["role"].strip().lower() for r in _people.held_roles(restaurant_id, name) if r["role"].strip()}
    except Exception:
        pass
    return out


def worked_roles(restaurant_id, db_path=DB_PATH) -> dict:
    """{name_key: {role: the last date they worked it}} — every role each
    person has worked here, from the per-shift history (shift_facts) or,
    for a restaurant whose history predates it, the stored shifts file. The
    roster keeps only the most recent role; who a manager is, which roles
    somebody holds and whether a row's role is theirs read every role
    (schedule audit 10/3/26 E-14, D-15). Empty when nothing can be read."""
    out = {}
    conn = get_conn(db_path)
    try:
        has = conn.execute("SELECT 1 FROM shift_facts WHERE restaurant_id=? LIMIT 1", (restaurant_id,)).fetchone()
        rows = conn.execute(
            "SELECT employee_key, role, MAX(business_date) AS last FROM shift_facts WHERE restaurant_id=? "
            "AND role IS NOT NULL AND TRIM(role)<>'' GROUP BY employee_key, role", (restaurant_id,)).fetchall() if has else []
    except Exception:
        has, rows = None, []
    finally:
        conn.close()
    if has:
        for r in rows:
            role = " ".join(str(r["role"] or "").split())
            per = out.setdefault(r["employee_key"], {})
            per[role] = max(per.get(role, ""), str(r["last"] or "")[:10])
        return out
    try:
        from models import _cached_shifts
        for sh in _cached_shifts(restaurant_id) or []:
            n, role = name_key(sh.get("employee")), " ".join(str(sh.get("role") or "").split())
            if n and role:
                per = out.setdefault(n, {})
                per[role] = max(per.get(role, ""), str(sh.get("date") or "")[:10])
    except Exception:
        return {}
    return out


def active_names(restaurant_id, db_path=DB_PATH) -> list:
    return [e["name"] for e in roster(restaurant_id, db_path=db_path)]


def experienced_names(restaurant_id, db_path=DB_PATH) -> set:
    """Names the owner has marked as experienced, whatever the shift
    history window shows."""
    return {n for n, v in get_all(restaurant_id, db_path).items() if v.get("experienced")}


def stated_preferences(restaurant_id, db_path=DB_PATH) -> dict:
    """{name: {preferred_dayparts, desired_hours}} — what staff said they want."""
    out = {}
    for n, st in get_all(restaurant_id, db_path=db_path).items():
        if st.get("preferred_dayparts") or st.get("desired_hours"):
            out[n] = {"preferred_dayparts": st.get("preferred_dayparts") or [], "desired_hours": st.get("desired_hours")}
    return out


# ── pairings ───────────────────────────────────────────────────────────────

def pairs(restaurant_id, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_pairs WHERE restaurant_id=? ORDER BY kind, employee_a, employee_b",
                            (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return [{"id": r["id"], "a": r["employee_a"], "b": r["employee_b"], "kind": r["kind"],
             "note": r["note"], "created_at": r["created_at"]} for r in rows]


def pair_sets(restaurant_id, db_path=DB_PATH) -> dict:
    """{"prefer": {frozenset({a, b}), ...}, "avoid": {...}} with lowercase names."""
    out = {"prefer": set(), "avoid": set()}
    for p in pairs(restaurant_id, db_path=db_path):
        out.setdefault(p["kind"], set()).add(frozenset((p["a"].lower(), p["b"].lower())))
    return out


def set_pair(restaurant_id, a, b, kind, note=None, created_by=None, db_path=DB_PATH) -> dict:
    a, b = (a or "").strip()[:120], (b or "").strip()[:120]
    if not a or not b or a.lower() == b.lower():
        raise StaffSettingsError("two different people are needed")
    if kind not in PAIR_KINDS:
        raise StaffSettingsError("a pairing is prefer or avoid")
    if a.lower() > b.lower():
        a, b = b, a
    conn = get_conn(db_path)
    try:
        # One statement per pair, whichever way round it was typed.
        conn.execute("DELETE FROM staff_pairs WHERE restaurant_id=? AND lower(employee_a)=? AND lower(employee_b)=?",
                     (restaurant_id, a.lower(), b.lower()))
        cur = conn.execute("INSERT INTO staff_pairs (restaurant_id, employee_a, employee_b, kind, note, created_by) "
                           "VALUES (?,?,?,?,?,?)",
                           (restaurant_id, a, b, kind, (note or "").strip()[:200] or None,
                            (created_by or "").strip()[:120] or None))
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()
    return {"id": pid, "a": a, "b": b, "kind": kind, "note": note}


def delete_pair(restaurant_id, pair_id, db_path=DB_PATH) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM staff_pairs WHERE id=? AND restaurant_id=?", (int(pair_id), restaurant_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ── reliability, from the shift history ────────────────────────────────────

# Attendance is weighted by recency (memory audit 9/29/26, staff_notes): a
# shift this many days old counts half as much as one today, so three
# no-shows last winter stop marking someone unreliable after six clean
# months. Past RELIABILITY_WINDOW_DAYS a shift weighs under a sixteenth and
# is left out.
RELIABILITY_HALF_LIFE_DAYS = 90
RELIABILITY_WINDOW_DAYS = 360


def recency_weight(day, today, half_life=RELIABILITY_HALF_LIFE_DAYS) -> float:
    """0.5 ** (age / half_life) for an ISO date; 0.0 when it cannot be read
    or is older than RELIABILITY_WINDOW_DAYS. A future date weighs 1, and so
    does a row with no date at all (the stored history always has one; a
    caller's hand-built row without one keeps the unweighted count)."""
    from datetime import date as _date
    if day in (None, ""):
        return 1.0
    try:
        d = _date.fromisoformat(str(day)[:10])
    except (TypeError, ValueError):
        return 0.0
    age = max(0, (today - d).days)
    if age > RELIABILITY_WINDOW_DAYS:
        return 0.0
    return 0.5 ** (age / float(half_life))


# ── the one attendance reader ──────────────────────────────────────────────
#
# Reliability, attendance by weekday, standby days and the schedule prompt's
# no-show block all read attendance through here (schedule audit 10/3/26
# L-17): one window (RELIABILITY_WINDOW_DAYS), one decay (recency_weight's
# RELIABILITY_HALF_LIFE_DAYS), one smoothing toward this restaurant's own
# base rate. Reliability used a 90-day half-life over 360 days, standby and
# by-weekday an unweighted 360 days, the prompt an unweighted 26 weeks — so
# the review, the prompt and the scorer disagreed about the same person and
# day.
#
# Three rates, because they answer three questions:
#   no_show_rate   reliability — who to rely on. A miss weighs by what it
#                  cost the manager: an unannounced no-show 1, a call-out
#                  less the more notice it gave (L-18: a sick call with a
#                  day's notice counted the same as walking out on a shift).
#   absence_rate   standby planning — the chance a body is missing, so a
#                  call-out counts in full whatever its notice; call_out_rate
#                  is its call-out part, kept separately (L-18).
#   late_rate      lateness (D-44), read only on shifts whose arrival was
#                  clocked, and only once LATE_MIN_SHIFTS of them exist.
#
# A shift somebody else took, or one a manager let them off (an approved
# drop — attendance.NOT_OWED, E-5), was not theirs to work or miss: never
# counted.

# A call-out's weight against a no-show's 1, by the notice it gave: at least
# a day's notice lets the manager cover it before service, two hours lets
# them make calls; less, or unknown (a closer's "who didn't make it"), is
# nearly a no-show.
CALL_OUT_WEIGHTS = ((24 * 60, 0.25), (2 * 60, 0.5))
CALL_OUT_WEIGHT_SHORT = 0.75
# Clocked shifts needed before a late rate is said (the reliability floor's
# own six), and the share late at which somebody is a lateness risk for the
# shift that opens a role's day or the one that closes it (D-44).
LATE_MIN_SHIFTS = 6
LATE_RISK_RATE = 0.25
_NOT_OWED = ("covered", "excused")


def miss_weight(outcome, notice_minutes=None) -> float:
    """What one shift's outcome counts as a miss against reliability: a
    no-show 1, a call-out by its notice (CALL_OUT_WEIGHTS), anything else
    0."""
    if outcome == "no_show":
        return 1.0
    if outcome != "called_out":
        return 0.0
    try:
        notice = None if notice_minutes is None else float(notice_minutes)
    except (TypeError, ValueError):
        notice = None
    if notice is not None:
        for at_least, weight in CALL_OUT_WEIGHTS:
            if notice >= at_least:
                return weight
    return CALL_OUT_WEIGHT_SHORT


def _event(e):
    """(name, iso_date, outcome, notice_minutes, timed) from an event as
    attendance.reliability_events gives it — (name, date, outcome), or with
    a 4th {"notice_minutes", "timed"} — or a dict of the same keys. Without
    the detail, a recorded on-time/late/left-early outcome reads as timed."""
    if isinstance(e, dict):
        name, day, outcome = e.get("name") or e.get("employee"), e.get("date"), e.get("outcome")
        extra = e
    else:
        name, day, outcome = e[0], e[1], e[2]
        extra = e[3] if len(e) > 3 and isinstance(e[3], dict) else None
    timed = (extra or {}).get("timed")
    if timed is None:
        timed = outcome in ("on_time", "late", "left_early")
    return name, day, outcome, (extra or {}).get("notice_minutes"), bool(timed)


def _blank_tally() -> dict:
    return {"shifts": 0, "no_show": 0, "called_out": 0, "short": 0, "late": 0, "timed": 0,
            "w": 0.0, "w_miss": 0.0, "w_absent": 0.0, "w_callout": 0.0, "w_timed": 0.0, "w_late": 0.0,
            "last_miss": None, "first": None, "last": None}


def _add(t, day, outcome, notice, timed, w):
    t["shifts"] += 1
    t["w"] += w
    mw = miss_weight(outcome, notice)
    if outcome in ("no_show", "called_out"):
        t["no_show"] += 1
        t["w_miss"] += w * mw
        t["w_absent"] += w
        t["last_miss"] = max(t["last_miss"] or "", str(day)[:10])
        if outcome == "called_out":
            t["called_out"] += 1
            t["w_callout"] += w
    elif outcome in ("short", "left_early"):
        t["short"] += 1
    if timed:
        t["timed"] += 1
        t["w_timed"] += w
        if outcome == "late":
            t["late"] += 1
            t["w_late"] += w
    elif outcome == "late":
        t["late"] += 1                     # said, but not a clocked reading
    t["first"] = min(t["first"] or str(day)[:10], str(day)[:10])
    t["last"] = max(t["last"] or "", str(day)[:10])


def attendance_tally(events, today=None) -> dict:
    """The recency-weighted tally every attendance reader starts from:
    {name: {..counts and weights.., "days": {weekday: {...}}}}, inside
    RELIABILITY_WINDOW_DAYS of `today`, shifts not owed left out."""
    from datetime import date as _date
    today = today or _date.today()
    tally = {}
    for e in events or ():
        name, day, outcome, notice, timed = _event(e)
        n = " ".join(str(name or "").split())
        w = recency_weight(day, today)
        if not n or w <= 0 or outcome in _NOT_OWED:
            continue
        t = tally.setdefault(n, dict(_blank_tally(), days={}))
        _add(t, day, outcome, notice, timed, w)
        try:
            wd = _date.fromisoformat(str(day)[:10]).strftime("%A")
        except (TypeError, ValueError):
            wd = None
        if wd:
            _add(t["days"].setdefault(wd, _blank_tally()), day, outcome, notice, timed, w)
    return tally


def attendance_bases(tally) -> dict:
    """This restaurant's own weighted base rates — the point every person's
    rate is smoothed toward: {"miss", "absence", "call_out", "late"}."""
    w = sum(t["w"] for t in (tally or {}).values())
    wt = sum(t["w_timed"] for t in (tally or {}).values())
    return {"miss": (sum(t["w_miss"] for t in tally.values()) / w) if w else 0.0,
            "absence": (sum(t["w_absent"] for t in tally.values()) / w) if w else 0.0,
            "call_out": (sum(t["w_callout"] for t in tally.values()) / w) if w else 0.0,
            "late": (sum(t["w_late"] for t in tally.values()) / wt) if wt else 0.0}


def _rates(t, base) -> dict:
    late = (smoothed_rate(t["w_late"], t["w_timed"], base["late"]) if t["timed"] >= LATE_MIN_SHIFTS else None)
    return {"no_show_rate": smoothed_rate(t["w_miss"], t["w"], base["miss"]),
            "absence_rate": smoothed_rate(t["w_absent"], t["w"], base["absence"]),
            "call_out_rate": smoothed_rate(t["w_callout"], t["w"], base["call_out"]),
            "late_rate": late}


def weighted_attendance(events, today=None, min_shifts=6) -> dict:
    """{name: {...}} from attendance events (attendance.reliability_events,
    plain or with detail) — the reliability every reader shares. `shifts` /
    `no_shows` (no-shows and call-outs) / `called_out` / `late` are the raw
    counts inside the window (what is said: "missed 2 of 9"); every rate is
    recency-weighted and smoothed toward the restaurant's own base
    (smoothed_rate): `no_show_rate` with each call-out weighed by its notice
    (miss_weight), `absence_rate` / `call_out_rate` counting every absence in
    full, `late_rate` over clocked shifts (None below LATE_MIN_SHIFTS) and
    `late_risk` once it is at LATE_RISK_RATE."""
    from shift_quality import UNRELIABLE_RATE
    from datetime import date as _date
    today = today or _date.today()
    tally = attendance_tally(events, today=today)
    base = attendance_bases(tally)
    out = {}
    for n, t in tally.items():
        if t["shifts"] < min_shifts:
            continue
        rates = _rates(t, base)
        rate = rates["no_show_rate"]
        out[n] = {"shifts": t["shifts"], "no_shows": t["no_show"], "called_out": t["called_out"], "late": t["late"],
                  "no_show_rate": rate,
                  "raw_no_show_rate": round(t["no_show"] / t["shifts"], 2),
                  "absence_rate": rates["absence_rate"], "call_out_rate": rates["call_out_rate"],
                  "late_rate": rates["late_rate"], "late_shifts": t["timed"],
                  "late_risk": rates["late_rate"] is not None and rates["late_rate"] >= LATE_RISK_RATE,
                  "late_threshold": LATE_RISK_RATE,
                  "base_rate": round(base["miss"], 3), "no_show_threshold": UNRELIABLE_RATE,
                  "unreliable": rate >= UNRELIABLE_RATE,
                  "short_rate": round(t["short"] / t["shifts"], 2),
                  "last_miss": t["last_miss"], "since": t["first"], "through": t["last"],
                  "half_life_days": RELIABILITY_HALF_LIFE_DAYS}
    return out


def weekday_attendance(events, today=None, min_shifts=4) -> dict:
    """{name: {weekday: {"shifts", "no_shows", "no_show_rate",
    "absence_rate", "call_out_rate"}}} — the same weighted tally as
    reliability, per weekday, for weekdays with at least `min_shifts`
    watched shifts. Rates here are recency-weighted shares of that
    weekday's shifts (no smoothing: the floor stands in for it)."""
    from datetime import date as _date
    tally = attendance_tally(events, today=today or _date.today())
    out = {}
    for n, t in tally.items():
        for wd in DAYS:
            e = t["days"].get(wd)
            if not e or e["shifts"] < min_shifts or not e["w"]:
                continue
            out.setdefault(n, {})[wd] = {"shifts": e["shifts"], "no_shows": e["no_show"],
                                         "no_show_rate": round(e["w_miss"] / e["w"], 2),
                                         "absence_rate": round(e["w_absent"] / e["w"], 2),
                                         "call_out_rate": round(e["w_callout"] / e["w"], 2)}
    return out


def weekday_absence(events, today=None) -> dict:
    """The restaurant's own absence per weekday, for the schedule prompt's
    no-show risk block: {weekday: {"shifts", "misses", "rate"}} — `shifts`
    and `misses` the raw counts, `rate` the recency-weighted share of
    watched shifts somebody missed (a call-out counts in full: either way
    the shift was short a person)."""
    from datetime import date as _date
    tally = attendance_tally(events, today=today or _date.today())
    out = {}
    for t in tally.values():
        for wd, e in t["days"].items():
            d = out.setdefault(wd, {"shifts": 0, "misses": 0, "w": 0.0, "w_absent": 0.0})
            d["shifts"] += e["shifts"]
            d["misses"] += e["no_show"]
            d["w"] += e["w"]
            d["w_absent"] += e["w_absent"]
    return {wd: {"shifts": d["shifts"], "misses": d["misses"],
                 "rate": round(d["w_absent"] / d["w"], 3) if d["w"] else 0.0} for wd, d in out.items()}


def attendance_record(restaurant_id, today=None, db_path=DB_PATH) -> dict:
    """{"tally", "base", "today"} — the weighted per-person, per-weekday
    tally (attendance_tally) and this restaurant's base rates, from one read
    of attendance_events: what standby planning ranks people by."""
    today = today or _today(restaurant_id)
    tally = attendance_tally(attendance_events(restaurant_id, today=today, db_path=db_path), today=today)
    return {"tally": tally, "base": attendance_bases(tally), "today": today}


def local_today(restaurant_id):
    """The restaurant's own local date — the `today` every attendance
    weight is measured from."""
    return _today(restaurant_id)


def attendance_events(restaurant_id, today=None, db_path=DB_PATH) -> list:
    """Every watched shift inside the one window (RELIABILITY_WINDOW_DAYS of
    the restaurant's own `today`), with each call-out's notice and whether
    it was clocked — attendance.reliability_events(detail=True). The read
    every attendance reader shares (L-17)."""
    import attendance
    from datetime import timedelta as _td_ev
    today = today or _today(restaurant_id)
    return attendance.reliability_events(restaurant_id, since=(today - _td_ev(days=RELIABILITY_WINDOW_DAYS)).isoformat(),
                                         db_path=None if db_path == DB_PATH else db_path, detail=True)


def reliability(restaurant_id, db_path=DB_PATH, min_shifts=6, today=None) -> dict:
    """{employee_name: {"no_show_rate": 0.0-1.0, "short_rate": ..., "shifts": n,
    "late_rate", "absence_rate", "call_out_rate", ...}} for everyone with
    enough watched shifts to say anything — the one weighted reader
    (attendance_events → weighted_attendance).

    A scheduled shift with actual_hours of zero is a no-show; one worked
    at least an hour and a half short of schedule is a short shift. Only
    rows that carry a clock-in reading count — a CSV without the column
    says nothing about attendance (the same rule labor.py's no-show block
    applies).
    """
    # Every shift somebody WATCHED (attendance.reliability_events — memory
    # audit 9/29/26): the outcomes the live check, the close-out and the
    # nightly published-week-vs-punches join recorded, and the shifts from a
    # source with a real schedule. A POS row whose "scheduled" hours were its
    # actual hours copied (RPOWER, Square, Clover, Toast without a schedule)
    # can never show a no-show, and reading it made everyone "reliable".
    today = today or _today(restaurant_id)
    events = attendance_events(restaurant_id, today=today, db_path=db_path)
    # `no_show_rate` is SMOOTHED toward this restaurant's own base rate
    # (a Beta prior worth NO_SHOW_PRIOR_SHIFTS shifts; fix I9, CA1 L14): two
    # misses in six shifts read as a flat 33%, and the engine then treated
    # that person as unreliable on the strength of two nights. The raw count
    # travels beside it. `no_show_threshold` is the engine's own line
    # (shift_quality.UNRELIABLE_RATE), so a client colours a row red exactly
    # when the scheduler treats that person as unreliable — the web used 10%
    # against the engine's 20%.
    return weighted_attendance(events, today=today, min_shifts=min_shifts)


def _today(restaurant_id):
    """The restaurant's own local date (Chicago when it can't be read)."""
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        from datetime import date as _date
        return _date.today()


# Pseudo-shifts of the restaurant's own base rate every person's no-show
# rate starts from. Six is the reliability floor (min_shifts): at the floor a
# person's own record and the base rate carry equal weight.
NO_SHOW_PRIOR_SHIFTS = 6


def no_show_base_rate(tally) -> float:
    """The restaurant's own no-show rate across every clocked shift in
    `tally` ({name: {"shifts", "no_show", ...}} or {name: {"all": [n, k]}})."""
    shifts = misses = 0
    for t in (tally or {}).values():
        if "all" in t:
            n, k = t["all"][0], t["all"][1]
        else:
            n, k = t.get("shifts", 0), t.get("no_show", 0)
        shifts += n
        misses += k
    return (misses / shifts) if shifts else 0.0


def smoothed_rate(misses, shifts, base, prior=NO_SHOW_PRIOR_SHIFTS) -> float:
    """(misses + prior·base) / (shifts + prior), rounded to 2 — a person's
    rate shrunk toward the restaurant's base rate in proportion to how
    little of their own record there is."""
    try:
        return round((float(misses) + prior * float(base)) / (float(shifts) + prior), 2)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


# ── an employee's own availability (staff app and the /s/<token> link) ──────
#
# One record, one save, whichever surface it comes from (employee audit M5:
# MISS-8, WF-32, LG-35, PERF-05). The weekdays someone can't work live in
# staff_availability (unavailable_days, plus day_bounds for the ones blocked
# only between two dates); the hours they can work on a day are the SAME
# time windows the owner sets above (staff_settings.time_windows, with an
# optional from/until) — one representation, read by schedule_rules as a
# hard constraint. A save carries the version it loaded (staff_availability
# .updated_at) and is refused when the row has moved on, so the app and the
# link cannot silently overwrite each other, and a failed load can never be
# saved back as an empty week.

AVAILABILITY_STATUSES = ("any", "off", "window")
KEEP = object()
# A save that sent no version at all (an app from before PERF-05): never
# matches, so it is refused once the request itself is valid.
MISSING = object()
TIME_OFF_HINT = {"kind": "time_off",
                 "text": "Dates you're away go in a time-off request — your manager answers it and the schedule "
                         "keeps you off those days. Availability is your usual week."}
_AWAY_RE = None


def _av_conn(db_path):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports):
    this section's callers are routes that pass no db_path."""
    import models
    return models.get_conn() if db_path in (None, DB_PATH) else models.get_conn(db_path)


def _hhmm(t):
    """'5:00pm' → '17:00' (what the app's time pickers read), or None."""
    from schedule_rules import parse_minutes
    m = parse_minutes(t or "")
    return None if m is None else f"{m // 60:02d}:{m % 60:02d}"


def _windows_for(restaurant_id, name, conn) -> tuple:
    """(stored name or None, {day: window}) from this person's staff_settings row."""
    key = name_key(name)
    for r in conn.execute("SELECT * FROM staff_settings WHERE restaurant_id=?", (restaurant_id,)).fetchall():
        if name_key(r["employee_name"]) == key:
            return r["employee_name"], _row(r)["time_windows"]
    return None, {}


def _state_of(row, windows) -> dict:
    """{off: set, bounds: {day: {from, until}}, windows: {day: window}}."""
    from models import availability_stored_blocks, availability_day_bounds
    off = availability_stored_blocks(row) if row else set()
    return {"off": off, "bounds": availability_day_bounds(row) if row else {},
            "windows": {d: w for d, w in (windows or {}).items() if d not in off}}


def _payload(row, windows, today) -> dict:
    """What GET /staff/api/availability answers: `week` is the new shape,
    `unavailable_days` the one older apps read. An entry whose dates have
    passed reads as "any"."""
    st, iso = _state_of(row, windows), today.isoformat()
    week, unavailable = [], []
    for d in DAYS:
        b = st["bounds"].get(d) or {}
        w = st["windows"].get(d) or {}
        if d in st["off"] and not (b.get("until") and b["until"] < iso):
            week.append({"day": d, "status": "off", "earliest": None, "latest": None,
                         "from": b.get("from"), "until": b.get("until")})
            unavailable.append(d)
        elif w and not (w.get("until") and w["until"] < iso):
            week.append({"day": d, "status": "window", "earliest": _hhmm(w.get("earliest")),
                         "latest": _hhmm(w.get("latest")), "from": w.get("from"), "until": w.get("until")})
        else:
            week.append({"day": d, "status": "any", "earliest": None, "latest": None, "from": None, "until": None})
    return {"days": list(DAYS), "week": week, "unavailable_days": unavailable,
            "notes": (row or {}).get("notes") or "", "updated_at": (row or {}).get("updated_at"),
            "time_off_hint": TIME_OFF_HINT["text"]}


def own_availability(restaurant_id, employee_name, db_path=DB_PATH, today=None) -> dict:
    """This person's availability as the staff app and the link show it."""
    from models import staff_availability_for
    conn = _av_conn(db_path)
    try:
        row = staff_availability_for(restaurant_id, employee_name, conn=conn)
        _, windows = _windows_for(restaurant_id, employee_name, conn)
    finally:
        conn.close()
    return _payload(row, windows, today or _today(restaurant_id))


def away_hint(notes):
    """A note that reads like dates away ("away Oct 3–6", "vacation
    12/20-12/27") belongs in a time-off request, where a manager answers
    it and the schedule blocks those dates (WF-32). None otherwise."""
    import re
    global _AWAY_RE
    if _AWAY_RE is None:
        _AWAY_RE = re.compile(
            r"\b(away|vacation|holiday|trip|out of town|wedding)\b|\b\d{1,2}/\d{1,2}\b|"
            r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b", re.I)
    return dict(TIME_OFF_HINT) if notes and _AWAY_RE.search(str(notes)) else None


def _target_from_week(week, today) -> tuple:
    """(off days, their dates, time windows) from the app's `week`, or
    raises StaffSettingsError. A day left out is "any"."""
    from schedule_rules import parse_minutes, _fmt_minutes
    if not isinstance(week, list):
        raise StaffSettingsError("week is a list of {day, status}")
    off, bounds, raw_windows = set(), {}, {}
    for e in week:
        if not isinstance(e, dict):
            raise StaffSettingsError("week is a list of {day, status}")
        d = str(e.get("day") or "").strip().capitalize()
        if d not in DAYS:
            raise StaffSettingsError(f"'{e.get('day')}' is not a weekday")
        status = str(e.get("status") or "any").strip().lower()
        if status not in AVAILABILITY_STATUSES:
            raise StaffSettingsError(f"{d}: status is any, off or window")
        f, u = _clean_bound(e.get("from"), d), _clean_bound(e.get("until"), d)
        if status == "any":
            continue
        if u and u < today.isoformat():
            raise StaffSettingsError(f"{d}: that end date has passed")
        if f and u and f > u:
            raise StaffSettingsError(f"{d}: the end date is before the start date")
        if status == "off":
            off.add(d)
            if f or u:
                bounds[d] = {"from": f, "until": u}
            continue
        lo, hi = parse_minutes(e.get("earliest") or ""), parse_minutes(e.get("latest") or "")
        if lo is None and hi is None:
            raise StaffSettingsError(f"{d}: give the earliest start or the latest finish")
        raw_windows[d] = {"earliest": _fmt_minutes(lo) if lo is not None else "",
                          "latest": _fmt_minutes(hi) if hi is not None else "",
                          "from": f or "", "until": u or ""}
    return off, bounds, _clean_windows(raw_windows)


def _conflicts(shifts, state) -> list:
    """The shifts `state` rules out, each with a `reason`."""
    from datetime import datetime
    from schedule_rules import parse_minutes, window_allows, window_holds, _fmt_minutes
    from models import bounds_apply
    out = []
    for s in shifts:
        try:
            d = datetime.strptime(s["date"], "%Y-%m-%d").strftime("%A")
        except (KeyError, ValueError, TypeError):
            continue
        reason = None
        if d in state["off"] and bounds_apply(state["bounds"].get(d), s["date"]):
            reason = f"you marked {d}s unavailable"
        else:
            w = state["windows"].get(d)
            if w and window_holds(w, d, [s["date"]]):
                lo, hi = parse_minutes(w.get("earliest") or ""), parse_minutes(w.get("latest") or "")
                ok, which = window_allows(lo, hi, parse_minutes(s.get("shift_start")), parse_minutes(s.get("shift_end")))
                if not ok:
                    reason = (f"you said not before {_fmt_minutes(lo)} on {d}s" if which == "early"
                              else f"you said not after {_fmt_minutes(hi)} on {d}s")
        if reason:
            out.append(dict(s, reason=reason))
    return out


def _shift_label(s) -> str:
    """"Fri 10/9/26 5:00pm"."""
    from datetime import datetime
    from time_utils import mdy
    from schedule_rules import parse_minutes, _fmt_minutes
    m = parse_minutes(s.get("shift_start"))
    try:
        dow = datetime.strptime(s["date"], "%Y-%m-%d").strftime("%a")
    except (KeyError, ValueError, TypeError):
        dow = ""
    return f"{dow} {mdy(s.get('date'))} {_fmt_minutes(m) if m is not None else s.get('shift_start') or ''}".strip()


def _listed(conflicts) -> str:
    labels = [_shift_label(c) for c in conflicts]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def conflicts_text(conflicts) -> str:
    """"You're still on Fri 10/9/26 5:00pm — ask to drop it." ("" for none)."""
    if not conflicts:
        return ""
    return f"You're still on {_listed(conflicts)} — ask to drop {'it' if len(conflicts) == 1 else 'them'}."


def _tell_deciders(restaurant_id, name, new_conflicts, db_path) -> int:
    """The people who can change the schedule hear about published shifts
    the new availability rules out (LG-35). Returns how many were reached;
    never raises."""
    if not new_conflicts:
        return 0
    try:
        import strategy_jobs
        from permissions import SCHEDULE_DRAFT
        one = len(new_conflicts) == 1
        title = f"{name}'s availability changed"
        body = (f"{name} is still scheduled {_listed(new_conflicts)}, which "
                f"{'no longer fits' if one else 'no longer fit'} what they said they can work. "
                f"Find cover or talk to them.")
        return int(strategy_jobs._reach(restaurant_id, "shift_request", title, body, {"tab": "labor"}, db_path,
                                        lines=[body], deciders=True, permissions=[SCHEDULE_DRAFT]) or 0)
    except Exception as e:
        print(f"[staff_settings] availability conflict notice failed rid={restaurant_id}: {e!r}")
        return 0


def save_own_availability(restaurant_id, employee_name, expected_updated_at, week=None,
                          unavailable_days=None, notes=KEEP, source="app", db_path=DB_PATH, today=None) -> dict:
    """The one save for an employee's own availability — the staff app
    (POST /staff/api/availability) and the schedule link
    (POST /s/<token>/availability) both call it.

    `expected_updated_at` is the `updated_at` the caller loaded (None when
    the person had no row); a row that has moved on since refuses the save
    with status 409 and the record as it is now. `week` ([{day, status:
    any|off|window, earliest, latest, from, until}]) replaces the whole week,
    time windows included; without it, `unavailable_days` (the link's and
    older apps' list of weekdays) changes only which days are off — a day
    still off keeps its dates, and the windows are left alone. `notes` left
    as KEEP keeps the stored note; None or "" clears it.

    Returns {ok, status, error?, stale?, availability, updated_at,
    conflicts, conflicts_text, hint, managers_told}: `conflicts` are this
    person's shifts on live published weeks from today that the new
    availability rules out, each with a `reason`; the deciders are told of
    the ones the old availability allowed."""
    from models import staff_availability_for, write_staff_availability, log_event
    import time_off as _to
    from datetime import timedelta
    today = today or _today(restaurant_id)
    name = " ".join(str(employee_name or "").split())
    if not name:
        return {"ok": False, "status": 400, "error": "No employee name on this session."}
    try:
        if week is not None:
            off, bounds, windows = _target_from_week(week, today)
        else:
            if not isinstance(unavailable_days, list):
                raise StaffSettingsError("unavailable_days must be a list of weekday names.")
            wanted = {str(x).strip().capitalize() for x in unavailable_days}
            off, bounds, windows = {d for d in DAYS if d in wanted}, None, None
    except StaffSettingsError as e:
        return {"ok": False, "status": 400, "error": str(e)}
    if len(off) == len(DAYS):
        return {"ok": False, "status": 400, "error": "Every day blocked — leave at least one you can work.",
                "hint": dict(TIME_OFF_HINT)}
    clean_notes = KEEP if notes is KEEP else ((str(notes or "").strip())[:300] or None)

    conn = _av_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = staff_availability_for(restaurant_id, name, conn=conn)
        ss_name, stored_windows = _windows_for(restaurant_id, name, conn)
        version = (row or {}).get("updated_at")
        if expected_updated_at is MISSING or (expected_updated_at or None) != (version or None):
            conn.rollback()
            return {"ok": False, "status": 409, "stale": True,
                    "error": ("Reload your availability, then make your change again." if expected_updated_at is MISSING
                              else "Your availability was changed somewhere else after this screen loaded. "
                                   "Here it is now — make your change again."),
                    "availability": _payload(row, stored_windows, today)}
        before = _state_of(row, stored_windows)
        if bounds is None:
            bounds = {d: b for d, b in before["bounds"].items() if d in off}
        keep_note = (row or {}).get("notes") if clean_notes is KEEP else clean_notes
        stamp = write_staff_availability(conn, restaurant_id, row["employee_name"] if row else name,
                                         off, keep_note, bounds, previous=version)
        windows_changed = windows is not None and windows != stored_windows
        if windows_changed and ss_name:
            conn.execute("UPDATE staff_settings SET time_windows=?, updated_by=?, updated_at=datetime('now') "
                         "WHERE restaurant_id=? AND employee_name=?",
                         (json.dumps(windows), name[:120], restaurant_id, ss_name))
        elif windows_changed:
            conn.execute("INSERT INTO staff_settings (restaurant_id, employee_name, time_windows, updated_by, "
                         "updated_at) VALUES (?,?,?,?,datetime('now'))",
                         (restaurant_id, name[:120], json.dumps(windows), name[:120]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    final_windows = windows if windows is not None else stored_windows
    after = {"off": set(off), "bounds": bounds, "windows": {d: w for d, w in final_windows.items() if d not in off}}
    if windows_changed:
        _log_roster_changes(restaurant_id, ss_name or name, {"time_windows": stored_windows},
                            {"time_windows": windows}, {"time_windows": windows}, db_path=db_path)
    try:
        shifts = _to.published_conflicts(restaurant_id, name, today, today + timedelta(days=70), db_path=db_path)
    except Exception as e:
        print(f"[staff_settings] published shifts unread rid={restaurant_id}: {e!r}")
        shifts = []
    conflicts = _conflicts(shifts, after)
    was = {(c["date"], c["shift_start"]) for c in _conflicts(shifts, before)}
    told = _tell_deciders(restaurant_id, name, [c for c in conflicts if (c["date"], c["shift_start"]) not in was],
                          db_path)
    try:
        log_event(restaurant_id, "availability_updated",
                  {"employee": name, "unavailable_days": [d for d in DAYS if d in off], "source": source,
                   "time_windows": sorted(after["windows"]), "conflicts": len(conflicts)}, db_path=db_path)
    except Exception:
        pass
    payload = _payload(staff_availability_for(restaurant_id, name, db_path=db_path), final_windows, today)
    return {"ok": True, "status": 200, "availability": payload, "updated_at": stamp,
            "conflicts": conflicts, "conflicts_text": conflicts_text(conflicts),
            "hint": away_hint(keep_note), "managers_told": told}
