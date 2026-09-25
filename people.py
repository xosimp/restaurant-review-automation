"""people.py — one person record, composed from the stores that already
hold each fact (Friction audit #25, 9/25/26: "One employee, six places to
type them").

A person here is someone on the roster (shift history ∪ hand-added names,
staff_settings.roster) or someone with a staff login (an employee
membership). Nothing new is stored: every field is read from, and written
back to, the store that already owns it —

  role, active, hours, availability,
  certifications                 → staff_settings (the schedule reads these)
  job title, PIN                 → memberships (the staff portal reads these)
  phone, email, POS id           → staff_contacts (publish and comp/void read these)
  rating                         → the capability layer (models.set_capability)
  portal availability            → staff_availability (read only here)
  pay                            → the restaurant's per-role rates (read only)

A person's `key` is a URL-safe slug of their name ("Dana K." → "dana-k"),
the id a nav path carries ("person/dana-k", nav.py). Names are matched the
way every staff table matches them (staff_settings.name_key), so "Dana K."
and "dana k." are one person with one key.

Also here: which channel reaches a person when their week is published
(`reach`) — the app, a text they agreed to, or email as the fallback.
"""
import re

from models import DB_PATH


def _db(db_path):
    return db_path or DB_PATH


def person_key(name) -> str:
    """"Dana K." → "dana-k". Lowercase letters and digits, one dash between
    runs; "" for a name with neither."""
    import staff_settings
    return re.sub(r"[^a-z0-9]+", "-", staff_settings.name_key(name)).strip("-")


def _memberships(restaurant_id, db_path):
    from auth import get_memberships_for_restaurant
    try:
        return get_memberships_for_restaurant(restaurant_id, role="employee", include_inactive=True,
                                              db_path=_db(db_path))
    except Exception:
        return []


def _contacts(restaurant_id, db_path):
    import staff_settings
    from models import get_staff_contacts
    try:
        return {staff_settings.name_key(c["employee_name"]): c
                for c in get_staff_contacts(restaurant_id, db_path=_db(db_path))}
    except Exception:
        return {}


def list_people(restaurant_id, db_path=None) -> list:
    """[{key, name, role, active, has_login, phone, email}] — everyone on the
    roster and everyone with a staff login, each once, active first."""
    import staff_settings
    db = _db(db_path)
    seen = {}
    try:
        for e in staff_settings.roster(restaurant_id, db_path=db, include_inactive=True):
            seen[staff_settings.name_key(e["name"])] = {"name": e["name"], "role": e.get("role"),
                                                        "active": bool(e.get("active", True)),
                                                        "has_login": False}
    except Exception:
        pass
    for m in _memberships(restaurant_id, db):
        name = " ".join(str(m.get("employee_name") or "").split())
        if not name:
            continue
        k = staff_settings.name_key(name)
        live = bool(m.get("is_active")) and bool(m.get("user_is_active", 1))
        row = seen.setdefault(k, {"name": name, "role": m.get("job_role"), "active": live, "has_login": False})
        row["has_login"] = row["has_login"] or live
        if not row.get("role") and m.get("job_role"):
            row["role"] = m["job_role"]
    contacts = _contacts(restaurant_id, db)
    out = []
    for k, row in seen.items():
        c = contacts.get(k) or {}
        out.append({"key": person_key(row["name"]), "name": row["name"], "role": row.get("role") or None,
                    "active": row["active"], "has_login": row["has_login"],
                    "phone": c.get("phone") or "", "email": c.get("email") or ""})
    out.sort(key=lambda p: (not p["active"], p["name"].lower()))
    return out


def find(restaurant_id, key, db_path=None):
    """The list row for a key (or a name typed as-is), or None."""
    want = str(key or "").strip()
    if not want:
        return None
    slug = person_key(want)
    for p in list_people(restaurant_id, db_path=db_path):
        if p["key"] == want or p["key"] == slug:
            return p
    return None


def _pay_rate(restaurant_id, role, db_path):
    """The rate this person's hours are costed at: their role's rate when the
    owner set one, else the blended rate. Read only — rates are per role."""
    from models import get_role_rates, get_restaurant
    try:
        rates = get_role_rates(restaurant_id, db_path=db_path) or {}
    except Exception:
        rates = {}
    low = (role or "").strip().lower()
    for k, v in rates.items():
        if k != "_default" and k.strip().lower() == low and low:
            return {"role": k, "rate": float(v), "source": "role"}
    r = get_restaurant(restaurant_id, db_path)
    base = rates.get("_default") or (getattr(r, "hourly_rate", None) if r else None)
    return {"role": role or None, "rate": float(base) if base else None, "source": "blended"}


def get_person(restaurant_id, key, db_path=None, include_pay=True):
    """Every fact about one person, from the store that owns it, or None."""
    import staff_settings
    from models import get_capabilities, get_staff_availability
    db = _db(db_path)
    row = find(restaurant_id, key, db_path=db)
    if not row:
        return None
    name, k = row["name"], staff_settings.name_key(row["name"])
    st = staff_settings.for_name(restaurant_id, name, db_path=db) or {}
    member = next((m for m in _memberships(restaurant_id, db)
                   if staff_settings.name_key(m.get("employee_name")) == k), None)
    contact = _contacts(restaurant_id, db).get(k) or {}
    try:
        caps = {staff_settings.name_key(n): v for n, v in get_capabilities(restaurant_id, db_path=db).items()}.get(k) or {}
    except Exception:
        caps = {}
    overall = caps.get("overall") or {}
    portal = None
    try:
        for a in get_staff_availability(restaurant_id, db_path=db):
            if staff_settings.name_key(a.get("employee_name")) == k:
                portal = {"available_days": a.get("available_days"), "unavailable_days": a.get("unavailable_days"),
                          "notes": a.get("notes"), "updated_at": a.get("updated_at")}
                break
    except Exception:
        portal = None
    out = {
        "key": row["key"], "name": name, "role": row.get("role"),
        "job_title": (member or {}).get("job_role"),
        "active": row["active"], "has_login": row["has_login"],
        "membership_id": (member or {}).get("id"),
        "pin_set": bool((member or {}).get("pin_hash")),
        "phone": contact.get("phone") or "", "email": contact.get("email") or "",
        "pos_id": contact.get("pos_id") or "",
        "hours": {"employment_type": st.get("employment_type"), "min_hours": st.get("min_hours"),
                  "max_hours": st.get("max_hours"), "desired_hours": st.get("desired_hours")},
        "availability": {"dayparts": st.get("daypart_availability") or {},
                         "time_windows": st.get("time_windows") or {},
                         "preferred_dayparts": st.get("preferred_dayparts") or [],
                         "portal": portal},
        "rating": {"score": overall.get("score"), "notes": overall.get("notes"),
                   "can_close": bool((caps.get("can_close") or {}).get("flag"))},
        "certifications": st.get("certifications") or [],
        "is_minor": bool(st.get("is_minor")),
        "experienced": bool(st.get("experienced")),
        "schedule_texts": bool((member or {}).get("schedule_texts_at")),
        # What the sheet's controls may hold — the store's own lists.
        "choices": {"employment_type": list(staff_settings.EMPLOYMENT_TYPES),
                    "daypart": list(staff_settings.DAYPART_CHOICES), "days": list(staff_settings.DAYS),
                    "certifications": list(staff_settings.CERTIFICATIONS)},
    }
    if include_pay:
        out["pay_rate"] = _pay_rate(restaurant_id, row.get("role"), db)
    return out


class PersonError(ValueError):
    """A field the owner can fix, in words they can read."""


# Fields routed to staff_settings.upsert, by the name the sheet posts.
_SETTINGS_FIELDS = ("active", "employment_type", "min_hours", "max_hours", "desired_hours",
                    "daypart_availability", "time_windows", "certifications", "preferred_dayparts",
                    "is_minor", "experienced")


def update_person(restaurant_id, key, fields, updated_by=None, may_manage_logins=False, db_path=None):
    """Write each field the sheet sent to the store that owns it. Returns
    (person, changed_field_names). Raises PersonError on a bad value.

    A PIN or job title needs a staff login and the right to manage logins;
    everything else needs only the name to be on the roster."""
    import staff_settings
    from models import set_staff_contact, set_capability, CapabilityError
    db = _db(db_path)
    row = find(restaurant_id, key, db_path=db)
    if not row:
        raise PersonError("That person isn't on the roster.")
    name = row["name"]
    f = dict(fields or {})
    changed = []

    settings = {k: f[k] for k in _SETTINGS_FIELDS if k in f}
    if settings:
        try:
            staff_settings.upsert(restaurant_id, name, updated_by=updated_by, db_path=db, **settings)
        except staff_settings.StaffSettingsError as e:
            raise PersonError(str(e))
        changed += sorted(settings)

    if any(k in f for k in ("phone", "email", "pos_id")):
        email = None
        if "email" in f:
            email = str(f.get("email") or "").strip()
            if email and "@" not in email:
                raise PersonError("That doesn't look like an email address.")
        phone = None
        if "phone" in f:
            phone, perr = staff_settings.clean_phone(f.get("phone"))
            if perr:
                raise PersonError(perr)
        pos_id = str(f.get("pos_id") or "").strip() if "pos_id" in f else None
        set_staff_contact(restaurant_id, name, email, phone, db_path=db, pos_id=pos_id)
        changed += [k for k in ("phone", "email", "pos_id") if k in f]

    if "rating" in f:
        try:
            set_capability(restaurant_id, name, "overall", score=f.get("rating"), updated_by=updated_by, db_path=db)
        except CapabilityError as e:
            raise PersonError(str(e))
        changed.append("rating")
    if "can_close" in f:
        try:
            set_capability(restaurant_id, name, "can_close", flag=bool(f.get("can_close")),
                           updated_by=updated_by, db_path=db)
        except CapabilityError as e:
            raise PersonError(str(e))
        changed.append("can_close")

    if "pin" in f or "job_title" in f:
        if not may_manage_logins:
            raise PersonError("Only the account owner can change a staff login.")
        member = next((m for m in _memberships(restaurant_id, db)
                       if staff_settings.name_key(m.get("employee_name")) == staff_settings.name_key(name)), None)
        if not member:
            raise PersonError(f"{name} has no staff login yet — add one in Account → People.")
        from auth import set_membership_pin, validate_pin, PinError, update_membership_details
        if "pin" in f:
            try:
                set_membership_pin(member["id"], restaurant_id, validate_pin(str(f.get("pin") or "")), db_path=db)
            except PinError as pe:
                raise PersonError(str(pe))
            changed.append("pin")
        if "job_title" in f:
            update_membership_details(member["id"], restaurant_id, job_role=str(f.get("job_title") or "").strip(),
                                      db_path=db)
            changed.append("job_title")
    return get_person(restaurant_id, row["key"], db_path=db), changed


# ── Reaching a person when their week goes out (Friction audit #17) ─────────

def staff_sms_ready() -> bool:
    """Staff texts go only on their own registered messaging service
    (notify.send_sms use_case="staff"): a schedule text on the owner-alert
    campaign is the mixed use carriers filter. Unset → no texts, email is
    the fallback."""
    import notify
    return bool(notify.TWILIO_SID and notify.TWILIO_TOKEN and notify.TWILIO_STAFF_MESSAGING_SERVICE_SID)


def reach(restaurant_id, names, db_path=None) -> dict:
    """{name: {"push_user_id", "sms", "email"}} for each scheduled name.

    push_user_id — the staff login whose phone has the app registered.
    sms          — the number they signed up with, ONLY when they ticked
                   "text me when my schedule is posted" (memberships.
                   schedule_texts_at) and staff texts are configured.
    email        — the address on file (staff_contacts), the fallback.
    """
    import staff_settings
    from models import get_conn
    db = _db(db_path)
    contacts = _contacts(restaurant_id, db)
    members = {}
    for m in _memberships(restaurant_id, db):
        if m.get("is_active") and m.get("user_is_active", 1) and m.get("employee_name"):
            members[staff_settings.name_key(m["employee_name"])] = m
    tokens, phones = set(), {}
    ids = [int(m["user_id"]) for m in members.values() if m.get("user_id")]
    if ids:
        conn = get_conn(db)
        try:
            marks = ",".join("?" * len(ids))
            try:
                tokens = {r["user_id"] for r in conn.execute(
                    f"SELECT DISTINCT user_id FROM device_tokens WHERE disabled_reason IS NULL AND user_id IN ({marks})",
                    ids).fetchall()}
            except Exception:
                tokens = set()
            phones = {r["id"]: r["phone"] for r in conn.execute(
                f"SELECT id, phone FROM users WHERE id IN ({marks})", ids).fetchall()}
        finally:
            conn.close()
    sms_on = staff_sms_ready()
    out = {}
    for n in names or []:
        k = staff_settings.name_key(n)
        m = members.get(k) or {}
        uid = m.get("user_id")
        sms = None
        if sms_on and m.get("schedule_texts_at"):
            sms = (phones.get(uid) or m.get("claimed_by_phone") or "").strip() or None
        out[n] = {"push_user_id": uid if uid in tokens else None, "sms": sms,
                  "email": ((contacts.get(k) or {}).get("email") or "").strip() or None}
    return out


def reach_summary(reachable: dict) -> dict:
    """How many of the scheduled names each channel reaches, for the send
    button's caption ("14 of 16 reachable")."""
    total = len(reachable)
    ok = sum(1 for r in reachable.values() if r.get("push_user_id") or r.get("sms") or r.get("email"))
    return {"total": total, "reachable": ok,
            "by_app": sum(1 for r in reachable.values() if r.get("push_user_id")),
            "by_text": sum(1 for r in reachable.values() if not r.get("push_user_id") and r.get("sms")),
            "by_email": sum(1 for r in reachable.values()
                            if not r.get("push_user_id") and not r.get("sms") and r.get("email")),
            "unreachable": [n for n, r in reachable.items()
                            if not (r.get("push_user_id") or r.get("sms") or r.get("email"))]}
