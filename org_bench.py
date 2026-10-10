"""
org_bench.py — the certified bench across one owner's locations: staff who
opted in to pick up shifts at their organization's other sites, with their
roles, certifications and attendance shared by consent (scheduler audit
10/10/26 MARKET-30).

Short-notice cover is a restaurant's worst problem, and staffing apps fill
it with strangers. The people already working at a sibling location are
better — known roles, certifications, attendance — but there was no
cross-site identity or consent to offer them a shift.

What is built (organization-internal):

  identity     one login (auth users) with a membership at the home site;
               the bench row is (user, home restaurant). Siblings are
               schedule_rules.org_sibling_ids — the same organization (or
               the same group under the same owner) — never an unrelated
               restaurant.
  consent      two keys, both required: the SITE (`org_bench_sites`: the
               owner of each location turns the bench on — it shares its
               opted-in staff and may ask the others'), and the PERSON
               (`org_bench_members`: written, time-stamped consent to the
               exact words — that managers at the organization's other
               locations may see their name, roles, certifications and
               attendance record and offer them shifts; revocable any time,
               which removes them at once).
  candidates() for a gap at one site (date, role, hours): consented members
               of enabled sibling sites who hold the role family at home,
               carry every certification the role needs here (role_
               requirements_json against their home staff_settings), are
               not working an overlapping shift at home, ranked by their
               home attendance (no-show rate, staff_settings.
               attendance_record) — what they consented to share, nothing
               else.
  ask()        the asking site's manager asks one of them; the person is
               told through their home site's channel (people.tell) and
               answers in their own staff app (`respond`).
  respond()    a yes tells the asking site's deciders; the next step is
               theirs — add the person to that site's roster (one login,
               a second membership) and offer the shift through the normal
               offer, where the full legality sweep runs with their
               home-site hours counted (models.sibling_location_shifts).

EXTERNAL (not built, by design): a bench across UNRELATED employers. A
shift for a different employer is employment by that employer (or a
staffing agency's): it needs a staffing-agency partner for the employment
relationship, payroll and insurance, and counsel. Nothing here crosses an
organization boundary. Within one organization whose locations are
separate legal employers, payroll for the second site is the owner's
(ROADMAP: payroll is partnered).

J2's cross-location sharing (MARKET-16) can read `opted_in()` as the
"opted in to travel" flag. Nothing calls a model.
"""
import logging
from datetime import date, datetime

log = logging.getLogger(__name__)

CONSENT_VERSION = 1
CONSENT_WORDS = ("I agree that managers at {org}'s other locations may see my name, the roles I work, my "
                 "certifications and my attendance record, and may offer me shifts there. I can leave the bench at "
                 "any time and they stop seeing it at once. A shift at another location is only mine once I accept "
                 "it, and I'm added to that location's team.")



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
            home_restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            employee_name       TEXT    NOT NULL,
            consent_at          TEXT    NOT NULL,
            consent_text        TEXT    NOT NULL,
            consent_version     INTEGER NOT NULL,
            revoked_at          TEXT,
            UNIQUE(user_id, home_restaurant_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS org_bench_requests (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id       INTEGER NOT NULL REFERENCES restaurants(id),
            member_id           INTEGER NOT NULL,
            home_restaurant_id  INTEGER NOT NULL,
            employee_name       TEXT    NOT NULL,
            date                TEXT    NOT NULL,
            role                TEXT    NOT NULL,
            shift_start         TEXT    NOT NULL,
            shift_end           TEXT    NOT NULL,
            status              TEXT    NOT NULL DEFAULT 'asked',
            asked_by            TEXT,
            asked_at            TEXT    NOT NULL DEFAULT (datetime('now')),
            answered_at         TEXT,
            note                TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_org_bench_req ON org_bench_requests(restaurant_id, date)")
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


# ── the two keys ───────────────────────────────────────────────────────────

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


def consent_text(home_restaurant_id, db_path=None) -> str:
    return CONSENT_WORDS.format(org=_org_name(home_restaurant_id, db_path))


def join(user_id, home_restaurant_id, employee_name, agree=False, db_path=None) -> dict:
    """The person's own opt-in, from their staff app. Needs `agree` — their
    consent to the exact words shown — and a sibling to share with."""
    import schedule_rules
    if not agree:
        raise BenchError("tick that you agree to share this with the other locations to join")
    if not site_enabled(home_restaurant_id, db_path):
        raise BenchError("your manager hasn't turned on the bench for this location")
    if not schedule_rules.org_sibling_ids(home_restaurant_id, db_path=db_path or _models_db()):
        raise BenchError("this location has no other locations to share with")
    text = consent_text(home_restaurant_id, db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO org_bench_members (user_id, home_restaurant_id, employee_name, consent_at, "
                     "consent_text, consent_version) VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, home_restaurant_id) "
                     "DO UPDATE SET employee_name=excluded.employee_name, consent_at=excluded.consent_at, "
                     "consent_text=excluded.consent_text, consent_version=excluded.consent_version, revoked_at=NULL",
                     (int(user_id), home_restaurant_id, employee_name, _now(), text, CONSENT_VERSION))
        conn.commit()
        row = conn.execute("SELECT * FROM org_bench_members WHERE user_id=? AND home_restaurant_id=?",
                           (int(user_id), home_restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(row)


def leave(user_id, home_restaurant_id, db_path=None) -> bool:
    """Consent withdrawn: off the bench at once, every open ask withdrawn."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE org_bench_members SET revoked_at=? WHERE user_id=? AND home_restaurant_id=? AND "
                           "revoked_at IS NULL", (_now(), int(user_id), home_restaurant_id))
        conn.execute("UPDATE org_bench_requests SET status='withdrawn', answered_at=? WHERE status='asked' AND "
                     "member_id IN (SELECT id FROM org_bench_members WHERE user_id=? AND home_restaurant_id=?)",
                     (_now(), int(user_id), home_restaurant_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def membership(user_id, home_restaurant_id, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM org_bench_members WHERE user_id=? AND home_restaurant_id=?",
                         (int(user_id), home_restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def opted_in(home_restaurant_id, employee_name, db_path=None) -> bool:
    """Whether this person has consented to the bench (J2's travel flag)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT employee_name FROM org_bench_members WHERE home_restaurant_id=? AND revoked_at IS "
                            "NULL", (home_restaurant_id,)).fetchall()
    finally:
        conn.close()
    return any(_key(r["employee_name"]) == _key(employee_name) for r in rows) and site_enabled(home_restaurant_id, db_path)


# ── finding and asking ─────────────────────────────────────────────────────

def _overlaps(a0, a1, b0, b1):
    return a0 < b1 and b0 < a1


def candidates(restaurant_id, day, role, shift_start, shift_end, db_path=None) -> dict:
    """{available, reason?, candidates: [{member_id, name, home, roles,
    certifications, no_show_rate, why}]} — module doc."""
    import models
    import schedule_rules
    import schedule_time as st
    from shift_quality import role_family
    if not site_enabled(restaurant_id, db_path):
        return {"available": False, "reason": "Turn the bench on for this location to see your other locations' bench.",
                "candidates": []}
    sibs = [s for s in schedule_rules.org_sibling_ids(restaurant_id, db_path=db_path or _models_db())
            if site_enabled(s, db_path)]
    if not sibs:
        return {"available": False, "reason": "None of your other locations has the bench turned on.", "candidates": []}
    here = models.get_restaurant(restaurant_id, db_path or models.DB_PATH)
    fams_here = schedule_rules.role_families(here) or {}
    fam = role_family(role or "", fams_here)
    try:
        import json
        needs = {str(k).strip().lower(): [str(x).strip().lower() for x in (v or [])] for k, v in
                 (json.loads(getattr(here, "role_requirements_json", None) or "{}") or {}).items()}
    except (TypeError, ValueError):
        needs = {}
    needed = set(needs.get(str(role or "").strip().lower()) or [])
    sp = st.span({"shift_start": shift_start, "shift_end": shift_end})
    conn = get_conn(db_path)
    try:
        members = [dict(r) for r in conn.execute(
            f"SELECT * FROM org_bench_members WHERE revoked_at IS NULL AND home_restaurant_id IN "
            f"({','.join('?' for _ in sibs)})", sibs).fetchall()]
    finally:
        conn.close()
    out = []
    for m in members:
        home = m["home_restaurant_id"]
        try:
            import shift_facts
            import staff_settings
            hist = shift_facts.person_rows(home, db_path=db_path)
            home_r = models.get_restaurant(home, db_path or models.DB_PATH)
            fams_home = schedule_rules.role_families(home_r) or {}
            mine = [r for r in hist if _key(r.get("employee")) == _key(m["employee_name"])]
            roles = sorted({r.get("role") for r in mine if r.get("role")})
            if fam and not any(role_family(r_, fams_home) == fam for r_ in roles):
                continue
            settings = {_key(n): v for n, v in (staff_settings.get_all(home, db_path=db_path or _models_db()) or {}).items()}.get(_key(m["employee_name"])) or {}
            certs = {str(c).strip().lower() for c in settings.get("certifications") or []}
            if needed - certs:
                continue
            busy = False
            for r in models.sibling_location_shifts(restaurant_id, [day], db_path=db_path or models.DB_PATH).get(
                    m["employee_name"], []) or []:
                osp = st.span({"shift_start": r.get("shift_start"), "shift_end": r.get("shift_end")})
                if sp and osp and _overlaps(sp[0], sp[1], osp[0], osp[1]):
                    busy = True
            if busy:
                continue
            rec = staff_settings.attendance_record(home, db_path=db_path or _models_db())
            t = (rec.get("tally") or {}).get(m["employee_name"]) or {}
            rate = staff_settings.smoothed_rate(t["w_absent"], t["w"], rec["base"]["absence"]) if t.get("shifts") else None
        except Exception as e:
            log.warning("bench candidate unreadable rid=%s member=%s: %s", restaurant_id, m["id"], e)
            continue
        out.append({"member_id": m["id"], "name": m["employee_name"], "home": _place(home, db_path),
                    "home_restaurant_id": home, "roles": roles, "certifications": sorted(certs),
                    "no_show_rate": round(rate, 3) if rate is not None else None,
                    "why": ", ".join(b for b in (f"works {', '.join(roles[:3])} at {_place(home, db_path)}",
                                                 f"{int(round(rate * 100))}% missed shifts" if rate is not None else
                                                 "no attendance record yet") if b)})
    out.sort(key=lambda c: (c["no_show_rate"] is None, c["no_show_rate"] or 0, c["name"]))
    return {"available": True, "candidates": out}


def ask(restaurant_id, member_id, day, role, shift_start, shift_end, by=None, db_path=None) -> dict:
    """Ask one bench member (who must still be a candidate) to take the
    shift; they are told through their home site. Raises BenchError."""
    got = candidates(restaurant_id, day, role, shift_start, shift_end, db_path=db_path)
    c = next((x for x in got.get("candidates") or [] if x["member_id"] == int(member_id)), None)
    if c is None:
        raise BenchError(got.get("reason") or "they can't take that shift (role, certification, a shift at home, or "
                                               "they left the bench)")
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO org_bench_requests (restaurant_id, member_id, home_restaurant_id, employee_name, "
                           "date, role, shift_start, shift_end, asked_by) VALUES (?,?,?,?,?,?,?,?,?)",
                           (restaurant_id, c["member_id"], c["home_restaurant_id"], c["name"], day, role, shift_start,
                            shift_end, (by or "")[:120] or None))
        conn.commit()
        req = dict(conn.execute("SELECT * FROM org_bench_requests WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()
    try:
        import people
        from time_utils import mdy
        req["via"] = people.tell(c["home_restaurant_id"], c["name"],
                                 f"{_place(restaurant_id, db_path)} is short a {role}",
                                 [f"Can you pick up {date.fromisoformat(day).strftime('%A')} {mdy(day)}, "
                                  f"{shift_start}–{shift_end}? Answer in the app under Requests."],
                                 kind="request", nav={"tab": "requests", "bench_request_id": req["id"]},
                                 shift_date=day, db_path=db_path)
    except Exception as e:
        log.warning("bench ask notice failed rid=%s: %s", restaurant_id, e)
        req["via"] = None
    return req


def for_staff(user_id, home_restaurant_id, db_path=None) -> dict:
    """The person's own bench: whether they are on it, the words they agreed
    to, and the asks waiting on them."""
    m = membership(user_id, home_restaurant_id, db_path)
    asks = []
    if m:
        conn = get_conn(db_path)
        try:
            asks = [dict(r) for r in conn.execute(
                "SELECT id, restaurant_id, date, role, shift_start, shift_end, status FROM org_bench_requests WHERE "
                "member_id=? AND date>=? ORDER BY date", (m["id"], date.today().isoformat())).fetchall()]
        finally:
            conn.close()
        for a in asks:
            a["place"] = _place(a["restaurant_id"], db_path)
    return {"site_enabled": site_enabled(home_restaurant_id, db_path),
            "on_bench": bool(m and not m.get("revoked_at")), "consent_at": (m or {}).get("consent_at"),
            "consent_text": consent_text(home_restaurant_id, db_path), "asks": asks}


def respond(request_id, user_id, home_restaurant_id, accept, db_path=None) -> dict:
    m = membership(user_id, home_restaurant_id, db_path)
    if not m or m.get("revoked_at"):
        raise BenchError("you're not on the bench")
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM org_bench_requests WHERE id=? AND member_id=?", (int(request_id), m["id"])).fetchone()
        if not r or r["status"] != "asked":
            raise BenchError("that ask is closed")
        conn.execute("UPDATE org_bench_requests SET status=?, answered_at=? WHERE id=?",
                     ("accepted" if accept else "declined", _now(), r["id"]))
        conn.commit()
        req = dict(conn.execute("SELECT * FROM org_bench_requests WHERE id=?", (r["id"],)).fetchone())
    finally:
        conn.close()
    try:
        import people
        from time_utils import mdy
        people.tell_deciders(req["restaurant_id"],
                             f"{req['employee_name']} {'can' if accept else 'can’t'} cover {mdy(req['date'])}",
                             (f"{req['role']} {req['shift_start']}–{req['shift_end']}, from "
                              f"{_place(home_restaurant_id, db_path)}. Add them to your team and offer the shift."
                              if accept else f"{req['role']} {req['shift_start']}–{req['shift_end']}."),
                             alert_type="shift_request")
    except Exception as e:
        log.warning("bench answer notice failed: %s", e)
    return req


def owner_view(restaurant_id, db_path=None) -> dict:
    """The Studio's read: the site switch, the siblings and whether each has
    it on, this site's members (count and names — the owner's own staff),
    and the asks made from here."""
    import schedule_rules
    sibs = schedule_rules.org_sibling_ids(restaurant_id, db_path=db_path or _models_db())
    conn = get_conn(db_path)
    try:
        mine = [dict(r) for r in conn.execute(
            "SELECT employee_name, consent_at FROM org_bench_members WHERE home_restaurant_id=? AND revoked_at IS NULL "
            "ORDER BY employee_name", (restaurant_id,)).fetchall()]
        asks = [dict(r) for r in conn.execute(
            "SELECT * FROM org_bench_requests WHERE restaurant_id=? ORDER BY asked_at DESC LIMIT 50",
            (restaurant_id,)).fetchall()]
    finally:
        conn.close()
    return {"ok": True, "enabled": site_enabled(restaurant_id, db_path),
            "siblings": [{"restaurant_id": s, "name": _place(s, db_path), "enabled": site_enabled(s, db_path)} for s in sibs],
            "members": mine, "asks": asks,
            "external": ("Covering from restaurants outside your organization needs a staffing-agency partner to "
                         "employ the person; Cavnar AI doesn't do that.")}
