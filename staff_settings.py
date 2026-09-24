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

from models import get_conn, DB_PATH

EMPLOYMENT_TYPES = ("full", "part")
DAYPART_CHOICES = ("any", "morning", "night", "off")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
PAIR_KINDS = ("prefer", "avoid")


class StaffSettingsError(ValueError):
    pass


# ── settings ───────────────────────────────────────────────────────────────

CERTIFICATIONS = ("alcohol", "food_handler", "manager", "keyholder", "trainer", "allergen", "first_aid")


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
        "updated_by": r["updated_by"],
        "updated_at": r["updated_at"],
    }


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
    return out


def _flag(v) -> bool:
    """bool("false") is True; a client sending strings would have switched
    a flag ON by asking for off."""
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def upsert(restaurant_id, employee_name, active=None, employment_type=None, min_hours=None,
           max_hours=None, daypart_availability=None, is_minor=None, updated_by=None,
           time_windows=None, certifications=None, preferred_dayparts=None, desired_hours=None,
           experienced=None, minor_age_band=None, db_path=DB_PATH) -> dict:
    """Set any subset of one person's facts. Unset arguments keep their
    stored value; the caller passes only what changed.

    `minor_age_band` is "14-15", "16-17" or "" (clear). Setting a band marks
    the person a minor; switching is_minor off clears the band (NS5 H4)."""
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
                                         "desired_hours": None, "experienced": False, "minor_age_band": None}
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
                 "experienced": experienced, "minor_age_band": minor_age_band}
        sets = [f"{col}=excluded.{col}" for col, v in given.items() if v is not None]
        sets += ["updated_by=excluded.updated_by", "updated_at=excluded.updated_at"]
        conn.execute("""INSERT INTO staff_settings (restaurant_id, employee_name, active, employment_type,
                            min_hours, max_hours, daypart_availability, is_minor, time_windows, certifications,
                            preferred_dayparts, desired_hours, experienced, minor_age_band, updated_by, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                        ON CONFLICT(restaurant_id, employee_name) DO UPDATE SET """ + ", ".join(sets),
                     (restaurant_id, name, new["active"], new["employment_type"], new["min_hours"],
                      new["max_hours"], json.dumps(new["daypart_availability"]), new["is_minor"],
                      json.dumps(new["time_windows"]), json.dumps(new["certifications"]),
                      json.dumps(new["preferred_dayparts"]), new["desired_hours"], new["experienced"],
                      new["minor_age_band"], (updated_by or "").strip()[:120] or None))
        conn.commit()
        row = conn.execute("SELECT * FROM staff_settings WHERE restaurant_id=? AND employee_name=?",
                           (restaurant_id, name)).fetchone()
    finally:
        conn.close()
    if active is not None:
        _sync_portal_access(restaurant_id, name, _flag(active), db_path)
    return _row(row)


def _sync_portal_access(restaurant_id, name, active, db_path=DB_PATH):
    """Roster deactivation is the one switch: it also deactivates the
    person's staff-portal membership (ending their sessions and PIN sign-in)
    and expires their schedule share links; reactivating restores the
    membership. The two were unrelated switches, so someone taken off the
    roster kept reading the schedule (MOD-EMP-3 / DATA-59)."""
    key = name_key(name)
    try:
        conn = get_conn(db_path)
        try:
            members = [r["id"] for r in conn.execute(
                "SELECT id, employee_name FROM memberships WHERE restaurant_id=? AND employee_name IS NOT NULL",
                (restaurant_id,)).fetchall() if name_key(r["employee_name"]) == key]
            if not active:
                for r in conn.execute("SELECT id, employee_name FROM schedule_shares WHERE restaurant_id=?",
                                      (restaurant_id,)).fetchall():
                    if name_key(r["employee_name"]) == key:
                        conn.execute("UPDATE schedule_shares SET expires_at=datetime('now') WHERE id=?", (r["id"],))
                conn.commit()
        finally:
            conn.close()
        import auth
        for mid in members:
            auth.set_membership_active(mid, restaurant_id, active, db_path=db_path)
    except Exception as e:
        print(f"[staff_settings] portal access sync failed rid={restaurant_id}: {e!r}")


# ── the roster ─────────────────────────────────────────────────────────────

def roster(restaurant_id, db_path=DB_PATH, include_inactive=False) -> list:
    """[{name, role, shifts, last_worked, is_manual, active, settings}].

    Shift history ∪ hand-added names, each once; the most recent role wins;
    a deactivated person is left out unless asked for. This is the one list
    the generator, the roster check, the replacement pickers and the team
    screen should all read.
    """
    from models import get_manual_team_members, _cached_shifts
    # Keyed by name_key: one person however their name was typed in the
    # POS, the hand-added list or a settings row (MOD-EMP-2). The display
    # name is the spelling on their most recent shift.
    seen = {}
    try:
        for sh in _cached_shifts(restaurant_id):
            n = " ".join(str(sh.get("employee") or "").split())
            if not n:
                continue
            e = seen.setdefault(name_key(n), {"name": n, "role": None, "shifts": 0, "last_worked": "", "is_manual": False})
            e["shifts"] += 1
            d = sh.get("date") or ""
            if d >= e["last_worked"]:
                e["last_worked"] = d
                e["name"] = n
                e["role"] = (sh.get("role") or "").strip() or e["role"]
    except Exception:
        pass
    try:
        for m in get_manual_team_members(restaurant_id, db_path=db_path):
            n = " ".join(str(m.get("name") or "").split())
            if n and name_key(n) not in seen:
                seen[name_key(n)] = {"name": n, "role": m.get("role"), "shifts": 0, "last_worked": "", "is_manual": True}
            elif n and m.get("role") and not seen[name_key(n)]["role"]:
                seen[name_key(n)]["role"] = m["role"]
    except Exception:
        pass
    settings = {name_key(n): st for n, st in get_all(restaurant_id, db_path=db_path).items()}
    out = []
    for k, e in seen.items():
        st = settings.get(k) or {}
        active = st.get("active", True)
        if not active and not include_inactive:
            continue
        out.append({**e, "active": bool(active), "settings": st})
    out.sort(key=lambda e: (not e["active"], e["name"].lower()))
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

def reliability(restaurant_id, db_path=DB_PATH, min_shifts=6) -> dict:
    """{employee_name: {"no_show_rate": 0.0-1.0, "short_rate": ..., "shifts": n}}
    for everyone with enough clocked shifts to say anything.

    A scheduled shift with actual_hours of zero is a no-show; one worked
    at least an hour and a half short of schedule is a short shift. Only
    rows that carry a clock-in reading count — a CSV without the column
    says nothing about attendance (the same rule labor.py's no-show block
    applies).
    """
    from models import _cached_shifts
    from labor import _has_actual_hours
    tally = {}
    for s in _cached_shifts(restaurant_id):
        n = (s.get("employee") or "").strip()
        if not n or not _has_actual_hours(s):
            continue
        try:
            sched = float(s.get("scheduled_hours") or s.get("hours") or 0)
            actual = float(s.get("actual_hours") or 0)
        except (TypeError, ValueError):
            continue
        if sched <= 0:
            continue
        t = tally.setdefault(n, {"shifts": 0, "no_show": 0, "short": 0})
        t["shifts"] += 1
        if actual == 0:
            t["no_show"] += 1
        elif sched - actual >= 1.5:
            t["short"] += 1
    # `no_show_rate` is SMOOTHED toward this restaurant's own base rate
    # (a Beta prior worth NO_SHOW_PRIOR_SHIFTS shifts; fix I9, CA1 L14): two
    # misses in six shifts read as a flat 33%, and the engine then treated
    # that person as unreliable on the strength of two nights. The raw count
    # travels beside it. `no_show_threshold` is the engine's own line
    # (shift_quality.UNRELIABLE_RATE), so a client colours a row red exactly
    # when the scheduler treats that person as unreliable — the web used 10%
    # against the engine's 20%.
    from shift_quality import UNRELIABLE_RATE
    base = no_show_base_rate(tally)
    return {n: {"shifts": t["shifts"],
                "no_shows": t["no_show"],
                "no_show_rate": smoothed_rate(t["no_show"], t["shifts"], base),
                "raw_no_show_rate": round(t["no_show"] / t["shifts"], 2),
                "base_rate": round(base, 3),
                "no_show_threshold": UNRELIABLE_RATE,
                "unreliable": smoothed_rate(t["no_show"], t["shifts"], base) >= UNRELIABLE_RATE,
                "short_rate": round(t["short"] / t["shifts"], 2)}
            for n, t in tally.items() if t["shifts"] >= min_shifts}


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
