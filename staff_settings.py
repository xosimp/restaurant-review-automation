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
        "employee_name": r["employee_name"],
        "active": bool(r["active"]) if r["active"] is not None else True,
        "employment_type": r["employment_type"],
        "min_hours": r["min_hours"],
        "max_hours": r["max_hours"],
        "daypart_availability": {d: v for d, v in avail.items() if d in DAYS and v in DAYPART_CHOICES},
        "is_minor": bool(r["is_minor"]),
        "updated_by": r["updated_by"],
        "updated_at": r["updated_at"],
    }


def get_all(restaurant_id, db_path=DB_PATH) -> dict:
    """{employee_name: settings} for everyone with a row."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_settings WHERE restaurant_id=?", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    return {r["employee_name"]: _row(r) for r in rows}


def _clean_windows(raw):
    from schedule_rules import parse_minutes
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
        if lo and hi and parse_minutes(lo) >= parse_minutes(hi):
            raise StaffSettingsError(f"{d}: the window ends before it starts")
        if lo or hi:
            out[d] = {"earliest": lo or None, "latest": hi or None}
    return out


def upsert(restaurant_id, employee_name, active=None, employment_type=None, min_hours=None,
           max_hours=None, daypart_availability=None, is_minor=None, updated_by=None,
           time_windows=None, certifications=None, preferred_dayparts=None, desired_hours=None,
           db_path=DB_PATH) -> dict:
    """Set any subset of one person's facts. Unset arguments keep their
    stored value; the caller passes only what changed."""
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
        cur = conn.execute("SELECT * FROM staff_settings WHERE restaurant_id=? AND employee_name=?",
                           (restaurant_id, name)).fetchone()
        current = _row(cur) if cur else {"active": True, "employment_type": None, "min_hours": None,
                                         "max_hours": None, "daypart_availability": {}, "is_minor": False,
                                         "time_windows": {}, "certifications": [], "preferred_dayparts": [],
                                         "desired_hours": None}
        new = {
            "active": int(bool(active)) if active is not None else int(current["active"]),
            "employment_type": (employment_type or None) if employment_type is not None else current["employment_type"],
            "min_hours": (float(min_hours) if min_hours not in (None, "") else None) if min_hours is not None else current["min_hours"],
            "max_hours": (float(max_hours) if max_hours not in (None, "") else None) if max_hours is not None else current["max_hours"],
            "daypart_availability": daypart_availability if daypart_availability is not None else current["daypart_availability"],
            "is_minor": int(bool(is_minor)) if is_minor is not None else int(current["is_minor"]),
            "time_windows": time_windows if time_windows is not None else current.get("time_windows") or {},
            "certifications": certifications if certifications is not None else current.get("certifications") or [],
            "preferred_dayparts": preferred_dayparts if preferred_dayparts is not None else current.get("preferred_dayparts") or [],
            "desired_hours": ((desired_hours if desired_hours != "" else None) if desired_hours is not None else current.get("desired_hours")),
        }
        if new["min_hours"] is not None and new["max_hours"] is not None and new["min_hours"] > new["max_hours"]:
            raise StaffSettingsError("minimum hours cannot exceed maximum hours")
        conn.execute("""INSERT INTO staff_settings (restaurant_id, employee_name, active, employment_type,
                            min_hours, max_hours, daypart_availability, is_minor, time_windows, certifications,
                            preferred_dayparts, desired_hours, updated_by, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                        ON CONFLICT(restaurant_id, employee_name) DO UPDATE SET
                            active=excluded.active, employment_type=excluded.employment_type,
                            min_hours=excluded.min_hours, max_hours=excluded.max_hours,
                            daypart_availability=excluded.daypart_availability, is_minor=excluded.is_minor,
                            time_windows=excluded.time_windows, certifications=excluded.certifications,
                            preferred_dayparts=excluded.preferred_dayparts, desired_hours=excluded.desired_hours,
                            updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                     (restaurant_id, name, new["active"], new["employment_type"], new["min_hours"],
                      new["max_hours"], json.dumps(new["daypart_availability"]), new["is_minor"],
                      json.dumps(new["time_windows"]), json.dumps(new["certifications"]),
                      json.dumps(new["preferred_dayparts"]), new["desired_hours"],
                      (updated_by or "").strip()[:120] or None))
        conn.commit()
        row = conn.execute("SELECT * FROM staff_settings WHERE restaurant_id=? AND employee_name=?",
                           (restaurant_id, name)).fetchone()
    finally:
        conn.close()
    return _row(row)


# ── the roster ─────────────────────────────────────────────────────────────

def roster(restaurant_id, db_path=DB_PATH, include_inactive=False) -> list:
    """[{name, role, shifts, last_worked, is_manual, active, settings}].

    Shift history ∪ hand-added names, each once; the most recent role wins;
    a deactivated person is left out unless asked for. This is the one list
    the generator, the roster check, the replacement pickers and the team
    screen should all read.
    """
    from models import get_manual_team_members, _cached_shifts
    seen = {}
    try:
        for sh in _cached_shifts(restaurant_id):
            n = (sh.get("employee") or "").strip()
            if not n:
                continue
            e = seen.setdefault(n, {"name": n, "role": None, "shifts": 0, "last_worked": "", "is_manual": False})
            e["shifts"] += 1
            d = sh.get("date") or ""
            if d >= e["last_worked"]:
                e["last_worked"] = d
                e["role"] = (sh.get("role") or "").strip() or e["role"]
    except Exception:
        pass
    try:
        for m in get_manual_team_members(restaurant_id, db_path=db_path):
            n = (m.get("name") or "").strip()
            if n and n not in seen:
                seen[n] = {"name": n, "role": m.get("role"), "shifts": 0, "last_worked": "", "is_manual": True}
            elif n and m.get("role") and not seen[n]["role"]:
                seen[n]["role"] = m["role"]
    except Exception:
        pass
    settings = get_all(restaurant_id, db_path=db_path)
    out = []
    for n, e in seen.items():
        st = settings.get(n) or {}
        active = st.get("active", True)
        if not active and not include_inactive:
            continue
        out.append({**e, "active": bool(active), "settings": st})
    out.sort(key=lambda e: (not e["active"], e["name"].lower()))
    return out


def active_names(restaurant_id, db_path=DB_PATH) -> list:
    return [e["name"] for e in roster(restaurant_id, db_path=db_path)]


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
    return {n: {"shifts": t["shifts"],
                "no_show_rate": round(t["no_show"] / t["shifts"], 2),
                "short_rate": round(t["short"] / t["shifts"], 2)}
            for n, t in tally.items() if t["shifts"] >= min_shifts}
