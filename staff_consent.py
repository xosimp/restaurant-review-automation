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
# void: given for a claim that never went through (BUGSLIFE-7) — kept as
# the record of what was said, never in force.
STATUSES = ("pending", "agreed", "declined", "revoked", "withdrawn", "void")
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
        # The shift a consent is about (schedule re-audit 10/11/26
        # BUGSLIFE-7, contract #5): a rest waiver binds to the one shift it
        # was given for — never every shift on that date.
        if "shift_id" not in {r[1] for r in conn.execute("PRAGMA table_info(staff_consents)").fetchall()}:
            try:
                conn.execute("ALTER TABLE staff_consents ADD COLUMN shift_id TEXT")
            except Exception as e:
                if "duplicate column" not in str(e).lower():
                    raise
        conn.commit()
    finally:
        conn.close()
    void_unused_rest_waivers(db_path)


def void_unused_rest_waivers(db_path=None) -> int:
    """At boot: a per-shift rest waiver recorded for a claim that never went
    through is void (BUGSLIFE-7). Until 10/11/26 the consent was written
    BEFORE the claim ran, so a lost race or an overtime hold left an agreed
    waiver for a clopen the person never got — and, keyed by date, it made
    any short turnaround that day read as consented. A waiver whose request
    (`source_ref` request:<id>) is not covered BY that person is set `void`
    and stamped; the row stays (the record of what was said). Idempotent;
    returns rows voided."""
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT c.id, c.employee_key, r.status, r.replacement_name FROM staff_consents c "
                "LEFT JOIN shift_change_requests r ON r.restaurant_id=c.restaurant_id "
                "AND ('request:' || r.id)=c.source_ref WHERE c.kind='rest_waiver' AND c.status='agreed' "
                "AND c.source_ref LIKE 'request:%'").fetchall()
        except Exception as e:
            if "no such table" in str(e).lower():
                return 0
            raise
        n = 0
        for r in rows:
            if r["status"] == "covered" and name_key(r["replacement_name"]) == r["employee_key"]:
                continue
            n += conn.execute("UPDATE staff_consents SET status='void', revoked_at=? WHERE id=? AND status='agreed'",
                              (_now(), r["id"])).rowcount
        conn.commit()
        return n
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


def record_on(conn, restaurant_id, employee_name, kind, text, subject_date=None, subject=None, channel="app",
              source_ref=None, shift_id=None, person_id=None) -> int:
    """Consent given in the same act that uses it, written AGREED on the
    caller's connection inside its write transaction (schedule re-audit
    10/11/26 BUGSLIFE-7): a claim's rest waiver is recorded only in the
    transaction that writes the week, so a claim that is lost, held or
    refused leaves no consent behind. `shift_id` binds it to the one shift.
    Returns the row id."""
    if kind not in KINDS:
        raise ConsentError("unknown consent")
    if channel not in CHANNELS:
        channel = "app"
    name = " ".join(str(employee_name or "").split())
    text = " ".join(str(text or "").split())[:600]
    if not name or not text:
        raise ConsentError("a consent needs the person and the words")
    now = _now()
    cur = conn.execute(
        "INSERT INTO staff_consents (restaurant_id, person_id, employee_name, employee_key, kind, subject_date, "
        "subject_json, text, status, asked_by, asked_at, answered_at, channel, source_ref, shift_id) "
        "VALUES (?,?,?,?,?,?,?,?, 'agreed', ?, ?, ?, ?, ?, ?)",
        (restaurant_id, person_id, name, name_key(name), kind, subject_date,
         json.dumps(subject) if subject is not None else None, text, name[:120], now, now, channel,
         (str(source_ref) if source_ref is not None else None), (str(shift_id).lower() if shift_id else None)))
    return cur.lastrowid


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


def waiver_shift(subject) -> tuple:
    """(date, start minutes) of the one shift a rest waiver's subject binds
    it to, or None for a waiver written before consents named their shift."""
    sh = (subject or {}).get("shift") if isinstance(subject, dict) else None
    if not isinstance(sh, dict) or not sh.get("date"):
        return None
    from schedule_time import parse_minutes
    m = parse_minutes(sh.get("start") or sh.get("shift_start") or "")
    return (str(sh["date"])[:10], m) if m is not None else None


def rest_waivers(restaurant_id, week_dates=None, db_path=None) -> dict:
    """{employee_key: {"standing": bool, "dates": set(iso), "shifts": {iso:
    set(start minutes)}}} — agreed rest consents in force: every standing
    one not revoked, and the per-shift ones for `week_dates` (all when
    None). A waiver given for one shift (BUGSLIFE-7: every claim's since
    10/11/26) is in `shifts` — it allows the short rest before THAT shift
    only; `dates` holds the older date-wide ones. What
    Constraints.rest_waived reads."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT employee_key, kind, subject_date, subject_json FROM staff_consents "
                            "WHERE restaurant_id=? AND status='agreed' "
                            "AND kind IN ('rest_waiver','rest_waiver_standing')", (restaurant_id,)).fetchall()
    except Exception as e:
        if "no such table" in str(e).lower():
            return {}
        raise
    finally:
        conn.close()
    wanted = {str(d)[:10] for d in (week_dates or [])}
    out = {}
    for r in rows:
        w = out.setdefault(r["employee_key"], {"standing": False, "dates": set(), "shifts": {}})
        if r["kind"] == "rest_waiver_standing":
            w["standing"] = True
            continue
        try:
            bound = waiver_shift(json.loads(r["subject_json"] or "null"))
        except (TypeError, ValueError):
            bound = None
        if bound is not None:
            if not wanted or bound[0] in wanted:
                w["shifts"].setdefault(bound[0], set()).add(bound[1])
        elif r["subject_date"] and (not wanted or r["subject_date"] in wanted):
            w["dates"].add(r["subject_date"])
    return out


def waiver_allows(w, date_str, start=None) -> bool:
    """Whether one person's waivers (rest_waivers' value) allow a short rest
    before a shift on `date_str` starting at `start` (a clock string or
    minutes): a standing waiver, a date-wide one from before waivers named
    their shift, or one bound to exactly that shift."""
    if not w:
        return False
    if w.get("standing"):
        return True
    d = str(date_str or "")[:10]
    if d in (w.get("dates") or ()):
        return True
    if start is None:
        return False
    if not isinstance(start, int):
        from schedule_time import parse_minutes
        start = parse_minutes(start or "")
    shifts = (w.get("shifts") or {}).get(d) or ()
    return start is not None and start in set(shifts)


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
