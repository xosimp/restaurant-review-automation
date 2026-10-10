"""
cross_site.py — people from the group's other locations, offered a shift here
(schedule audit 10/10/26 MARKET-16).

A five-unit group short a Friday bartender at unit A could not offer the shift
to a certified bartender at unit B, though the platform already knew both
people's hours and legality: sibling sites appeared only as hours and dates
already worked elsewhere (schedule_rules._published_tail — their published
rows ride in the tail as base_rows/base_hours under the person's key here).

  * A person opts in to travel at their HOME site (set_travel: from the staff
    app — "I'll pick up shifts at our other locations" — or the owner marking
    it with their agreement). Nobody is offered a sibling's shift otherwise.
  * candidates() lists, for an open shift here, the opted-in people at every
    organisation sibling (schedule_rules.org_sibling_ids) who hold the shift's
    role (or its family) at home, each JUDGED HERE: this site's Constraints
    for the week, with the visitor's own facts adopted from their home site's
    Constraints for the same dates (adopt_visitor: availability, time off,
    windows, minor status, certificates, hours limits, roles) and their hours
    at home already in the tail — then schedule_rules.judge_change, the one
    engine (a manager every minute, floors, closers, rest, hours, overtime).
  * offer() / respond(): the offer reaches them through their home site's
    channel (people.tell); their yes is judged again on the week as it is
    and written into THIS site's live week (schedule_versions.write_on, a
    `swap` version, in one transaction with the request's status) — so
    their hours count toward this site's labor %, and toward their own
    overtime line at home through the tail.
  * A visitor is never counted as a manager here (their home manager status
    is not adopted): the manager-every-minute rule is never met by somebody
    this site never made a manager.
"""
import json
from datetime import datetime

import models as _models_mod


class CrossSiteError(ValueError):
    pass


def get_conn(db_path=None):
    return _models_mod.get_conn(db_path) if db_path else _models_mod.get_conn()


def init_cross_site(db_path=None):
    """Boot DDL (models.init_db)."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS cross_site_travel (
            restaurant_id INTEGER NOT NULL,
            name_key      TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            source        TEXT NOT NULL DEFAULT 'staff',
            opted_in_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (restaurant_id, name_key)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS cross_site_offers (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            target_restaurant_id INTEGER NOT NULL,
            request_id         INTEGER NOT NULL,
            home_restaurant_id INTEGER NOT NULL,
            employee_name      TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'offered',
            created_by         TEXT,
            created_at         TEXT NOT NULL DEFAULT (datetime('now')),
            answered_at        TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cross_site_offers_home ON cross_site_offers "
                     "(home_restaurant_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cross_site_offers_created ON cross_site_offers (created_at)")
        conn.commit()
    finally:
        conn.close()


def _nk(name):
    import staff_settings as _ss
    return _ss.name_key(name)


# ── opting in ───────────────────────────────────────────────────────────────

def set_travel(home_restaurant_id, name, on, source="staff", db_path=None) -> bool:
    name = " ".join(str(name or "").split())
    if not name:
        raise CrossSiteError("who?")
    conn = get_conn(db_path)
    try:
        if on:
            conn.execute("INSERT INTO cross_site_travel (restaurant_id, name_key, employee_name, source) VALUES "
                         "(?,?,?,?) ON CONFLICT(restaurant_id, name_key) DO UPDATE SET employee_name=excluded."
                         "employee_name, source=excluded.source", (home_restaurant_id, _nk(name), name, source))
        else:
            conn.execute("DELETE FROM cross_site_travel WHERE restaurant_id=? AND name_key=?",
                         (home_restaurant_id, _nk(name)))
        conn.commit()
    finally:
        conn.close()
    return bool(on)


def travels(home_restaurant_id, name, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        return conn.execute("SELECT 1 FROM cross_site_travel WHERE restaurant_id=? AND name_key=?",
                            (home_restaurant_id, _nk(name))).fetchone() is not None
    finally:
        conn.close()


def travellers(home_restaurant_id, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        return [r["employee_name"] for r in conn.execute(
            "SELECT employee_name FROM cross_site_travel WHERE restaurant_id=? ORDER BY employee_name",
            (home_restaurant_id,)).fetchall()]
    finally:
        conn.close()


# ── judging a visitor here ──────────────────────────────────────────────────

# The per-person facts a visitor brings from home, keyed by their name key.
_PERSON_DICTS = ("blocked_dates", "blocked_parts", "pending_off", "unavailable_days", "daypart_avail",
                 "hours_limits", "employment", "minor_bands", "notes", "time_windows", "certifications",
                 "cert_expiry", "held_roles", "known_roles", "note_caution", "work_days", "dormant")
_PERSON_SETS = ("minors",)


def adopt_visitor(c, home_c, name):
    """Copy one visitor's own facts from their home site's Constraints
    (`home_c`, built for the same dates) into this site's (`c`), under
    this site's key for them. Manager, keyholder and salaried status are
    NOT adopted: this site never made them those."""
    kh, k = home_c.key(name), c.key(name)
    for attr in _PERSON_DICTS:
        src, dst = getattr(home_c, attr, None), getattr(c, attr, None)
        if isinstance(src, dict) and isinstance(dst, dict) and kh in src:
            dst[k] = src[kh]
    for attr in _PERSON_SETS:
        src, dst = getattr(home_c, attr, None), getattr(c, attr, None)
        if isinstance(src, set) and isinstance(dst, set) and kh in src:
            dst.add(k)
    if isinstance(getattr(c, "active", None), set):
        c.active.add(k)
    if isinstance(getattr(c, "display", None), dict):
        c.display.setdefault(k, (getattr(home_c, "display", {}) or {}).get(kh, name))
    return c


def _request(restaurant_id, request_id, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM shift_change_requests WHERE id=? AND restaurant_id=? AND status='open'",
                         (int(request_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _target_week(restaurant_id, req, db_path=None):
    """(hist, rows, idx) of the live week here holding the open shift."""
    import shift_requests as _srq
    from schedule_versions import rows_from_csv
    conn = get_conn(db_path)
    try:
        hist = _srq._live_hist(conn, restaurant_id, req)
    finally:
        conn.close()
    if not hist:
        return None, None, None
    rows, idx = _srq._locate(rows_from_csv(hist["schedule_csv"]), req)
    return hist, rows, idx


def _holds_role(home_restaurant_id, name, role, families=None) -> bool:
    import staff_settings as _ss
    from shift_quality import role_family
    try:
        held = {x.strip().lower() for x in _ss.roles_for(home_restaurant_id, name)}
    except Exception:
        held = set()
    want = (role or "").strip().lower()
    return bool(want) and (want in held or role_family(want, families) in {role_family(x, families) for x in held})


def judge(restaurant_id, req, home_restaurant_id, name, rows=None, idx=None, c=None, db_path=None):
    """(ok, why) — may `name`, from `home_restaurant_id`, take the open shift
    `req` here? This site's week and rules, the visitor's own facts adopted,
    their hours at home in the tail; judge_change decides."""
    import schedule_rules as _sr
    import shift_requests as _srq
    if rows is None:
        _hist, rows, idx = _target_week(restaurant_id, req, db_path)
    if rows is None or idx is None:
        return False, "the shift is no longer on the schedule here"
    dates = sorted({r["date"] for r in rows if r.get("date")})
    days = [datetime.strptime(x, "%Y-%m-%d").strftime("%A") for x in dates]
    c = c or _srq._week_constraints(restaurant_id, rows)
    home_c = _sr.build_constraints(home_restaurant_id, dates, days)
    if home_c.key(name) in (home_c.inactive or set()):
        return False, f"{name} isn't active at their home location"
    adopt_visitor(c, home_c, name)
    trial = [dict(r) for r in rows]
    trial[idx] = dict(trial[idx], employee=name)
    worse = _sr.judge_change(rows, trial, c, takers=[(idx, name)])
    if worse:
        return False, worse[0].get("label") or "it breaks a rule"
    return True, None


def candidates(restaurant_id, request_id, db_path=None) -> list:
    """Opted-in people at the group's other locations who hold the shift's
    role, each judged here: [{name, home_restaurant_id, home, ok, why}],
    the legal ones first."""
    import schedule_rules as _sr
    import shift_requests as _srq
    req = _request(restaurant_id, request_id, db_path)
    if not req:
        return []
    _hist, rows, idx = _target_week(restaurant_id, req, db_path)
    if rows is None or idx is None:
        return []
    out = []
    here = {_nk(r.get("employee")) for r in rows}
    for sid in _sr.org_sibling_ids(restaurant_id, **({"db_path": db_path} if db_path else {})):
        try:
            label = getattr(_models_mod.get_restaurant(sid), "location_name", None) or \
                getattr(_models_mod.get_restaurant(sid), "name", None) or f"location {sid}"
        except Exception:
            label = f"location {sid}"
        for name in travellers(sid, db_path):
            if _nk(name) in here or not _holds_role(sid, name, req.get("role")):
                continue
            c = _srq._week_constraints(restaurant_id, rows)
            ok, why = judge(restaurant_id, req, sid, name, rows=rows, idx=idx, c=c, db_path=db_path)
            out.append({"name": name, "home_restaurant_id": sid, "home": label, "ok": ok, "why": why})
    out.sort(key=lambda x: (not x["ok"], x["home"], x["name"].lower()))
    return out


def offer(restaurant_id, request_id, name, home_restaurant_id, actor=None, db_path=None) -> dict:
    """Offer the open shift to one visitor, judged first; told on their home
    site's channel. Returns {offer, via}."""
    import schedule_rules as _sr
    from time_utils import mdy
    if home_restaurant_id not in _sr.org_sibling_ids(restaurant_id, **({"db_path": db_path} if db_path else {})):
        raise CrossSiteError("that location isn't part of this group")
    if not travels(home_restaurant_id, name, db_path):
        raise CrossSiteError(f"{name} hasn't said they'll pick up shifts at other locations")
    req = _request(restaurant_id, request_id, db_path)
    if not req:
        raise CrossSiteError("that shift is not open")
    if not _holds_role(home_restaurant_id, name, req.get("role")):
        raise CrossSiteError(f"{name} doesn't work {req.get('role') or 'that role'} at their location")
    ok, why = judge(restaurant_id, req, home_restaurant_id, name, db_path=db_path)
    if not ok:
        raise CrossSiteError(f"{name} can't take it: {why}")
    who = (actor.get("username") or actor.get("email")) if isinstance(actor, dict) else (actor or "a manager")
    conn = get_conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM cross_site_offers WHERE request_id=? AND home_restaurant_id=? AND "
                        "employee_name=? AND status='offered'", (req["id"], home_restaurant_id, name)).fetchone():
            raise CrossSiteError(f"{name} has already been offered that shift")
        oid = conn.execute("INSERT INTO cross_site_offers (target_restaurant_id, request_id, home_restaurant_id, "
                           "employee_name, created_by) VALUES (?,?,?,?,?)",
                           (restaurant_id, req["id"], home_restaurant_id, name, str(who)[:120])).lastrowid
        conn.commit()
    finally:
        conn.close()
    place = getattr(_models_mod.get_restaurant(restaurant_id), "location_name", None) or \
        getattr(_models_mod.get_restaurant(restaurant_id), "name", None) or "another location"
    via = None
    try:
        import people
        via = people.tell(home_restaurant_id, name, f"A shift at {place}",
                          [f"{place} is short a {req.get('role') or 'shift'} on {mdy(req['date'])} "
                           f"{req['shift_start']}-{req.get('shift_end') or ''} — it's yours if you want it.",
                           "Answer in the Cavnar AI app (Requests)."], email_type="shift_request",
                          **({"db_path": db_path} if db_path else {}))
    except Exception as e:
        print(f"[cross_site] offer notice failed rid={restaurant_id}: {e!r}")
    return {"offer": {"id": oid, "name": name, "home_restaurant_id": home_restaurant_id}, "via": via or "app"}


def offers_for(home_restaurant_id, name, db_path=None) -> list:
    """The shifts at other locations offered to this person, still open."""
    from time_utils import mdy
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT o.id, o.target_restaurant_id, r.date, r.shift_start, r.shift_end, r.role, "
            "COALESCE(t.location_name, t.name) AS place FROM cross_site_offers o JOIN shift_change_requests r "
            "ON r.id=o.request_id JOIN restaurants t ON t.id=o.target_restaurant_id WHERE o.home_restaurant_id=? "
            "AND o.employee_name=? AND o.status='offered' AND r.status='open' ORDER BY r.date, r.shift_start",
            (home_restaurant_id, name)).fetchall()
    finally:
        conn.close()
    return [dict(dict(r), date_label=mdy(r["date"])) for r in rows]


def respond(home_restaurant_id, name, offer_id, accept, db_path=None) -> dict:
    """The visitor's answer. Yes: judged again on the week as it is now and
    written into the target site's live week with the request covered, in
    one transaction; no: recorded and the target's managers told."""
    import shift_requests as _srq
    from schedule_engine import _rows_to_csv_text
    from schedule_versions import write_on
    conn = get_conn(db_path)
    try:
        o = conn.execute("SELECT * FROM cross_site_offers WHERE id=? AND home_restaurant_id=? AND employee_name=? "
                         "AND status='offered'", (int(offer_id), home_restaurant_id, name)).fetchone()
    finally:
        conn.close()
    if not o:
        raise CrossSiteError("that offer isn't open any more")
    o = dict(o)
    target = o["target_restaurant_id"]
    req = _request(target, o["request_id"], db_path)
    if not req:
        _set(o["id"], "withdrawn", db_path)
        raise CrossSiteError("somebody already took that shift")
    if not accept:
        _set(o["id"], "declined", db_path)
        _srq._tell_managers(target, "Offer declined", f"{name} (from another location) said no to "
                                                      f"{_srq._when(req)}.", db_path or _models_mod.DB_PATH)
        return {"status": "declined"}
    for _attempt in range(_srq.WEEK_CAS_ATTEMPTS):
        hist, rows, idx = _target_week(target, req, db_path)
        ok, why = judge(target, req, home_restaurant_id, name, rows=rows, idx=idx, db_path=db_path)
        if not ok:
            raise CrossSiteError(f"you can't take it now: {why}")
        conn = get_conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            fresh = _srq._live_hist(conn, target, req)
            if not _srq._same_week(fresh, hist):
                conn.rollback()
                continue
            cur = conn.execute("UPDATE shift_change_requests SET status='covered', replacement_name=?, "
                               "decided_at=COALESCE(decided_at, datetime('now')) WHERE id=? AND status='open'",
                               (name, req["id"]))
            if cur.rowcount != 1:
                raise CrossSiteError("somebody else just took that shift")
            new = [dict(r) for r in rows]
            new[idx] = dict(new[idx], employee=name,
                            notes=((new[idx].get("notes") or "").strip() + " (from another location)").strip())
            write_on(conn, target, hist["id"], "swap", _rows_to_csv_text(new), saved_by=name)
            _srq._close_offers(conn, req["id"])
            conn.execute("UPDATE cross_site_offers SET status=CASE WHEN id=? THEN 'accepted' ELSE 'withdrawn' END, "
                         "answered_at=datetime('now') WHERE request_id=? AND status='offered'", (o["id"], req["id"]))
            conn.commit()
            break
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        raise CrossSiteError("the schedule kept changing while that was checked — try again")
    _srq._tell_managers(target, "Shift covered from another location",
                        f"{name} (from another location) took {_srq._when(req)}.", db_path or _models_mod.DB_PATH)
    return {"status": "accepted", "request_id": req["id"], "target_restaurant_id": target}


def _set(offer_id, status, db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE cross_site_offers SET status=?, answered_at=datetime('now') WHERE id=?",
                     (status, int(offer_id)))
        conn.commit()
    finally:
        conn.close()
