"""
staff_consent.py — written, time-stamped consent from staff.

Predictive-scheduling laws turn on the employee's WRITTEN consent: a shorter
rest between shifts with the premium (Chicago, Seattle, Oregon), a change
to a posted shift that owes no predictability pay because the employee
agreed to it, hours added that the employee accepted. The product had none
of it: accepting a shift offer in the app was a time-stamped record nothing
treated as consent, and a clopen a willing closer wanted could only be
refused (schedule audit 10/10/26 MARKET-2, MARKET-3).

One table, `staff_consents`, one row per ask:

    kind     rest_waiver          a shorter rest before a shift on subject_date
             rest_waiver_standing every short turnaround until revoked
             shift_change         a change to a posted shift (subject_json: before/after)
             added_hours          hours added to their week (an accepted shift offer)
    status   pending -> agreed | declined | revoked | withdrawn
    text     the exact words the person agreed to — what the record proves
    asked_*, answered_at (UTC), channel (portal | app | offer), source_ref

A row is never edited after it is answered except to revoke a standing
waiver (revoked_at). Kept three years (ops retention, records rule).
"""
import json
from datetime import datetime

from models import DB_PATH
import models as _models

KINDS = ("rest_waiver", "rest_waiver_standing", "shift_change", "added_hours")
STATUSES = ("pending", "agreed", "declined", "revoked", "withdrawn")
CHANNELS = ("portal", "app", "offer", "manager")


class ConsentError(ValueError):
    pass


def get_conn(db_path=None):
    return _models.get_conn(db_path or DB_PATH)


def init_staff_consent(db_path=None):
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_consents (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
            person_id      INTEGER,
            employee_name  TEXT    NOT NULL,
            employee_key   TEXT    NOT NULL,
            kind           TEXT    NOT NULL,
            subject_date   TEXT,
            subject_json   TEXT,
            text           TEXT    NOT NULL,
            status         TEXT    NOT NULL DEFAULT 'pending',
            asked_by       TEXT,
            asked_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            answered_at    TEXT,
            revoked_at     TEXT,
            channel        TEXT,
            source_ref     TEXT,
            created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_consents_person ON staff_consents"
                     "(restaurant_id, employee_key, kind, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_consents_created ON staff_consents(created_at)")
        conn.commit()
    finally:
        conn.close()


def name_key(name) -> str:
    return " ".join(str(name or "").lower().split())


def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _person_id(restaurant_id, name, db_path=None):
    try:
        import people
        return people.person_id_for(restaurant_id, name, db_path=db_path, create=False)
    except Exception:
        return None


def _row(r) -> dict:
    d = dict(r)
    try:
        d["subject"] = json.loads(d.pop("subject_json") or "null")
    except (TypeError, ValueError):
        d["subject"] = None
    return d


def ask(restaurant_id, employee_name, kind, text, subject_date=None, subject=None, asked_by=None,
        source_ref=None, db_path=None) -> dict:
    """A consent asked of one person (pending until they answer in the app
    or portal). A second identical pending ask returns the first."""
    if kind not in KINDS:
        raise ConsentError("unknown consent")
    name = " ".join(str(employee_name or "").split())
    if not name:
        raise ConsentError("who is being asked?")
    text = " ".join(str(text or "").split())[:600]
    if not text:
        raise ConsentError("say what they are agreeing to")
    key = name_key(name)
    conn = get_conn(db_path)
    try:
        dup = conn.execute("SELECT * FROM staff_consents WHERE restaurant_id=? AND employee_key=? AND kind=? "
                           "AND COALESCE(subject_date,'')=? AND status='pending' AND text=?",
                           (restaurant_id, key, kind, subject_date or "", text)).fetchone()
        if dup:
            return _row(dup)
        cur = conn.execute(
            "INSERT INTO staff_consents (restaurant_id, person_id, employee_name, employee_key, kind, subject_date, "
            "subject_json, text, status, asked_by, asked_at, source_ref) VALUES (?,?,?,?,?,?,?,?, 'pending', ?, ?, ?)",
            (restaurant_id, _person_id(restaurant_id, name, db_path), name, key, kind, subject_date,
             json.dumps(subject) if subject is not None else None, text, (asked_by or "")[:120] or None, _now(),
             (str(source_ref) if source_ref is not None else None)))
        conn.commit()
        return _row(conn.execute("SELECT * FROM staff_consents WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        conn.close()


def record(restaurant_id, employee_name, kind, text, subject_date=None, subject=None, channel="app",
           source_ref=None, db_path=None) -> dict:
    """Consent the person gave in one step (a claim of a clopen that asks in
    the same tap, a standing rest waiver, an accepted offer): written as
    agreed, stamped now."""
    row = ask(restaurant_id, employee_name, kind, text, subject_date=subject_date, subject=subject,
              asked_by=employee_name, source_ref=source_ref, db_path=db_path)
    return answer(restaurant_id, row["id"], employee_name, True, channel=channel, db_path=db_path)


def answer(restaurant_id, consent_id, employee_name, agree: bool, channel="app", db_path=None) -> dict:
    """The person's own answer: only theirs, only once, only while pending."""
    if channel not in CHANNELS:
        channel = "app"
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM staff_consents WHERE id=? AND restaurant_id=?",
                           (int(consent_id), restaurant_id)).fetchone()
        if not row or row["employee_key"] != name_key(employee_name):
            raise ConsentError("that isn't yours to answer")
        if row["status"] != "pending":
            raise ConsentError("that was already answered")
        cur = conn.execute("UPDATE staff_consents SET status=?, answered_at=?, channel=? WHERE id=? AND status='pending'",
                           ("agreed" if agree else "declined", _now(), channel, row["id"]))
        if cur.rowcount != 1:
            raise ConsentError("that was already answered")
        conn.commit()
        return _row(conn.execute("SELECT * FROM staff_consents WHERE id=?", (row["id"],)).fetchone())
    finally:
        conn.close()


def revoke(restaurant_id, consent_id, employee_name, db_path=None) -> dict:
    """A standing waiver ends when the person says so (stamped)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM staff_consents WHERE id=? AND restaurant_id=?",
                           (int(consent_id), restaurant_id)).fetchone()
        if not row or row["employee_key"] != name_key(employee_name):
            raise ConsentError("that isn't yours")
        if row["kind"] != "rest_waiver_standing" or row["status"] != "agreed":
            raise ConsentError("only a standing agreement in force can be withdrawn")
        conn.execute("UPDATE staff_consents SET status='revoked', revoked_at=? WHERE id=?", (_now(), row["id"]))
        conn.commit()
        return _row(conn.execute("SELECT * FROM staff_consents WHERE id=?", (row["id"],)).fetchone())
    finally:
        conn.close()


def withdraw_ask(restaurant_id, consent_id, db_path=None) -> bool:
    """A manager takes back an ask nobody answered yet."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE staff_consents SET status='withdrawn', answered_at=? WHERE id=? AND "
                           "restaurant_id=? AND status='pending'", (_now(), int(consent_id), restaurant_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def for_person(restaurant_id, employee_name, db_path=None, limit=50) -> dict:
    """{"pending": [...], "recent": [...], "standing_rest": row|None} for
    the person's own screen."""
    key = name_key(employee_name)
    conn = get_conn(db_path)
    try:
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM staff_consents WHERE restaurant_id=? AND employee_key=? ORDER BY id DESC LIMIT ?",
            (restaurant_id, key, int(limit))).fetchall()]
    finally:
        conn.close()
    standing = next((r for r in rows if r["kind"] == "rest_waiver_standing" and r["status"] == "agreed"), None)
    return {"pending": [r for r in rows if r["status"] == "pending"],
            "recent": [r for r in rows if r["status"] != "pending"][:20], "standing_rest": standing}


def for_restaurant(restaurant_id, db_path=None, since=None, limit=500) -> list:
    conn = get_conn(db_path)
    try:
        q = "SELECT * FROM staff_consents WHERE restaurant_id=?"
        args = [restaurant_id]
        if since:
            q += " AND created_at>=?"
            args.append(str(since))
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [_row(r) for r in conn.execute(q, args).fetchall()]
    finally:
        conn.close()


def rest_waivers(restaurant_id, week_dates=None, db_path=None) -> dict:
    """{employee_key: {"standing": bool, "dates": set(iso)}} — agreed rest
    consents in force: every standing one not revoked, and the per-shift
    ones for `week_dates` (all when None). What Constraints.rest_waived
    reads."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT employee_key, kind, subject_date FROM staff_consents WHERE restaurant_id=? "
                            "AND status='agreed' AND kind IN ('rest_waiver','rest_waiver_standing')",
                            (restaurant_id,)).fetchall()
    except Exception as e:
        if "no such table" in str(e).lower():
            return {}
        raise
    finally:
        conn.close()
    wanted = {str(d)[:10] for d in (week_dates or [])}
    out = {}
    for r in rows:
        w = out.setdefault(r["employee_key"], {"standing": False, "dates": set()})
        if r["kind"] == "rest_waiver_standing":
            w["standing"] = True
        elif r["subject_date"] and (not wanted or r["subject_date"] in wanted):
            w["dates"].add(r["subject_date"])
    return out


def consented_change(restaurant_id, employee_name, subject_date, after=None, before=None, db_path=None):
    """The agreed consent that covers a change to this person's shift on
    `subject_date` (a shift_change whose 'after' matches, or an accepted
    offer of hours that day), answered at or before `before` when given —
    or None."""
    key = name_key(employee_name)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_consents WHERE restaurant_id=? AND employee_key=? AND "
                            "subject_date=? AND status='agreed' AND kind IN ('shift_change','added_hours') "
                            "ORDER BY answered_at", (restaurant_id, key, str(subject_date)[:10])).fetchall()
    except Exception as e:
        if "no such table" in str(e).lower():
            return None
        raise
    finally:
        conn.close()
    for r in rows:
        d = _row(r)
        if before and d.get("answered_at") and d["answered_at"] > str(before):
            continue
        if after is not None and d["kind"] == "shift_change":
            want = (d.get("subject") or {}).get("after")
            if want is not None and _norm_shift(want) != _norm_shift(after):
                continue
        return d
    return None


def _norm_shift(s):
    if not s:
        return None
    if isinstance(s, dict):
        return ((s.get("shift_start") or s.get("start") or "").strip().lower(),
                (s.get("shift_end") or s.get("end") or "").strip().lower())
    return str(s).strip().lower()
