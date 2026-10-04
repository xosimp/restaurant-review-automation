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


def change_source(user) -> str:
    """change_log's `source` for a change a login made: "admin" for an admin
    or support login and anyone acting through view-as, "owner" for an
    account holder, else "manager" (permissions.answer_authority — one rule
    for whose words and whose changes these are)."""
    try:
        import permissions
        auth_ = permissions.answer_authority(user)
    except Exception:
        auth_ = "delegate"
    return {"admin": "admin", "principal": "owner"}.get(auth_, "manager")


def answer_authority(user) -> str:
    """permissions.answer_authority for a stored answer: "admin" (incl.
    view-as), "principal" or "delegate"; "system" for none (a job)."""
    if not user:
        return "system"
    try:
        import permissions
        return permissions.answer_authority(user)
    except Exception:
        return "delegate"


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
    # Two names that slug alike ("Jo-Ann" and "Jo Ann", "José" and "Jos")
    # each get a key of their own — the slug plus a short, stable tag of the
    # name — so each opens its own record (F2-13).
    counts = {}
    for p in out:
        counts[p["key"]] = counts.get(p["key"], 0) + 1
    for p in out:
        if counts[p["key"]] > 1:
            p["key"] = _distinct_key(p["name"])
    return out


def _distinct_key(name) -> str:
    import hashlib
    tag = hashlib.sha1(" ".join(str(name or "").split()).casefold().encode("utf-8")).hexdigest()[:4]
    return f"{person_key(name)}-{tag}".strip("-")


class AmbiguousPerson(LookupError):
    """Two people on the roster share one key ("Jo-Ann" and "Jo Ann" are
    both jo-ann; "José" and "Jos" both jos)."""


def find(restaurant_id, key, db_path=None):
    """The list row for a key (or a name typed as-is), or None. Raises
    AmbiguousPerson when the key names more than one person: an edit to
    the second used to land on the first (F2-13)."""
    want = str(key or "").strip()
    if not want:
        return None
    slug = person_key(want)
    everyone = list_people(restaurant_id, db_path=db_path)
    # A key as the list gave it, or a name typed exactly as it is on file.
    for p in everyone:
        if p["key"] == want:
            return p
    typed = " ".join(want.split()).casefold()
    exact = [p for p in everyone if " ".join(p["name"].split()).casefold() == typed]
    if len(exact) == 1:
        return exact[0]
    hits = [p for p in everyone if person_key(p["name"]) == slug]
    if len(hits) > 1:
        raise AmbiguousPerson(f"Two people share that name ({', '.join(p['name'] for p in hits[:3])}) "
                              "— open them from the list.")
    return hits[0] if hits else None


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
    row = find(restaurant_id, key, db_path=db)      # AmbiguousPerson propagates to the route
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
                    "certifications": list(staff_settings.CERTIFICATIONS),
                    # How each certificate reads: "Floor manager (can run the
                    # shift)" apart from the food-safety card (schedule audit
                    # 10/3/26 E-15) — the sheet's chips say what the roster's do.
                    "certification_labels": dict(staff_settings.CERTIFICATION_LABELS)},
    }
    if include_pay:
        out["pay_rate"] = _pay_rate(restaurant_id, row.get("role"), db)
    # What else is known about them (memory audit 9/29/26): the roles they
    # hold beyond the shifts (a promotion, "trained on bar"), their record
    # of taking covers, the guests who named them (confirmed by the owner),
    # and their attendance on the shifts somebody watched — "unknown", never
    # a clean record, when nobody did.
    try:
        out["roles_held"] = [{"role": r["role"], "since": r["since"], "primary": r["primary"]}
                             for r in held_roles(restaurant_id, name, db_path=db_path)]
    except Exception:
        out["roles_held"] = []
    try:
        cov = cover_record(restaurant_id, db_path=db_path).get(k) or {}
        out["covers"] = {"taken": int(cov.get("accepted") or 0), "declined": int(cov.get("declined") or 0),
                         "days": 180}
    except Exception:
        out["covers"] = None
    # Their task sheets over 30 days (task_sheets.py): sheets, lines done,
    # late ticks, critical misses and the last few misses — beside the
    # operational score, where "who is reliable" is already decided.
    try:
        import task_sheets
        out["task_record"] = task_sheets.person_record(restaurant_id, name, days=30, db_path=db)
    except Exception:
        out["task_record"] = None
    try:
        from datetime import date as _d_pm, timedelta as _td_pm
        _since_pm = (_d_pm.today() - _td_pm(days=PERSON_MENTION_DAYS)).isoformat()
        out["guest_mentions"] = [m for m in mentions(restaurant_id, status="confirmed", db_path=db_path,
                                                     since=_since_pm)
                                 if staff_settings.name_key(m["name"]) == k][:5]
    except Exception:
        out["guest_mentions"] = []
    try:
        rel = {staff_settings.name_key(n): r for n, r in staff_settings.reliability(restaurant_id, db_path=db).items()}.get(k)
        # Lateness over the CLOCKED shifts only, and call-outs apart from
        # no-shows (schedule audit 10/3/26 D-44, L-18): "late to 3 of 12
        # clocked shifts", "called out 1 time" — late_rate None below the
        # floor of clocked shifts, said as "—", never 0%.
        out["attendance"] = ({"known": True, "shifts": rel["shifts"], "missed": rel["no_shows"],
                              "late": rel.get("late", 0), "no_show_rate": rel["no_show_rate"],
                              "called_out": rel.get("called_out", 0), "late_shifts": rel.get("late_shifts", 0),
                              "late_rate": rel.get("late_rate"),
                              "unreliable": rel["unreliable"], "last_miss": rel.get("last_miss")}
                             if rel else {"known": False})
    except Exception:
        out["attendance"] = {"known": False}
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
    try:
        row = find(restaurant_id, key, db_path=db)
    except AmbiguousPerson as e:
        raise PersonError(str(e))
    if not row:
        raise PersonError("That person isn't on the roster.")
    name = row["name"]
    f = dict(fields or {})
    changed = []
    # Every refusal before any write: a PIN refused after the contacts and
    # rating were already saved answered 400 over a partial write (F2-13).
    member = None
    if "pin" in f or "job_title" in f:
        if not may_manage_logins:
            raise PersonError("Only the account owner can change a staff login.")
        # The person's live login: the active one (one per name, B1's rule),
        # else the newest that was neither unlinked nor deleted. The first
        # row under the name — an old, switched-off login — used to win and
        # take the PIN (LG-13).
        mine = [m for m in _memberships(restaurant_id, db)
                if staff_settings.name_key(m.get("employee_name")) == staff_settings.name_key(name)
                and not m.get("unlinked_at") and not m.get("deleted_at")]
        mine.sort(key=lambda m: (bool(m.get("is_active")), m.get("id") or 0), reverse=True)
        member = mine[0] if mine else None
        if not member:
            raise PersonError(f"{name} has no staff login yet — add one in Account → People.")
        if "pin" in f:
            from auth import validate_pin, PinError
            try:
                validate_pin(str(f.get("pin") or ""))
            except PinError as pe:
                raise PersonError(str(pe))
    if "email" in f:
        email = str(f.get("email") or "").strip()
        if email and "@" not in email:
            raise PersonError("That doesn't look like an email address.")
    if "phone" in f:
        _p, perr = staff_settings.clean_phone(f.get("phone"))
        if perr:
            raise PersonError(perr)
    # Role and pay rate have no store here: a role is the one the shifts
    # carry, a rate is the role's (Labor → Targets & rates). The same value
    # echoed back is fine; a changed one is refused in words — it used to be
    # dropped while the sheet said "Saved." (F3-5).
    if "role" in f:
        want = " ".join(str(f.get("role") or "").split())
        if want.casefold() != " ".join(str(row.get("role") or "").split()).casefold():
            raise PersonError("A role comes from the shifts someone works, so it can't be changed here"
                              + (" — their staff-portal job title can." if row.get("has_login") else "."))
    if "pay_rate" in f:
        v = f.get("pay_rate")
        if isinstance(v, dict):
            v = v.get("rate")
        if v not in (None, ""):
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise PersonError("Pay rate must be a number.")
            current = _pay_rate(restaurant_id, row.get("role"), db).get("rate")
            if current is None or abs(v - float(current)) > 0.005:
                raise PersonError(f"Pay is set per role — change the {row.get('role') or 'role'} rate in "
                                  "Labor → Targets & rates.")

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
        if pos_id:
            # The id the owner typed is the person's alias on the connected
            # POS (memory audit 9/29/26, identity): the next sync resolves
            # that id to this person, whatever the POS spells them.
            try:
                link_pos_id(restaurant_id, name, pos_id, db_path=db_path)
            except PeopleError as e:
                raise PersonError(str(e))

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

    if member is not None:
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


# ── Reaching a person on staff (Friction #17; employee audit C4/M8/H14) ─────
#
# ONE rule for every notice to one employee — a published week, a request
# decided, a swap asked of them, a reminder, an announcement, a message:
#
#   1. The app, when their login has a live device here (push.fire_push
#      narrowed to that login, a staff alert type). It counts only when
#      Apple took it: if no device did, `on_failed` runs the rest of the
#      chain (COM-12/LG-33). A queued push used to be "told".
#   2. A text, when they ticked the consent box AND it covers this notice's
#      purpose (preferences.staff_sms_scope, COM-09) AND the staff messaging
#      service is configured (staff_sms_ready). Between 10pm and 8am
#      restaurant time it is held until 8am (staff_reminders releases it),
#      unless it is about a shift before then (M8/COM-11).
#   3. Email to the address on file.
#
# Between 10pm and 8am a push still goes, silently (data["quiet"]: no sound,
# no banner) — except an urgent announcement.

STAFF_QUIET_START_HOUR = 22     # 10pm restaurant-local
STAFF_QUIET_END_HOUR = 8        # 8am

# What a notice is FOR — the consent scope a text needs. A reminder is worth
# a push or nothing (an hour-before text per shift is not what anyone
# consented to); the rest fall back to email when there is no text consent.
_TYPE_PURPOSE = {"staff_schedule": "schedule", "staff_request": "request", "staff_reminder": "reminder",
                 "staff_announcement": "announcement", "staff_urgent": "announcement",
                 "staff_message": "message", "staff_notice": "notice"}
_PUSH_ONLY_PURPOSES = frozenset({"reminder"})
# tell()'s email_type → the alert type, for callers that predate the types.
_EMAIL_TYPE_ALERT = {"shift_request": "staff_request", "time_off": "staff_request",
                     "staff_schedule": "staff_schedule", "staff_announcement": "staff_announcement",
                     "staff_message": "staff_message"}
# A caller's `nav` that names a tab rather than a staff path ("inbox",
# "messages") is read as the tab.
_TAB_ALIASES = {"messages": "inbox", "message": "inbox", "schedule": "today", "profile": "me"}


def _is_urgent(priority) -> bool:
    """"urgent", "p1", 1 or 0 (push.P1_ACT_NOW / P0) → an urgent notice."""
    if isinstance(priority, bool):
        return priority
    if isinstance(priority, int):
        return priority <= 1
    return str(priority or "").strip().lower() in ("urgent", "p1", "p0", "1", "0")


def _staff_data(alert_type, nav=None, data=None) -> dict:
    """The push payload fields for a staff notice, from a caller's `nav`
    and `data`: every key rides the payload; `kind` is the short type
    ("request", "announcement"…); a `nav` that is a tab name, not a
    "staff/…" path, becomes the `tab` (push.staff_nav then builds the path
    with the request, announcement, thread or sheet id)."""
    import push
    out = {}
    for src in (data, nav):
        if isinstance(src, dict):
            out.update({k: v for k, v in src.items() if v is not None})
        elif isinstance(src, str) and src.strip():
            out["nav"] = src.strip()
    nav_s = str(out.get("nav") or "").strip()
    if nav_s and not nav_s.startswith("staff/"):
        tab = _TAB_ALIASES.get(nav_s.lower(), nav_s.lower())
        out.pop("nav", None)
        if tab in push.STAFF_TABS:
            out.setdefault("tab", tab)
    short = alert_type[len("staff_"):]
    kind = str(out.get("kind") or "").strip().lower()
    out["kind"] = kind[len("staff_"):] if kind.startswith("staff_") else (kind or short)
    return out

# The consent the staff app shows beside its "text me" switch
# (GET /staff/api/preferences → schedule_texts_consent). The app sends
# consent_version=preferences.STAFF_SMS_CONSENT_VERSION with the switch, and
# only that wording covers request notices. Register the staff A2P campaign
# with this same sentence (H14 — an ops task); /staff-sms-optin-preview shows
# it verbatim to the carrier reviewer. It carries every disclosure a reviewer
# checks for in the consent itself (what is texted, frequency, rates,
# STOP/HELP — the owner campaign's 30896 rejection was a missing frequency).
# The frequency and HELP were added 10/2/26, before the switch was ever
# shown (sms_available is false until the campaign is registered), so the
# scope a version-2 consent covers is unchanged.
STAFF_SMS_CONSENT_TEXT = ("Text me about my schedule and my requests: a posted or changed week, swaps, "
                          "open shifts and time off. Msg frequency varies, usually 1–4 a week. "
                          "Msg & data rates may apply. Reply HELP for help, STOP to stop.")


def staff_sms_ready() -> bool:
    """Staff texts go only on their own registered messaging service
    (notify.send_sms use_case="staff"): a schedule text on the owner-alert
    campaign is the mixed use carriers filter. Unset → no texts, email is
    the fallback."""
    import notify
    return bool(notify.TWILIO_SID and notify.TWILIO_TOKEN and notify.TWILIO_STAFF_MESSAGING_SERVICE_SID)


def staff_alert_type(kind) -> str:
    """"request" or "staff_request" → "staff_request"; anything unknown is
    a "staff_notice"."""
    import push
    k = str(kind or "").strip().lower()
    if k in push.STAFF_ALERT_TYPES:
        return k
    k = f"staff_{k}"
    return k if k in push.STAFF_ALERT_TYPES else "staff_notice"


def staff_local_now(restaurant_id):
    """The restaurant's wall clock (naive local) — the one clock staff quiet
    hours read. Tests pin it here."""
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(restaurant_id, naive=True)


def staff_quiet(now_local) -> bool:
    """Whether a staff notice now would land between 10pm and 8am."""
    return now_local.hour >= STAFF_QUIET_START_HOUR or now_local.hour < STAFF_QUIET_END_HOUR


def staff_release_at(now_local):
    """The next 8am (naive local) — when a text held overnight goes."""
    from datetime import datetime as _dt, timedelta as _td
    at = _dt.combine(now_local.date(), _dt.min.time()).replace(hour=STAFF_QUIET_END_HOUR)
    return at if now_local < at else at + _td(days=1)


def _before_release(now_local, shift_date) -> bool:
    """A notice about a shift on or before the morning a held text would go
    out is a same-day change: it cannot wait for 8am."""
    if not shift_date or now_local is None:
        return False
    from datetime import date as _date
    try:
        d = _date.fromisoformat(str(shift_date)[:10])
    except ValueError:
        return False
    return d <= staff_release_at(now_local).date()


def reach(restaurant_id, names, purpose=None, db_path=None) -> dict:
    """{name: {"push_user_id", "sms", "sms_scope", "email"}} for each name.

    push_user_id — the login with a live device here that a staff push
                   reaches (push.staff_device_users: what fire_push delivers
                   to, an active membership).
    sms          — the number they signed up with, ONLY when they ticked
                   the "text me" box (memberships.schedule_texts_at), staff
                   texts are configured, the number never replied STOP, and
                   — with `purpose` — their consent covers it.
    sms_scope    — the purposes that consent covers (preferences.
                   staff_sms_scope; a consent from before scopes covers
                   "schedule" only). tell/deliver re-check it per notice.
    email        — the address on file (staff_contacts), the fallback.
    """
    import staff_settings
    import preferences
    from models import get_conn
    db = _db(db_path)
    contacts = _contacts(restaurant_id, db)
    members = {}
    for m in _memberships(restaurant_id, db):
        if m.get("is_active") and m.get("user_is_active", 1) and m.get("employee_name"):
            members[staff_settings.name_key(m["employee_name"])] = m
    tokens, phones, scopes = set(), {}, {}
    ids = [int(m["user_id"]) for m in members.values() if m.get("user_id")]
    if ids:
        try:
            import push
            tokens = push.staff_device_users(restaurant_id, ids, db_path=db)
        except Exception:
            tokens = set()
        conn = get_conn(db)
        try:
            marks = ",".join("?" * len(ids))
            phones = {r["id"]: r["phone"] for r in conn.execute(
                f"SELECT id, phone FROM users WHERE id IN ({marks})", ids).fetchall()}
        finally:
            conn.close()
        scopes = preferences.staff_sms_scopes([(u, restaurant_id) for u in ids], db_path=db)
    sms_on = staff_sms_ready()
    out = {}
    for n in names or []:
        k = staff_settings.name_key(n)
        m = members.get(k) or {}
        uid = m.get("user_id")
        sms, scope = None, []
        if sms_on and m.get("schedule_texts_at"):
            sms = (phones.get(uid) or m.get("claimed_by_phone") or "").strip() or None
            if sms:
                legacy = preferences.STAFF_SMS_LEGACY_SCOPE
                scope = list(scopes.get((int(uid), int(restaurant_id)), legacy) if uid else legacy)
            if not scope or (purpose is not None and purpose not in scope):
                sms = None
        out[n] = {"push_user_id": uid if uid in tokens else None, "sms": sms, "sms_scope": scope if sms else [],
                  "email": ((contacts.get(k) or {}).get("email") or "").strip() or None}
    # A number that replied STOP is not a text channel, whatever its consent
    # says (#107): staff reach checked the tick-box alone, so a STOP was
    # texted anyway and "reachable by text" counted it.
    candidates = [c["sms"] for c in out.values() if c["sms"]]
    if candidates:
        import notify
        stopped = notify.sms_stopped_phones(candidates, db_path=db)
        for c in out.values():
            if c["sms"] in stopped:
                c["sms"], c["sms_scope"] = None, []
    return out


def _place(restaurant_id, db):
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id, db)
        return (getattr(r, "location_name", None) or getattr(r, "name", None) or "your restaurant") if r \
            else "your restaurant"
    except Exception:
        return "your restaurant"


def _send_staff_text(restaurant_id, phone, text) -> bool:
    import notify
    with notify.sms_context(restaurant_id):
        return bool(notify.send_sms(phone, text, use_case="staff"))


def _send_staff_email(restaurant_id, email, place, title, lines, email_type="staff_notice") -> bool:
    import html as _h
    import emails
    from config import base_url
    lines = [str(x) for x in (lines or []) if str(x or "").strip()] or [title]
    html = emails.report_shell(kicker=_h.escape(place), title=_h.escape(title), subtitle="",
                               sections=[emails.report_paragraph(_h.escape(x)) for x in lines],
                               cta_label="Open the staff portal", cta_url=base_url() + "/staff")
    res = emails.deliver(email_type=email_type, restaurant_id=restaurant_id, payload={
        "from": emails.sender("client"), "to": [email],
        "subject": f"{title} — {place}", "preheader": lines[0][:120], "html": html})
    return bool(getattr(res, "ok", False))


class _Once:
    """A caller's outcome hook, run at most once (the push pool and the
    synchronous path may both reach the end)."""

    def __init__(self, fn):
        import threading
        self._fn, self._lock, self.done = fn, threading.Lock(), False

    def __call__(self, outcome):
        with self._lock:
            if self.done:
                return
            self.done = True
        if self._fn is not None:
            try:
                self._fn(outcome)
            except Exception as e:
                print(f"[people] outcome hook failed: {e!r}")


def deliver(restaurant_id, name, channel, *, alert_type, title, body, data=None, purpose=None,
            email_lines=None, email_type="staff_notice", sms_text=None, sms_undo=None, sms_meta=None,
            email_send=None, shift_date=None, on_outcome=None, now_local=None, db_path=None):
    """Deliver one notice to one employee by the rule above. Returns
    "push" (queued to their phone; the text/email chain runs by itself if no
    device takes it), "sms", "sms_held" (goes at 8am), "email" or None.

    `on_outcome(channel_or_None)` runs once with what actually happened —
    "push" only when Apple accepted it, else the fallback's result. A None
    means nobody was told: tell a manager there (the "Open shift, nobody
    told" pattern).

    `sms_text` (str or callable(phone) -> str), `sms_undo()` and
    `sms_meta()` let a caller with its own text (a published week's share
    link) use the chain; `email_send()` -> bool likewise replaces the
    generic email. `shift_date` (ISO) marks a notice about a shift: one on
    or before the next 8am is sent through the night."""
    import push
    db = _db(db_path)
    channel = channel or {}
    alert_type = alert_type if alert_type in push.STAFF_ALERT_TYPES else staff_alert_type(alert_type)
    purpose = purpose or _TYPE_PURPOSE.get(alert_type, "notice")
    done = _Once(on_outcome)
    try:
        now_local = now_local or staff_local_now(restaurant_id)
    except Exception:
        now_local = None
    quiet = bool(now_local is not None and staff_quiet(now_local)) and alert_type != "staff_urgent"
    place = _place(restaurant_id, db)
    lines = [str(x) for x in ([body] + list(email_lines or [])) if str(x or "").strip()] or [title]

    def _fallback():
        if purpose in _PUSH_ONLY_PURPOSES:
            return None
        phone = channel.get("sms")
        scope = channel.get("sms_scope")
        if scope is None:
            import preferences
            scope = preferences.STAFF_SMS_LEGACY_SCOPE
        if phone and purpose in scope:
            try:
                if callable(sms_text):
                    text = sms_text(phone)
                else:
                    from config import base_url
                    text = sms_text or (f"{place}: {' '.join(lines)} {base_url()}/staff "
                                        "Reply STOP to stop these texts.")
                if quiet and not _before_release(now_local, shift_date):
                    import staff_reminders
                    from time_utils import restaurant_tz
                    from models import get_restaurant
                    release_local = staff_release_at(now_local)
                    tz = restaurant_tz(get_restaurant(restaurant_id, db))
                    if staff_reminders.hold_text(restaurant_id, name, purpose, title, text, lines,
                                                 release_local.replace(tzinfo=tz), email_type=email_type,
                                                 meta=(sms_meta() if callable(sms_meta) else None), db_path=db):
                        return "sms_held"
                elif _send_staff_text(restaurant_id, phone, text):
                    return "sms"
            except Exception as e:
                print(f"[people] staff text failed rid={restaurant_id}: {e!r}")
            if callable(sms_undo):
                try:
                    sms_undo()
                except Exception:
                    pass
        email = channel.get("email")
        if email:
            try:
                ok = email_send() if callable(email_send) else \
                    _send_staff_email(restaurant_id, email, place, title, lines, email_type)
                if ok:
                    return "email"
            except Exception as e:
                print(f"[people] staff email failed rid={restaurant_id}: {e!r}")
        return None

    uid = channel.get("push_user_id")
    if uid:
        pdata = _staff_data(alert_type, data=data)
        if quiet:
            pdata["quiet"] = True
        try:
            queued = push.fire_push(restaurant_id, alert_type, f"{title} — {place}", " ".join(lines)[:220],
                                    data=pdata, db_path=db, user_ids=[uid],
                                    on_delivered=lambda: done("push"),
                                    on_failed=lambda: done(_fallback()))
        except Exception as e:
            print(f"[people] staff push failed rid={restaurant_id}: {e!r}")
            queued = 0
        if queued:
            return "push"
    res = _fallback()
    done(res)
    return res


def tell_staff(restaurant_id, employee_name, kind, title, body, *, nav=None, data=None, priority=None, lines=None,
               purpose=None, shift_date=None, email_type=None, channel=None, on_outcome=None, db_path=None):
    """THE helper for telling one employee something (employee audit C4).

    kind     — "schedule" | "request" | "notice" | "reminder" |
               "announcement" | "urgent" | "message" (or the full
               push.STAFF_ALERT_TYPES name, "staff_request").
    title    — the headline ("Your swap went through").
    body     — one sentence: the push and text body, the email's first line.
    nav      — where the staff app opens: {"tab": "today|tasks|requests|me|
               inbox", "request_id" | "announcement_id" | "thread_id" |
               "assignment_id": id, "event": "swap_asked", ...}, or just a
               tab name ("inbox"); every key rides the push payload. Without
               a tab, the kind's default (push.STAFF_DEFAULT_TAB).
    data     — more payload fields (merged under nav's).
    priority — "urgent" (or 1 / "p1") sends it as "staff_urgent": P1, breaks
               Focus, never quiet, nobody can mute it. Otherwise the kind's.
    lines    — further sentences for the email.
    purpose  — the consent scope a text needs; defaults from the kind
               (schedule, request; others never text).
    shift_date — ISO date of the shift this is about: on or before the next
               8am it is sent through the night.
    on_outcome — callable(channel or None), once, with what really happened.

    Returns "push" | "sms" | "sms_held" | "email" | None (see deliver)."""
    db = _db(db_path)
    base = staff_alert_type(kind)
    urgent = _is_urgent(priority)
    alert_type = "staff_urgent" if urgent else base
    purpose = purpose or _TYPE_PURPOSE.get(base, "notice")
    if channel is None:
        try:
            channel = reach(restaurant_id, [employee_name], db_path=db).get(employee_name) or {}
        except Exception as e:
            print(f"[people] reach failed rid={restaurant_id}: {e!r}")
            channel = {}
    payload = _staff_data(base, nav=nav, data=data)       # `kind` stays what it is about
    if urgent:
        payload["urgent"] = True
    return deliver(restaurant_id, employee_name, channel, alert_type=alert_type, title=title, body=body,
                   data=payload, purpose=purpose, email_lines=lines,
                   email_type=email_type or "staff_notice", shift_date=shift_date, on_outcome=on_outcome,
                   db_path=db)


def tell(restaurant_id, name, title, lines, *, email_type="staff_notice", channel=None, kind=None, nav=None,
         data=None, priority=None, purpose=None, shift_date=None, on_outcome=None, db_path=None):
    """One notice to one person on staff, on the channel `reach` picks —
    the app, a text they agreed to, email as the fallback — the same order
    a published week uses: tell_staff with the first line as the body.
    Returns "push", "sms", "sms_held", "email" or None.

    Staff notices went by email only, so a person with no address on file
    was never told their drop was approved, their swap went through or
    their time off was decided (F2-5, F2-12). `lines` are plain sentences;
    the first is the push/text body. Without `kind`, `email_type` names it
    (shift_request / time_off → a request notice, opening Requests)."""
    lines = [str(x) for x in (lines or []) if str(x or "").strip()] or [title]
    return tell_staff(restaurant_id, name, kind or _EMAIL_TYPE_ALERT.get(email_type, "staff_notice"), title,
                      lines[0], lines=lines[1:], nav=nav, data=data, priority=priority, purpose=purpose,
                      shift_date=shift_date, email_type=email_type, channel=channel, on_outcome=on_outcome,
                      db_path=db_path)


def tell_deciders(restaurant_id, title, body, *, request_id=None, request_kind=None, alert_type="shift_request",
                  data=None, db_path=None) -> int:
    """THE helper for telling the people who decide staff requests — every
    console login holding SCHEDULE_DRAFT, whether or not their brief is on
    (strategy_jobs._reach(deciders=True), F2-6). With `request_id` and
    `request_kind` ("shift" | "swap" | "time_off" | …) the push offers
    Approve / Deny and opens that request (push.CATEGORY_REQUEST). Never a
    staff-app phone: _reach reads the console's devices only. Returns how
    many people it reached. Never raises."""
    try:
        import strategy_jobs
        from permissions import SCHEDULE_DRAFT
        payload = dict(data or {})
        payload.setdefault("tab", "labor")
        if request_id:
            payload.update(request_id=request_id, request_kind=request_kind or "shift")
        return int(strategy_jobs._reach(restaurant_id, alert_type, title, body, payload, _db(db_path),
                                        lines=[body], deciders=True, permissions=[SCHEDULE_DRAFT]) or 0)
    except Exception as e:
        print(f"[people] deciders notice failed rid={restaurant_id}: {e!r}")
        return 0


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


# ═══ Identity: one person per employee, whatever the POS calls them ═════════
#
# Memory audit 9/29/26 ("identity"). An employee used to BE a display name.
# The POS's own ids were dropped at import, so a rename stranded every
# rating, setting, note and tenure record: Gia Mia's owner rated 17 people,
# the POS spelled them differently, and the engine scheduled everyone as
# unrated; a 15-year-old flagged 14-15 under "Jake S." who re-imported as
# "Jacob Smith" lost the minor rules with the flag; a five-year server
# under a new spelling was one departure and one new hire. Now:
#
#   people           one row per person per restaurant — `display_name` is
#                    the spelling every name-keyed store uses, `name_key`
#                    its staff_settings.name_key; a merged person keeps its
#                    row with `merged_into`, for the record
#   person_aliases   every way a source has named them: (source,
#                    external_id) for a POS's own id, (source, name_key) for
#                    a spelling — kept forever
#   person_questions a possible match ("Kim T." / "Kim Tran") the OWNER
#                    decides. Nothing similar is ever merged automatically
#   person_merges    every merge and rename, with what moved and what was
#                    folded, so either can be read back
#
# Ingest (resolve_rows) resolves by the POS id first, then the exact
# name_key (the person's own spelling or an alias); a similar name opens a
# question. A POS id we know arriving under a new spelling is the same
# person renamed — exact, so it is applied: every name-keyed store is
# re-pointed (rename_person). The stores keep their names (the display) and
# carry a person_id beside them (stamp_person_ids).

import json as _json
import logging as _logging

_log = _logging.getLogger(__name__)

# Every store that holds something about a person under their name. `cols`
# are the name columns (the first also gets person_id); `unique` the columns
# (besides restaurant_id) that make a row one person's own, and `fold` how
# two rows that land on one person after a merge become one. Adding a store
# that keeps a name means adding it here — merge_people and rename_person
# re-point exactly this list (auth.py's memberships comment names the same).
NAME_STORES = (
    {"table": "staff_settings", "cols": ("employee_name",), "unique": ("employee_name",), "fold": "fill"},
    {"table": "staff_capabilities", "cols": ("employee_name",), "unique": ("employee_name", "attribute"),
     "fold": "newest"},
    {"table": "staff_availability", "cols": ("employee_name",), "unique": ("employee_name",), "fold": "newest"},
    {"table": "staff_contacts", "cols": ("employee_name",), "unique": ("employee_name",), "fold": "fill"},
    {"table": "staff_notes", "cols": ("employee_name",), "unique": ("employee_name",), "fold": "notes"},
    {"table": "staff_first_seen", "cols": ("employee_name",), "unique": ("employee_name",), "fold": "tenure"},
    {"table": "manual_team_members", "cols": ("employee_name",), "unique": ("employee_name",), "fold": "fill"},
    {"table": "staff_time_off", "cols": ("employee_name",)},
    # The person asked to swap (target_name) is another person's mention:
    # renamed with them, cleared — not deleted — when they are erased.
    {"table": "shift_change_requests", "cols": ("employee_name", "replacement_name", "target_name")},
    {"table": "schedule_shares", "cols": ("employee_name",)},
    {"table": "memberships", "cols": ("employee_name",)},
    {"table": "staff_pairs", "cols": ("employee_a", "employee_b"), "fold": "pairs", "no_person_id": True},
    {"table": "capability_changes", "cols": ("subject",), "where": "kind='rating'"},
    {"table": "shift_facts", "cols": ("employee_name",), "key": "employee_key",
     "unique": ("business_date", "employee_key", "shift_start"), "fold": "drop"},
    {"table": "attendance_events", "cols": ("employee_name",), "key": "employee_key",
     "unique": ("employee_key", "business_date", "shift_start"), "fold": "newest"},
    {"table": "person_signals", "cols": ("employee_name",), "key": "employee_key",
     "unique": ("kind", "ref", "employee_key"), "fold": "drop"},
    {"table": "person_roles", "cols": ("employee_name",), "key": "employee_key",
     "unique": ("employee_key", "role"), "fold": "fill"},
    {"table": "person_quarters", "cols": ("employee_name",), "key": "employee_key",
     "unique": ("employee_key", "quarter"), "fold": "sum"},
    # A server's floor section per shift (models.shift_sections, V12).
    {"table": "shift_sections", "cols": ("employee_name",), "key": "employee_key",
     "unique": ("date", "employee_key", "shift_start"), "fold": "newest"},
    # What the draft learned about a person, and the owner's dismissals of
    # it (memory re-audit 9/29/26, INVENTORY-2): the pattern key carries the
    # name, so these are re-keyed, not just re-pointed (_repoint_patterns).
    {"table": "schedule_standing_patterns", "cols": ("employee",), "fold": "patterns", "no_create": True},
    {"table": "schedule_pattern_dismissals", "cols": ("employee",), "fold": "patterns", "no_create": True},
    # The scheduling memory and its observation log (schedule_memory,
    # schedule audit 10/3/26 L-29): re-pointed with the person, deleted with
    # them on an erase. A memory's key carries their people id where they
    # have one, so a rename keeps the row; the nightly consolidation rebuilds
    # what a merge folded.
    {"table": "schedule_observations", "cols": ("person",), "no_create": True},
    {"table": "schedule_memory", "cols": ("person",), "no_create": True},
    # The staff app (employee audit fix round, 10/2/26). Each is keyed by the
    # name (or by the login, which a rename leaves alone) and carries no
    # person_id; a rename re-points the name, an erase deletes the rows. An
    # open shift offered to them; their running-late reports; the
    # announcements they were sent; their thread with the managers (its
    # messages go with it — `children`); held texts and reminder claims
    # (a held text is delivered to its employee_name); their certificates.
    {"table": "shift_offers", "cols": ("name",), "key": "name_key", "no_person_id": True},
    {"table": "staff_running_late", "cols": ("employee_name",), "key": "employee_key", "no_person_id": True},
    {"table": "staff_announcement_recipients", "cols": ("employee_name",), "no_person_id": True},
    {"table": "staff_threads", "cols": ("employee_name",), "no_person_id": True,
     "children": (("staff_thread_messages", "thread_id"),)},
    {"table": "staff_notices", "cols": ("employee_name",), "no_person_id": True},
    {"table": "staff_certs", "cols": ("employee_name",), "key": "employee_key", "unique": ("employee_key", "cert"),
     "fold": "newest", "no_person_id": True},
)

# Stores keyed only by a staff login (memberships.id), with no name to
# match: erase_person deletes the rows of every login the person held here
# (after the NAME_STORES pass; a rename needs nothing — the login keeps its
# id). (table, membership column, child tables deleted first as (table, fk)).
MEMBERSHIP_STORES = (
    ("staff_shift_pulse", "membership_id", ()),
    ("staff_calendar_links", "membership_id", ()),
    ("staff_language", "membership_id", ()),
    ("staff_running_late", "membership_id", ()),
    ("staff_announcement_recipients", "membership_id", ()),
    ("staff_threads", "membership_id", (("staff_thread_messages", "thread_id"),)),
)

# A source name as aliases store it.
POS_SOURCES = ("rpower", "rpower_payroll", "toast", "square", "clover")
SOURCES = POS_SOURCES + ("upload", "manual", "portal", "merge", "rename", "backfill")

QUESTION_KINDS = ("same_person", "same_name")


class PeopleError(ValueError):
    """An identity change the owner can fix, in words they can read."""


def _conn(db_path=None):
    """models.get_conn at call time (CLAUDE.md, bound imports), with the
    name_key rule registered as an SQL function so a store is matched on
    exactly the key Python matches it on."""
    import models
    import staff_settings
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        conn.create_function("cav_name_key", 1, staff_settings.name_key, deterministic=True)
    except TypeError:                      # an sqlite3 without `deterministic`
        conn.create_function("cav_name_key", 1, staff_settings.name_key)
    return conn


def _nk(name) -> str:
    import staff_settings
    return staff_settings.name_key(name)


def _clean(name) -> str:
    return " ".join(str(name or "").split())[:120]


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _cols(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_people(db_path=None):
    """The identity tables and a person_id on every name-keyed store — at
    boot (models.init_db), never on a request. Kept forever: a person and
    every name they have had is the key every other people memory needs."""
    conn = _conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS people (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            organization_id  INTEGER,
            display_name     TEXT    NOT NULL,
            name_key         TEXT    NOT NULL,
            active           INTEGER NOT NULL DEFAULT 1,
            merged_into      INTEGER REFERENCES people(id),
            created_via      TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_people_live_key ON people(restaurant_id, name_key) "
                     "WHERE merged_into IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_people_rest ON people(restaurant_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS person_aliases (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id      INTEGER NOT NULL REFERENCES people(id),
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            source         TEXT    NOT NULL,
            external_id    TEXT,
            name_key       TEXT    NOT NULL,
            display_name   TEXT,
            first_seen     TEXT,
            last_seen      TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_person_alias_ext ON person_aliases"
                     "(restaurant_id, source, external_id) WHERE external_id IS NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_person_alias_spelling ON person_aliases"
                     "(restaurant_id, person_id, source, name_key) WHERE external_id IS NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_person_alias_key ON person_aliases(restaurant_id, name_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_person_alias_person ON person_aliases(person_id)")
        conn.execute("""CREATE TABLE IF NOT EXISTS person_questions (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            person_a       INTEGER NOT NULL,
            person_b       INTEGER NOT NULL,
            kind           TEXT    NOT NULL DEFAULT 'same_person',
            reason         TEXT,
            evidence_json  TEXT,
            status         TEXT    NOT NULL DEFAULT 'open',
            created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            answered_at    TEXT,
            answered_by    INTEGER,
            answered_authority TEXT,
            UNIQUE(restaurant_id, person_a, person_b, kind)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_person_questions_open ON person_questions(restaurant_id, status)")
        conn.execute("""CREATE TABLE IF NOT EXISTS person_merges (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            kind           TEXT    NOT NULL,
            from_person    INTEGER NOT NULL,
            into_person    INTEGER NOT NULL,
            from_name      TEXT,
            into_name      TEXT,
            moved_json     TEXT,
            actor_user_id  INTEGER,
            source         TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_person_merges_rest ON person_merges(restaurant_id, created_at)")
        _init_person_stores(conn)
        # Whose answer each is (permissions.answer_authority) — added to a
        # database an earlier build of these tables made.
        for table, col, typ in (("person_questions", "answered_authority", "TEXT"),
                                ("person_signals", "authority", "TEXT"),
                                ("person_quarters", "covers_taken", "INTEGER NOT NULL DEFAULT 0"),
                                ("person_quarters", "covers_declined", "INTEGER NOT NULL DEFAULT 0"),
                                ("person_quarters", "mentions_positive", "INTEGER NOT NULL DEFAULT 0"),
                                ("person_quarters", "mentions_negative", "INTEGER NOT NULL DEFAULT 0"),
                                # An undone merge (memory re-audit 9/29/26, INVENTORY-11).
                                ("person_merges", "undone_at", "TEXT"),
                                ("person_merges", "undone_by", "INTEGER")):
            if col not in _cols(conn, table):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
        _add_person_id_columns(conn)
        conn.commit()
    finally:
        conn.close()


def _add_person_id_columns(conn):
    """A person_id on every name-keyed store that exists yet."""
    have = _tables(conn)
    for store in NAME_STORES:
        if store.get("no_person_id") or store["table"] not in have:
            continue
        if "person_id" not in _cols(conn, store["table"]):
            try:
                conn.execute(f"ALTER TABLE {store['table']} ADD COLUMN person_id INTEGER")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    raise


def ensure_person_id_columns(db_path=None):
    """For stores created after init_db (auth's `memberships`, made by
    init_auth, which boots after init_db): on a fresh database init_people
    ran before the table existed, so its person_id arrived only on the
    second boot. init_auth calls this at its end."""
    conn = _conn(db_path)
    try:
        _add_person_id_columns(conn)
        conn.commit()
    finally:
        conn.close()


def _init_person_stores(conn):
    """The per-person stores this module owns beside identity (created here
    so NAME_STORES can find them): the roles a person holds (a promotion,
    "trained on bar from 9/1"), the signals about them (covers taken,
    guests naming them) and their quarterly summaries — the per-shift and
    attendance tables are shift_facts' and attendance's own."""
    conn.execute("""CREATE TABLE IF NOT EXISTS person_roles (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
        person_id       INTEGER,
        employee_name   TEXT    NOT NULL,
        employee_key    TEXT    NOT NULL,
        role            TEXT    NOT NULL,
        source          TEXT    NOT NULL DEFAULT 'owner',
        qualified_since TEXT,
        is_primary      INTEGER NOT NULL DEFAULT 0,
        created_by      INTEGER,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        removed_at      TEXT,
        UNIQUE(restaurant_id, employee_key, role)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS person_signals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
        person_id       INTEGER,
        employee_name   TEXT    NOT NULL,
        employee_key    TEXT    NOT NULL,
        kind            TEXT    NOT NULL,
        polarity        INTEGER,
        signal_date     TEXT    NOT NULL,
        ref             TEXT    NOT NULL DEFAULT '',
        status          TEXT    NOT NULL DEFAULT 'confirmed',
        detail          TEXT,
        created_by      INTEGER,
        authority       TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(restaurant_id, kind, ref, employee_key)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_person_signals_person ON person_signals(restaurant_id, employee_key, kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_person_signals_date ON person_signals(signal_date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS person_quarters (
        restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
        person_id        INTEGER,
        employee_name    TEXT,
        employee_key     TEXT    NOT NULL,
        quarter          TEXT    NOT NULL,
        shifts           INTEGER NOT NULL DEFAULT 0,
        hours            REAL,
        scheduled_shifts INTEGER NOT NULL DEFAULT 0,
        roles_json       TEXT,
        dayparts_json    TEXT,
        weekdays_json    TEXT,
        watched          INTEGER NOT NULL DEFAULT 0,
        no_shows         INTEGER NOT NULL DEFAULT 0,
        called_out       INTEGER NOT NULL DEFAULT 0,
        late             INTEGER NOT NULL DEFAULT 0,
        left_early       INTEGER NOT NULL DEFAULT 0,
        covered          INTEGER NOT NULL DEFAULT 0,
        covers_taken     INTEGER NOT NULL DEFAULT 0,
        covers_declined  INTEGER NOT NULL DEFAULT 0,
        mentions_positive INTEGER NOT NULL DEFAULT 0,
        mentions_negative INTEGER NOT NULL DEFAULT 0,
        first_date       TEXT,
        last_date        TEXT,
        updated_at       TEXT    NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (restaurant_id, employee_key, quarter)
    )""")


# ── the registry, in memory for one ingest ──────────────────────────────────

class _Index:
    """The restaurant's live people and aliases, read once per resolution."""

    def __init__(self, conn, restaurant_id):
        self.rid = restaurant_id
        self.people = {}            # pid -> dict (live and merged)
        self.by_key = {}            # name_key -> {live pid}
        self.by_ext = {}            # (source, external_id) -> pid
        self.ext_of = {}            # (pid, source) -> external_id
        for r in conn.execute("SELECT * FROM people WHERE restaurant_id=?", (restaurant_id,)).fetchall():
            self.people[r["id"]] = dict(r)
        for pid, p in self.people.items():
            if p["merged_into"] is None:
                self.by_key.setdefault(p["name_key"], set()).add(pid)
        for a in conn.execute("SELECT person_id, source, external_id, name_key FROM person_aliases "
                              "WHERE restaurant_id=?", (restaurant_id,)).fetchall():
            pid = self.live(a["person_id"])
            if pid is None:
                continue
            if a["external_id"]:
                self.by_ext[(a["source"], a["external_id"])] = pid
                self.ext_of[(pid, a["source"])] = a["external_id"]
            if a["name_key"]:
                self.by_key.setdefault(a["name_key"], set()).add(pid)

    def live(self, pid):
        """The live person `pid` is (following merges), or None."""
        seen = set()
        while pid is not None and pid in self.people and pid not in seen:
            seen.add(pid)
            nxt = self.people[pid].get("merged_into")
            if nxt is None:
                return pid
            pid = nxt
        return None

    def for_key(self, key) -> set:
        return {p for p in (self.by_key.get(key) or set()) if self.live(p) == p}

    def keys_of(self, pid) -> set:
        return {k for k, ps in self.by_key.items() if pid in ps}


def _create_person(conn, idx, name, via, seen_on=None):
    try:
        row = conn.execute("SELECT organization_id FROM restaurants WHERE id=?", (idx.rid,)).fetchone()
        org = row[0] if row else None
    except Exception:
        org = None
    display = _clean(name)
    cur = conn.execute("INSERT INTO people (restaurant_id, organization_id, display_name, name_key, created_via) "
                       "VALUES (?,?,?,?,?)", (idx.rid, org, display, _nk(display), via))
    pid = cur.lastrowid
    row = {"id": pid, "restaurant_id": idx.rid, "organization_id": org, "display_name": display,
           "name_key": _nk(display), "active": 1, "merged_into": None, "created_via": via}
    idx.people[pid] = row
    idx.by_key.setdefault(row["name_key"], set()).add(pid)
    return pid


def _alias(conn, idx, pid, source, name, external_id=None, seen_on=None):
    """Record (or refresh) how `source` names this person."""
    key, ext = _nk(name), (str(external_id).strip() or None) if external_id is not None else None
    if not key:
        return
    day = str(seen_on or "")[:10] or None
    if ext:
        row = conn.execute("SELECT id, person_id FROM person_aliases WHERE restaurant_id=? AND source=? "
                           "AND external_id=?", (idx.rid, source, ext)).fetchone()
        if row:
            conn.execute("UPDATE person_aliases SET person_id=?, name_key=?, display_name=?, "
                         "first_seen=COALESCE(MIN(first_seen, ?), first_seen, ?), "
                         "last_seen=CASE WHEN last_seen IS NULL OR ? > last_seen THEN ? ELSE last_seen END WHERE id=?",
                         (pid, key, _clean(name), day, day, day, day, row["id"]))
        else:
            conn.execute("INSERT INTO person_aliases (person_id, restaurant_id, source, external_id, name_key, "
                         "display_name, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                         (pid, idx.rid, source, ext, key, _clean(name), day, day))
        idx.by_ext[(source, ext)] = pid
        idx.ext_of[(pid, source)] = ext
    else:
        conn.execute("INSERT OR IGNORE INTO person_aliases (person_id, restaurant_id, source, name_key, display_name, "
                     "first_seen, last_seen) VALUES (?,?,?,?,?,?,?)",
                     (pid, idx.rid, source, key, _clean(name), day, day))
        if day:
            conn.execute("UPDATE person_aliases SET last_seen=CASE WHEN last_seen IS NULL OR ? > last_seen THEN ? "
                         "ELSE last_seen END WHERE restaurant_id=? AND person_id=? AND source=? AND name_key=? "
                         "AND external_id IS NULL", (day, day, idx.rid, pid, source, key))
    idx.by_key.setdefault(key, set()).add(pid)


# ── similar names: a question, never a merge ────────────────────────────────

NICKNAMES = {
    "jake": "jacob", "jim": "james", "jimmy": "james", "jamie": "james", "mike": "michael", "mikey": "michael",
    "bob": "robert", "rob": "robert", "robbie": "robert", "bill": "william", "will": "william", "billy": "william",
    "liz": "elizabeth", "beth": "elizabeth", "betsy": "elizabeth", "kate": "katherine", "katie": "katherine",
    "kathy": "katherine", "tony": "anthony", "chris": "christopher", "matt": "matthew", "dan": "daniel",
    "danny": "daniel", "dave": "david", "joe": "joseph", "joey": "joseph", "sam": "samuel", "alex": "alexander",
    "nick": "nicholas", "steve": "steven", "tom": "thomas", "tommy": "thomas", "ben": "benjamin",
    "andy": "andrew", "drew": "andrew", "jen": "jennifer", "jenny": "jennifer", "sue": "susan",
    "pat": "patricia", "rick": "richard", "rich": "richard", "ricky": "richard", "ed": "edward",
    "eddie": "edward", "ted": "edward", "greg": "gregory", "josh": "joshua", "zach": "zachary",
    "abby": "abigail", "maggie": "margaret", "meg": "margaret", "peggy": "margaret", "gabe": "gabriel",
    "vic": "victor", "manny": "manuel", "pepe": "jose", "lupe": "guadalupe", "memo": "guillermo",
    "paco": "francisco", "pancho": "francisco", "nacho": "ignacio", "chuy": "jesus", "lalo": "eduardo",
    "tim": "timothy", "timmy": "timothy", "nate": "nathan", "charlie": "charles", "chuck": "charles",
    "fred": "frederick", "larry": "lawrence", "ron": "ronald", "don": "donald", "ken": "kenneth",
    "jon": "jonathan", "johnny": "john", "jack": "john", "cathy": "catherine", "cindy": "cynthia",
    "debbie": "deborah", "deb": "deborah", "vicky": "victoria", "tori": "victoria", "mandy": "amanda",
    "becky": "rebecca", "becca": "rebecca", "sandy": "sandra", "jess": "jessica", "jessie": "jessica",
}


def _tokens(name):
    import re
    return [t for t in re.split(r"[^a-z0-9]+", _nk(name).replace(".", " ")) if t]


def _first(tok):
    return NICKNAMES.get(tok, tok)


def similar(a_name, b_name):
    """Why two different spellings might be one person, or None. Only ever
    the reason for a QUESTION to the owner — "Kim T." and "Kim Tran" are the
    Gia Mia case, but so are two real Maria G.s."""
    import difflib
    ta, tb = _tokens(a_name), _tokens(b_name)
    if not ta or not tb or ta == tb:
        return None
    fa, fb = ta[0], tb[0]
    la, lb = (ta[-1] if len(ta) > 1 else ""), (tb[-1] if len(tb) > 1 else "")
    same_first = fa == fb or _first(fa) == _first(fb)
    if same_first and la and lb and (len(la) == 1 or len(lb) == 1) and la[0] == lb[0]:
        return "same first name and last initial"
    if same_first and (not la or not lb) and (la or lb):
        return "the same first name, one with no last name"
    if la and lb and la == lb and (fa[0] == fb[0]) and (len(fa) == 1 or len(fb) == 1 or _first(fa) == _first(fb)):
        return "the same last name and first initial"
    if la and lb and la[0] == lb[0] and fa != fb and _first(fa) == _first(fb):
        return "a nickname of the same first name"
    if difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= 0.85:
        return "a close spelling"
    return None


def _ask(conn, idx, pid_new, pid_old, kind, reason, evidence=None):
    """Open one question for the owner (idempotent per pair and kind; a pair
    they answered "different" is never asked again)."""
    a, b = int(pid_new), int(pid_old)
    if a == b:
        return None
    lo, hi = min(a, b), max(a, b)
    # Two exact lookups on the UNIQUE(restaurant_id, person_a, person_b,
    # kind) index — the stored order (hi, lo) first. The one OR-query over
    # both orders scanned the table, and a big sync asks thousands of these
    # (a 25-page Toast window: 72s of one test, 10/2/26).
    answered = None
    for pa, pb in ((hi, lo), (lo, hi)):
        answered = conn.execute("SELECT id FROM person_questions WHERE restaurant_id=? AND person_a=? "
                                "AND person_b=? AND kind=?", (idx.rid, pa, pb, kind)).fetchone()
        if answered:
            break
    if answered:
        return answered["id"]
    cur = conn.execute("INSERT OR IGNORE INTO person_questions (restaurant_id, person_a, person_b, kind, reason, "
                       "evidence_json) VALUES (?,?,?,?,?,?)",
                       (idx.rid, hi, lo, kind, reason, _json.dumps(evidence or {}, default=str)[:2000]))
    return cur.lastrowid


def _question_similar(conn, idx, pid):
    """A question for every live person whose name is like this new one's."""
    me = idx.people.get(pid)
    if not me:
        return 0
    n = 0
    for other, p in idx.people.items():
        if other == pid or p.get("merged_into") is not None:
            continue
        why = similar(me["display_name"], p["display_name"])
        if why:
            _ask(conn, idx, pid, other, "same_person",
                 f"{me['display_name']} and {p['display_name']}: {why}",
                 {"new": me["display_name"], "existing": p["display_name"]})
            n += 1
    return n


def _looks_like_name(name, external_id=None) -> bool:
    """A real spelling, not a POS code standing in for one: a letter in it,
    not the id itself, not "Unknown", and not one all-caps token with
    digits ("20CY2G" — Simple EJ's first sync, 9/28/26)."""
    n = _clean(name)
    if not n or not any(ch.isalpha() for ch in n) or n.lower() in ("unknown", "staff"):
        return False
    # A labelled string is never a name: "Gideon Kopalchick · overall" (a
    # rating log's old subject) or a code like "can_close" (10/1/26).
    if "·" in n or "_" in n:
        return False
    if external_id is not None and n.strip().lower() == str(external_id).strip().lower():
        return False
    if " " not in n and any(ch.isdigit() for ch in n) and n.upper() == n:
        return False
    return True


# ── ingest ───────────────────────────────────────────────────────────────────

def resolve_rows(restaurant_id, rows, source, db_path=None, seen_on=None) -> dict:
    """Give every shift row its person, in place: `person_id`, and `employee`
    set to that person's display name (the spelling every store is keyed
    by). By the POS's own id first (`employee_ext_id`, and RPOWER's
    `employee_payroll_id` as its second id), then the exact name_key — the
    person's own spelling or one of their aliases. A row with neither match
    is a new person; a new person with a name like an existing one's opens a
    question for the owner — never a merge. A known id under a new spelling
    renames the person (every store follows); one whose new spelling is
    already somebody else's is asked about instead. A row the POS could not
    name (`employee_named` "0": RPOWER's payroll-code fallback) keeps its
    code and no person. Returns {people, created, renamed, questions}."""
    src = str(source or "upload").strip().lower() or "upload"
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return {"people": 0, "created": 0, "renamed": 0, "questions": 0}
    conn = _conn(db_path)
    out = {"people": 0, "created": 0, "renamed": 0, "questions": 0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        idx = _Index(conn, restaurant_id)
        groups = {}
        for r in rows:
            name = _clean(r.get("employee"))
            ext = str(r.get("employee_ext_id") or "").strip() or None
            if not name and not ext:
                continue
            groups.setdefault((ext, name), []).append(r)
        latest = {}
        # Oldest spelling first: a POS that renamed someone mid-window has
        # their older punches under the old name, and the newest spelling
        # is the one that should stand.
        order = sorted(groups.items(), key=lambda kv: max((str(x.get("date") or "")[:10] for x in kv[1]), default=""))
        for (ext, name), grp in order:
            day = max((str(x.get("date") or "")[:10] for x in grp), default="") or seen_on
            named = all(str(x.get("employee_named", "1")) not in ("0", "False", "false") for x in grp)
            named = named and _looks_like_name(name, ext)
            payroll = next((str(x.get("employee_payroll_id") or "").strip() for x in grp
                            if str(x.get("employee_payroll_id") or "").strip()), None)
            pid = _resolve_one(conn, idx, src, ext, payroll, name, named, day, out)
            if pid is None:
                for x in grp:
                    x["person_id"] = None
                continue
            display = idx.people[pid]["display_name"]
            for x in grp:
                x["person_id"] = pid
                x["employee"] = display
            latest[pid] = max(latest.get(pid, ""), day or "")
        conn.commit()
        out["people"] = len(latest)
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return out


def _resolve_one(conn, idx, src, ext, payroll, name, named, day, out):
    key = _nk(name)
    if ext:
        pid = idx.by_ext.get((src, ext))
        if pid is None and payroll and src == "rpower":
            pid = idx.by_ext.get(("rpower_payroll", payroll))
        if pid is not None:
            pid = idx.live(pid)
        if pid is not None:
            person = idx.people[pid]
            if named and key and key != person["name_key"]:
                others = idx.for_key(key) - {pid}
                if others:
                    # The POS's new spelling is already somebody else's name:
                    # the id says one person, the name another. The owner
                    # decides; the rows stay on the person the id names.
                    for o in others:
                        _ask(conn, idx, pid, o, "same_person",
                             f"{src.upper()} now calls {person['display_name']} \"{name}\" — the same name as "
                             f"{idx.people[o]['display_name']}", {"source": src, "new_spelling": name})
                        out["questions"] += 1
                elif key not in idx.keys_of(pid):
                    _rename_in(conn, idx, pid, name, source=src)
                    out["renamed"] += 1
            if named:
                _alias(conn, idx, pid, src, name, external_id=ext, seen_on=day)
            if payroll and src == "rpower":
                _alias(conn, idx, pid, "rpower_payroll", name if named else idx.people[pid]["display_name"],
                       external_id=payroll, seen_on=day)
            return pid
        if not named:
            return None
        cands = idx.for_key(key)
        free = [c for c in cands if (c, src) not in idx.ext_of]
        if len(free) == 1:
            pid = free[0]
        elif cands:
            # One spelling, two people in this POS (two Maria G.s): the new
            # one gets a name of its own the owner can change, and a question.
            pid = _create_person(conn, idx, f"{name} #{str(ext)[-4:]}", f"pos:{src}", day)
            out["created"] += 1
            for c in cands:
                _ask(conn, idx, pid, c, "same_name",
                     f"Two people in {src.upper()} are named {name} — one is shown as "
                     f"{idx.people[pid]['display_name']} until you rename them", {"source": src})
                out["questions"] += 1
        else:
            pid = _create_person(conn, idx, name, f"pos:{src}", day)
            out["created"] += 1
            out["questions"] += _question_similar(conn, idx, pid)
        _alias(conn, idx, pid, src, name, external_id=ext, seen_on=day)
        if payroll and src == "rpower":
            _alias(conn, idx, pid, "rpower_payroll", name, external_id=payroll, seen_on=day)
        return pid
    if not key:
        return None
    cands = idx.for_key(key)
    if len(cands) == 1:
        pid = next(iter(cands))
        if key != idx.people[pid]["name_key"]:
            _alias(conn, idx, pid, src, name, seen_on=day)
        return pid
    if len(cands) > 1:
        return None                      # one spelling, two people: never guessed
    pid = _create_person(conn, idx, name, src, day)
    out["created"] += 1
    out["questions"] += _question_similar(conn, idx, pid)
    return pid


def person_id_for(restaurant_id, name, db_path=None, create=True, source="manual"):
    """The live person a name means (exact name_key or alias), creating one
    when there is none and `create`; None when the spelling is two people."""
    rows = [{"employee": name}]
    if create:
        resolve_rows(restaurant_id, rows, source, db_path=db_path)
        return rows[0].get("person_id")
    conn = _conn(db_path)
    try:
        idx = _Index(conn, restaurant_id)
    finally:
        conn.close()
    cands = idx.for_key(_nk(name))
    return next(iter(cands)) if len(cands) == 1 else None


def canonical_names(restaurant_id, names, db_path=None) -> dict:
    """{name as given: the display name of the one live person it means},
    for a spelling a store or an old schedule still carries (a person
    renamed since). A name that means nobody, or two people, maps to
    itself."""
    conn = _conn(db_path)
    try:
        idx = _Index(conn, restaurant_id)
    finally:
        conn.close()
    out = {}
    for n in names or []:
        cands = idx.for_key(_nk(n))
        out[n] = idx.people[next(iter(cands))]["display_name"] if len(cands) == 1 else n
    return out


def canonical_key(restaurant_id, name, db_path=None) -> str:
    """name_key of the person a spelling means (its display name's key)."""
    return _nk(canonical_names(restaurant_id, [name], db_path=db_path).get(name) or name)


# ── one key per person for the schedule's rules (schedule audit 10/3/26) ─────
#
# The rules keyed every per-person fact — availability, time off, a note, a
# rating, a station skill, a salaried entry — by the name as typed, so a
# spelling this module knows for someone (an alias, the name before a POS
# rename, an owner's "Mike" for the roster's "Michael") matched nobody: a
# legal time-off block stopped applying with no error (D-8), a salaried
# manager punching as "Gabe" was held to overtime (D-7), and "Kim T." and
# "Kimberly Tran" were checked as two people (E-25). identity_index maps
# every spelling of each roster person to the roster's own spelling;
# spellings() gives every spelling of the one person a name means.

def fold(name) -> str:
    """A name the way the schedule's rules key it (schedule_rules.
    Constraints.key): spacing collapsed, lower case."""
    return " ".join(str(name or "").split()).lower()


def identity_index(restaurant_id, names, db_path=None) -> dict:
    """{"key_of": {spelling: roster key}, "linked": {roster key: group key},
    "questions": [{"a", "b", "reason"}]} for the roster `names` (as
    staff_settings.roster spells them).

    key_of — every spelling of the one live person each roster name means
    (their display name, every alias, every name before a rename or a
    merge), folded, to that roster name's fold. A spelling two roster
    people answer to maps to neither — never guessed.
    linked — roster people an OPEN "same person?" question joins (kind
    same_person; a same_name question is two people the POS told apart):
    until the owner answers, the rules' sweep reads them as one person —
    overlaps, rest, hours, days in a row (E-25). Each maps to its group's
    first key. Roster names that are one person already are linked too.
    Raises when the identity tables can't be read; every caller catches
    it and keys by the plain fold."""
    out = {"key_of": {}, "linked": {}, "questions": []}
    names = [n for n in (names or []) if str(n or "").strip()]
    if not names:
        return out
    conn = _conn(db_path)
    try:
        idx = _Index(conn, restaurant_id)
        try:
            open_q = conn.execute("SELECT person_a, person_b, reason FROM person_questions WHERE restaurant_id=? "
                                  "AND status='open' AND kind='same_person'", (restaurant_id,)).fetchall()
        except Exception:
            open_q = []
    finally:
        conn.close()
    roster_of = {}                      # pid -> [roster folds]
    for n in names:
        cands = idx.for_key(_nk(n))
        if len(cands) == 1:
            roster_of.setdefault(next(iter(cands)), []).append(fold(n))
    parent = {}

    def _find(k):
        while parent.get(k, k) != k:
            k = parent[k]
        return k

    def _join(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            lo, hi = sorted((ra, rb))
            parent[hi] = lo
    owner = {}                          # spelling -> roster fold (None: two roster people answer to it)
    for pid, folds in roster_of.items():
        for f in folds[1:]:
            _join(folds[0], f)          # one person under two roster spellings
        spellings_ = set(idx.keys_of(pid)) | {fold(idx.people[pid]["display_name"])}
        for s in spellings_:
            s = fold(s)
            if s in owner and owner[s] != folds[0]:
                owner[s] = None
            else:
                owner.setdefault(s, folds[0])
    roster_folds = {fold(n) for n in names}
    for s, f in owner.items():
        if f is not None and s not in roster_folds:
            out["key_of"][s] = f
    for q in open_q:
        a, b = idx.live(q["person_a"]), idx.live(q["person_b"])
        if a is None or b is None or a == b or a not in roster_of or b not in roster_of:
            continue
        _join(roster_of[a][0], roster_of[b][0])
        out["questions"].append({"a": idx.people[a]["display_name"], "b": idx.people[b]["display_name"],
                                 "reason": q["reason"]})
    for k in list(parent):
        rep = _find(k)
        out["linked"][k] = rep
        out["linked"][rep] = rep
    return out


def spellings(restaurant_id, names, db_path=None) -> dict:
    """{name: {every folded spelling of the one live person it means}} —
    the name's own fold always among them; a name that means nobody, or two
    people, is just itself. For a list kept by name outside the people
    stores (the salaried staff, per-person pay rates): "Gabe Huerta" on a
    punch is the "Gabriel Huerta" an entry was linked to (D-7)."""
    out = {n: {fold(n)} for n in (names or []) if str(n or "").strip()}
    if not out:
        return out
    conn = _conn(db_path)
    try:
        idx = _Index(conn, restaurant_id)
    finally:
        conn.close()
    for n in out:
        cands = idx.for_key(_nk(n))
        if len(cands) == 1:
            pid = next(iter(cands))
            out[n] |= {fold(k) for k in idx.keys_of(pid)} | {fold(idx.people[pid]["display_name"])}
    return out


def spellings_of_ids(restaurant_id, person_ids, db_path=None) -> set:
    """Every folded spelling of the live people `person_ids` are (a merged
    id follows its merge) — for an entry linked to a person when it was
    saved (models.salaried_staff person_id)."""
    conn = _conn(db_path)
    try:
        idx = _Index(conn, restaurant_id)
    finally:
        conn.close()
    out = set()
    for pid in person_ids or []:
        try:
            live = idx.live(int(pid))
        except (TypeError, ValueError):
            continue
        if live is not None:
            out |= {fold(k) for k in idx.keys_of(live)} | {fold(idx.people[live]["display_name"])}
    return out


def link_name(restaurant_id, name, db_path=None):
    """(person_id, display name) of the one live person `name` means — its
    own spelling or an alias, exactly — or (None, name). For a list the
    owner keeps by name outside the people stores (the salaried staff): the
    entry is saved under the spelling every store uses, with the person's
    id, so a rename or merge carries it (D-7). Never a guess: a similar
    name is a suggestion (similar_on_roster), not a link."""
    try:
        pid = person_id_for(restaurant_id, name, db_path=db_path, create=False)
    except Exception:
        pid = None
    if pid is None:
        return None, " ".join(str(name or "").split())
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT display_name FROM people WHERE id=?", (pid,)).fetchone()
    finally:
        conn.close()
    return pid, (row["display_name"] if row else " ".join(str(name or "").split()))


def who_is(restaurant_id, spellings_, db_path=None) -> dict:
    """{spelling: display name of the one live person it means here, or
    None} — exactly (their own spelling or an alias); a spelling two people
    answer to means nobody. One read for many spellings (a sibling site's
    names matched to this site's people, models.sibling_location_shifts)."""
    conn = _conn(db_path)
    try:
        idx = _Index(conn, restaurant_id)
    finally:
        conn.close()
    out = {}
    for sp in spellings_ or []:
        cands = idx.for_key(_nk(sp))
        out[sp] = idx.people[next(iter(cands))]["display_name"] if len(cands) == 1 else None
    return out


def similar_on_roster(name, roster_names) -> list:
    """Roster spellings that might be the same person as `name` (similar):
    a suggestion to put to the owner — never applied."""
    return [r for r in (roster_names or []) if r and fold(r) != fold(name) and similar(name, r)]


# ── re-pointing every store: merge and rename ───────────────────────────────

def _store_rows(conn, rid, store, col, keys, pid):
    marks = ",".join("?" * len(keys))
    has_pid = (not store.get("no_person_id")) and col == store["cols"][0] and "person_id" in _cols(conn, store["table"])
    where = f"restaurant_id=? AND (cav_name_key({col}) IN ({marks})" + (" OR person_id=?" if has_pid and pid else "") + ")"
    if store.get("where"):
        where += f" AND {store['where']}"
    args = [rid, *keys] + ([pid] if has_pid and pid else [])
    return where, args, has_pid


def _json_maps_sum(a, b) -> str:
    """Two {label: count} JSON maps added together (roles, dayparts,
    weekdays of one quarter)."""
    out = {}
    for raw in (a, b):
        try:
            m = _json.loads(raw or "{}") or {}
        except (TypeError, ValueError):
            m = {}
        for k, v in (m.items() if isinstance(m, dict) else []):
            try:
                out[k] = out.get(k, 0) + (float(v) if isinstance(v, float) else int(v))
            except (TypeError, ValueError):
                continue
    return _json.dumps(out)


def _fold(conn, store, keep, gone):
    """Fold one person's row into the surviving one's (same unique slot)."""
    table, how = store["table"], store.get("fold")
    skip = {"rowid", "id", "restaurant_id", "person_id", "created_at", *store["cols"], *(store.get("unique") or ()),
            store.get("key") or ""}
    cols = [c for c in gone.keys() if c not in skip]
    if how == "newest":
        stamp = "updated_at" if "updated_at" in gone.keys() else ("created_at" if "created_at" in gone.keys() else None)
        if stamp and str(gone[stamp] or "") > str(keep[stamp] or "") and cols:
            conn.execute(f"UPDATE {table} SET " + ", ".join(f"{c}=?" for c in cols) + " WHERE rowid=?",
                         (*[gone[c] for c in cols], keep["rowid"]))
    elif how == "fill" and cols:
        conn.execute(f"UPDATE {table} SET " + ", ".join(f"{c}=COALESCE(NULLIF({c}, ''), ?)" for c in cols)
                     + " WHERE rowid=?", (*[gone[c] for c in cols], keep["rowid"]))
    elif how == "notes":
        import models
        parts = models._note_parts(keep) + [p for p in models._note_parts(gone)
                                            if p["text"].lower() not in {q["text"].lower() for q in models._note_parts(keep)}]
        models._write_parts(conn, keep["rowid"] if "id" not in keep.keys() else keep["id"], parts, None)
    elif how == "tenure":
        conn.execute("UPDATE staff_first_seen SET first_seen=MIN(first_seen, ?), "
                     "last_seen=MAX(COALESCE(last_seen, ''), COALESCE(?, '')), shifts_seen=shifts_seen+? WHERE rowid=?",
                     (gone["first_seen"], gone["last_seen"], int(gone["shifts_seen"] or 0), keep["rowid"]))
    elif how == "sum":
        # Every count, both people's maps and the wider of the two date
        # ranges (memory re-audit 9/29/26, FORGET-13): a quarter past its raw
        # window is never re-summarised, so what a merge drops here — the
        # covers, the guest mentions, the roles, the first day — is gone.
        nums = [c for c in ("shifts", "hours", "scheduled_shifts", "watched", "no_shows", "called_out", "late",
                            "left_early", "covered", "covers_taken", "covers_declined", "mentions_positive",
                            "mentions_negative") if c in gone.keys()]
        sets, vals = [f"{c}=COALESCE({c},0)+COALESCE(?,0)" for c in nums], [gone[c] for c in nums]
        for c in ("roles_json", "dayparts_json", "weekdays_json"):
            if c in gone.keys():
                sets.append(f"{c}=?")
                vals.append(_json_maps_sum(keep[c], gone[c]))
        for c, pick in (("first_date", min), ("last_date", max)):
            if c in gone.keys():
                both = [d for d in (keep[c], gone[c]) if d]
                sets.append(f"{c}=?")
                vals.append(pick(both) if both else None)
        if sets:
            conn.execute(f"UPDATE {table} SET " + ", ".join(sets) + " WHERE rowid=?", (*vals, keep["rowid"]))
    conn.execute(f"DELETE FROM {table} WHERE rowid=?", (gone["rowid"],))


_PATTERN_STATUS_RANK = {"ruled": 3, "active": 2, "retest": 1.5, "dormant": 1, "retired": 0}


def _repoint_patterns(conn, rid, store, keys, from_pid, into_name, into_pid) -> dict:
    """A person's learned schedule patterns (schedule_standing_patterns) and
    the owner's dismissals of them (schedule_pattern_dismissals) moved to
    `into_name`: their keys carry the name, so they are re-keyed with it —
    a rename used to strand "Bob S. off Tuesday dinner" under a name no
    schedule carried any more, where it read as kept every week (memory
    re-audit 9/29/26, INVENTORY-2). Two rows landing on one key fold: the
    counts add, the earliest learning and newest confirmation stand, and a
    rule beats a habit. {moved, folded, rows, keeps}, the undo's record."""
    import schedule_intel as _si
    table = store["table"]
    rec = {"moved": 0, "folded": [], "rows": [], "keeps": []}
    tcols = _cols(conn, table)
    if table == "schedule_standing_patterns":
        found = conn.execute("SELECT rowid AS rowid, * FROM schedule_standing_patterns WHERE restaurant_id=?",
                             (rid,)).fetchall()
        for row in found:
            if not (_nk(row["employee"]) in keys or (from_pid and "person_id" in tcols and row["person_id"] == from_pid)):
                continue
            if not (row["employee"] or "").strip():
                continue
            try:
                det = _json.loads(row["detail"] or "{}") if "detail" in row.keys() else {}
            except (TypeError, ValueError):
                det = {}
            new_key = _si.pattern_key({"kind": row["kind"], "employee": into_name, "day": row["day"],
                                       "daypart": row["daypart"], "role": row["role"],
                                       "was_role": det.get("was_role"), "time": row["time"]})
            old_name = row["employee"]
            text = (row["text"] or "").replace(old_name, into_name) if old_name else row["text"]
            keep = conn.execute("SELECT rowid AS rowid, * FROM schedule_standing_patterns WHERE restaurant_id=? AND "
                                "pattern_key=? AND rowid!=?", (rid, new_key, row["rowid"])).fetchone()
            if keep is not None:
                rec["folded"].append({k: row[k] for k in row.keys() if k != "rowid"})
                rec["keeps"].append({"rowid": keep["rowid"], "before": {k: keep[k] for k in keep.keys() if k != "rowid"}})
                status = max((keep["status"], row["status"]), key=lambda s_: _PATTERN_STATUS_RANK.get(s_, 0))
                conn.execute("UPDATE schedule_standing_patterns SET times_applied=times_applied+?, "
                             "times_confirmed=COALESCE(times_confirmed,0)+?, "
                             "times_overridden=MAX(times_overridden, ?), first_learned=MIN(first_learned, ?), "
                             "last_confirmed=MAX(last_confirmed, ?), checked_through=MAX(checked_through, ?), status=?, "
                             "rule_note=COALESCE(rule_note, ?), ruled_by=COALESCE(ruled_by, ?), "
                             "updated_at=datetime('now') WHERE rowid=?",
                             (int(row["times_applied"] or 0), int(row["times_confirmed"] or 0)
                              if "times_confirmed" in row.keys() else 0, int(row["times_overridden"] or 0),
                              row["first_learned"], row["last_confirmed"], int(row["checked_through"] or 0), status,
                              row["rule_note"], row["ruled_by"], keep["rowid"]))
                # The evidence adds too (schedule audit 10/3/26 L-6, L-30): the
                # weeks that tested and kept it, and the newer hand confirmation.
                if "opportunities" in row.keys():
                    conn.execute("UPDATE schedule_standing_patterns SET opportunities=COALESCE(opportunities,0)+?, "
                                 "hits=COALESCE(hits,0)+?, last_hand=MAX(COALESCE(last_hand,''), ?) WHERE rowid=?",
                                 (int(row["opportunities"] or 0), int(row["hits"] or 0), row["last_hand"] or "",
                                  keep["rowid"]))
                conn.execute("DELETE FROM schedule_standing_patterns WHERE rowid=?", (row["rowid"],))
                continue
            was = {"employee": old_name, "pattern_key": row["pattern_key"], "text": row["text"]}
            sets, vals = ["employee=?", "pattern_key=?", "text=?"], [into_name, new_key, text]
            if "person_id" in tcols:
                was["person_id"] = row["person_id"]
                sets.append("person_id=?")
                vals.append(into_pid)
            conn.execute(f"UPDATE schedule_standing_patterns SET {', '.join(sets)} WHERE rowid=?",
                         (*vals, row["rowid"]))
            rec["rows"].append({"rowid": row["rowid"], "col": "employee", "was": was})
            rec["moved"] += 1
        return rec
    # schedule_pattern_dismissals: the name is the key's second field (and,
    # from 9/29/26, the employee column beside it).
    found = conn.execute("SELECT rowid AS rowid, * FROM schedule_pattern_dismissals WHERE restaurant_id=?",
                         (rid,)).fetchall()
    for row in found:
        parts = str(row["key"] or "").split("|")
        if len(parts) < 4:
            continue
        who = (row["employee"] if "employee" in row.keys() and row["employee"] else parts[1]) or ""
        if not who.strip():
            continue
        if not (_nk(who) in keys or (from_pid and "person_id" in tcols and row["person_id"] == from_pid)):
            continue
        parts[1] = into_name.lower()
        new_key = "|".join(parts)
        if conn.execute("SELECT 1 FROM schedule_pattern_dismissals WHERE restaurant_id=? AND key=? AND rowid!=?",
                        (rid, new_key, row["rowid"])).fetchone():
            rec["folded"].append({k: row[k] for k in row.keys() if k != "rowid"})
            rec["keeps"].append(None)
            conn.execute("DELETE FROM schedule_pattern_dismissals WHERE rowid=?", (row["rowid"],))
            continue
        was = {"key": row["key"]}
        sets, vals = ["key=?"], [new_key]
        for c, v in (("employee", into_name), ("person_id", into_pid)):
            if c in tcols:
                was[c] = row[c]
                sets.append(f"{c}=?")
                vals.append(v)
        conn.execute(f"UPDATE schedule_pattern_dismissals SET {', '.join(sets)} WHERE rowid=?", (*vals, row["rowid"]))
        rec["rows"].append({"rowid": row["rowid"], "col": "employee", "was": was})
        rec["moved"] += 1
    return rec


def _repoint_all(conn, rid, from_keys, from_pid, into_name, into_pid) -> dict:
    """Every NAME_STORES row of the person known by `from_keys` (or carrying
    `from_pid`) moved to `into_name` / `into_pid`; two rows landing in one
    unique slot are folded by the store's rule. {table: {moved, folded,
    rows, keeps, deleted}} — `rows` each moved row's rowid and its values
    before, `keeps` each fold's surviving row as it was, `deleted` rows a
    tidy-up removed: what unmerge_people replays (INVENTORY-11)."""
    moved = {}
    keys = sorted({k for k in from_keys if k})
    if not keys:
        return moved
    have = _tables(conn)
    into_key = _nk(into_name)
    for store in NAME_STORES:
        table = store["table"]
        if table not in have:
            continue
        if store.get("fold") == "patterns":
            rec = _repoint_patterns(conn, rid, store, set(keys), from_pid, into_name, into_pid)
            if rec["moved"] or rec["folded"]:
                moved[table] = rec
            continue
        tcols = _cols(conn, table)
        rec = {"moved": 0, "folded": [], "rows": [], "keeps": [], "deleted": []}
        for col in store["cols"]:
            if col not in tcols:
                continue
            where, args, has_pid = _store_rows(conn, rid, store, col, keys, from_pid)
            rows = conn.execute(f"SELECT rowid AS rowid, * FROM {table} WHERE {where}", args).fetchall()
            uniq = store.get("unique")
            for row in rows:
                if uniq and col == store["cols"][0]:
                    tail = [u for u in uniq if u not in (col, store.get("key"))]
                    cond = " AND ".join(f"{u} IS ?" for u in tail)
                    kcol = store.get("key")
                    sql = (f"SELECT rowid AS rowid, * FROM {table} WHERE restaurant_id=? AND rowid!=? AND "
                           + (f"{kcol}=?" if kcol else f"cav_name_key({col})=?") + (f" AND {cond}" if cond else ""))
                    keep = conn.execute(sql, (rid, row["rowid"], into_key, *[row[u] for u in tail])).fetchone()
                    if keep is not None:
                        rec["folded"].append({k: row[k] for k in row.keys() if k != "rowid"})
                        rec["keeps"].append({"rowid": keep["rowid"],
                                             "before": {k: keep[k] for k in keep.keys() if k != "rowid"}})
                        _fold(conn, store, keep, row)
                        continue
                sets, vals = [f"{col}=?"], [into_name]
                was = {col: row[col]}
                if has_pid and into_pid:
                    sets.append("person_id=?")
                    vals.append(into_pid)
                    was["person_id"] = row["person_id"]
                if store.get("key") and col == store["cols"][0]:
                    sets.append(f"{store['key']}=?")
                    vals.append(into_key)
                    was[store["key"]] = row[store["key"]]
                conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE rowid=?", (*vals, row["rowid"]))
                rec["rows"].append({"rowid": row["rowid"], "col": col, "was": was})
                rec["moved"] += 1
        if store.get("fold") == "pairs":
            # A pairing of the person with themself, or the same pair twice.
            for r in conn.execute("SELECT rowid AS rowid, * FROM staff_pairs WHERE restaurant_id=? ORDER BY id DESC",
                                  (rid,)).fetchall():
                if _nk(r["employee_a"]) == _nk(r["employee_b"]):
                    rec["deleted"].append({k: r[k] for k in r.keys() if k != "rowid"})
                    conn.execute("DELETE FROM staff_pairs WHERE id=?", (r["id"],))
            seen = set()
            for r in conn.execute("SELECT rowid AS rowid, * FROM staff_pairs WHERE restaurant_id=? ORDER BY id DESC",
                                  (rid,)).fetchall():
                k = (frozenset((_nk(r["employee_a"]), _nk(r["employee_b"]))), r["kind"])
                if k in seen:
                    rec["deleted"].append({k2: r[k2] for k2 in r.keys() if k2 != "rowid"})
                    conn.execute("DELETE FROM staff_pairs WHERE id=?", (r["id"],))
                seen.add(k)
        if rec["moved"] or rec["folded"] or rec["deleted"]:
            moved[table] = rec
    # The shift history itself (client_data.shifts_csv) — every reader of it
    # would otherwise still see two people.
    sig = []
    n = _rewrite_shifts_csv(conn, rid, set(keys), into_name, record=sig)
    if n:
        moved["shifts_csv"] = {"moved": n, "folded": [], "rows": sig}
    return moved


def _shift_sig(r) -> list:
    return [str(r.get(k) or "") for k in ("date", "shift_start", "shift_end", "role")]


def _rewrite_shifts_csv(conn, rid, keys, into_name, ext_map=None, record=None) -> int:
    """Rows of the stored shifts file under one of `keys` (or, with
    `ext_map`, carrying a POS id in it) renamed to `into_name` / the id's
    person. Returns rows rewritten; `record` (a list) collects each one's
    [date, start, end, role] and its name before, for an undo."""
    import csv as _csv
    import io as _io
    row = conn.execute("SELECT shifts_csv FROM client_data WHERE restaurant_id=?", (rid,)).fetchone()
    text = (row["shifts_csv"] if row else "") or ""
    if not text.strip():
        return 0
    rows = list(_csv.DictReader(_io.StringIO(text)))
    if not rows:
        return 0
    n = 0
    for r in rows:
        ext = str(r.get("employee_ext_id") or "").strip()
        if ext_map and ext and ext in ext_map and r.get("employee") != ext_map[ext]:
            r["employee"] = ext_map[ext]
            n += 1
        elif keys and _nk(r.get("employee")) in keys and r.get("employee") != into_name:
            if record is not None:
                record.append(_shift_sig(r) + [r.get("employee")])
            r["employee"] = into_name
            n += 1
    if not n:
        return 0
    _write_shifts_csv(conn, rid, rows)
    return n


def _write_shifts_csv(conn, rid, rows):
    import csv as _csv
    import io as _io
    fields = []
    for r in rows:
        for k in r:
            if k is not None and k not in fields:
                fields.append(k)
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    conn.execute("UPDATE client_data SET shifts_csv=? WHERE restaurant_id=?", (buf.getvalue(), rid))


def _rename_in(conn, idx, pid, new_name, source="rename", actor_user_id=None):
    """Rename one person on `conn` (inside the caller's transaction): every
    store re-pointed from their old spelling, the old spelling kept as an
    alias, the change recorded."""
    person = idx.people[pid]
    old = person["display_name"]
    new = _clean(new_name)
    old_keys = {person["name_key"]}
    moved = _repoint_all(conn, idx.rid, old_keys, pid, new, pid)
    conn.execute("UPDATE people SET display_name=?, name_key=?, updated_at=datetime('now') WHERE id=?",
                 (new, _nk(new), pid))
    idx.by_key.get(person["name_key"], set()).discard(pid)
    person["display_name"], person["name_key"] = new, _nk(new)
    idx.by_key.setdefault(person["name_key"], set()).add(pid)
    _alias(conn, idx, pid, "rename", old)
    conn.execute("INSERT INTO person_merges (restaurant_id, kind, from_person, into_person, from_name, into_name, "
                 "moved_json, actor_user_id, source) VALUES (?,?,?,?,?,?,?,?,?)",
                 (idx.rid, "rename", pid, pid, old, new, _merge_record(moved), actor_user_id, source))
    return moved


def rename_person(restaurant_id, person_id, new_name, actor_user_id=None, source="owner", db_path=None,
                  user=None) -> dict:
    """The owner renames a person: every store follows, the old spelling is
    kept as an alias. Refused when the new name is already somebody else's
    (that is a merge — merge_people)."""
    new = _clean(new_name)
    if not new or not any(ch.isalpha() for ch in new):
        raise PeopleError("A name needs at least one letter.")
    conn = _conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        idx = _Index(conn, restaurant_id)
        pid = idx.live(int(person_id))
        if pid is None:
            raise PeopleError("That person isn't on this restaurant's roster.")
        before = idx.people[pid]["display_name"]
        if _nk(new) != idx.people[pid]["name_key"] and (idx.for_key(_nk(new)) - {pid}):
            raise PeopleError(f"{new} is already someone on your roster — merge the two instead.")
        moved = _rename_in(conn, idx, pid, new, source=source, actor_user_id=actor_user_id)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    _rename_salaried(restaurant_id, {_nk(before)}, new, db_path)
    _after_change(restaurant_id, "rename", before, new, actor_user_id, source, user=user)
    return {"ok": True, "person_id": pid, "from": before, "to": new, "moved": moved}


def merge_people(restaurant_id, from_id, into_id, actor_user_id=None, source="owner", db_path=None,
                 user=None) -> dict:
    """One person, where there were two: every store row of `from_id` —
    ratings, settings, the minor band, notes, availability, time off,
    contacts, tenure, pairings, requests, the staff login, the shift history
    — re-pointed to `into_id`'s name and id, and two rows in one slot folded
    (a rating: the newer judgment stays, the other is in the merge record
    and the rating history; settings: blanks filled; notes: added together;
    tenure: the earliest first day, the latest last day). `from_id` is kept,
    merged_into, with its spellings and POS ids moved to `into_id` as
    aliases. The owner's decision only — nothing calls this on a guess."""
    conn = _conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        idx = _Index(conn, restaurant_id)
        a, b = idx.live(int(from_id)), idx.live(int(into_id))
        if a is None or b is None:
            raise PeopleError("One of those people isn't on this restaurant's roster.")
        if a == b:
            raise PeopleError("That's the same person already.")
        gone, keep = idx.people[a], idx.people[b]
        # Only the merged person's own spellings: a key the survivor answers
        # to as well is theirs already.
        from_keys = (idx.keys_of(a) | {gone["name_key"]}) - idx.keys_of(b) - {keep["name_key"]}
        moved = _repoint_all(conn, restaurant_id, from_keys, a, keep["display_name"], b)
        # What the identity tables looked like, for an undo (memory
        # re-audit 9/29/26, INVENTORY-11): the aliases that move, the alias
        # the merge adds, the questions it closes, re-points or drops.
        undo = {"aliases": [r[0] for r in conn.execute(
                    "SELECT id FROM person_aliases WHERE person_id=? AND restaurant_id=?", (a, restaurant_id))],
                "gone_active": gone.get("active", 1), "from_keys": sorted(from_keys)}
        had_alias = {r[0] for r in conn.execute("SELECT id FROM person_aliases WHERE person_id=? AND restaurant_id=?",
                                                (b, restaurant_id))}
        # Their POS ids and spellings are the survivor's now.
        conn.execute("UPDATE person_aliases SET person_id=? WHERE person_id=? AND restaurant_id=?",
                     (b, a, restaurant_id))
        _alias(conn, idx, b, "merge", gone["display_name"])
        undo["added_aliases"] = [r[0] for r in conn.execute(
            "SELECT id FROM person_aliases WHERE person_id=? AND restaurant_id=?", (b, restaurant_id))
            if r[0] not in had_alias and r[0] not in undo["aliases"]]
        conn.execute("UPDATE people SET merged_into=?, active=0, updated_at=datetime('now') WHERE id=?", (b, a))
        undo["closed_questions"] = [r[0] for r in conn.execute(
            "SELECT id FROM person_questions WHERE restaurant_id=? AND status='open' AND ((person_a=? AND person_b=?) "
            "OR (person_a=? AND person_b=?))", (restaurant_id, a, b, b, a))]
        conn.execute("UPDATE person_questions SET status='merged', answered_at=datetime('now'), answered_by=? "
                     "WHERE restaurant_id=? AND status='open' AND ((person_a=? AND person_b=?) OR (person_a=? AND "
                     "person_b=?))", (actor_user_id, restaurant_id, a, b, b, a))
        # Anything else asked about the merged person is now about the survivor.
        undo["repointed_a"] = [r[0] for r in conn.execute(
            "SELECT id FROM person_questions WHERE restaurant_id=? AND person_a=? AND status='open'", (restaurant_id, a))]
        undo["repointed_b"] = [r[0] for r in conn.execute(
            "SELECT id FROM person_questions WHERE restaurant_id=? AND person_b=? AND status='open'", (restaurant_id, a))]
        conn.execute("UPDATE OR IGNORE person_questions SET person_a=? WHERE restaurant_id=? AND person_a=? AND status='open'",
                     (b, restaurant_id, a))
        conn.execute("UPDATE OR IGNORE person_questions SET person_b=? WHERE restaurant_id=? AND person_b=? AND status='open'",
                     (b, restaurant_id, a))
        undo["dropped_questions"] = [dict(r) for r in conn.execute(
            "SELECT * FROM person_questions WHERE restaurant_id=? AND status='open' AND person_a=person_b",
            (restaurant_id,))]
        conn.execute("DELETE FROM person_questions WHERE restaurant_id=? AND status='open' AND person_a=person_b",
                     (restaurant_id,))
        try:
            import models as _m_sal
            undo["salaried_before"] = _m_sal.salaried_staff(_m_sal.get_restaurant(
                restaurant_id, *([db_path] if db_path else [])))
        except Exception:
            undo["salaried_before"] = None
        record = dict(moved, _undo=undo)
        merge_id = conn.execute(
            "INSERT INTO person_merges (restaurant_id, kind, from_person, into_person, from_name, into_name, "
            "moved_json, actor_user_id, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (restaurant_id, "merge", a, b, gone["display_name"], keep["display_name"], _merge_record(record),
             actor_user_id, source)).lastrowid
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    _rename_salaried(restaurant_id, from_keys, keep["display_name"], db_path)
    _after_change(restaurant_id, "merge", gone["display_name"], keep["display_name"], actor_user_id, source,
                  user=user)
    kept = []
    for table, rec in moved.items():
        for f in rec.get("folded") or []:
            kept.append({"table": table, "row": {k: v for k, v in f.items() if k not in ("person_id",)}})
    return {"ok": True, "into": keep["display_name"], "from": gone["display_name"], "into_id": b, "from_id": a,
            "moved": {t: r["moved"] for t, r in moved.items()}, "folded": kept, "merge_id": merge_id,
            "undo_days": UNMERGE_DAYS}


# ── undoing a merge (memory re-audit 9/29/26, INVENTORY-11) ─────────────────
#
# person_merges said "so either can be read back", and nothing read it: an
# owner's wrong "same person" answer could not be taken back. A merge's
# record now keeps every moved row's values before, every fold's surviving
# row as it was and the folded row itself, the identity rows it touched,
# and the shift-file rows it renamed; unmerge_people replays it, for
# UNMERGE_DAYS, provided nothing has renamed or merged either person since.

UNMERGE_DAYS = 30
_MERGE_RECORD_MAX = 4_000_000          # characters; a larger record keeps counts only (not undoable)


def _merge_record(moved) -> str:
    text = _json.dumps(moved, default=str)
    if len(text) <= _MERGE_RECORD_MAX:
        return text
    return _json.dumps({**{t: {"moved": r.get("moved", 0), "folded": len(r.get("folded") or [])}
                           for t, r in moved.items() if not t.startswith("_") and isinstance(r, dict)},
                        "_undo": None}, default=str)


def _later_change(conn, restaurant_id, merge_row):
    """A rename or merge of either person after this merge, still standing."""
    a, b = merge_row["from_person"], merge_row["into_person"]
    return conn.execute("SELECT id, kind, into_name FROM person_merges WHERE restaurant_id=? AND id>? AND "
                        "kind IN ('merge', 'rename') AND undone_at IS NULL AND (from_person IN (?,?) OR "
                        "into_person IN (?,?)) ORDER BY id LIMIT 1",
                        (restaurant_id, merge_row["id"], a, b, a, b)).fetchone()


def recent_merges(restaurant_id, db_path=None) -> list:
    """The merges of the last UNMERGE_DAYS, newest first: [{merge_id, from,
    into, merged_on, undo_until (M/D/YY), undoable, why_not, undone}]."""
    from datetime import datetime as _dt, timedelta as _td
    from time_utils import mdy
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM person_merges WHERE restaurant_id=? AND kind='merge' AND "
                            "created_at >= datetime('now', ?) ORDER BY id DESC",
                            (restaurant_id, f"-{UNMERGE_DAYS} days")).fetchall()
        out = []
        for m in rows:
            try:
                has_undo = bool((_json.loads(m["moved_json"] or "{}") or {}).get("_undo"))
            except (TypeError, ValueError):
                has_undo = False
            later = _later_change(conn, restaurant_id, m) if not m["undone_at"] else None
            why = None
            if m["undone_at"]:
                why = "Already undone."
            elif not has_undo:
                why = "This merge has no full record, so it can't be undone here."
            elif later is not None:
                why = f"{later['into_name']} changed again after this merge — undo that first."
            try:
                until = mdy((_dt.strptime(str(m["created_at"])[:19], "%Y-%m-%d %H:%M:%S")
                             + _td(days=UNMERGE_DAYS)).strftime("%Y-%m-%d"))
            except ValueError:
                until = None
            out.append({"merge_id": m["id"], "from": m["from_name"], "into": m["into_name"],
                        "merged_on": mdy(m["created_at"]), "undo_until": until, "undoable": why is None,
                        "why_not": why, "undone": bool(m["undone_at"])})
        return out
    finally:
        conn.close()


def _reinsert(conn, table, row, tcols) -> bool:
    cols = [c for c in row if c in tcols]
    if not cols:
        return False
    cur = conn.execute(f"INSERT OR IGNORE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                       [row[c] for c in cols])
    return bool(cur.rowcount)


def unmerge_people(restaurant_id, merge_id, user=None, db_path=None) -> dict:
    """The owner's "those were two people after all": the merge `merge_id`
    replayed backwards — every row it moved back under the merged person's
    name and id, every fold's surviving row as it was and the folded row
    restored, their aliases and open questions back, the shift file's rows
    renamed back, the merged person live again. For UNMERGE_DAYS, and only
    while neither person has been renamed or merged since (undo that
    first). The question that merged them is closed as "different
    people". Returns {ok, from, into, restored: {table: rows}}."""
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    conn = _conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        m = conn.execute("SELECT * FROM person_merges WHERE id=? AND restaurant_id=? AND kind='merge'",
                         (int(merge_id), restaurant_id)).fetchone()
        if not m:
            raise PeopleError("That merge wasn't found.")
        if m["undone_at"]:
            raise PeopleError("That merge was already undone.")
        young = conn.execute("SELECT created_at >= datetime('now', ?) FROM person_merges WHERE id=?",
                             (f"-{UNMERGE_DAYS} days", m["id"])).fetchone()[0]
        if not young:
            raise PeopleError(f"A merge can be undone for {UNMERGE_DAYS} days; this one is older.")
        try:
            rec = _json.loads(m["moved_json"] or "{}") or {}
        except (TypeError, ValueError):
            rec = {}
        undo = rec.pop("_undo", None)
        if not undo:
            raise PeopleError("This merge has no full record, so it can't be undone here.")
        later = _later_change(conn, restaurant_id, m)
        if later is not None:
            raise PeopleError(f"{later['into_name']} changed again after this merge — undo that first.")
        a, b = m["from_person"], m["into_person"]
        pa = conn.execute("SELECT merged_into FROM people WHERE id=? AND restaurant_id=?", (a, restaurant_id)).fetchone()
        if not pa or pa["merged_into"] != b:
            raise PeopleError("Those two aren't merged any more.")
        into_key = _nk(m["into_name"])
        have = _tables(conn)
        restored = {}
        for table, r in rec.items():
            if table == "shifts_csv" or table not in have or not isinstance(r, dict):
                continue
            tcols = _cols(conn, table)
            n = 0
            for mv in r.get("rows") or []:
                was = {k: v for k, v in (mv.get("was") or {}).items() if k in tcols}
                col = mv.get("col")
                if not was or col not in tcols:
                    continue
                cur = conn.execute(f"SELECT {col} FROM {table} WHERE rowid=? AND restaurant_id=?",
                                   (mv["rowid"], restaurant_id)).fetchone()
                if cur is None or _nk(cur[0]) != into_key:
                    continue                   # gone or changed since: left as it is
                conn.execute(f"UPDATE OR IGNORE {table} SET " + ", ".join(f"{k}=?" for k in was) + " WHERE rowid=?",
                             (*was.values(), mv["rowid"]))
                n += 1
            for keep_rec, gone_row in zip(r.get("keeps") or [], r.get("folded") or []):
                if keep_rec:
                    before = {k: v for k, v in (keep_rec.get("before") or {}).items() if k in tcols}
                    if before and conn.execute(f"SELECT 1 FROM {table} WHERE rowid=?", (keep_rec["rowid"],)).fetchone():
                        conn.execute(f"UPDATE {table} SET " + ", ".join(f"{k}=?" for k in before) + " WHERE rowid=?",
                                     (*before.values(), keep_rec["rowid"]))
                n += 1 if _reinsert(conn, table, gone_row, tcols) else 0
            for gone_row in r.get("deleted") or []:
                n += 1 if _reinsert(conn, table, gone_row, tcols) else 0
            if n:
                restored[table] = n
        # The shift file: the rows the merge renamed, by date, times and role.
        sigs = {}
        for sg in (rec.get("shifts_csv") or {}).get("rows") or []:
            sigs.setdefault(tuple(sg[:4]), []).append(sg[4])
        if sigs:
            import csv as _csv
            import io as _io
            row = conn.execute("SELECT shifts_csv FROM client_data WHERE restaurant_id=?", (restaurant_id,)).fetchone()
            rows = list(_csv.DictReader(_io.StringIO((row["shifts_csv"] if row else "") or "")))
            n = 0
            for r in rows:
                k = tuple(_shift_sig(r))
                if _nk(r.get("employee")) == into_key and sigs.get(k):
                    r["employee"] = sigs[k].pop()
                    n += 1
            if n:
                _write_shifts_csv(conn, restaurant_id, rows)
                restored["shifts_csv"] = n
        # Identity: their aliases back, the merge's own alias gone, them live.
        for aid in undo.get("aliases") or []:
            conn.execute("UPDATE person_aliases SET person_id=? WHERE id=? AND restaurant_id=?", (a, aid, restaurant_id))
        for aid in undo.get("added_aliases") or []:
            conn.execute("DELETE FROM person_aliases WHERE id=? AND restaurant_id=? AND person_id=?",
                         (aid, restaurant_id, b))
        try:
            conn.execute("UPDATE people SET merged_into=NULL, active=?, updated_at=datetime('now') WHERE id=?",
                         (int(undo.get("gone_active", 1) or 0), a))
        except Exception as e:
            raise PeopleError(f"{m['from_name']} can't be brought back: another record now uses that name ({e}).")
        for qid in undo.get("closed_questions") or []:
            conn.execute("UPDATE person_questions SET status='different', answered_at=datetime('now'), answered_by=?, "
                         "answered_authority=? WHERE id=? AND restaurant_id=?",
                         (uid, answer_authority(user), qid, restaurant_id))
        for qid in undo.get("repointed_a") or []:
            conn.execute("UPDATE OR IGNORE person_questions SET person_a=? WHERE id=? AND status='open'", (a, qid))
        for qid in undo.get("repointed_b") or []:
            conn.execute("UPDATE OR IGNORE person_questions SET person_b=? WHERE id=? AND status='open'", (a, qid))
        qcols = _cols(conn, "person_questions")
        for q in undo.get("dropped_questions") or []:
            _reinsert(conn, "person_questions", q, qcols)
        conn.execute("UPDATE person_merges SET undone_at=datetime('now'), undone_by=? WHERE id=?", (uid, m["id"]))
        conn.execute("INSERT INTO person_merges (restaurant_id, kind, from_person, into_person, from_name, into_name, "
                     "moved_json, actor_user_id, source) VALUES (?,?,?,?,?,?,?,?,?)",
                     (restaurant_id, "unmerge", b, a, m["into_name"], m["from_name"],
                      _json.dumps({"undid": m["id"], "restored": restored}), uid,
                      change_source(user) if isinstance(user, dict) else "owner"))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    # A salaried entry the merge renamed, back — only while the list is still
    # exactly what the merge left.
    before = undo.get("salaried_before")
    if isinstance(before, list):
        try:
            import models
            r = models.get_restaurant(restaurant_id, db_path) if db_path else models.get_restaurant(restaurant_id)
            now_staff = models.salaried_staff(r)
            keys = set(undo.get("from_keys") or [])
            after = [dict(s_, name=m["into_name"]) if _nk(s_.get("name")) in keys else s_ for s_ in before]
            if now_staff == after and now_staff != before:
                models.update_restaurant(restaurant_id, {"salaried_staff_json": _json.dumps(before)},
                                         **({"db_path": db_path} if db_path else {}))
        except Exception as e:
            _log.warning("[people] salaried unmerge failed rid=%s: %s", restaurant_id, e)
    _after_change(restaurant_id, "unmerge", m["into_name"], m["from_name"], uid, change_source(user) if user else "owner",
                  user=user if isinstance(user, dict) else None)
    return {"ok": True, "from": m["from_name"], "into": m["into_name"], "restored": restored}


# ── erasing one person (memory re-audit 9/29/26, FORGET-12) ────────────────

def erase_person(restaurant_id, person_id, user=None, db_path=None) -> dict:
    """A departed employee's request to be forgotten: every NAME_STORES row
    about them deleted — ratings, settings, notes, availability, time off,
    contacts, tenure, pairings, requests, every shift (shift_facts),
    attendance, covers and guest mentions, their quarterly summaries and
    what the draft learned about them — their rows removed from the shift
    file, a salaried entry removed, their aliases, questions and merge
    records gone, and their people row left as an anonymous tombstone
    ("Erased #id", so ids elsewhere still resolve to nobody). Where another
    person's row only mentions them (a shift request's replacement or swap
    target) the mention is cleared, not the row. The staff app's records go
    too: offers, running-late reports, announcement receipts, their message
    thread, held texts, certificates (NAME_STORES), and what their staff
    logins here hold by login alone — the after-shift pulse and its notes,
    calendar feeds, their language (MEMBERSHIP_STORES).

    Refused while they are on the active roster or hold a staff login —
    take them off the roster and remove the login first. Not touched: the
    published schedules archived as they were sent (schedule_history /
    schedule_versions), the change log, and anything the POS sends again —
    a re-sync of a window they worked brings those shifts back. Returns
    {ok, erased: {table: rows}}."""
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    conn = _conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        idx = _Index(conn, restaurant_id)
        pid = idx.live(int(person_id))
        if pid is None:
            raise PeopleError("That person isn't on this restaurant's roster.")
        person = idx.people[pid]
        keys = sorted(idx.keys_of(pid) | {person["name_key"]})
        have = _tables(conn)
        # Off the roster means deactivated (staff_settings.active=0): anyone
        # with a shift in the history is on it otherwise (staff_settings.roster).
        st = conn.execute("SELECT active FROM staff_settings WHERE restaurant_id=? AND cav_name_key(employee_name) IN "
                          f"({','.join('?' * len(keys))})", (restaurant_id, *keys)).fetchall() \
            if "staff_settings" in have else []
        if not st or any(r["active"] is None or int(r["active"]) for r in st):
            raise PeopleError(f"{person['display_name']} is on the roster — take them off it first.")
        m_pid = "person_id=? OR " if "memberships" in have and "person_id" in _cols(conn, "memberships") else ""
        if "memberships" in have and conn.execute(
                f"SELECT 1 FROM memberships WHERE restaurant_id=? AND is_active=1 AND ({m_pid}"
                f"cav_name_key(employee_name) IN ({','.join('?' * len(keys))}))",
                (restaurant_id, *([pid] if m_pid else []), *keys)).fetchone():
            raise PeopleError(f"{person['display_name']} still has a staff login — remove it first.")
        erased = {}
        marks = ",".join("?" * len(keys))
        for store in NAME_STORES:
            table = store["table"]
            if table not in have or table == "memberships":
                continue
            tcols = _cols(conn, table)
            n = 0
            if store.get("fold") == "patterns":
                for r in conn.execute(f"SELECT rowid AS rowid, * FROM {table} WHERE restaurant_id=?",
                                      (restaurant_id,)).fetchall():
                    who = r["employee"] if "employee" in r.keys() and r["employee"] else \
                        (str(r["key"]).split("|")[1] if table == "schedule_pattern_dismissals"
                         and len(str(r["key"]).split("|")) > 1 else "")
                    if (who and _nk(who) in keys) or ("person_id" in tcols and r["person_id"] == pid):
                        conn.execute(f"DELETE FROM {table} WHERE rowid=?", (r["rowid"],))
                        n += 1
            else:
                for i, col in enumerate(store["cols"]):
                    if col not in tcols:
                        continue
                    where = f"restaurant_id=? AND (cav_name_key({col}) IN ({marks})"
                    args = [restaurant_id, *keys]
                    if i == 0 and "person_id" in tcols and not store.get("no_person_id"):
                        where += " OR person_id=?"
                        args.append(pid)
                    where += ")"
                    if store.get("where"):
                        where += f" AND {store['where']}"
                    if i == 0 or store.get("fold") == "pairs":
                        for child, fk in store.get("children") or ():
                            if child in have:
                                n += conn.execute(f"DELETE FROM {child} WHERE {fk} IN (SELECT id FROM {table} "
                                                  f"WHERE {where})", args).rowcount or 0
                        cur = conn.execute(f"DELETE FROM {table} WHERE {where}", args)
                    else:
                        cur = conn.execute(f"UPDATE {table} SET {col}=NULL WHERE {where}", args)
                    n += cur.rowcount or 0
            if n:
                erased[table] = n
        # What their staff logins here hold under the login alone — the
        # after-shift pulse and its notes, calendar feeds, their language,
        # and anything above a rename left under another spelling.
        if "memberships" in have:
            mids = [r[0] for r in conn.execute(
                f"SELECT id FROM memberships WHERE restaurant_id=? AND ({m_pid}"
                f"cav_name_key(employee_name) IN ({marks}))",
                (restaurant_id, *([pid] if m_pid else []), *keys)).fetchall()]
            if mids:
                mm = ",".join("?" * len(mids))
                for table, col, children in MEMBERSHIP_STORES:
                    if table not in have:
                        continue
                    n = 0
                    for child, fk in children:
                        if child in have:
                            n += conn.execute(f"DELETE FROM {child} WHERE {fk} IN (SELECT id FROM {table} WHERE "
                                              f"restaurant_id=? AND {col} IN ({mm}))",
                                              (restaurant_id, *mids)).rowcount or 0
                    n += conn.execute(f"DELETE FROM {table} WHERE restaurant_id=? AND {col} IN ({mm})",
                                      (restaurant_id, *mids)).rowcount or 0
                    if n:
                        erased[table] = erased.get(table, 0) + n
        # The shift file.
        import csv as _csv
        import io as _io
        row = conn.execute("SELECT shifts_csv FROM client_data WHERE restaurant_id=?", (restaurant_id,)).fetchone()
        text = (row["shifts_csv"] if row else "") or ""
        if text.strip():
            rows = list(_csv.DictReader(_io.StringIO(text)))
            kept = [r for r in rows if _nk(r.get("employee")) not in keys]
            if len(kept) != len(rows):
                _write_shifts_csv(conn, restaurant_id, kept)
                erased["shifts_csv"] = len(rows) - len(kept)
        # Identity: their spellings and ids, the questions and merge records
        # that name them; the row itself stays as a nameless tombstone.
        merged_in = [r[0] for r in conn.execute("SELECT id FROM people WHERE restaurant_id=? AND merged_into=?",
                                                (restaurant_id, pid))]
        everyone = [pid, *merged_in]
        pm = ",".join("?" * len(everyone))
        erased["person_aliases"] = conn.execute(
            f"DELETE FROM person_aliases WHERE restaurant_id=? AND person_id IN ({pm})",
            (restaurant_id, *everyone)).rowcount or 0
        erased["person_questions"] = conn.execute(
            f"DELETE FROM person_questions WHERE restaurant_id=? AND (person_a IN ({pm}) OR person_b IN ({pm}))",
            (restaurant_id, *everyone, *everyone)).rowcount or 0
        erased["person_merges"] = conn.execute(
            f"DELETE FROM person_merges WHERE restaurant_id=? AND (from_person IN ({pm}) OR into_person IN ({pm}))",
            (restaurant_id, *everyone, *everyone)).rowcount or 0
        for p_ in everyone:
            conn.execute("UPDATE people SET display_name=?, name_key=?, active=0, updated_at=datetime('now') "
                         "WHERE id=?", (f"Erased #{p_}", f"erased #{p_}", p_))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    # A salaried entry under any of their spellings.
    try:
        import models
        r = models.get_restaurant(restaurant_id, db_path) if db_path else models.get_restaurant(restaurant_id)
        staff = models.salaried_staff(r)
        left = [s_ for s_ in staff if _nk(s_.get("name")) not in keys]
        if len(left) != len(staff):
            models.update_restaurant(restaurant_id, {"salaried_staff_json": _json.dumps(left)},
                                     **({"db_path": db_path} if db_path else {}))
            erased["salaried_staff"] = len(staff) - len(left)
    except Exception as e:
        _log.warning("[people] salaried erase failed rid=%s: %s", restaurant_id, e)
    # The change log records that a person was erased — never who.
    try:
        import change_log
        if isinstance(user, dict) and user:
            change_log.record(restaurant_id, "roster", "erase", None, {"erased": f"person #{pid}"},
                              subject=f"person #{pid}", user=user)
        else:
            change_log.record(restaurant_id, "roster", "erase", None, {"erased": f"person #{pid}"},
                              subject=f"person #{pid}", actor_user_id=uid, source="owner")
    except Exception as e:
        _log.warning("[people] change_log failed rid=%s: %s", restaurant_id, e)
    try:
        import client_api
        client_api.invalidate_insight_cache(restaurant_id)
    except Exception as e:
        _log.warning("[people] cache invalidation failed rid=%s: %s", restaurant_id, e)
    return {"ok": True, "person_id": pid, "erased": {k: v for k, v in erased.items() if v}}


def _rename_salaried(restaurant_id, keys, into_name, db_path=None):
    """A salaried entry under the old spelling follows the person."""
    try:
        import models
        r = models.get_restaurant(restaurant_id, db_path) if db_path else models.get_restaurant(restaurant_id)
        staff = models.salaried_staff(r)
        if not any(_nk(s["name"]) in keys for s in staff):
            return
        for s in staff:
            if _nk(s["name"]) in keys:
                s["name"] = into_name
        models.update_restaurant(restaurant_id, {"salaried_staff_json": _json.dumps(staff)},
                                 **({"db_path": db_path} if db_path else {}))
    except Exception as e:
        _log.warning("[people] salaried rename failed rid=%s: %s", restaurant_id, e)


def _after_change(restaurant_id, kind, before, after, actor_user_id, source, user=None):
    """The roster changed under a person: the change log (subject= the
    person as they are now; with the login, whose change it is — an admin
    through view-as is the admin, change_log.actor_context), and every
    cache built from names."""
    try:
        import change_log
        if user:
            change_log.record(restaurant_id, "roster", kind, before, after, subject=after, user=user)
        else:
            change_log.record(restaurant_id, "roster", kind, before, after, subject=after,
                              actor_user_id=actor_user_id, source=source)
    except Exception as e:
        _log.warning("[people] change_log failed rid=%s: %s", restaurant_id, e)
    try:
        import client_api
        client_api.invalidate_insight_cache(restaurant_id)
    except Exception as e:
        _log.warning("[people] cache invalidation failed rid=%s: %s", restaurant_id, e)


# ── the owner's questions ───────────────────────────────────────────────────

def open_questions(restaurant_id, db_path=None) -> list:
    """The identity questions waiting on the owner: [{id, kind, reason,
    a: {person_id, name, key, shifts, rated, sources}, b: {...}, asked}] —
    each answered "same person" (merge) or "different people"."""
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM person_questions WHERE restaurant_id=? AND status='open' "
                            "ORDER BY created_at, id", (restaurant_id,)).fetchall()
        idx = _Index(conn, restaurant_id)
        srcs = {}
        for a in conn.execute("SELECT person_id, source FROM person_aliases WHERE restaurant_id=?",
                              (restaurant_id,)).fetchall():
            srcs.setdefault(a["person_id"], set()).add(a["source"])
    finally:
        conn.close()
    try:
        from models import get_operational_scores
        rated = {_nk(n) for n in (get_operational_scores(restaurant_id) or {})}
    except Exception:
        rated = set()
    from time_utils import mdy

    def _side(pid):
        live = idx.live(pid)
        p = idx.people.get(live) or {}
        return {"person_id": live, "name": p.get("display_name"), "key": person_key(p.get("display_name")),
                "rated": p.get("name_key") in rated,
                "sources": sorted(s for s in srcs.get(live, ()) if s not in ("merge", "rename"))}
    out = []
    for q in rows:
        a, b = _side(q["person_a"]), _side(q["person_b"])
        if not a["person_id"] or not b["person_id"] or a["person_id"] == b["person_id"]:
            continue
        out.append({"id": q["id"], "kind": q["kind"], "reason": q["reason"], "a": a, "b": b,
                    "asked": mdy(q["created_at"]) if q["created_at"] else None})
    return out


def answer_question(restaurant_id, question_id, same: bool, user=None, keep=None, db_path=None) -> dict:
    """The owner's answer. "Same person" merges them — into `keep` (a
    person_id on the question) or into the one with the POS id / the older
    record; "different people" closes the question and it is never asked
    again (a same-name pair keeps its #id suffix until renamed)."""
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    conn = _conn(db_path)
    try:
        q = conn.execute("SELECT * FROM person_questions WHERE id=? AND restaurant_id=?",
                         (int(question_id), restaurant_id)).fetchone()
        if not q or q["status"] != "open":
            raise PeopleError("That question was already answered.")
        idx = _Index(conn, restaurant_id)
        a, b = idx.live(q["person_a"]), idx.live(q["person_b"])
        if not same:
            conn.execute("UPDATE person_questions SET status='different', answered_at=datetime('now'), answered_by=?, "
                         "answered_authority=? WHERE id=?", (uid, answer_authority(user), q["id"]))
            conn.commit()
            return {"ok": True, "status": "different"}
        pos_a = any(idx.ext_of.get((a, s)) for s in POS_SOURCES)
        pos_b = any(idx.ext_of.get((b, s)) for s in POS_SOURCES)
    finally:
        conn.close()
    if keep in (a, b):
        into = keep
    elif pos_a != pos_b:
        into = a if pos_a else b                   # the one the POS knows keeps its spelling
    else:
        into = min(a, b)                           # else the older record
    src = change_source(user) if user else "owner"
    res = merge_people(restaurant_id, b if into == a else a, into, actor_user_id=uid, source=src, db_path=db_path,
                       user=user if isinstance(user, dict) else None)
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE person_questions SET answered_authority=? WHERE id=?", (answer_authority(user), q["id"]))
        conn.commit()
    finally:
        conn.close()
    return {**res, "status": "merged"}


# ── stamping person_id onto every store ─────────────────────────────────────

def stamp_person_ids(restaurant_id, db_path=None, create=True) -> dict:
    """Every name-keyed store row without a person_id gets one: its name
    resolved the way ingest resolves one (exact name_key or alias). A name
    that is nobody yet — a rating typed for "Kim Tran" — is a person now,
    and a question when it is like somebody on the roster (the Gia Mia
    case: 17 ratings under spellings the POS never used). Returns
    {table: rows stamped}."""
    conn = _conn(db_path)
    stamped = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        idx = _Index(conn, restaurant_id)
        have = _tables(conn)
        for store in NAME_STORES:
            table = store["table"]
            if store.get("no_person_id") or table not in have or "person_id" not in _cols(conn, table):
                continue
            col = store["cols"][0]
            where = f"restaurant_id=? AND person_id IS NULL AND {col} IS NOT NULL AND trim({col})!=''"
            if store.get("where"):
                where += f" AND {store['where']}"
            names = [r[0] for r in conn.execute(f"SELECT DISTINCT {col} FROM {table} WHERE {where}",
                                                (restaurant_id,)).fetchall()]
            n = 0
            for name in names:
                key = _nk(name)
                cands = idx.for_key(key)
                if len(cands) == 1:
                    pid = next(iter(cands))
                elif not cands and create and not store.get("no_create") and _looks_like_name(name):
                    pid = _create_person(conn, idx, name, f"store:{table}")
                    _question_similar(conn, idx, pid)
                else:
                    continue
                cur = conn.execute(f"UPDATE {table} SET person_id=? WHERE {where} AND cav_name_key({col})=?",
                                   (pid, restaurant_id, key))
                n += cur.rowcount or 0
            if n:
                stamped[table] = n
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
    return stamped


def repair_labelled_people(db_path=None) -> dict:
    """Undo the phantom people a labelled name made (owner, 10/1/26): the
    rating log's subject was "Name · attribute", and stamp_person_ids read it
    as a new person — 109 at Simple EJ's, each with same-person questions
    ("Is Antonio Corona Martinez · overall the same person as Antonio Corona
    Martinez?"). A person counts as one only when its name carries " · ", it
    was created from that store, and no other store points at it. Its open
    questions and aliases go, the log rows are un-stamped, and the log is
    re-stamped against the real people. Idempotent; at boot."""
    import models
    models.init_capability_changes(db_path or models.DB_PATH)
    conn = _conn(db_path)
    out = {"people": 0, "questions": 0, "restaurants": []}
    try:
        if not {"people", "person_questions", "capability_changes"} <= _tables(conn):
            return out
        rows = conn.execute("SELECT id, restaurant_id FROM people WHERE created_via='store:capability_changes' "
                            "AND instr(display_name, ' · ') > 0").fetchall()
        if not rows:
            return out
        have = _tables(conn)
        others = [s["table"] for s in NAME_STORES if s["table"] != "capability_changes" and s["table"] in have
                  and not s.get("no_person_id") and "person_id" in _cols(conn, s["table"])]
        ids, rids = [], set()
        for r in rows:
            pid = r[0]
            if any(conn.execute(f"SELECT 1 FROM {t} WHERE person_id=? LIMIT 1", (pid,)).fetchone() for t in others):
                continue
            ids.append(pid)
            rids.add(r[1])
        if not ids:
            return out
        marks = ",".join("?" for _ in ids)
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(f"DELETE FROM person_questions WHERE status='open' AND (person_a IN ({marks}) "
                           f"OR person_b IN ({marks}))", ids + ids)
        out["questions"] = cur.rowcount or 0
        if "person_aliases" in have:
            conn.execute(f"DELETE FROM person_aliases WHERE person_id IN ({marks})", ids)
        conn.execute(f"UPDATE capability_changes SET person_id=NULL WHERE person_id IN ({marks})", ids)
        cur = conn.execute(f"DELETE FROM people WHERE id IN ({marks})", ids)
        out["people"] = cur.rowcount or 0
        conn.commit()
        out["restaurants"] = sorted(rids)
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        _log.warning("[people] labelled-name repair skipped: %s", e)
        return out
    finally:
        conn.close()
    for rid in out["restaurants"]:
        try:
            stamp_person_ids(rid, db_path=db_path)
        except Exception as e:
            _log.warning("[people] re-stamp after repair skipped restaurant %s: %s", rid, e)
    if out["people"]:
        _log.warning("[people] removed %s phantom people from labelled names (%s questions)", out["people"],
                     out["questions"])
    return out


def backfill_people(db_path=None, max_seconds=20.0) -> int:
    """Once per restaurant that has no people yet: its people from the names
    in its shift history and every store, then person_ids stamped — at boot
    and from the nightly people job, bounded by `max_seconds` (the rest wait
    for the next pass). Idempotent."""
    import time as _t
    conn = _conn(db_path)
    try:
        rids = [r[0] for r in conn.execute(
            "SELECT id FROM restaurants WHERE id NOT IN (SELECT DISTINCT restaurant_id FROM people) "
            "AND (id IN (SELECT restaurant_id FROM client_data WHERE shifts_csv IS NOT NULL AND shifts_csv != '') "
            "     OR id IN (SELECT restaurant_id FROM staff_settings) "
            "     OR id IN (SELECT restaurant_id FROM staff_capabilities))").fetchall()]
    finally:
        conn.close()
    stop = _t.monotonic() + float(max_seconds)
    done = 0
    for rid in rids:
        if _t.monotonic() > stop:
            break
        try:
            # Read on THIS database: init_db(scratch) (the restore drill) must
            # never read another file's shifts into the one it is building.
            from labor import load_shifts
            c2 = _conn(db_path)
            try:
                got = c2.execute("SELECT shifts_csv FROM client_data WHERE restaurant_id=?", (rid,)).fetchone()
            finally:
                c2.close()
            text = (got[0] if got else "") or ""
            rows = [dict(r) for r in (load_shifts(csv_string=text) if text.strip() else [])]
            # The stored history's own POS ids, when it carries them, link
            # each person to their provider id from the first pass.
            src = _history_source(rid, db_path)
            resolve_rows(rid, rows, src, db_path=db_path)
            stamp_person_ids(rid, db_path=db_path)
            done += 1
        except Exception as e:
            _log.warning("[people] backfill skipped restaurant %s: %s", rid, e)
    return done


def _history_source(restaurant_id, db_path=None) -> str:
    """The source the stored shift history came from (client_data.
    shifts_source: a POS name after a sync), as an alias source."""
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute("SELECT shifts_source FROM client_data WHERE restaurant_id=?", (restaurant_id,)).fetchone()
        finally:
            conn.close()
        src = str((row[0] if row else "") or "").strip().lower()
        return src if src in POS_SOURCES else "backfill"
    except Exception:
        return "backfill"


def link_pos_id(restaurant_id, name, pos_id, source=None, db_path=None):
    """The owner says this person is `pos_id` on the POS: an alias on the
    connected provider (else "manual"). Refused when that id is already
    somebody else's — that is a merge, the owner's own decision."""
    ext = str(pos_id or "").strip()
    if not ext:
        return None
    if source is None:
        try:
            import pos
            source = pos.connected_provider(restaurant_id)[0] or "manual"
        except Exception:
            source = "manual"
    conn = _conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        idx = _Index(conn, restaurant_id)
        cands = idx.for_key(_nk(name))
        if len(cands) != 1:
            pid = _create_person(conn, idx, name, "manual") if not cands else None
            if pid is None:
                raise PeopleError(f"Two people are named {_clean(name)} — open them from the list.")
        else:
            pid = next(iter(cands))
        other = idx.by_ext.get((source, ext))
        if other is not None and idx.live(other) != pid:
            raise PeopleError(f"POS id {ext} is already {idx.people[idx.live(other)]['display_name']} — "
                              "merge the two if they are one person.")
        _alias(conn, idx, pid, source, idx.people[pid]["display_name"], external_id=ext)
        conn.commit()
        return pid
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def external_ids(restaurant_id, source, db_path=None) -> dict:
    """{name_key of every live person's spellings: their id on `source`}
    (e.g. "rpower_payroll" — what RPOWER's schedule write-back needs and no
    store used to keep)."""
    conn = _conn(db_path)
    try:
        idx = _Index(conn, restaurant_id)
    finally:
        conn.close()
    out = {}
    for (pid, src), ext in idx.ext_of.items():
        if src != source:
            continue
        for k in idx.keys_of(pid) | {idx.people[pid]["name_key"]}:
            out.setdefault(k, ext)
    return out


# ═══ What else is known about a person (memory audit 9/29/26, uncaptured) ═══
#
# Roles a person is trained for or promoted into (person_roles — a server
# trained on bar was never offered a bartender gap until she had worked bar
# shifts), who takes a cover when asked (person_signals cover_accepted /
# cover_declined — suggestions ignored who said yes last time), and guests
# naming them in reviews (review_mention, proposed until the owner confirms
# — "Maria was amazing" never reached Maria).

SIGNAL_KINDS = ("cover_accepted", "cover_declined", "review_mention")
# How far back a person's record lists the guests who named them (inside
# person_signals' retention floor — ops._RETENTION_READERS).
PERSON_MENTION_DAYS = 365


def _person_for(conn, restaurant_id, name):
    idx = _Index(conn, restaurant_id)
    cands = idx.for_key(_nk(name))
    if len(cands) == 1:
        pid = next(iter(cands))
        return pid, idx.people[pid]["display_name"]
    return None, _clean(name)


def add_role(restaurant_id, name, role, since=None, primary=False, created_by=None, source="owner",
             db_path=None, user=None, record=True) -> dict:
    """A role this person holds — "trained on bar from 9/1" — that every
    reader of who-can-work-what sees (staff_settings.roles_for, the
    replacement picker, the roster's role when `primary`: a promotion). A
    second primary role replaces the first as primary. `record=False` for a
    POS's own job list mirrored in (pos_archive.sync_roles): not a roster
    change anyone made, so no change_log row — outcomes read roster changes
    as concurrent changes to labor."""
    role = " ".join(str(role or "").split())[:60]
    if not role:
        raise PeopleError("Name the role.")
    try:
        import models
        since_iso = models._iso_or_none(since) if since else None
    except Exception:
        since_iso = None
    if since and not since_iso:
        raise PeopleError("That start date isn't a date — use M/D/YY.")
    conn = _conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        pid, display = _person_for(conn, restaurant_id, name)
        key = _nk(display)
        if primary:
            conn.execute("UPDATE person_roles SET is_primary=0 WHERE restaurant_id=? AND employee_key=?",
                         (restaurant_id, key))
        conn.execute("INSERT INTO person_roles (restaurant_id, person_id, employee_name, employee_key, role, source, "
                     "qualified_since, is_primary, created_by) VALUES (?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(restaurant_id, employee_key, role) DO UPDATE SET removed_at=NULL, "
                     "qualified_since=COALESCE(excluded.qualified_since, qualified_since), "
                     "is_primary=excluded.is_primary, source=excluded.source, person_id=excluded.person_id",
                     (restaurant_id, pid, display, key, role, source, since_iso, 1 if primary else 0, created_by))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if not record:
        return {"name": display, "role": role, "since": since_iso, "primary": bool(primary)}
    try:
        import change_log
        after = {"role": role, "since": since_iso, "primary": bool(primary)}
        if user:
            change_log.record(restaurant_id, "roster", "role", None, after, subject=display, user=user)
        else:
            change_log.record(restaurant_id, "roster", "role", None, after, subject=display,
                              actor_user_id=created_by, source=source)
    except Exception as e:
        _log.warning("[people] change_log failed rid=%s: %s", restaurant_id, e)
    return {"name": display, "role": role, "since": since_iso, "primary": bool(primary)}


def remove_role(restaurant_id, name, role, db_path=None, user=None, record=True) -> bool:
    role = " ".join(str(role or "").split())
    conn = _conn(db_path)
    try:
        cur = conn.execute("UPDATE person_roles SET removed_at=datetime('now'), is_primary=0 WHERE restaurant_id=? "
                           "AND employee_key=? AND lower(role)=lower(?) AND removed_at IS NULL",
                           (restaurant_id, canonical_key(restaurant_id, name, db_path=db_path), role))
        conn.commit()
        done = (cur.rowcount or 0) > 0
    finally:
        conn.close()
    if done and record:
        try:
            import change_log
            change_log.record(restaurant_id, "roster", "role", {"role": role}, None, subject=_clean(name),
                              **({"user": user} if user else {}))
        except Exception as e:
            _log.warning("[people] change_log failed rid=%s: %s", restaurant_id, e)
    return done


def held_roles(restaurant_id, name=None, db_path=None, today=None) -> list:
    """[{name, role, since, primary, source}] in force (not removed, and its
    start date reached) — one person's, or everyone's."""
    from datetime import date as _d
    day = (today or _d.today()).isoformat()
    conn = _conn(db_path)
    try:
        sql = ("SELECT employee_name, employee_key, role, qualified_since, is_primary, source FROM person_roles "
               "WHERE restaurant_id=? AND removed_at IS NULL AND (qualified_since IS NULL OR qualified_since <= ?)")
        args = [restaurant_id, day]
        if name is not None:
            sql += " AND employee_key=?"
            args.append(_nk(name))
        rows = conn.execute(sql + " ORDER BY is_primary DESC, role", args).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [{"name": r["employee_name"], "key": r["employee_key"], "role": r["role"], "since": r["qualified_since"],
             "primary": bool(r["is_primary"]), "source": r["source"]} for r in rows]


def record_signal(restaurant_id, name, kind, signal_date, ref="", polarity=None, status="confirmed", detail=None,
                  created_by=None, authority=None, db_path=None) -> bool:
    """One thing that happened to a person (SIGNAL_KINDS). Idempotent per
    (kind, ref, person); a confirmed or rejected signal is never downgraded
    back to proposed. `authority` is whose word it is (answer_authority;
    "system" for what a job inferred)."""
    if kind not in SIGNAL_KINDS:
        raise ValueError(f"unknown person signal {kind!r}")
    conn = _conn(db_path)
    try:
        pid, display = _person_for(conn, restaurant_id, name)
        cur = conn.execute(
            "INSERT INTO person_signals (restaurant_id, person_id, employee_name, employee_key, kind, polarity, "
            "signal_date, ref, status, detail, created_by, authority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, kind, ref, employee_key) DO UPDATE SET "
            "status=CASE WHEN person_signals.status='proposed' THEN excluded.status ELSE person_signals.status END, "
            "polarity=COALESCE(excluded.polarity, person_signals.polarity)",
            (restaurant_id, pid, display, _nk(display), kind, polarity, str(signal_date)[:10], str(ref or ""),
             status, (detail or "")[:300] or None, created_by, authority or ("system" if created_by is None else None)))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


def answer_cover(restaurant_id, name, issue_id, accepted: bool, signal_date, user=None, db_path=None) -> bool:
    """The manager's word on a cover ask — they took it, or they didn't. It
    stands over what the nightly job inferred from the punches (the other
    answer for the same ask is set aside, never counted), and the job never
    overrides it. Records who answered and whose word it is."""
    kind, other = ("cover_accepted", "cover_declined") if accepted else ("cover_declined", "cover_accepted")
    ref = f"issue:{int(issue_id)}"
    uid = (user or {}).get("id") if isinstance(user, dict) else None
    conn = _conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        pid, display = _person_for(conn, restaurant_id, name)
        key = _nk(display)
        conn.execute("UPDATE person_signals SET status='rejected' WHERE restaurant_id=? AND kind=? AND ref=? "
                     "AND employee_key=?", (restaurant_id, other, ref, key))
        conn.execute(
            "INSERT INTO person_signals (restaurant_id, person_id, employee_name, employee_key, kind, signal_date, ref, "
            "status, detail, created_by, authority) VALUES (?,?,?,?,?,?,?,'confirmed',?,?,?) "
            "ON CONFLICT(restaurant_id, kind, ref, employee_key) DO UPDATE SET status='confirmed', "
            "detail=excluded.detail, created_by=excluded.created_by, authority=excluded.authority",
            (restaurant_id, pid, display, key, kind, str(signal_date)[:10], ref, "the manager's word", uid,
             answer_authority(user)))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def cover_record(restaurant_id, days=180, db_path=None) -> dict:
    """{name_key: {"accepted": n, "declined": n}} over `days` — covers they
    took when asked (a coverage issue's ask, then a punch that day, or the
    manager's word) and claims of an open shift that went through."""
    from datetime import date as _d, timedelta as _td
    since = (_d.today() - _td(days=days)).isoformat()
    out = {}
    conn = _conn(db_path)
    try:
        for r in conn.execute("SELECT employee_key, kind, COUNT(*) AS n FROM person_signals WHERE restaurant_id=? "
                              "AND kind IN ('cover_accepted','cover_declined') AND status='confirmed' AND signal_date>=? "
                              "GROUP BY employee_key, kind", (restaurant_id, since)).fetchall():
            e = out.setdefault(r["employee_key"], {"accepted": 0, "declined": 0})
            e["accepted" if r["kind"] == "cover_accepted" else "declined"] += int(r["n"])
        claims = conn.execute("SELECT replacement_name, COUNT(*) AS n FROM shift_change_requests WHERE "
                              "restaurant_id=? AND status='covered' AND replacement_name IS NOT NULL AND date>=? "
                              "GROUP BY replacement_name", (restaurant_id, since)).fetchall()
        # One person however the claim spelled them (an alias, an old POS
        # spelling) — the signals above are keyed by the person already.
        canon = canonical_names(restaurant_id, [r["replacement_name"] for r in claims], db_path=db_path) \
            if claims else {}
        for r in claims:
            e = out.setdefault(_nk(canon.get(r["replacement_name"]) or r["replacement_name"]),
                               {"accepted": 0, "declined": 0})
            e["accepted"] += int(r["n"])
    except Exception:
        return out
    finally:
        conn.close()
    return out


# A missing shift's span when its end is not on the issue (a person-keyed
# issue before 10/3/26): this long from its start.
COVER_SPAN_MINUTES = 4 * 60


def _span(start, end=None):
    """(start, end) minutes of a shift, its end read across midnight; the
    end COVER_SPAN_MINUTES after the start when it is not known. None
    without a start."""
    from schedule_rules import parse_minutes
    s = parse_minutes(str(start or ""))
    if s is None:
        return None
    e = parse_minutes(str(end or "")) if end else None
    if e is None:
        e = s + COVER_SPAN_MINUTES
    elif e <= s:
        e += 1440
    return s, e


def _overlaps(a, b) -> bool:
    return a is not None and b is not None and a[0] < b[1] and b[0] < a[1]


def cover_verdicts(gaps, asked, punches, own_rows=None, offers=None) -> dict:
    """{asked name key: "accepted" | "declined" | None} for one coverage
    issue — who took a cover when asked, and who said no (schedule re-audit
    10/4/26 LEARN-8: everyone asked who did not punch that day was blamed
    as having declined even after somebody else took the shift, and anyone
    asked with ANY punch that day — their own lunch — was credited with the
    cover). Pure.

      gaps      issues.coverage_people: [{employee, shift_start, shift_end,
                status, covered_by}] — the missing shifts
      asked     the issue's asks: [{name, for?, shift_start?, answer?,
                offer_id?}]
      punches   {name key: [(start, end) minutes]} that day
      own_rows  {name key: [(start, end)]} — each person's OWN published
                shifts that day: a punch inside one is their shift, not a
                cover
      offers    {offer id: status} — an app offer's answer

    An ask resolves for the person who covered — the gap's covered_by, an
    accepted offer, or a punch of theirs that overlaps the missing shift
    and is not their own shift (accepted). An ask the person turned down (a
    declined offer, the manager's "declined" on the ask) is declined. Any
    other ask is "not needed" (None) once the gap was covered by somebody
    else or the missing person came in; declined only when the gap stayed
    open — nobody covered it — and they did not come in."""
    from staff_settings import name_key as _k
    own_rows = own_rows or {}
    offers = offers or {}

    def gap_for(a):
        if len(gaps) == 1:
            return gaps[0]
        for g in gaps:
            if _k(g.get("employee")) == _k(a.get("for")) and (
                    not a.get("shift_start") or not g.get("shift_start") or a["shift_start"] == g["shift_start"]):
                return g
        return gaps[0] if gaps else {}

    def covered(who, g):
        span = _span(g.get("shift_start"), g.get("shift_end"))
        mine = punches.get(who) or []
        if span is None:
            return bool(mine)                   # no time on the issue: any punch that day, as before
        own = own_rows.get(who) or []
        return any(_overlaps(p, span) and not any(_overlaps(p, o) and _overlaps(o, span) for o in own)
                   for p in mine)

    out, took = {}, {}
    for a in asked:
        who = _k(a.get("name"))
        if not who:
            continue
        g = gap_for(a)
        verdict = None
        if _k(g.get("covered_by")) == who or offers.get(a.get("offer_id")) == "accepted" or a.get("answer") == "took":
            verdict = "accepted"
        elif offers.get(a.get("offer_id")) == "declined" or a.get("answer") == "declined":
            verdict = "declined"
        elif covered(who, g):
            verdict = "accepted"
        out[who] = verdict
        if verdict == "accepted":
            took[id(g)] = who
    for a in asked:
        who = _k(a.get("name"))
        if not who or out.get(who) is not None:
            continue
        g = gap_for(a)
        filled = (id(g) in took or bool(g.get("covered_by"))
                  or (g.get("status") or "") in ("covered", "arrived"))
        out[who] = None if filled else "declined"
    return out


def record_cover_signals(restaurant_id, days=7, db_path=None, today=None) -> int:
    """From the coverage issues the live check opened: each ask, resolved
    by cover_verdicts — the person who covered took it (cover_accepted);
    somebody who turned it down, or who was asked while the gap stayed
    open and did not come in, declined (cover_declined); everyone else
    asked was not needed and is not recorded (a job's earlier guess for
    them is set aside). A day still going on is left for tomorrow, and
    somebody's word on an ask (people.answer_cover) stands over all of it.
    Returns signals written."""
    import json as _j
    from datetime import date as _d, timedelta as _td
    today = today or _d.today()
    since = (today - _td(days=days)).isoformat()
    conn = _conn(db_path)
    try:
        issues_ = [dict(r) for r in conn.execute(
            "SELECT * FROM ops_issues WHERE restaurant_id=? AND kind='coverage' AND source_key >= ?",
            (restaurant_id, f"coverage:{since}")).fetchall()]
    except Exception:
        issues_ = []
    finally:
        conn.close()
    n = 0
    for iss in issues_:
        try:
            meta = _j.loads(iss["meta_json"] or "null") or {}
        except (TypeError, ValueError):
            meta = {}
        asked = [a for a in (meta.get("asked") or []) if isinstance(a, dict) and a.get("name")]
        day = (iss["source_key"] or "").split(":")[1] if (iss["source_key"] or "").count(":") >= 2 else None
        if not asked or not day:
            continue
        try:
            import issues as _issues
            gaps = _issues.coverage_people(iss)
        except Exception:
            gaps = []
        if not gaps and meta.get("missing"):
            gaps = [{"employee": meta.get("missing"), "shift_start": meta.get("shift_start"),
                     "shift_end": meta.get("shift_end"), "status": "missing"}]
        for g in gaps:
            if not g.get("shift_end") and meta.get("shift_end") and len(gaps) == 1:
                g["shift_end"] = meta.get("shift_end")
        names = [a["name"] for a in asked] + [g.get("employee") for g in gaps if g.get("employee")]
        canon = canonical_names(restaurant_id, names, db_path=db_path)
        asked = [dict(a, name=canon.get(a["name"]) or a["name"]) for a in asked]
        punches, own_rows = {}, {}
        try:
            import shift_facts
            for r in shift_facts.rows(restaurant_id, since=day, until=day, db_path=db_path):
                span = _span(r.get("shift_start"), r.get("shift_end"))
                if span is not None or not r.get("shift_start"):
                    punches.setdefault(_nk(canon.get(r["employee"]) or r["employee"]), []).append(
                        span or (0, 2880))
        except Exception:
            punches = {}
        try:
            import intraday
            from datetime import date as _date
            for r in intraday.published_rows(restaurant_id, _date.fromisoformat(day),
                                             db_path=db_path or DB_PATH):
                span = _span(r.get("shift_start"), r.get("shift_end"))
                if span is not None:
                    own_rows.setdefault(_nk(canon.get(r["employee"]) or r["employee"]), []).append(span)
        except Exception:
            own_rows = {}
        offers = {}
        ids = [int(a["offer_id"]) for a in asked if str(a.get("offer_id") or "").isdigit()]
        if ids:
            try:
                conn = _conn(db_path)
                try:
                    offers = {r["id"]: r["status"] for r in conn.execute(
                        "SELECT id, status FROM shift_offers WHERE restaurant_id=? AND id IN (%s)"
                        % ",".join("?" * len(ids)), [restaurant_id] + ids).fetchall()}
                finally:
                    conn.close()
            except Exception:
                offers = {}
            asked = [dict(a, offer_id=int(a["offer_id"])) if str(a.get("offer_id") or "").isdigit() else a
                     for a in asked]
        verdicts = cover_verdicts(gaps, asked, punches, own_rows, offers)
        try:
            conn = _conn(db_path)
            try:
                answered = {r["employee_key"] for r in conn.execute(
                    "SELECT employee_key FROM person_signals WHERE restaurant_id=? AND ref=? AND "
                    "kind IN ('cover_accepted','cover_declined') AND COALESCE(authority, 'system') <> 'system'",
                    (restaurant_id, f"issue:{iss['id']}")).fetchall()}
            finally:
                conn.close()
        except Exception:
            answered = set()
        ref = f"issue:{iss['id']}"
        for a in asked:
            key = _nk(a["name"])
            if key in answered:
                continue                           # somebody's word stands over the punches
            verdict = verdicts.get(key)
            if verdict == "declined" and day >= today.isoformat():
                continue                           # the day is not over: they may still come in
            # The job's own earlier guess for this ask that no longer holds
            # is set aside (the rule answer_cover keeps).
            keep = {"accepted": "cover_accepted", "declined": "cover_declined"}.get(verdict)
            conn = _conn(db_path)
            try:
                conn.execute("UPDATE person_signals SET status='rejected' WHERE restaurant_id=? AND ref=? AND "
                             "employee_key=? AND kind IN ('cover_accepted','cover_declined') AND kind<>? AND "
                             "COALESCE(authority, 'system')='system'", (restaurant_id, ref, key, keep or ""))
                conn.commit()
            finally:
                conn.close()
            if keep and record_signal(restaurant_id, a["name"], keep, day, ref=ref,
                                      detail=f"asked to cover {meta.get('missing') or 'a shift'}", db_path=db_path):
                n += 1
    return n


# Everyday words that are also first names: a review saying "will be back"
# or "a real joy" names nobody. Such a name counts only with its last name
# or last initial beside it.
_WORD_NAMES = frozenset(
    "will grace hope joy faith mark bill rose may june april summer dawn art chase hunter rich pat sue don ray "
    "max jack sky star chip gene guy ivy jade lane reed wade drew sage rob robin holly iris lily daisy bo "
    "ben dean frank grant hank jay kit lee les lou mac mo ned nick norm pearl rusty sandy scott sonny stan "
    "ted tom ty val van victor wes will".split())


def _mention_patterns(restaurant_id, db_path=None):
    """(first name, full name, display) for each active roster person whose
    first name is theirs alone — two Marias on staff is never guessed."""
    import re
    import staff_settings
    firsts = {}
    for e in staff_settings.roster(restaurant_id, db_path=db_path or DB_PATH):
        toks = [t for t in re.split(r"[^A-Za-z'\-]+", e["name"]) if t]
        if not toks:
            continue
        firsts.setdefault(toks[0].lower(), []).append((toks, e["name"]))
    out = []
    for first, people_ in firsts.items():
        if len(first) < 3 or len(people_) != 1:
            continue
        toks, display = people_[0]
        last = toks[-1] if len(toks) > 1 else ""
        out.append((first, last, display))
    return out


def match_review_mentions(restaurant_id, days=30, db_path=None) -> int:
    """Guests naming a staff member (memory audit 9/29/26, uncaptured): each
    recent analysed review whose text names someone on the roster — their
    first name, when it is theirs alone and not an everyday word, else their
    first name with their last name or initial — becomes a PROPOSED
    review_mention (+1 positive, −1 negative), the owner's to confirm
    before it counts. Returns proposals written."""
    import re
    pats = _mention_patterns(restaurant_id, db_path)
    if not pats:
        return 0
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT id, text, sentiment, COALESCE(NULLIF(review_date,''), fetched_at) AS at FROM reviews "
                            "WHERE restaurant_id=? AND deleted_at IS NULL AND processed=1 AND text IS NOT NULL AND "
                            "COALESCE(NULLIF(review_date,''), fetched_at) >= date('now', ?)",
                            (restaurant_id, f"-{int(days)} days")).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    n = 0
    for r in rows:
        text = str(r["text"] or "")
        for first, last, display in pats:
            f = re.escape(first.capitalize())
            if first in _WORD_NAMES:
                # "Will" is a verb at the start of half the reviews: only
                # with the last name or its initial beside it.
                if not last:
                    continue
                hit = re.search(rf"\b{f}\s+{re.escape(last[0].upper())}(?:{re.escape(last[1:])}\b|\.|\b)", text)
            else:
                hit = re.search(rf"\b{f}\b", text)
            if not hit:
                continue
            pol = {"positive": 1, "negative": -1}.get(str(r["sentiment"] or "").lower(), 0)
            snippet = text[max(0, hit.start() - 60): hit.end() + 80].replace("\n", " ").strip()
            if record_signal(restaurant_id, display, "review_mention", str(r["at"] or "")[:10], ref=f"review:{r['id']}",
                             polarity=pol, status="proposed", detail=snippet, db_path=db_path):
                n += 1
    return n


def mentions(restaurant_id, status="proposed", db_path=None, limit=50, since=None) -> list:
    """Guest mentions of staff: [{id, name, key, date, polarity, review_id,
    snippet, status}] — "proposed" ones wait on the owner's confirmation.
    `since` (ISO) keeps a reader inside person_signals' retention window
    (ops._RETENTION_READERS); what is older lives on in person_quarters."""
    from time_utils import mdy
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM person_signals WHERE restaurant_id=? AND kind='review_mention' AND status=? "
                            "AND signal_date >= ? ORDER BY signal_date DESC, id DESC LIMIT ?",
                            (restaurant_id, status, str(since or "0000-00-00")[:10], int(limit))).fetchall()
    except Exception:
        return []
    finally:
        conn.close()
    return [{"id": r["id"], "name": r["employee_name"], "key": person_key(r["employee_name"]),
             "date": mdy(r["signal_date"]) if r["signal_date"] else None, "date_iso": r["signal_date"],
             "polarity": r["polarity"], "review_id": (r["ref"] or "").split(":", 1)[-1] or None,
             "snippet": r["detail"], "status": r["status"]} for r in rows]


def answer_mention(restaurant_id, signal_id, confirm: bool, user=None, db_path=None) -> bool:
    """The owner confirms a guest's mention is about this person, or not."""
    conn = _conn(db_path)
    try:
        cur = conn.execute("UPDATE person_signals SET status=?, created_by=COALESCE(created_by, ?), authority=? "
                           "WHERE id=? AND restaurant_id=? AND kind='review_mention' AND status='proposed'",
                           ("confirmed" if confirm else "rejected",
                            (user or {}).get("id") if isinstance(user, dict) else None, answer_authority(user),
                            int(signal_id), restaurant_id))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


# ── the people memory (memory_context provider "people") ───────────────────
#
# What Cavnar AI remembers about the staff, as dated lines for the model
# calls that decide who works or read what guests said about service
# (memory audit 9/29/26: memory_context "people"). Each surface reads only
# what no block of its own already says — the schedule prompt carries
# attendance (its RELIABILITY block), the standing patterns (its learned
# block), staff notes, tenure and last month's pattern itself — so the
# section never pays twice for one fact. Every line names a person, so
# none is trusted: the assembler fences them all.

_MEMORY_PARTS = {
    "schedule": ("roles", "covers", "mentions"),
    "labor_read": ("attendance", "standing", "roles", "changes", "covers"),
    "review_diagnosis": ("attendance", "changes", "mentions"),
}
_MEMORY_DEFAULT_PARTS = ("attendance", "standing", "roles", "changes", "covers", "mentions")
MEMORY_NEW_DAYS = 28           # "new since" — a first shift in the last four weeks
MEMORY_GONE_DAYS = 21          # "no shifts since" — three weeks without one…
MEMORY_GONE_WITHIN_DAYS = 90   # …after working inside the last quarter
MEMORY_HISTORY_DAYS = 56       # below eight weeks of history everyone looks new: say nothing
MEMORY_COVER_DAYS = 180
MEMORY_MENTION_DAYS = 180


def memory_lines(req) -> list:
    """memory_context provider "people": [{text, date, source, subject,
    weight, trusted, module}] for `req.surface` (schedule, labor_read,
    review_diagnosis; any other surface — Ask — reads every part). Parts:

      attendance  who missed or was late on watched shifts, and on which
                  weekday (attendance.summary_lines); or that nothing was
                  watched yet, so no one's reliability is known
      standing    the manager's standing preferences still in force
                  (schedule_standing_patterns, active)
      roles       promotions and roles someone is trained for beyond the one
                  they work (person_roles)
      changes     who is new in the last four weeks and who has had no
                  shift for three, against the restaurant's own last shift
                  (never today: a sync that stopped is not a departure)
      covers      who picks up shifts for teammates, and who said no
      mentions    guests naming someone in reviews the owner CONFIRMED"""
    rid = req.restaurant_id
    db = getattr(req, "db_path", None)
    now = getattr(req, "now", None)
    try:
        from datetime import datetime as _dt
        today = now.date() if isinstance(now, _dt) else (now or None)
    except Exception:
        today = None
    from datetime import date as _date
    today = today or _date.today()
    surface = getattr(req, "surface", None)
    parts = _MEMORY_PARTS.get(surface, _MEMORY_DEFAULT_PARTS)
    out = []
    for part in parts:
        try:
            out += _MEMORY_READERS[part](rid, today, db)
        except Exception as e:             # one part's failure never costs the others
            _log.warning("[people] memory part %s failed rid=%s: %s", part, rid, e)
    # "Nobody's attendance is known" is said only where a staffing call is
    # being made on it — the labor read — never to Ask or the diagnosis of
    # a restaurant that simply has nothing to say about its people yet.
    if surface != "labor_read":
        out = [l for l in out if not l.get("unwatched")]
    return [{k: v for k, v in l.items() if k != "unwatched"} for l in out]


def _mem_line(text, date_, subject, weight, source="system", module="labor", trusted=False, measured=None):
    """One people-memory line. `module` gates it by the viewer's view
    permission and `audience` "team" says any login with that module may
    read it (memory_context.visible) — facts about the staff, never a
    principal's private note. `measured` is what Cavnar AI counted about
    the person (attendance, covers, confirmed mentions), rendered trusted
    under the fenced name so a count can be cited (memory re-audit
    9/29/26, PROMPTS-3: fenced with the name, it never verified)."""
    line = {"text": text, "date": date_, "source": source, "subject": subject, "weight": float(weight),
            "trusted": trusted, "module": module, "audience": "team"}
    if measured:
        line["measured"] = measured
    return line


def _mem_attendance(rid, today, db):
    import attendance
    lines = attendance.summary_lines(rid, today=today, db_path=db)
    if not lines:
        if attendance.watched(rid, days=90, db_path=db):
            return []                      # watched, and nobody missed: nothing to say
        import shift_facts
        if not shift_facts.has_facts(rid, db_path=db):
            return []                      # no staff history at all: nothing to say
        return [dict(_mem_line("Attendance is not watched here yet: no published week has been checked against the "
                               "punches, so no one's reliability is known. Say nothing about who shows up.",
                               None, "labor", 1.0, trusted=True), unwatched=True)]
    out = []
    for l in lines:
        name, _sep, counted = str(l["text"]).partition(": ")
        if not (l.get("name") and _sep and counted):
            name, counted = l["text"], None
        out.append(_mem_line(name, l["date"],
                             f"labor:day:{l['top_day'].lower()}" if l.get("top_day") else "labor",
                             3.0 + min(int(l.get("misses") or 0), 6) * 0.5,
                             measured=f"Measured: {counted}." if counted else None))
    return out


def _mem_standing(rid, today, db):
    import schedule_versions
    kw = {"db_path": db} if db else {}
    # Active only: a retired pattern was reversed, a ruled one is the
    # person's availability now, a dormant one is about someone with no
    # shifts (memory re-audit 9/29/26, FORGET-5).
    rows = [r for r in schedule_versions.standing_patterns(rid, include_retired=False, **kw)
            if r.get("status") == "active"]
    # Weighed by the weeks the MANAGER's own hand kept it (QUALITY-14): the
    # weeks the draft merely carried it are not new evidence.
    rows.sort(key=lambda r: -(int(r.get("times_confirmed") or 0)))
    out = []
    for r in rows[:6]:
        text = str(r.get("text") or "").split(" — ")[0].strip().rstrip(".")
        if not text:
            continue
        kept = int(r.get("times_confirmed") or 0)
        out.append(_mem_line(f"Standing preference (learned {r.get('first_learned')}"
                             + (f", confirmed by the manager's own edits in {kept} week{'s' if kept != 1 else ''}"
                                if kept else "")
                             + f"): {text}.",
                             r.get("last_confirmed_iso"),
                             f"labor:day:{str(r['day']).lower()}" if r.get("day") else "schedule",
                             2.0 + min(kept, 10) / 10.0, source="manager"))
    return out


def _mem_roles(rid, today, db):
    from time_utils import mdy
    held = held_roles(rid, db_path=db, today=today)
    if not held:
        return []
    import staff_settings
    try:
        worked = {_nk(e["name"]): e.get("role") for e in staff_settings.roster(rid, db_path=db or DB_PATH)}
    except Exception:
        worked = {}
    out = []
    for r in held[:8]:
        base = worked.get(r["key"])
        since = f" on {mdy(r['since'])}" if r.get("since") else ""
        if r["primary"]:
            text = f"{r['name']} was promoted to {r['role']}{since}."
        elif base and str(base).lower() != str(r["role"]).lower():
            text = f"{r['name']} is trained for {r['role']}" + (f" since {mdy(r['since'])}" if r.get("since") else "") \
                   + f" as well as {base}, and can fill a {r['role']} gap."
        else:
            text = f"{r['name']} is trained for {r['role']}" + (f" since {mdy(r['since'])}" if r.get("since") else "") + "."
        out.append(_mem_line(text, r.get("since"), "schedule", 2.0,
                             source="owner" if r.get("source") in (None, "owner") else str(r["source"])))
    return out


def _mem_changes(rid, today, db):
    import shift_facts
    from datetime import date as _d, timedelta as _td
    from time_utils import mdy
    ten = shift_facts.tenure(rid, db_path=db)
    if not ten:
        return []
    firsts = [v["first"] for v in ten.values() if v.get("first")]
    lasts = [v["last"] for v in ten.values() if v.get("last")]
    if not firsts or not lasts:
        return []
    start, end = _d.fromisoformat(min(firsts)[:10]), _d.fromisoformat(max(lasts)[:10])
    if (end - start).days < MEMORY_HISTORY_DAYS:
        return []
    new = sorted((v["first"], n) for n, v in ten.items() if v.get("first")
                 and _d.fromisoformat(v["first"][:10]) > end - _td(days=MEMORY_NEW_DAYS))
    gone = sorted(((v["last"], n) for n, v in ten.items() if v.get("last")
                   and end - _td(days=MEMORY_GONE_WITHIN_DAYS) <= _d.fromisoformat(v["last"][:10])
                   <= end - _td(days=MEMORY_GONE_DAYS) and int(v.get("shifts") or 0) >= 4), reverse=True)
    out = []
    if new:
        names = ", ".join(f"{n} (first shift {mdy(f)})" for f, n in new[:6])
        out.append(_mem_line(f"New on the staff in the four weeks to {mdy(end)}: {names}.", new[0][0],
                             "labor", 2.5))
    if gone:
        names = ", ".join(f"{n} (last shift {mdy(l_)})" for l_, n in gone[:6])
        out.append(_mem_line(f"No shifts in the three weeks to {mdy(end)} after working before: {names}. "
                             "Whether they left isn't known.", gone[0][0], "labor", 2.5))
    return out


def _mem_covers(rid, today, db):
    rec = cover_record(rid, days=MEMORY_COVER_DAYS, db_path=db)
    if not rec:
        return []
    names = _display_names(rid, list(rec), db)
    rows = sorted(rec.items(), key=lambda kv: (-kv[1]["accepted"], kv[1]["declined"]))
    out = []
    for key, c in rows:
        a, d = int(c.get("accepted") or 0), int(c.get("declined") or 0)
        if a < 2 and d < 2:
            continue
        bits = []
        if a:
            bits.append(f"covered {a} shift{'s' if a != 1 else ''} for teammates")
        if d:
            bits.append(f"didn't take {d} cover{'s' if d != 1 else ''} they were asked to")
        out.append(_mem_line(f"{names.get(key, key)}", None, "schedule", 1.0 + min(a, 10) / 10.0,
                             measured="Measured: " + " and ".join(bits) + " in the last 6 months."))
        if len(out) >= 4:
            break
    return out


def _mem_mentions(rid, today, db):
    from datetime import timedelta as _td
    from time_utils import mdy
    since = (today - _td(days=MEMORY_MENTION_DAYS)).isoformat()
    by = {}
    for m in mentions(rid, status="confirmed", db_path=db, limit=200, since=since):
        if not m.get("date_iso"):
            continue
        e = by.setdefault(m["name"], {"pos": 0, "neg": 0, "n": 0, "first": m["date_iso"], "last": m["date_iso"]})
        e["n"] += 1
        if (m.get("polarity") or 0) > 0:
            e["pos"] += 1
        elif (m.get("polarity") or 0) < 0:
            e["neg"] += 1
        e["first"], e["last"] = min(e["first"], m["date_iso"]), max(e["last"], m["date_iso"])
    out = []
    for name, e in sorted(by.items(), key=lambda kv: -kv[1]["n"])[:4]:
        tone = []
        if e["pos"]:
            tone.append(f"{e['pos']} positive")
        if e["neg"]:
            tone.append(f"{e['neg']} negative")
        out.append(_mem_line(f"Guests named {name}", e["last"], "reviews", 1.5 + min(e["n"], 10) / 10.0,
                             module="reviews",
                             measured=(f"Measured: in {e['n']} review{'s' if e['n'] != 1 else ''} since "
                                       f"{mdy(e['first'])}" + (f" ({', '.join(tone)})" if tone else "")
                                       + " — each confirmed by the owner.")))
    return out


def _display_names(rid, keys, db):
    """{name_key: display name} for keys a store holds, through the people
    table (the canonical spelling) and then the roster."""
    out = {}
    conn = _conn(db)
    try:
        for r in conn.execute("SELECT name_key, display_name FROM people WHERE restaurant_id=? AND merged_into IS NULL",
                              (rid,)).fetchall():
            if r["name_key"] in keys:
                out[r["name_key"]] = r["display_name"]
    except Exception:
        pass
    finally:
        conn.close()
    missing = [k for k in keys if k not in out]
    if missing:
        try:
            import staff_settings
            for e in staff_settings.roster(rid, db_path=db or DB_PATH):
                k = _nk(e["name"])
                if k in missing and k not in out:
                    out[k] = e["name"]
        except Exception:
            pass
    return out


_MEMORY_READERS ={"attendance": _mem_attendance, "standing": _mem_standing, "roles": _mem_roles,
                   "changes": _mem_changes, "covers": _mem_covers, "mentions": _mem_mentions}

