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
                    "certifications": list(staff_settings.CERTIFICATIONS)},
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
    try:
        out["guest_mentions"] = [m for m in mentions(restaurant_id, status="confirmed", db_path=db_path)
                                 if staff_settings.name_key(m["name"]) == k][:5]
    except Exception:
        out["guest_mentions"] = []
    try:
        rel = {staff_settings.name_key(n): r for n, r in staff_settings.reliability(restaurant_id, db_path=db).items()}.get(k)
        out["attendance"] = ({"known": True, "shifts": rel["shifts"], "missed": rel["no_shows"],
                              "late": rel.get("late", 0), "no_show_rate": rel["no_show_rate"],
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
        member = next((m for m in _memberships(restaurant_id, db)
                       if staff_settings.name_key(m.get("employee_name")) == staff_settings.name_key(name)), None)
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
    # A number that replied STOP is not a text channel, whatever its consent
    # says (#107): staff reach checked the tick-box alone, so a STOP was
    # texted anyway and "reachable by text" counted it.
    candidates = [c["sms"] for c in out.values() if c["sms"]]
    if candidates:
        import notify
        stopped = notify.sms_stopped_phones(candidates, db_path=db)
        for c in out.values():
            if c["sms"] in stopped:
                c["sms"] = None
    return out


def tell(restaurant_id, name, title, lines, *, email_type="staff_notice", channel=None, db_path=None):
    """One notice to one person on staff, on the channel `reach` picks —
    the app, a text they agreed to, email as the fallback — the same order
    a published week uses. Returns "push", "sms", "email" or None (nobody
    could be reached, or every channel failed).

    Staff notices went by email only, so a person with no address on file
    was never told their drop was approved, their swap went through or
    their time off was decided (F2-5, F2-12). `lines` are plain sentences;
    the first is the push/text body."""
    import html as _h
    lines = [str(x) for x in (lines or []) if str(x or "").strip()] or [title]
    db = _db(db_path)
    if channel is None:
        try:
            channel = reach(restaurant_id, [name], db_path=db).get(name) or {}
        except Exception as e:
            print(f"[people] reach failed rid={restaurant_id}: {e!r}")
            channel = {}
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id, db)
        place = (getattr(r, "location_name", None) or getattr(r, "name", None) or "your restaurant") if r else "your restaurant"
    except Exception:
        place = "your restaurant"
    if channel.get("push_user_id"):
        try:
            import push
            if push.fire_push(restaurant_id, "staff_schedule", f"{title} — {place}", " ".join(lines)[:220],
                              data={"kind": "staff_notice", "module": "staff"}, db_path=db,
                              user_ids=[channel["push_user_id"]]):
                return "push"
        except Exception as e:
            print(f"[people] staff push failed rid={restaurant_id}: {e!r}")
    if channel.get("sms"):
        try:
            import notify
            from config import base_url
            with notify.sms_context(restaurant_id):
                if notify.send_sms(channel["sms"], f"{place}: {' '.join(lines)} {base_url()}/staff "
                                                   "Reply STOP to stop these texts.", use_case="staff"):
                    return "sms"
        except Exception as e:
            print(f"[people] staff text failed rid={restaurant_id}: {e!r}")
    if channel.get("email"):
        try:
            import emails
            from config import base_url
            html = emails.report_shell(kicker=_h.escape(place), title=_h.escape(title), subtitle="",
                                       sections=[emails.report_paragraph(_h.escape(x)) for x in lines],
                                       cta_label="Open the staff portal", cta_url=base_url() + "/staff")
            res = emails.deliver(email_type=email_type, restaurant_id=restaurant_id, payload={
                "from": emails.sender("client"), "to": [channel["email"]],
                "subject": f"{title} — {place}", "preheader": lines[0][:120], "html": html})
            if getattr(res, "ok", False):
                return "email"
        except Exception as e:
            print(f"[people] staff email failed rid={restaurant_id}: {e!r}")
    return None


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
    {"table": "shift_change_requests", "cols": ("employee_name", "replacement_name")},
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
    answered = conn.execute("SELECT id FROM person_questions WHERE restaurant_id=? AND kind=? AND "
                            "((person_a=? AND person_b=?) OR (person_a=? AND person_b=?))",
                            (idx.rid, kind, a, b, b, a)).fetchone()
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


# ── re-pointing every store: merge and rename ───────────────────────────────

def _store_rows(conn, rid, store, col, keys, pid):
    marks = ",".join("?" * len(keys))
    has_pid = (not store.get("no_person_id")) and col == store["cols"][0] and "person_id" in _cols(conn, store["table"])
    where = f"restaurant_id=? AND (cav_name_key({col}) IN ({marks})" + (" OR person_id=?" if has_pid and pid else "") + ")"
    if store.get("where"):
        where += f" AND {store['where']}"
    args = [rid, *keys] + ([pid] if has_pid and pid else [])
    return where, args, has_pid


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
        nums = [c for c in ("shifts", "hours", "scheduled_shifts", "watched", "no_shows", "called_out", "late",
                            "left_early", "covered") if c in gone.keys()]
        if nums:
            conn.execute(f"UPDATE {table} SET " + ", ".join(f"{c}=COALESCE({c},0)+COALESCE(?,0)" for c in nums)
                         + " WHERE rowid=?", (*[gone[c] for c in nums], keep["rowid"]))
    conn.execute(f"DELETE FROM {table} WHERE rowid=?", (gone["rowid"],))


def _repoint_all(conn, rid, from_keys, from_pid, into_name, into_pid) -> dict:
    """Every NAME_STORES row of the person known by `from_keys` (or carrying
    `from_pid`) moved to `into_name` / `into_pid`; two rows landing in one
    unique slot are folded by the store's rule. {table: {moved, folded}}."""
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
        tcols = _cols(conn, table)
        rec = {"moved": 0, "folded": []}
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
                        _fold(conn, store, keep, row)
                        continue
                sets, vals = [f"{col}=?"], [into_name]
                if has_pid and into_pid:
                    sets.append("person_id=?")
                    vals.append(into_pid)
                if store.get("key") and col == store["cols"][0]:
                    sets.append(f"{store['key']}=?")
                    vals.append(into_key)
                conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE rowid=?", (*vals, row["rowid"]))
                rec["moved"] += 1
        if store.get("fold") == "pairs":
            # A pairing of the person with themself, or the same pair twice.
            for r in conn.execute("SELECT id, employee_a, employee_b, kind FROM staff_pairs WHERE restaurant_id=? "
                                  "ORDER BY id DESC", (rid,)).fetchall():
                if _nk(r["employee_a"]) == _nk(r["employee_b"]):
                    conn.execute("DELETE FROM staff_pairs WHERE id=?", (r["id"],))
            seen = set()
            for r in conn.execute("SELECT id, employee_a, employee_b, kind FROM staff_pairs WHERE restaurant_id=? "
                                  "ORDER BY id DESC", (rid,)).fetchall():
                k = (frozenset((_nk(r["employee_a"]), _nk(r["employee_b"]))), r["kind"])
                if k in seen:
                    conn.execute("DELETE FROM staff_pairs WHERE id=?", (r["id"],))
                seen.add(k)
        if rec["moved"] or rec["folded"]:
            moved[table] = rec
    # The shift history itself (client_data.shifts_csv) — every reader of it
    # would otherwise still see two people.
    n = _rewrite_shifts_csv(conn, rid, set(keys), into_name)
    if n:
        moved["shifts_csv"] = {"moved": n, "folded": []}
    return moved


def _rewrite_shifts_csv(conn, rid, keys, into_name, ext_map=None) -> int:
    """Rows of the stored shifts file under one of `keys` (or, with
    `ext_map`, carrying a POS id in it) renamed to `into_name` / the id's
    person. Returns rows rewritten."""
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
            r["employee"] = into_name
            n += 1
    if not n:
        return 0
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
    return n


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
                 (idx.rid, "rename", pid, pid, old, new, _json.dumps(moved, default=str)[:20000], actor_user_id, source))
    return moved


def rename_person(restaurant_id, person_id, new_name, actor_user_id=None, source="owner", db_path=None) -> dict:
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
    _after_change(restaurant_id, "rename", before, new, actor_user_id, source)
    return {"ok": True, "person_id": pid, "from": before, "to": new, "moved": moved}


def merge_people(restaurant_id, from_id, into_id, actor_user_id=None, source="owner", db_path=None) -> dict:
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
        # Their POS ids and spellings are the survivor's now.
        conn.execute("UPDATE person_aliases SET person_id=? WHERE person_id=? AND restaurant_id=?",
                     (b, a, restaurant_id))
        _alias(conn, idx, b, "merge", gone["display_name"])
        conn.execute("UPDATE people SET merged_into=?, active=0, updated_at=datetime('now') WHERE id=?", (b, a))
        conn.execute("UPDATE person_questions SET status='merged', answered_at=datetime('now'), answered_by=? "
                     "WHERE restaurant_id=? AND status='open' AND ((person_a=? AND person_b=?) OR (person_a=? AND "
                     "person_b=?))", (actor_user_id, restaurant_id, a, b, b, a))
        # Anything else asked about the merged person is now about the survivor.
        conn.execute("UPDATE OR IGNORE person_questions SET person_a=? WHERE restaurant_id=? AND person_a=? AND status='open'",
                     (b, restaurant_id, a))
        conn.execute("UPDATE OR IGNORE person_questions SET person_b=? WHERE restaurant_id=? AND person_b=? AND status='open'",
                     (b, restaurant_id, a))
        conn.execute("DELETE FROM person_questions WHERE restaurant_id=? AND status='open' AND person_a=person_b",
                     (restaurant_id,))
        conn.execute("INSERT INTO person_merges (restaurant_id, kind, from_person, into_person, from_name, into_name, "
                     "moved_json, actor_user_id, source) VALUES (?,?,?,?,?,?,?,?,?)",
                     (restaurant_id, "merge", a, b, gone["display_name"], keep["display_name"],
                      _json.dumps(moved, default=str)[:20000], actor_user_id, source))
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
    _after_change(restaurant_id, "merge", gone["display_name"], keep["display_name"], actor_user_id, source)
    kept = []
    for table, rec in moved.items():
        for f in rec.get("folded") or []:
            kept.append({"table": table, "row": {k: v for k, v in f.items() if k not in ("person_id",)}})
    return {"ok": True, "into": keep["display_name"], "from": gone["display_name"], "into_id": b, "from_id": a,
            "moved": {t: r["moved"] for t, r in moved.items()}, "folded": kept}


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


def _after_change(restaurant_id, kind, before, after, actor_user_id, source):
    """The roster changed under a person: the change log, and every cache
    built from names."""
    try:
        import change_log
        change_log.record(restaurant_id, "roster", after, {kind: before}, {kind: after},
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
            conn.execute("UPDATE person_questions SET status='different', answered_at=datetime('now'), answered_by=? "
                         "WHERE id=?", (uid, q["id"]))
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
    res = merge_people(restaurant_id, b if into == a else a, into, actor_user_id=uid, source=src, db_path=db_path)
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
                elif not cands and create and _looks_like_name(name):
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


def _person_for(conn, restaurant_id, name):
    idx = _Index(conn, restaurant_id)
    cands = idx.for_key(_nk(name))
    if len(cands) == 1:
        pid = next(iter(cands))
        return pid, idx.people[pid]["display_name"]
    return None, _clean(name)


def add_role(restaurant_id, name, role, since=None, primary=False, created_by=None, source="owner",
             db_path=None) -> dict:
    """A role this person holds — "trained on bar from 9/1" — that every
    reader of who-can-work-what sees (staff_settings.roles_for, the
    replacement picker, the roster's role when `primary`: a promotion). A
    second primary role replaces the first as primary."""
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
    try:
        import change_log
        change_log.record(restaurant_id, "roster", display, None,
                          {"role": role, "since": since_iso, "primary": bool(primary)},
                          actor_user_id=created_by, source=source)
    except Exception as e:
        _log.warning("[people] change_log failed rid=%s: %s", restaurant_id, e)
    return {"name": display, "role": role, "since": since_iso, "primary": bool(primary)}


def remove_role(restaurant_id, name, role, db_path=None) -> bool:
    conn = _conn(db_path)
    try:
        cur = conn.execute("UPDATE person_roles SET removed_at=datetime('now'), is_primary=0 WHERE restaurant_id=? "
                           "AND employee_key=? AND lower(role)=lower(?) AND removed_at IS NULL",
                           (restaurant_id, canonical_key(restaurant_id, name, db_path=db_path),
                            " ".join(str(role or "").split())))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()


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
                  created_by=None, db_path=None) -> bool:
    """One thing that happened to a person (SIGNAL_KINDS). Idempotent per
    (kind, ref, person); a confirmed or rejected signal is never downgraded
    back to proposed."""
    if kind not in SIGNAL_KINDS:
        raise ValueError(f"unknown person signal {kind!r}")
    conn = _conn(db_path)
    try:
        pid, display = _person_for(conn, restaurant_id, name)
        cur = conn.execute(
            "INSERT INTO person_signals (restaurant_id, person_id, employee_name, employee_key, kind, polarity, "
            "signal_date, ref, status, detail, created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, kind, ref, employee_key) DO UPDATE SET "
            "status=CASE WHEN person_signals.status='proposed' THEN excluded.status ELSE person_signals.status END, "
            "polarity=COALESCE(excluded.polarity, person_signals.polarity)",
            (restaurant_id, pid, display, _nk(display), kind, polarity, str(signal_date)[:10], str(ref or ""),
             status, (detail or "")[:300] or None, created_by))
        conn.commit()
        return (cur.rowcount or 0) > 0
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
        for r in conn.execute("SELECT replacement_name, COUNT(*) AS n FROM shift_change_requests WHERE "
                              "restaurant_id=? AND status='covered' AND replacement_name IS NOT NULL AND date>=? "
                              "GROUP BY replacement_name", (restaurant_id, since)).fetchall():
            e = out.setdefault(_nk(r["replacement_name"]), {"accepted": 0, "declined": 0})
            e["accepted"] += int(r["n"])
    except Exception:
        return out
    finally:
        conn.close()
    return out


def record_cover_signals(restaurant_id, days=7, db_path=None, today=None) -> int:
    """From the coverage issues the live check opened: everyone asked to
    cover who then worked that day took it (cover_accepted); asked and did
    not work by the end of the day, cover_declined. Returns signals written."""
    import json as _j
    from datetime import date as _d, timedelta as _td
    today = today or _d.today()
    since = (today - _td(days=days)).isoformat()
    conn = _conn(db_path)
    try:
        issues_ = conn.execute("SELECT id, source_key, meta_json FROM ops_issues WHERE restaurant_id=? AND "
                               "kind='coverage' AND source_key >= ?", (restaurant_id, f"coverage:{since}")).fetchall()
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
        asked = meta.get("asked") or []
        day = (iss["source_key"] or "").split(":")[1] if (iss["source_key"] or "").count(":") >= 2 else None
        if not asked or not day:
            continue
        try:
            import shift_facts
            worked = {_nk(r["employee"]) for r in shift_facts.rows(restaurant_id, since=day, until=day, db_path=db_path)}
        except Exception:
            worked = set()
        for a in asked:
            who = a.get("name")
            if not who:
                continue
            if _nk(canonical_names(restaurant_id, [who], db_path=db_path).get(who) or who) in worked:
                kind = "cover_accepted"
            elif day < today.isoformat():
                kind = "cover_declined"
            else:
                continue
            if record_signal(restaurant_id, who, kind, day, ref=f"issue:{iss['id']}",
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


def mentions(restaurant_id, status="proposed", db_path=None, limit=50) -> list:
    """Guest mentions of staff: [{id, name, key, date, polarity, review_id,
    snippet, status}] — "proposed" ones wait on the owner's confirmation."""
    from time_utils import mdy
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM person_signals WHERE restaurant_id=? AND kind='review_mention' AND status=? "
                            "ORDER BY signal_date DESC, id DESC LIMIT ?", (restaurant_id, status, int(limit))).fetchall()
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
        cur = conn.execute("UPDATE person_signals SET status=?, created_by=COALESCE(created_by, ?) WHERE id=? AND "
                           "restaurant_id=? AND kind='review_mention' AND status='proposed'",
                           ("confirmed" if confirm else "rejected",
                            (user or {}).get("id") if isinstance(user, dict) else None, int(signal_id), restaurant_id))
        conn.commit()
        return (cur.rowcount or 0) > 0
    finally:
        conn.close()
