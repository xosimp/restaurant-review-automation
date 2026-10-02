"""
staff_comms.py — the staff ↔ manager channel (employee audit 10/1/26, fix B5):
"running late", announcements with acknowledgement, and one message thread
per employee with the restaurant's managers.

Why it exists. Every reason staff texted a manager's personal phone had no
home in the product (audit 4, COM-04/05/07; the "stop texting managers"
table): a late arrival became a high "X hasn't clocked in" issue texted to
the manager 15 minutes after start; "closing early tonight" went by group
text; "can I come in at 5?" went by personal text. This module is the one
store and the one delivery path for all three.

  RUNNING LATE (staff_running_late) — one row per employee per shift, an
      update replaces the ETA. It writes attendance outcome "late" from the
      `self_report` source (the weakest: any clock-in reading overwrites it,
      attendance.SOURCE_RANK), tells the decider set ONCE, and holds the
      coverage check's "hasn't clocked in" issue until shift start + ETA +
      LATE_HOLD_GRACE_MINUTES (late_hold, read by
      strategy_jobs.run_coverage_check).

  ANNOUNCEMENTS (staff_announcements + staff_announcement_recipients) — a
      manager's post to all staff, one job role, or everyone on one date's
      published schedule; normal or urgent; optional last day. The audience
      is fixed when it is sent (one recipient row per staff login), so
      "9 of 14 read" never moves because someone joined later. Each
      recipient is told through people.tell; "Got it" stamps acked_at.

  MESSAGES (staff_threads + staff_thread_messages) — one thread per staff
      login with the restaurant's DECIDERS (the console logins holding
      SCHEDULE_DRAFT — the same set a shift request reaches), optionally
      tied to a shift date or a request. Not team_messages: that table is
      1:1 between two console logins on users.restaurant_id, with one
      read_at for one recipient. A staff thread has a SET of managers on
      one side (any of them reading it reads it for the team) and context
      columns team_messages has no place for. No staff ↔ staff messages,
      deliberately (audit NOBUILD-4: moderation and harassment load).

Delivery. Managers are told through strategy_jobs._reach(deciders=True,
permissions=[SCHEDULE_DRAFT]) as alert type "shift_request" — P2, the Labor
module, never held by the briefing budget, never collapsed with another
(push.py) — with a `nav` naming the Team inbox ("labor/inbox"). Staff are
told through people.tell; `_tell_staff` passes `data` (announcement_id /
thread_id) and `priority` only once people.tell accepts them (the notice
pipeline is being rebuilt underneath, fix B2), so this module never breaks
on either side of that change.

Every row carries restaurant_id, so models.delete_restaurant takes it with
the restaurant. Identity is the membership (staff login), never a typed
name: a rename does not orphan a thread or an acknowledgement.
"""
import json
import logging
from datetime import date, datetime, timedelta

import models as _models

log = logging.getLogger(__name__)

# ── the vocabulary ─────────────────────────────────────────────────────────
ETA_CHOICES = (10, 20, 30, 45)        # the app's chips; any whole minute up to ETA_MAX is accepted
ETA_MAX = 120
# After start + ETA, how long the coverage check still waits before a
# missing clock-in becomes the manager's issue. The same grace the check
# gives everyone at the start (intraday.COVERAGE_GRACE_MINUTES).
LATE_HOLD_GRACE_MINUTES = 15
NOTE_MAX = 200
PRIORITIES = ("normal", "urgent")
AUDIENCES = ("all", "role", "shift_date")
TITLE_MAX = 120
BODY_MAX = 2000
MESSAGE_MAX = 1000
# A second staff message inside this window, while the first is still
# unread, is not pushed again: one buzz per burst, not one per line.
MESSAGE_PUSH_QUIET_MINUTES = 10
# The manager-side notice type. "shift_request" is the staff-request type
# push/notify already treat right: P2, Labor, never held by the briefing
# budget, uncollapsible. Its nav is overridden per notice (data["nav"]).
MANAGER_NOTICE_TYPE = "employee_message"
INBOX_NAV = "labor/inbox"
# Rate limits (per staff login): a stuck button or a script must not page
# every manager on the team.
MESSAGES_PER_HOUR = 30
LATE_REPORTS_PER_HOUR = 10


class CommsError(ValueError):
    """A request this module refuses, in a sentence the person can act on."""


class NotFound(LookupError):
    """The thing named is not this person's (or this restaurant's)."""


def get_conn(db_path=None):
    """models.get_conn at call time (CLAUDE.md, bound imports)."""
    return _models.get_conn(db_path) if db_path else _models.get_conn()


# ── boot DDL ───────────────────────────────────────────────────────────────

def init_staff_comms(db_path=None):
    """Boot DDL (models.init_db), never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_running_late (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id   INTEGER NOT NULL REFERENCES restaurants(id),
            membership_id   INTEGER NOT NULL,
            user_id         INTEGER,
            employee_name   TEXT    NOT NULL,
            employee_key    TEXT    NOT NULL,
            business_date   TEXT    NOT NULL,
            shift_start     TEXT    NOT NULL,
            role            TEXT,
            eta_minutes     INTEGER NOT NULL,
            note            TEXT,
            reported_at     TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            told_at         TEXT,
            told_count      INTEGER NOT NULL DEFAULT 0,
            UNIQUE(restaurant_id, membership_id, business_date, shift_start)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_running_late_day ON staff_running_late"
                     "(restaurant_id, business_date, employee_key)")
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_announcements (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            title            TEXT    NOT NULL,
            body             TEXT    NOT NULL DEFAULT '',
            priority         TEXT    NOT NULL DEFAULT 'normal',
            audience         TEXT    NOT NULL DEFAULT 'all',
            audience_value   TEXT,
            expires_on       TEXT,
            created_by       INTEGER,
            created_by_name  TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            withdrawn_at     TEXT,
            withdrawn_by     INTEGER
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_ann_rest ON staff_announcements"
                     "(restaurant_id, created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_announcement_recipients (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            announcement_id  INTEGER NOT NULL REFERENCES staff_announcements(id),
            membership_id    INTEGER NOT NULL,
            employee_name    TEXT    NOT NULL,
            delivered_via    TEXT,
            delivered_at     TEXT,
            acked_at         TEXT,
            created_at       TEXT    DEFAULT (datetime('now')),
            UNIQUE(announcement_id, membership_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_ann_rcpt_member ON staff_announcement_recipients"
                     "(restaurant_id, membership_id, acked_at)")
        # Its retention stamp (ops._RETENTION_DAYS): a database made before
        # it had the column gets it here, filled from the announcement.
        if "created_at" not in {r[1] for r in conn.execute("PRAGMA table_info(staff_announcement_recipients)")}:
            conn.execute("ALTER TABLE staff_announcement_recipients ADD COLUMN created_at TEXT")
            conn.execute("UPDATE staff_announcement_recipients SET created_at=(SELECT a.created_at FROM "
                         "staff_announcements a WHERE a.id=announcement_id) WHERE created_at IS NULL")
        # The retention deletes' indexes (ops._RETENTION_COLUMN).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_ann_rcpt_created ON staff_announcement_recipients(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_ann_created ON staff_announcements(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_running_late_date ON staff_running_late(business_date)")
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_threads (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            membership_id    INTEGER NOT NULL,
            employee_name    TEXT    NOT NULL,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            last_at          TEXT,
            UNIQUE(restaurant_id, membership_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS staff_thread_messages (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL REFERENCES restaurants(id),
            thread_id        INTEGER NOT NULL REFERENCES staff_threads(id),
            sender_kind      TEXT    NOT NULL,            -- staff | manager
            sender_user_id   INTEGER,
            sender_name      TEXT,
            body             TEXT    NOT NULL,
            shift_date       TEXT,
            request_id       INTEGER,
            request_kind     TEXT,                        -- shift | time_off
            created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
            read_at          TEXT,
            read_by          INTEGER
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_msg_thread ON staff_thread_messages"
                     "(thread_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_msg_unread ON staff_thread_messages"
                     "(restaurant_id, sender_kind, read_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_staff_msg_created ON staff_thread_messages(created_at)")
        conn.commit()
    finally:
        conn.close()


# ── small helpers ──────────────────────────────────────────────────────────

def _local_now(restaurant_id):
    """The restaurant's wall clock, naive. One seam, so a test pins it."""
    from time_utils import restaurant_now_by_id
    return restaurant_now_by_id(restaurant_id, naive=True)


def _stamp():
    from time_utils import utc_stamp
    return utc_stamp()


def iso(stamp):
    """A stored UTC stamp as ISO 8601 with a Z ("2026-10-01T22:05:00Z") — the
    one form the API hands out, so neither client guesses the zone."""
    if not stamp:
        return None
    from time_utils import parse_stamp
    dt = parse_stamp(stamp)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _nk(name):
    import staff_settings
    return staff_settings.name_key(name)


def _canon(restaurant_id, names, db_path=None) -> dict:
    """{name: the live person's display name} (people.canonical_names), or
    the names themselves when that read fails."""
    try:
        import people
        return people.canonical_names(restaurant_id, list(names), db_path=db_path)
    except Exception:
        return {n: n for n in names}


def _minutes(value):
    import schedule_requirements
    return schedule_requirements._minutes(value)


def _clock(dt) -> str:
    """6:45pm / 6pm — DESIGN_SYSTEM.md's time form."""
    h, m = dt.hour, dt.minute
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''}{'am' if h < 12 else 'pm'}"


def _clean(text, limit):
    return " ".join(str(text or "").split())[:limit]


def _clean_body(text, limit):
    """Message text: keeps line breaks, trims each line, bounded."""
    lines = [" ".join(ln.split()) for ln in str(text or "").replace("\r", "").split("\n")]
    out = "\n".join(lines).strip()
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out[:limit]


def _restaurant(restaurant_id, db_path=None):
    from models import get_restaurant
    return get_restaurant(restaurant_id, db_path) if db_path else get_restaurant(restaurant_id)


def _place(restaurant_id, db_path=None) -> str:
    try:
        r = _restaurant(restaurant_id, db_path)
        return (getattr(r, "location_name", None) or getattr(r, "name", None) or "your restaurant") if r else \
            "your restaurant"
    except Exception:
        return "your restaurant"


def _limited(bucket, key, max_calls, window_secs=3600) -> bool:
    """True when this staff login is over its budget for `bucket`. Fails open."""
    try:
        from ai_utils import ai_rate_limited
        return ai_rate_limited(f"{bucket}:{key}", max_calls=max_calls, window_secs=window_secs)
    except Exception:
        return False


def login_name(user) -> str:
    """The person's name for a console login (owner, 9/30/26: "jheflin" is
    Jim Heflin): the membership's employee name, else the username."""
    if not user:
        return ""
    uid, rid = user.get("id"), user.get("restaurant_id")
    try:
        conn = get_conn()
        try:
            row = conn.execute("SELECT employee_name FROM memberships WHERE user_id=? AND restaurant_id=?",
                               (uid, rid)).fetchone()
        finally:
            conn.close()
        if row and (row["employee_name"] or "").strip():
            return " ".join(row["employee_name"].split())
    except Exception:
        pass
    return str(user.get("username") or "Your manager")


# ── telling people ─────────────────────────────────────────────────────────

def _tell_deciders(restaurant_id, title, body, data=None, db_path=None) -> int:
    """The managers who decide staff requests (SCHEDULE_DRAFT), whether or
    not their morning brief is on — the shift-request audience
    (shift_requests._tell_managers). Returns how many were reached."""
    try:
        import strategy_jobs
        from permissions import SCHEDULE_DRAFT
        payload = dict({"tab": "labor", "nav": INBOX_NAV}, **(data or {}))
        return strategy_jobs._reach(restaurant_id, MANAGER_NOTICE_TYPE, title, body, payload,
                                    db_path or _models.DB_PATH, lines=[body],
                                    permissions=[SCHEDULE_DRAFT], deciders=True) or 0
    except Exception as e:
        log.warning("staff_comms: manager notice failed rid=%s: %r", restaurant_id, e)
        return 0


def _tell_staff(restaurant_id, name, title, lines, *, email_type, data=None, priority=None,
                channel=None, db_path=None):
    """people.tell, the one staff delivery path. `data` (what the notice is
    about — announcement_id, thread_id, kind, nav) and `priority` ("urgent"
    → P1) are passed only when people.tell takes them: the pipeline under it
    is being rebuilt (fix B2), and an unknown keyword must not stop a
    notice. Returns the channel ("push" | "sms" | "email") or None."""
    import inspect
    import people
    kw = {"email_type": email_type, "db_path": db_path}
    if channel is not None:
        kw["channel"] = channel
    try:
        params = inspect.signature(people.tell).parameters
    except (TypeError, ValueError):
        params = {}
    if data and "data" in params:
        kw["data"] = data
    if priority and "priority" in params:
        kw["priority"] = priority
    try:
        return people.tell(restaurant_id, name, title, lines, **kw)
    except Exception as e:
        log.warning("staff_comms: staff notice failed rid=%s: %r", restaurant_id, e)
        return None


# ═══ Running late (COM-05) ═════════════════════════════════════════════════

def _business_day(restaurant, local) -> date:
    from time_utils import business_date
    try:
        return business_date(restaurant, local)
    except Exception:
        return local.date()


def _due(day, shift_start):
    """The datetime a shift listed for `day` starts — a start before the
    business day's first hour is after midnight (intraday.coverage_gaps)."""
    from time_utils import BUSINESS_DAY_START_HOUR
    mins = _minutes(shift_start)
    if mins is None:
        return None
    due = datetime.combine(day, datetime.min.time()) + timedelta(minutes=mins)
    if mins // 60 < BUSINESS_DAY_START_HOUR:
        due += timedelta(days=1)
    return due


def _shift_end(day, row, due):
    end_m = _minutes(row.get("shift_end"))
    if end_m is None or due is None:
        return due + timedelta(hours=8) if due else None
    end = datetime.combine(due.date(), datetime.min.time()) + timedelta(minutes=end_m)
    if end <= due:
        end += timedelta(days=1)
    return end


def _my_rows(restaurant_id, name, day, db_path=None) -> list:
    """This person's rows on `day` from the published week, each name read
    as the person it means now."""
    import intraday
    try:
        rows = intraday.published_rows(restaurant_id, day, db_path or _models.DB_PATH)
    except Exception:
        return []
    canon = _canon(restaurant_id, [r["employee"] for r in rows] + [name], db_path)
    me = _nk(canon.get(name, name))
    return [r for r in rows if _nk(canon.get(r["employee"], r["employee"])) == me or _nk(r["employee"]) == _nk(name)]


def _late_row(r) -> dict:
    d = dict(r)
    return {"id": d["id"], "date": d["business_date"], "shift_start": d["shift_start"], "role": d.get("role"),
            "eta_minutes": d["eta_minutes"], "note": d.get("note"), "reported_at": iso(d["reported_at"]),
            "updated_at": iso(d["updated_at"]), "managers_told": bool(d.get("told_at"))}


def report_late(restaurant_id, membership, shift_date, shift_start, eta_minutes, note=None,
                now_local=None, db_path=None) -> dict:
    """The employee's own "running late" for their own shift today. Returns
    {"report", "created", "managers_told"}. One row per shift: a second
    report replaces the ETA and the note, and does not tell anyone again."""
    name = " ".join(str((membership or {}).get("employee_name") or "").split())
    mid = (membership or {}).get("id")
    if not name or not mid:
        raise CommsError("No employee name on this session.")
    try:
        eta = int(eta_minutes)
    except (TypeError, ValueError):
        raise CommsError("Say how late — 10, 20, 30 or 45 minutes, or up to 120.")
    if eta < 1 or eta > ETA_MAX:
        raise CommsError(f"An ETA is between 1 and {ETA_MAX} minutes. Longer than that, ask to drop the shift.")
    note = _clean(note, NOTE_MAX) or None
    restaurant = _restaurant(restaurant_id, db_path)
    local = now_local or _local_now(restaurant_id)
    today = _business_day(restaurant, local)
    try:
        day = date.fromisoformat(str(shift_date or "")[:10]) if shift_date else today
    except ValueError:
        raise CommsError("That date isn't readable.")
    if day not in (today, local.date()):
        raise CommsError("Running late is for today's shift only.")
    want = _minutes(shift_start)
    rows = _my_rows(restaurant_id, name, day, db_path)
    if want is not None:
        rows = [r for r in rows if _minutes(r.get("shift_start")) == want]
    elif len(rows) > 1:
        raise CommsError("You have more than one shift today — say which one.")
    if not rows:
        raise CommsError("That shift isn't on your published schedule today.")
    row = rows[0]
    due = _due(day, row["shift_start"])
    end = _shift_end(day, row, due)
    if end is not None and local >= end:
        raise CommsError("That shift is already over.")
    key = _nk(_canon(restaurant_id, [name], db_path).get(name, name))
    now = _stamp()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("SELECT * FROM staff_running_late WHERE restaurant_id=? AND membership_id=? AND "
                           "business_date=? AND shift_start=?",
                           (restaurant_id, mid, day.isoformat(), row["shift_start"])).fetchone()
        created = cur is None
        if created:
            conn.execute("INSERT INTO staff_running_late (restaurant_id, membership_id, user_id, employee_name, "
                         "employee_key, business_date, shift_start, role, eta_minutes, note, reported_at, "
                         "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (restaurant_id, mid, membership.get("user_id"), name, key, day.isoformat(),
                          row["shift_start"], row.get("role") or None, eta, note, now, now))
        else:
            conn.execute("UPDATE staff_running_late SET eta_minutes=?, note=?, updated_at=? WHERE id=?",
                         (eta, note, now, cur["id"]))
        conn.commit()
        rec = conn.execute("SELECT * FROM staff_running_late WHERE restaurant_id=? AND membership_id=? AND "
                           "business_date=? AND shift_start=?",
                           (restaurant_id, mid, day.isoformat(), row["shift_start"])).fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    # The attendance record: late, from the employee's own word — weaker
    # than any clock-in reading, which overwrites it (attendance.SOURCE_RANK).
    try:
        import attendance
        attendance.record(restaurant_id, name, day.isoformat(), "late", "self_report",
                          shift_start=row["shift_start"], minutes_late=eta, role=row.get("role") or None,
                          note=("said they were running late" + (f": {note}" if note else ""))[:300],
                          db_path=db_path)
    except Exception as e:
        log.warning("staff_comms: attendance self-report failed rid=%s: %r", restaurant_id, e)
    # A "hasn't clocked in" issue already open for this shift says what they
    # told us now (the coverage check names a report only when it opens one).
    try:
        import issues
        hold = late_hold(restaurant_id, name, row["shift_start"], local, restaurant=restaurant, db_path=db_path)
        if hold:
            kw = {"db_path": db_path} if db_path else {}
            issues.note_running_late(restaurant_id, {day.isoformat(), local.date().isoformat()},
                                     {key, _nk(name)}, hold_sentence(hold), **kw)
    except Exception as e:
        log.warning("staff_comms: coverage issue note failed rid=%s: %r", restaurant_id, e)
    told = bool(rec["told_at"])
    if not told:
        arrive = (due + timedelta(minutes=eta)) if due else None
        role = f" ({row.get('role')})" if row.get("role") else ""
        body = (f"{name} is running about {eta} minutes late for {row['shift_start']}{role}"
                + (f" — expect them around {_clock(arrive)}" if arrive else "") + "."
                + (f" “{note}”" if note else ""))
        reached = _tell_deciders(restaurant_id, f"{name} is running late", body,
                                 {"kind": "running_late", "late_id": rec["id"]}, db_path)
        conn = get_conn(db_path)
        try:
            # Stamped whether or not anyone could be reached: "once" is the
            # promise, and a manager with no phone or email is not paged on
            # the update instead.
            conn.execute("UPDATE staff_running_late SET told_at=?, told_count=? WHERE id=?",
                         (_stamp(), int(reached or 0), rec["id"]))
            conn.commit()
            rec = conn.execute("SELECT * FROM staff_running_late WHERE id=?", (rec["id"],)).fetchone()
        finally:
            conn.close()
        told = bool(reached)
    try:
        from models import log_event
        log_event(restaurant_id, "staff_running_late", {"employee": name, "date": day.isoformat(),
                                                        "start": row["shift_start"], "eta": eta,
                                                        "update": not created})
    except Exception:
        pass
    return {"report": _late_row(rec), "created": created, "managers_told": told}


def my_late_reports(restaurant_id, membership_id, now_local=None, db_path=None) -> list:
    """This login's running-late reports for today (the business day)."""
    restaurant = _restaurant(restaurant_id, db_path)
    local = now_local or _local_now(restaurant_id)
    days = {_business_day(restaurant, local).isoformat(), local.date().isoformat()}
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_running_late WHERE restaurant_id=? AND membership_id=? AND "
                            f"business_date IN ({','.join('?' * len(days))}) ORDER BY shift_start",
                            (restaurant_id, membership_id, *sorted(days))).fetchall()
    finally:
        conn.close()
    return [_late_row(r) for r in rows]


def late_today(restaurant_id, now_local=None, db_path=None) -> list:
    """The manager's view: everyone who said they're running late today,
    with when to expect them."""
    restaurant = _restaurant(restaurant_id, db_path)
    local = now_local or _local_now(restaurant_id)
    day = _business_day(restaurant, local)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_running_late WHERE restaurant_id=? AND business_date=? "
                            "ORDER BY shift_start, employee_name", (restaurant_id, day.isoformat())).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = _late_row(r)
        due = _due(day, r["shift_start"])
        d["employee_name"] = r["employee_name"]
        d["expected_at"] = _clock(due + timedelta(minutes=int(r["eta_minutes"]))) if due else None
        out.append(d)
    return out


def late_hold(restaurant_id, employee, shift_start, now_local, restaurant=None, db_path=None):
    """The running-late report behind a missing clock-in, or None.

    {"eta_minutes", "until" (naive local datetime), "holding" (now < until),
    "reported_at" (local datetime)} — strategy_jobs.run_coverage_check opens
    no "hasn't clocked in" issue while `holding`, and names the report in the
    issue once it does. Matched by the person (name key, canonical) and the
    shift start; a report with no matching start is not used."""
    restaurant = restaurant if restaurant is not None else _restaurant(restaurant_id, db_path)
    day = _business_day(restaurant, now_local)
    key = _nk(_canon(restaurant_id, [employee], db_path).get(employee, employee))
    want = _minutes(shift_start)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_running_late WHERE restaurant_id=? AND business_date=? AND "
                            "employee_key IN (?,?)", (restaurant_id, day.isoformat(), key, _nk(employee))).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    for r in rows:
        if want is not None and _minutes(r["shift_start"]) != want:
            continue
        due = _due(day, r["shift_start"])
        if due is None:
            continue
        until = due + timedelta(minutes=int(r["eta_minutes"]) + LATE_HOLD_GRACE_MINUTES)
        reported = None
        try:
            from time_utils import parse_stamp, restaurant_tz
            reported = parse_stamp(r["updated_at"]).astimezone(restaurant_tz(restaurant)).replace(tzinfo=None)
        except Exception:
            reported = None
        return {"eta_minutes": int(r["eta_minutes"]), "until": until, "holding": now_local < until,
                "reported_at": reported, "note": r["note"]}
    return None


def hold_sentence(hold) -> str:
    """The clause a coverage issue carries once the hold has run out."""
    if not hold:
        return ""
    when = f" at {_clock(hold['reported_at'])}" if hold.get("reported_at") else ""
    return f" They said{when} they'd be about {hold['eta_minutes']} minutes late."


# ═══ Announcements (COM-04a, COM-07) ════════════════════════════════════════

def _staff_members(restaurant_id, db_path=None) -> list:
    from auth import get_memberships_for_restaurant
    kw = {"db_path": db_path} if db_path else {}
    try:
        rows = get_memberships_for_restaurant(restaurant_id, role="employee", **kw)
    except Exception:
        return []
    return [m for m in rows if (m.get("employee_name") or "").strip()]


def _job_role(restaurant_id, member, roster=None) -> str:
    """memberships.job_role, else the owner roster's / schedule's job
    (staff_routes._employee_job_role's order, minus the request cache)."""
    if (member.get("job_role") or "").strip():
        return member["job_role"].strip()
    if roster is None:
        roster = _roster_roles(restaurant_id)
    return roster.get(_nk(member.get("employee_name"))) or ""


def _roster_roles(restaurant_id) -> dict:
    try:
        from staff_roster import roster_names_for_restaurant
        return {_nk(n): (j or "").strip() for n, j in roster_names_for_restaurant(restaurant_id) if j}
    except Exception:
        return {}


def roles(restaurant_id, db_path=None) -> list:
    """The job roles an announcement can be addressed to: those held by
    someone with the staff app."""
    roster = _roster_roles(restaurant_id)
    seen = {}
    for m in _staff_members(restaurant_id, db_path):
        r = _job_role(restaurant_id, m, roster)
        if r:
            seen.setdefault(r.casefold(), r)
    return sorted(seen.values(), key=str.casefold)


def audience_members(restaurant_id, audience, value=None, db_path=None) -> list:
    """The staff logins an announcement reaches: everyone, one job role, or
    everyone on one date's published schedule."""
    staff = _staff_members(restaurant_id, db_path)
    if audience == "all":
        return staff
    if audience == "role":
        want = _clean(value, 80).casefold()
        if not want:
            raise CommsError("Pick a role.")
        roster = _roster_roles(restaurant_id)
        return [m for m in staff if _job_role(restaurant_id, m, roster).casefold() == want]
    if audience == "shift_date":
        try:
            day = date.fromisoformat(str(value or "")[:10])
        except ValueError:
            raise CommsError("Pick the date whose schedule should get it.")
        import intraday
        try:
            rows = intraday.published_rows(restaurant_id, day, db_path or _models.DB_PATH)
        except Exception:
            rows = []
        names = [r["employee"] for r in rows]
        canon = _canon(restaurant_id, names + [m["employee_name"] for m in staff], db_path)
        on = {_nk(canon.get(n, n)) for n in names} | {_nk(n) for n in names}
        return [m for m in staff if _nk(canon.get(m["employee_name"], m["employee_name"])) in on
                or _nk(m["employee_name"]) in on]
    raise CommsError("Send it to everyone, one role, or one day's schedule.")


def _audience_label(audience, value) -> str:
    if audience == "role":
        return f"{value}s" if value and not str(value).endswith("s") else str(value or "")
    if audience == "shift_date":
        from time_utils import mdy
        return f"on the schedule {mdy(value)}"
    return "Everyone"


def create_announcement(restaurant_id, user, title, body="", priority="normal", audience="all",
                        audience_value=None, expires_on=None, now_local=None, db_path=None) -> dict:
    """A manager's post to staff. Returns {"announcement", "delivery":
    {"push", "sms", "email", "none"}}. The audience is fixed now; each
    recipient is told through people.tell, urgent at P1."""
    title = _clean(title, TITLE_MAX)
    body = _clean_body(body, BODY_MAX)
    if not title:
        raise CommsError("Give it a title — it is what the lock screen shows.")
    priority = (priority or "normal").strip().lower()
    if priority not in PRIORITIES:
        raise CommsError("Priority is normal or urgent.")
    audience = (audience or "all").strip().lower()
    if audience not in AUDIENCES:
        raise CommsError("Send it to everyone, one role, or one day's schedule.")
    value = None
    local = now_local or _local_now(restaurant_id)
    if audience == "role":
        value = _clean(audience_value, 80)
    elif audience == "shift_date":
        value = str(audience_value or "")[:10]
    members = audience_members(restaurant_id, audience, value, db_path)
    exp = None
    if expires_on:
        try:
            exp = date.fromisoformat(str(expires_on)[:10])
        except ValueError:
            raise CommsError("The last day isn't readable.")
        if exp < local.date():
            raise CommsError("The last day is already past.")
    elif audience == "shift_date":
        # A note for one night's crew is done once that night is.
        exp = date.fromisoformat(value)
    if not members:
        raise CommsError("Nobody with the staff app is in that group yet.")
    who = login_name(user)
    now = _stamp()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        aid = conn.execute("INSERT INTO staff_announcements (restaurant_id, title, body, priority, audience, "
                           "audience_value, expires_on, created_by, created_by_name, created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (restaurant_id, title, body, priority, audience, value,
                            exp.isoformat() if exp else None, (user or {}).get("id"), who, now)).lastrowid
        for m in members:
            conn.execute("INSERT OR IGNORE INTO staff_announcement_recipients (restaurant_id, announcement_id, "
                         "membership_id, employee_name, created_at) VALUES (?,?,?,?,?)",
                         (restaurant_id, aid, m["id"], " ".join(m["employee_name"].split()), now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    delivery = _deliver_announcement(restaurant_id, aid, title, body, priority, members, db_path)
    try:
        from models import log_event
        log_event(restaurant_id, "staff_announcement_sent",
                  {"id": aid, "title": title, "priority": priority, "audience": audience, "value": value,
                   "recipients": len(members), "by": who})
    except Exception:
        pass
    if (user or {}).get("is_admin"):
        try:
            import admin_events
            admin_events.record_admin_action(user, "staff_announcement.sent", restaurant_id=restaurant_id,
                                             target=("staff_announcement", aid),
                                             after={"title": title, "priority": priority, "audience": audience,
                                                    "recipients": len(members)},
                                             db_path=db_path)
        except Exception:
            pass
    return {"announcement": get_announcement(restaurant_id, aid, db_path=db_path), "delivery": delivery}


def _deliver_announcement(restaurant_id, aid, title, body, priority, members, db_path=None) -> dict:
    import people
    names = [" ".join(m["employee_name"].split()) for m in members]
    try:
        channels = people.reach(restaurant_id, names, db_path=db_path)
    except Exception as e:
        log.warning("staff_comms: reach failed rid=%s: %r", restaurant_id, e)
        channels = {}
    counts = {"push": 0, "sms": 0, "email": 0, "none": 0}
    for m, name in zip(members, names):
        # In the recipient's language when they set one (staff_knowledge,
        # V11): the manager wrote it, so it is approved text; the
        # translation is cached per language, so a crew of ten Spanish
        # readers costs one model call per text, and the inbox reads the
        # same cache. Untranslated (a failure, a refusal) it goes as written.
        t_title, t_body = _translated(m["id"], title, restaurant_id, db_path), \
            (_translated(m["id"], body, restaurant_id, db_path) if body else body)
        head = ("Urgent: " if priority == "urgent" else "") + t_title
        first = t_body.split("\n", 1)[0] if t_body else t_title
        lines = [first] + ([t_body] if t_body and t_body != first else [])
        via = _tell_staff(restaurant_id, name, head, lines, email_type="staff_announcement",
                          data={"kind": "announcement", "announcement_id": aid, "nav": "inbox"},
                          priority="urgent" if priority == "urgent" else None,
                          channel=channels.get(name) or {}, db_path=db_path)
        via = "sms" if via == "sms_held" else via   # held overnight, sent at 8am
        counts[via if via in counts else "none"] += 1
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE staff_announcement_recipients SET delivered_via=?, delivered_at=? "
                         "WHERE announcement_id=? AND membership_id=?",
                         (via or "none", _stamp() if via else None, aid, m["id"]))
            conn.commit()
        finally:
            conn.close()
    return counts


def _translated(membership_id, text, restaurant_id, db_path=None, cache_only=False) -> str:
    """staff_knowledge.translate_for — `text` in this login's language, or
    as written. Never raises."""
    try:
        import staff_knowledge
        kw = {"restaurant_id": restaurant_id, "cache_only": cache_only}
        if db_path:
            kw["db_path"] = db_path
        return staff_knowledge.translate_for(membership_id, text, **kw)
    except Exception as e:
        log.warning("staff_comms: translation skipped rid=%s: %r", restaurant_id, e)
        return text


def _announcement_out(a, rcpts, local_day=None) -> dict:
    a = dict(a)
    read = [r for r in rcpts if r["acked_at"]]
    unread = [r for r in rcpts if not r["acked_at"]]
    via = {"push": 0, "sms": 0, "email": 0, "none": 0}
    for r in rcpts:
        k = r["delivered_via"] or "none"
        k = "sms" if k == "sms_held" else k
        via[k if k in via else "none"] += 1
    expired = bool(a.get("expires_on") and local_day and a["expires_on"] < local_day.isoformat())
    return {"id": a["id"], "title": a["title"], "body": a["body"], "priority": a["priority"],
            "audience": a["audience"], "audience_value": a.get("audience_value"),
            "audience_label": _audience_label(a["audience"], a.get("audience_value")),
            "expires_on": a.get("expires_on"), "expired": expired, "withdrawn": bool(a.get("withdrawn_at")),
            "created_at": iso(a["created_at"]), "created_by_name": a.get("created_by_name") or "",
            "recipients": len(rcpts), "read": len(read),
            "read_line": f"{len(read)} of {len(rcpts)} read",
            "unread_names": sorted((r["employee_name"] for r in unread), key=str.casefold),
            "read_by": [{"name": r["employee_name"], "acked_at": iso(r["acked_at"])}
                        for r in sorted(read, key=lambda x: x["acked_at"])],
            "delivered": via}


def get_announcement(restaurant_id, aid, now_local=None, db_path=None):
    conn = get_conn(db_path)
    try:
        a = conn.execute("SELECT * FROM staff_announcements WHERE id=? AND restaurant_id=?",
                         (aid, restaurant_id)).fetchone()
        if not a:
            return None
        rcpts = [dict(r) for r in conn.execute("SELECT * FROM staff_announcement_recipients WHERE "
                                               "announcement_id=? AND restaurant_id=?", (aid, restaurant_id))]
    finally:
        conn.close()
    local = now_local or _local_now(restaurant_id)
    return _announcement_out(a, rcpts, local.date())


def list_announcements(restaurant_id, limit=30, now_local=None, db_path=None) -> list:
    """The manager's view, newest first: each with "N of M read" and who
    has not read it yet."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM staff_announcements WHERE restaurant_id=? "
                            "ORDER BY created_at DESC, id DESC LIMIT ?", (restaurant_id, int(limit))).fetchall()
        ids = [r["id"] for r in rows]
        rc = {}
        if ids:
            for r in conn.execute(f"SELECT * FROM staff_announcement_recipients WHERE restaurant_id=? AND "
                                  f"announcement_id IN ({','.join('?' * len(ids))})", (restaurant_id, *ids)):
                rc.setdefault(r["announcement_id"], []).append(dict(r))
    finally:
        conn.close()
    local = now_local or _local_now(restaurant_id)
    return [_announcement_out(a, rc.get(a["id"], []), local.date()) for a in rows]


def withdraw_announcement(restaurant_id, aid, user, db_path=None) -> dict:
    """Takes a post out of every inbox (a mistake, a cancelled meeting). The
    row and its read record stay."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE staff_announcements SET withdrawn_at=?, withdrawn_by=? WHERE id=? AND "
                           "restaurant_id=? AND withdrawn_at IS NULL",
                           (_stamp(), (user or {}).get("id"), aid, restaurant_id))
        conn.commit()
        exists = conn.execute("SELECT 1 FROM staff_announcements WHERE id=? AND restaurant_id=?",
                              (aid, restaurant_id)).fetchone()
    finally:
        conn.close()
    if not exists:
        raise NotFound("No such announcement.")
    if cur.rowcount:
        try:
            from models import log_event
            log_event(restaurant_id, "staff_announcement_withdrawn", {"id": aid, "by": login_name(user)})
        except Exception:
            pass
        if (user or {}).get("is_admin"):
            try:
                import admin_events
                admin_events.record_admin_action(user, "staff_announcement.withdrawn", restaurant_id=restaurant_id,
                                                 target=("staff_announcement", aid), db_path=db_path)
            except Exception:
                pass
    return get_announcement(restaurant_id, aid, db_path=db_path)


def staff_announcements(restaurant_id, membership_id, now_local=None, db_path=None) -> list:
    """This login's announcements: not withdrawn, not past their last day,
    unread first, then newest."""
    local = now_local or _local_now(restaurant_id)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT a.*, r.acked_at FROM staff_announcement_recipients r JOIN staff_announcements a "
            "ON a.id = r.announcement_id WHERE r.restaurant_id=? AND r.membership_id=? AND a.withdrawn_at IS NULL "
            "AND (a.expires_on IS NULL OR a.expires_on >= ?) ORDER BY a.created_at DESC, a.id DESC LIMIT 50",
            (restaurant_id, membership_id, local.date().isoformat())).fetchall()
    finally:
        conn.close()
    # In the reader's language when one is set and delivery translated it
    # (the cache only — a list read never calls the model), with what the
    # manager wrote beside it: `original_title` / `original_body`.
    lang = "en"
    try:
        import staff_knowledge
        lang = staff_knowledge.language_for(membership_id, **({"db_path": db_path} if db_path else {}))
    except Exception:
        lang = "en"
    out = []
    for r in rows:
        item = {"id": r["id"], "title": r["title"], "body": r["body"], "priority": r["priority"],
                "created_at": iso(r["created_at"]), "created_by_name": r["created_by_name"] or "",
                "expires_on": r["expires_on"], "acked_at": iso(r["acked_at"]), "language": lang,
                "translated": False, "original_title": None, "original_body": None}
        if lang != "en":
            t = _translated(membership_id, r["title"], restaurant_id, db_path, cache_only=True)
            b = _translated(membership_id, r["body"], restaurant_id, db_path, cache_only=True) if r["body"] else r["body"]
            if t != r["title"] or b != r["body"]:
                item.update(title=t, body=b, translated=True, original_title=r["title"], original_body=r["body"])
        out.append(item)
    out.sort(key=lambda x: x["acked_at"] is not None)          # stable: unread first, newest within
    return out


def ack(restaurant_id, membership_id, aid, db_path=None) -> str:
    """The employee's "Got it". Idempotent: the first stamp stands."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT r.id, r.acked_at FROM staff_announcement_recipients r JOIN staff_announcements a "
                           "ON a.id = r.announcement_id WHERE r.announcement_id=? AND r.membership_id=? AND "
                           "r.restaurant_id=? AND a.withdrawn_at IS NULL", (aid, membership_id, restaurant_id)).fetchone()
        if not row:
            raise NotFound("That announcement isn't in your inbox.")
        if not row["acked_at"]:
            conn.execute("UPDATE staff_announcement_recipients SET acked_at=? WHERE id=? AND acked_at IS NULL",
                         (_stamp(), row["id"]))
            conn.commit()
        stamp = conn.execute("SELECT acked_at FROM staff_announcement_recipients WHERE id=?",
                             (row["id"],)).fetchone()["acked_at"]
    finally:
        conn.close()
    return iso(stamp)


# ═══ Messages: one thread per employee with the deciders (COM-04b) ═════════

def _thread_row(conn, restaurant_id, membership, create=True):
    mid = membership["id"]
    row = conn.execute("SELECT * FROM staff_threads WHERE restaurant_id=? AND membership_id=?",
                       (restaurant_id, mid)).fetchone()
    if row or not create:
        return row
    conn.execute("INSERT OR IGNORE INTO staff_threads (restaurant_id, membership_id, employee_name) VALUES (?,?,?)",
                 (restaurant_id, mid, " ".join(str(membership.get("employee_name") or "").split()) or "Staff"))
    return conn.execute("SELECT * FROM staff_threads WHERE restaurant_id=? AND membership_id=?",
                        (restaurant_id, mid)).fetchone()


def _msg_out(m) -> dict:
    m = dict(m)
    return {"id": m["id"], "from": m["sender_kind"], "sender_name": m.get("sender_name") or "",
            "body": m["body"], "shift_date": m.get("shift_date"), "request_id": m.get("request_id"),
            "request_kind": m.get("request_kind"), "created_at": iso(m["created_at"]), "read_at": iso(m.get("read_at"))}


def _check_context(restaurant_id, name, shift_date, request_id, request_kind, db_path=None):
    """(shift_date, request_id, request_kind) cleaned, or CommsError. A
    request named must be this person's own."""
    sd = None
    if shift_date:
        try:
            sd = date.fromisoformat(str(shift_date)[:10]).isoformat()
        except ValueError:
            raise CommsError("That shift date isn't readable.")
    rid_, kind = None, None
    if request_id not in (None, "", 0):
        try:
            rid_ = int(request_id)
        except (TypeError, ValueError):
            raise CommsError("That request isn't readable.")
        kind = "time_off" if "time" in str(request_kind or "").lower() else "shift"
        conn = get_conn(db_path)
        try:
            if kind == "time_off":
                row = conn.execute("SELECT employee_name FROM staff_time_off WHERE id=? AND restaurant_id=?",
                                   (rid_, restaurant_id)).fetchone()
                mine = row and _nk(row["employee_name"]) == _nk(name)
            else:
                row = conn.execute("SELECT employee_name, target_name, replacement_name FROM shift_change_requests "
                                   "WHERE id=? AND restaurant_id=?", (rid_, restaurant_id)).fetchone()
                mine = row and _nk(name) in {_nk(row["employee_name"]), _nk(row["target_name"]),
                                             _nk(row["replacement_name"])}
        except Exception:
            mine = False
        finally:
            conn.close()
        if not mine:
            raise CommsError("That request isn't yours.")
    return sd, rid_, kind


def staff_post(restaurant_id, membership, body, shift_date=None, request_id=None, request_kind=None,
               db_path=None) -> dict:
    """An employee's message to the managers. Pushed to the deciders, once
    per burst (MESSAGE_PUSH_QUIET_MINUTES). Returns {"message", "managers_told"}."""
    name = " ".join(str((membership or {}).get("employee_name") or "").split())
    if not name or not (membership or {}).get("id"):
        raise CommsError("No employee name on this session.")
    text = _clean_body(body, MESSAGE_MAX)
    if not text:
        raise CommsError("A message can't be empty.")
    sd, rq, kind = _check_context(restaurant_id, name, shift_date, request_id, request_kind, db_path)
    now = _stamp()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        th = _thread_row(conn, restaurant_id, membership)
        recent = conn.execute(
            "SELECT 1 FROM staff_thread_messages WHERE thread_id=? AND sender_kind='staff' AND read_at IS NULL "
            "AND created_at >= datetime(?, ?)", (th["id"], now, f"-{MESSAGE_PUSH_QUIET_MINUTES} minutes")).fetchone()
        mid = conn.execute("INSERT INTO staff_thread_messages (restaurant_id, thread_id, sender_kind, sender_user_id, "
                           "sender_name, body, shift_date, request_id, request_kind, created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (restaurant_id, th["id"], "staff", membership.get("user_id"), name, text, sd, rq, kind,
                            now)).lastrowid
        conn.execute("UPDATE staff_threads SET last_at=?, employee_name=? WHERE id=?", (now, name, th["id"]))
        conn.commit()
        msg = conn.execute("SELECT * FROM staff_thread_messages WHERE id=?", (mid,)).fetchone()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    told = 0
    if not recent:
        ctx = ""
        if sd:
            from time_utils import mdy
            ctx = f" (about {mdy(sd)})"
        preview = text.replace("\n", " ")
        told = _tell_deciders(restaurant_id, f"Message from {name}",
                              f"{name}{ctx}: {preview[:180]}{'…' if len(preview) > 180 else ''}",
                              {"kind": "staff_message", "thread_id": th["id"],
                               "nav": f"{INBOX_NAV}?thread={th['id']}"}, db_path)
    return {"message": _msg_out(msg), "managers_told": bool(told), "thread_id": th["id"]}


def staff_thread(restaurant_id, membership, mark_read=True, limit=200, db_path=None) -> dict:
    """The employee's own thread, oldest first; opening it reads the
    managers' replies."""
    conn = get_conn(db_path)
    try:
        th = _thread_row(conn, restaurant_id, membership, create=False)
        if not th:
            return {"thread_id": None, "messages": [], "unread": 0}
        if mark_read:
            conn.execute("UPDATE staff_thread_messages SET read_at=?, read_by=? WHERE thread_id=? AND "
                         "sender_kind='manager' AND read_at IS NULL", (_stamp(), membership.get("user_id"), th["id"]))
            conn.commit()
        rows = conn.execute("SELECT * FROM (SELECT * FROM staff_thread_messages WHERE thread_id=? "
                            "ORDER BY created_at DESC, id DESC LIMIT ?) ORDER BY created_at, id",
                            (th["id"], int(limit))).fetchall()
        unread = conn.execute("SELECT COUNT(*) AS n FROM staff_thread_messages WHERE thread_id=? AND "
                              "sender_kind='manager' AND read_at IS NULL", (th["id"],)).fetchone()["n"]
    finally:
        conn.close()
    return {"thread_id": th["id"], "messages": [_msg_out(r) for r in rows], "unread": unread}


def staff_unread_messages(restaurant_id, membership_id, db_path=None) -> int:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM staff_thread_messages m JOIN staff_threads t ON t.id=m.thread_id "
                           "WHERE t.restaurant_id=? AND t.membership_id=? AND m.sender_kind='manager' AND "
                           "m.read_at IS NULL", (restaurant_id, membership_id)).fetchone()
    finally:
        conn.close()
    return int(row["n"] if row else 0)


def staff_inbox(restaurant_id, membership_id, now_local=None, db_path=None) -> dict:
    """GET /staff/api/inbox: announcements, how many are unread, and how
    many manager replies are unread (the app's badge)."""
    anns = staff_announcements(restaurant_id, membership_id, now_local=now_local, db_path=db_path)
    return {"announcements": anns, "unread": sum(1 for a in anns if not a["acked_at"]),
            "unread_messages": staff_unread_messages(restaurant_id, membership_id, db_path=db_path)}


def manager_inbox(restaurant_id, db_path=None) -> dict:
    """Every staff thread, unread first, then the latest; with the team's
    unread count (a staff message any manager has opened is read)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM staff_thread_messages m WHERE m.thread_id=t.id AND "
            "m.sender_kind='staff' AND m.read_at IS NULL) AS unread, "
            "(SELECT m.body FROM staff_thread_messages m WHERE m.thread_id=t.id ORDER BY m.created_at DESC, m.id DESC "
            "LIMIT 1) AS last_body, "
            "(SELECT m.sender_kind FROM staff_thread_messages m WHERE m.thread_id=t.id ORDER BY m.created_at DESC, "
            "m.id DESC LIMIT 1) AS last_from, "
            "(SELECT m.shift_date FROM staff_thread_messages m WHERE m.thread_id=t.id AND m.sender_kind='staff' "
            "ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_shift_date "
            "FROM staff_threads t WHERE t.restaurant_id=? AND t.last_at IS NOT NULL", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    threads = [{"thread_id": r["id"], "membership_id": r["membership_id"], "employee_name": r["employee_name"],
                "last_body": r["last_body"] or "", "last_from": r["last_from"] or "",
                "last_shift_date": r["last_shift_date"], "last_at": iso(r["last_at"]), "unread": int(r["unread"] or 0)}
               for r in rows]
    threads.sort(key=lambda t: t["last_at"] or "", reverse=True)
    threads.sort(key=lambda t: t["unread"] == 0)
    return {"threads": threads, "unread": sum(t["unread"] for t in threads)}


def manager_unread(restaurant_id, db_path=None) -> int:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM staff_thread_messages WHERE restaurant_id=? AND "
                           "sender_kind='staff' AND read_at IS NULL", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    return int(row["n"] if row else 0)


def manager_thread(restaurant_id, thread_id, reader, mark_read=True, limit=200, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        th = conn.execute("SELECT * FROM staff_threads WHERE id=? AND restaurant_id=?",
                          (thread_id, restaurant_id)).fetchone()
        if not th:
            raise NotFound("No such conversation.")
        if mark_read:
            conn.execute("UPDATE staff_thread_messages SET read_at=?, read_by=? WHERE thread_id=? AND "
                         "sender_kind='staff' AND read_at IS NULL", (_stamp(), (reader or {}).get("id"), th["id"]))
            conn.commit()
        rows = conn.execute("SELECT * FROM (SELECT * FROM staff_thread_messages WHERE thread_id=? "
                            "ORDER BY created_at DESC, id DESC LIMIT ?) ORDER BY created_at, id",
                            (th["id"], int(limit))).fetchall()
    finally:
        conn.close()
    return {"thread": {"thread_id": th["id"], "membership_id": th["membership_id"],
                       "employee_name": th["employee_name"]},
            "messages": [_msg_out(r) for r in rows]}


def manager_reply(restaurant_id, thread_id, user, body, db_path=None) -> dict:
    """A manager's answer; the employee is told through people.tell.
    Replying reads the thread. Returns {"message", "delivered_via"}."""
    text = _clean_body(body, MESSAGE_MAX)
    if not text:
        raise CommsError("A reply can't be empty.")
    who = login_name(user)
    now = _stamp()
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        th = conn.execute("SELECT * FROM staff_threads WHERE id=? AND restaurant_id=?",
                          (thread_id, restaurant_id)).fetchone()
        if not th:
            conn.rollback()
            raise NotFound("No such conversation.")
        conn.execute("UPDATE staff_thread_messages SET read_at=?, read_by=? WHERE thread_id=? AND sender_kind='staff' "
                     "AND read_at IS NULL", (now, (user or {}).get("id"), th["id"]))
        mid = conn.execute("INSERT INTO staff_thread_messages (restaurant_id, thread_id, sender_kind, sender_user_id, "
                           "sender_name, body, created_at) VALUES (?,?,?,?,?,?,?)",
                           (restaurant_id, th["id"], "manager", (user or {}).get("id"), who, text, now)).lastrowid
        conn.execute("UPDATE staff_threads SET last_at=? WHERE id=?", (now, th["id"]))
        conn.commit()
        msg = conn.execute("SELECT * FROM staff_thread_messages WHERE id=?", (mid,)).fetchone()
        member = conn.execute("SELECT employee_name, is_active FROM memberships WHERE id=? AND restaurant_id=?",
                              (th["membership_id"], restaurant_id)).fetchone()
    except NotFound:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    name = " ".join(str((member["employee_name"] if member else None) or th["employee_name"]).split())
    via = None
    if member and member["is_active"]:
        preview = text.replace("\n", " ")
        via = _tell_staff(restaurant_id, name, f"{who} replied", [preview[:220]], email_type="staff_message",
                          data={"kind": "staff_message", "thread_id": th["id"], "nav": "messages"},
                          db_path=db_path)
    if (user or {}).get("is_admin"):
        try:
            import admin_events
            admin_events.record_admin_action(user, "staff_message.replied", restaurant_id=restaurant_id,
                                             target=("staff_thread", th["id"]), db_path=db_path)
        except Exception:
            pass
    return {"message": _msg_out(msg), "delivered_via": via}
