"""
org_bench.py — the certified bench across one owner's locations: the
consent and identity model under the organization's shared staff, and the
owner's readout of who is on it with their roles, certifications and
attendance (scheduler audit 10/10/26 MARKET-30).

Short-notice cover is a restaurant's worst problem, and staffing apps fill
it with strangers. The people already working at a sibling location are
better — known roles, certifications, attendance. J2's cross_site (MARKET-
16) offers a sibling's open shift to people who opted in to travel, judged
against the whole rule engine and written into that site's week. This
module is the layer that makes it a CERTIFIED bench:

  identity     the login: a user with a membership at their HOME site; the
               consent row is (user, home restaurant_id), so people.
               NAME_STORES re-points it on a rename and deletes it on an
               erase. Siblings are schedule_rules.org_sibling_ids — the same
               organization (or group under one owner), never an unrelated
               restaurant.
  one opt-in   the person's travel switch (cross_site.cross_site_travel) is
               the ONE flag; turning it on in the staff app records here
               their written, time-stamped consent to the exact words shown
               (CONSENT_WORDS, versioned) — that managers at the other
               locations may see their name, roles, certifications and
               attendance and offer them shifts — and turning it off revokes
               it at once (`record_travel`, called by the travel route).
  the site key each location's owner decides whether ITS opted-in staff's
               certifications and attendance are shared with the others
               (`org_bench_sites`). Off, a sibling still sees that they can
               be offered a shift (J2's judge), never their record.
  candidates() for a gap at one site: the opted-in people of every sibling,
               who hold the role family at home, aren't on an overlapping
               shift there, and — where both keys allow it — carry the
               role's certifications here and their home attendance (no-show
               rate); ranked by it. The offer itself is J2's (the open
               shift's cross-site candidates): one offer path, one legality
               engine.

EXTERNAL (not built, by design): a bench across UNRELATED employers. A
shift for a different employer is employment by that employer (or a
staffing agency's): a staffing-agency partner for the employment
relationship, payroll and insurance, and counsel. Nothing here crosses an
organization boundary; within one organization whose locations are
separate legal employers, payroll for the second site is the owner's
(ROADMAP: payroll is partnered). Nothing calls a model.
"""
import logging
from datetime import datetime

log = logging.getLogger(__name__)

CONSENT_VERSION = 1
CONSENT_WORDS = ("I agree that managers at {org}'s other locations may see my name, the roles I work, my "
                 "certifications and my attendance record, and may offer me shifts there. I can turn this off at "
                 "any time and they stop seeing it at once. A shift at another location is only mine once I "
                 "accept it.")


def _models_db():
    """The database path as models has it now (CLAUDE.md, bound imports: a
    module's own DB_PATH is the one it was imported with)."""
    import models
    return models.DB_PATH


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if not db_path or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


def init_org_bench(db_path=None):
    """Boot DDL (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS org_bench_sites (
            restaurant_id   INTEGER PRIMARY KEY REFERENCES restaurants(id),
            enabled         INTEGER NOT NULL DEFAULT 0,
            updated_by      TEXT,
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS org_bench_members (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id             INTEGER NOT NULL,
            restaurant_id       INTEGER NOT NULL REFERENCES restaurants(id),
            employee_name       TEXT    NOT NULL,
            consent_at          TEXT    NOT NULL,
            consent_text        TEXT    NOT NULL,
            consent_version     INTEGER NOT NULL,
            revoked_at          TEXT,
            UNIQUE(user_id, restaurant_id)
        )""")
        conn.commit()
    finally:
        conn.close()


class BenchError(ValueError):
    """A refusal the caller shows as it is."""


def _key(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _org_name(restaurant_id, db_path=None):
    """The organization's own name (organizations.name by the location's
    organization_id), else its group, else the location's."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT o.name AS org, r.location_group AS grp, r.name AS name FROM restaurants r LEFT JOIN "
                         "organizations o ON o.id=r.organization_id WHERE r.id=?", (restaurant_id,)).fetchone()
    except Exception:
        r = None
    finally:
        conn.close()
    if not r:
        return "your organization"
    return (r["org"] or r["grp"] or r["name"] or "your organization").strip()


def _place(restaurant_id, db_path=None):
    try:
        import models
        r = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
        return (getattr(r, "location_name", None) or getattr(r, "name", None) or "the restaurant") if r else "the restaurant"
    except Exception:
        return "the restaurant"


# ── the site key ───────────────────────────────────────────────────────────

def site_enabled(restaurant_id, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT enabled FROM org_bench_sites WHERE restaurant_id=?", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    return bool(r and r["enabled"])


def set_site(restaurant_id, enabled, by=None, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO org_bench_sites (restaurant_id, enabled, updated_by, updated_at) VALUES "
                     "(?,?,?,datetime('now')) ON CONFLICT(restaurant_id) DO UPDATE SET enabled=excluded.enabled, "
                     "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                     (restaurant_id, 1 if enabled else 0, (by or "")[:120] or None))
        conn.commit()
    finally:
        conn.close()
    return {"enabled": bool(enabled)}


# ── the person's consent (one opt-in: the travel switch) ───────────────────

def consent_text(home_restaurant_id, db_path=None) -> str:
    return CONSENT_WORDS.format(org=_org_name(home_restaurant_id, db_path))


def membership(user_id, home_restaurant_id, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM org_bench_members WHERE user_id=? AND restaurant_id=?",
                         (int(user_id), home_restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def record_travel(user_id, home_restaurant_id, employee_name, on, db_path=None) -> dict:
    """The travel switch (cross_site.set_travel) and this consent in one
    step: on records the person's written, time-stamped consent to the exact
    words shown beside the switch; off revokes it. Raises BenchError for a
    location with no other locations."""
    import cross_site
    import schedule_rules
    name = " ".join(str(employee_name or "").split())
    if on and not schedule_rules.org_sibling_ids(home_restaurant_id, db_path=db_path or _models_db()):
        raise BenchError("this location has no other locations to share with")
    cross_site.set_travel(home_restaurant_id, name, bool(on), source="staff", db_path=db_path)
    conn = get_conn(db_path)
    try:
        if on:
            conn.execute("INSERT INTO org_bench_members (user_id, restaurant_id, employee_name, consent_at, "
                         "consent_text, consent_version) VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, restaurant_id) "
                         "DO UPDATE SET employee_name=excluded.employee_name, consent_at=excluded.consent_at, "
                         "consent_text=excluded.consent_text, consent_version=excluded.consent_version, revoked_at=NULL",
                         (int(user_id), home_restaurant_id, name, _now(), consent_text(home_restaurant_id, db_path),
                          CONSENT_VERSION))
        else:
            conn.execute("UPDATE org_bench_members SET revoked_at=? WHERE user_id=? AND restaurant_id=? AND "
                         "revoked_at IS NULL", (_now(), int(user_id), home_restaurant_id))
        conn.commit()
    finally:
        conn.close()
    return for_staff(user_id, home_restaurant_id, name, db_path=db_path)


def for_staff(user_id, home_restaurant_id, employee_name, db_path=None) -> dict:
    """What the travel card shows beside the switch: the exact words and,
    when on, when they agreed."""
    import cross_site
    m = membership(user_id, home_restaurant_id, db_path)
    on = cross_site.travels(home_restaurant_id, employee_name, db_path=db_path)
    return {"consent_text": consent_text(home_restaurant_id, db_path),
            "consent_at": (m or {}).get("consent_at") if (on and m and not m.get("revoked_at")) else None}


def opted_in(home_restaurant_id, employee_name, db_path=None) -> bool:
    """The one flag: cross_site's travel switch."""
    import cross_site
    return cross_site.travels(home_restaurant_id, employee_name, db_path=db_path)


def _consented(home_restaurant_id, db_path=None) -> dict:
    """{name key: consent row} of the people who agreed to share their record."""
    conn = get_conn(db_path)
    try:
        return {_key(r["employee_name"]): dict(r) for r in conn.execute(
            "SELECT * FROM org_bench_members WHERE restaurant_id=? AND revoked_at IS NULL",
            (home_restaurant_id,)).fetchall()}
    finally:
        conn.close()


# ── the readout ────────────────────────────────────────────────────────────

def _overlaps(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def candidates(restaurant_id, day, role, shift_start, shift_end, db_path=None) -> dict:
    """{available, reason?, candidates: [{name, home, home_restaurant_id,
    roles, certifications | None, no_show_rate | None, shared, why}], note}
    — module doc. A record is shown only where the person consented and
    their home site shares (both keys); otherwise `shared` is False and the
    record None — never guessed."""
    import json
    import cross_site
    import models
    import schedule_rules
    import schedule_time as st
    from shift_quality import role_family
    sibs = schedule_rules.org_sibling_ids(restaurant_id, db_path=db_path or _models_db())
    if not sibs:
        return {"available": False, "reason": "You have no other locations in Cavnar AI.", "candidates": []}
    here = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
    fam = role_family(role or "", schedule_rules.role_families(here) or {})
    try:
        needs = {str(k).strip().lower(): [str(x).strip().lower() for x in (v or [])] for k, v in
                 (json.loads(getattr(here, "role_requirements_json", None) or "{}") or {}).items()}
    except (TypeError, ValueError):
        needs = {}
    needed = set(needs.get(str(role or "").strip().lower()) or [])
    sp = st.span({"shift_start": shift_start, "shift_end": shift_end})
    busy = models.sibling_location_shifts(restaurant_id, [day], db_path=db_path or models.DB_PATH) if day else {}
    out = []
    for home in sibs:
        travellers = cross_site.travellers(home, db_path=db_path)
        if not travellers:
            continue
        shares = site_enabled(home, db_path)
        consented = _consented(home, db_path) if shares else {}
        try:
            import shift_facts
            import staff_settings
            home_r = models.get_restaurant(home, db_path or models.DB_PATH)
            fams_home = schedule_rules.role_families(home_r) or {}
            hist = shift_facts.person_rows(home, db_path=db_path)
            settings = {_key(n): v for n, v in (staff_settings.get_all(home, db_path=db_path or _models_db()) or {}).items()}
            rec = staff_settings.attendance_record(home, db_path=db_path or _models_db()) if consented else None
        except Exception as e:
            log.warning("bench: home %s unreadable: %s", home, e)
            continue
        for name in travellers:
            k = _key(name)
            roles = sorted({r.get("role") for r in hist if _key(r.get("employee")) == k and r.get("role")})
            if fam and not any(role_family(x, fams_home) == fam for x in roles):
                continue
            clash = False
            for r in busy.get(name, []) or []:
                osp = st.span({"shift_start": r.get("shift_start"), "shift_end": r.get("shift_end")})
                if sp and osp and _overlaps(sp[0], sp[1], osp[0], osp[1]):
                    clash = True
            if clash:
                continue
            shared = k in consented
            certs, rate = None, None
            if shared:
                certs = sorted({str(c).strip().lower() for c in (settings.get(k) or {}).get("certifications") or []})
                if needed - set(certs):
                    continue
                t = ((rec or {}).get("tally") or {}).get(name) or {}
                if t.get("shifts"):
                    rate = staff_settings.smoothed_rate(t["w_absent"], t["w"], rec["base"]["absence"])
            out.append({"name": name, "home": _place(home, db_path), "home_restaurant_id": home, "roles": roles,
                        "certifications": certs, "no_show_rate": round(rate, 3) if rate is not None else None,
                        "shared": shared,
                        "why": (f"works {', '.join(roles[:3]) or 'there'} at {_place(home, db_path)}"
                                + ((f", {int(round(rate * 100))}% missed shifts" if rate is not None
                                    else ", no attendance record yet") if shared
                                   else "; their record isn't shared (their location hasn't turned sharing on, or "
                                        "they haven't agreed)"))})
    out.sort(key=lambda c: (not c["shared"], c["no_show_rate"] is None, c["no_show_rate"] or 0, c["name"]))
    return {"available": True, "candidates": out,
            "note": "Offer one from the open shift — its other-locations list judges them against every rule here."}


def owner_view(restaurant_id, db_path=None) -> dict:
    """The Studio's read: the site switch, the siblings and whether each
    shares, and this site's people on the bench (the owner's own staff) with
    whether they agreed to share their record."""
    import cross_site
    import schedule_rules
    sibs = schedule_rules.org_sibling_ids(restaurant_id, db_path=db_path or _models_db())
    agreed = _consented(restaurant_id, db_path)
    members = [{"name": n, "consent_at": (agreed.get(_key(n)) or {}).get("consent_at")}
               for n in cross_site.travellers(restaurant_id, db_path=db_path)]
    return {"ok": True, "enabled": site_enabled(restaurant_id, db_path),
            "siblings": [{"restaurant_id": s, "name": _place(s, db_path), "enabled": site_enabled(s, db_path)} for s in sibs],
            "members": members,
            "external": ("Covering from restaurants outside your organization needs a staffing-agency partner to "
                         "employ the person; Cavnar AI doesn't do that.")}
