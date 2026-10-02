"""staff_reminders — the server side of staff reminders, and the staff texts
held through the night (employee audit H3 and M8, 10/1/26).

Reminders. An employee got no reminder before a shift or before a task
was due; a critical line past due alerted only the manager, after the fact
(MISS-2, WF-23, AI-03, AI-07). `run_job` — every 10 minutes, bounded and
resumable over restaurants (strategy_jobs._BoundedWalk) — pushes:

  * "Your shift starts at 4pm" about an hour before each PUBLISHED shift
    (task_sheets.published_day_shifts: a draft never reminds anyone), on the
    first pass with the start 15–65 minutes away, with the day's brief line
    (the manager-approved brief's first sentence, in the person's language,
    else the first deterministic pre-shift line — brief_line, AI-07);
  * "Due in 15 min: <line>" for each CRITICAL line on that person's open
    task sheet, on the first pass with it due within 20 minutes, unless it
    is already ticked.

Each reminder is claimed first — one `staff_notices` row per person, shift
or line, and kind (`claim_key` UNIQUE) — so two passes, a restart or a
second scheduler never send it twice. It goes only to a login with a live
staff device (push only: an hour-before text per shift is not what anyone
consented to), only when that login has not muted "staff_reminder" on its
own phone (preferences.push_allowed), and through people.deliver: silent
between 10pm and 8am, and recorded `sent` only when Apple took it.

The staff app may also schedule local notifications; it must not for these
two (the server sends them): /staff/api/device-tokens and
/staff/api/notifications answer `reminders_server_side: true`.

Held texts. A staff text between 10pm and 8am restaurant time waits for
8am (people.deliver → hold_text), unless it is about a shift before then.
`release_held` (run by the same job) sends each once it is due — claimed
held → sending first — after re-reading the person's consent and STOP
(people.reach with the notice's purpose); a text no longer allowed falls
back to the email on file, else is dropped with the reason.

Sends only where the scheduler may run (scheduler.scheduling_allowed).
The ledger prunes itself: rows older than RETENTION_DAYS go at the end of
each run (never a held one).
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    import models
    if db_path is None or db_path == models.DB_PATH:
        return models.get_conn()
    return models.get_conn(db_path)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS staff_notices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id  INTEGER NOT NULL,
    kind           TEXT NOT NULL,          -- shift_reminder | task_reminder | held_text
    claim_key      TEXT NOT NULL UNIQUE,   -- one per person + shift/line + kind; a held text's own uuid
    employee_name  TEXT NOT NULL,
    purpose        TEXT,                   -- held_text: the consent scope it needs
    title          TEXT,
    body           TEXT,                   -- held_text: the text itself
    meta_json      TEXT,                   -- held_text: email fallback lines, a share link to undo
    release_at     TEXT,                   -- held_text: UTC, when it may go
    state          TEXT NOT NULL,          -- claimed -> sent | failed; held -> sending -> sent | failed | dropped
    channel        TEXT,                   -- push | sms | email, once known
    error          TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    done_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_staff_notices_held ON staff_notices(state, release_at);
CREATE INDEX IF NOT EXISTS idx_staff_notices_created ON staff_notices(created_at);
"""

SHIFT_WINDOW_MINUTES = (15, 65)     # start in (now+15, now+65]: ~60 min ahead on a 10-min tick
TASK_WINDOW_MINUTES = (0, 20)       # due in (now, now+20]: ~15 min ahead
JOB_MAX_SECONDS = 4 * 60
RELEASE_MAX_ROWS = 200
RELEASE_MAX_SECONDS = 2 * 60
RETENTION_DAYS = 30


def init_staff_reminders(db_path=None):
    """Boot DDL (push.init_push calls it) — never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _utc_stamp(dt) -> str:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _allowed() -> bool:
    import scheduler
    return scheduler.scheduling_allowed()


def _clock(dt) -> str:
    """4pm, 4:30pm."""
    return dt.strftime("%-I:%M%p").lower().replace(":00", "")


# ── the ledger ──────────────────────────────────────────────────────────────

def _claim(restaurant_id, kind, key, name, title, db_path) -> int:
    """The row id when this reminder is ours to send, 0 when an earlier pass
    already claimed it."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT OR IGNORE INTO staff_notices (restaurant_id, kind, claim_key, employee_name, "
                           "title, state) VALUES (?,?,?,?,?, 'claimed')",
                           (restaurant_id, kind, key, name, (title or "")[:200]))
        conn.commit()
        return int(cur.lastrowid or 0) if cur.rowcount == 1 else 0
    finally:
        conn.close()


def _finish(notice_id, outcome, db_path, error=None, state=None):
    """Record what really happened: `outcome` is the channel that reached
    them, None for nobody."""
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE staff_notices SET state=?, channel=?, error=?, done_at=datetime('now') WHERE id=?",
                         (state or ("sent" if outcome else "failed"), outcome,
                          (str(error)[:300] if error else (None if outcome else "no device took it")), notice_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[staff_reminders] could not record notice {notice_id}: {e!r}")


def hold_text(restaurant_id, employee_name, purpose, title, text, lines, release_at, email_type="staff_notice",
              meta=None, db_path=None) -> bool:
    """Hold one staff text until `release_at` (an aware datetime, or naive
    UTC). False when it could not be stored — the caller sends by another
    channel rather than lose it."""
    try:
        m = dict(meta or {})
        m.update(lines=[str(x) for x in (lines or [])][:10], email_type=email_type)
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO staff_notices (restaurant_id, kind, claim_key, employee_name, purpose, title, "
                         "body, meta_json, release_at, state) VALUES (?, 'held_text', ?,?,?,?,?,?,?, 'held')",
                         (restaurant_id, f"text:{uuid.uuid4().hex}", employee_name, purpose, (title or "")[:200],
                          (text or "")[:1000], json.dumps(m, default=str)[:4000], _utc_stamp(release_at)))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="staff_text_hold", context=f"restaurant_id={restaurant_id}")
        except Exception:
            pass
        return False


def _undo_share(meta, db_path):
    """A held published-week text carried its own /s/ link; one that never
    went must not leave the status list saying it did."""
    share = (meta or {}).get("share")
    if not share:
        return
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("DELETE FROM schedule_shares WHERE schedule_id=? AND token=?", (share[0], share[1]))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[staff_reminders] share undo failed: {e!r}")


def release_held(now_utc=None, db_path=None) -> dict:
    """Send the staff texts whose night is over. Bounded by RELEASE_MAX_ROWS
    and RELEASE_MAX_SECONDS, oldest release first; each row is taken
    (held -> sending) before it is sent, so it goes once."""
    import time as _time
    import people
    db = db_path or DB_PATH
    now_utc = now_utc or datetime.utcnow()
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM staff_notices WHERE kind='held_text' AND state='held' AND release_at <= ? "
            "ORDER BY release_at, id LIMIT ?", (_utc_stamp(now_utc), RELEASE_MAX_ROWS)).fetchall()]
    finally:
        conn.close()
    started = _time.monotonic()
    for row in rows:
        if _time.monotonic() - started > RELEASE_MAX_SECONDS:
            out["hit_bound"] = True
            break
        conn = get_conn(db_path)
        try:
            won = conn.execute("UPDATE staff_notices SET state='sending' WHERE id=? AND state='held'",
                               (row["id"],)).rowcount == 1
            conn.commit()
        finally:
            conn.close()
        if not won:
            continue
        out["attempted"] += 1
        try:
            meta = json.loads(row.get("meta_json") or "{}") or {}
        except (TypeError, ValueError):
            meta = {}
        rid, name = row["restaurant_id"], row["employee_name"]
        try:
            ch = people.reach(rid, [name], purpose=row.get("purpose") or "notice", db_path=db).get(name) or {}
            if ch.get("sms") and people._send_staff_text(rid, ch["sms"], row["body"] or ""):
                _finish(row["id"], "sms", db_path)
                out["ok"] += 1
                continue
            _undo_share(meta, db_path)
            if ch.get("email") and people._send_staff_email(rid, ch["email"], people._place(rid, db),
                                                            row.get("title") or "", meta.get("lines") or [],
                                                            meta.get("email_type") or "staff_notice"):
                _finish(row["id"], "email", db_path)
                out["ok"] += 1
                continue
            _finish(row["id"], None, db_path, state="dropped",
                    error="no text consent, number or email on file by morning" if not ch.get("sms")
                    else "the text failed and there is no email on file")
            out["failed"] += 1
        except Exception as e:
            _finish(row["id"], None, db_path, error=e)
            out["failed"] += 1
            try:
                import ops
                ops.capture(e, job="staff_reminders", context=f"held text {row['id']} restaurant_id={rid}")
            except Exception:
                pass
    return out


# ── reminders ───────────────────────────────────────────────────────────────

def _due_reminders(restaurant_id, now_local, db_path):
    """[(kind, employee_name, claim_key, title, body, nav, shift_date)] due
    now: shifts starting in SHIFT_WINDOW_MINUTES, critical task lines due in
    TASK_WINDOW_MINUTES and not ticked."""
    import staff_settings
    import task_sheets
    out = []
    lo, hi = SHIFT_WINDOW_MINUTES
    days = sorted({now_local.date(), (now_local + timedelta(minutes=hi)).date()})
    for day in days:
        try:
            shifts, published = task_sheets.published_day_shifts(restaurant_id, day)
        except Exception:
            shifts, published = [], False
        if not published:
            continue
        for s in shifts:
            try:
                start, end = datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])
            except (KeyError, TypeError, ValueError):
                continue
            mins = (start - now_local).total_seconds() / 60.0
            if not (lo < mins <= hi):
                continue
            name = s.get("employee") or ""
            role = (s.get("role") or "").strip()
            out.append(("shift_reminder", name,
                        f"shift:{restaurant_id}:{staff_settings.name_key(name)}:{s['start']}",
                        f"Your shift starts at {_clock(start)}",
                        f"You're on {_clock(start)}–{_clock(end)}" + (f" ({role})" if role else "") + ".",
                        {"tab": "today", "event": "shift_starting", "shift_date": start.date().isoformat(),
                         "shift_start": start.strftime("%H:%M")},
                        start.date().isoformat()))
    lo, hi = TASK_WINDOW_MINUTES
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM task_assignments WHERE restaurant_id=? AND status='open' AND task_date >= ?",
                            (restaurant_id, (now_local.date() - timedelta(days=1)).isoformat())).fetchall()
        latest = task_sheets._latest(conn, [a["id"] for a in rows]) if rows else {}
    finally:
        conn.close()
    for a in rows:
        try:
            lines = json.loads(a["lines_json"] or "[]")
            names = json.loads(a["assignees_json"] or "[]")
        except (TypeError, ValueError):
            continue
        for l in lines:
            if not l.get("critical") or not l.get("due_at"):
                continue
            try:
                due = datetime.fromisoformat(l["due_at"])
            except (TypeError, ValueError):
                continue
            mins = (due - now_local).total_seconds() / 60.0
            if not (lo < mins <= hi) or task_sheets._line_state(latest, a["id"], l["line_id"]):
                continue
            label = str(l.get("label") or "a task").strip()
            for name in names:
                out.append(("task_reminder", name,
                            f"task:{restaurant_id}:{a['id']}:{l['line_id']}:{staff_settings.name_key(name)}",
                            f"Due in {max(1, int(round(mins)))} min: {label}"[:120],
                            f"{a['title'] or a['job_code']} — due at {_clock(due)}. Tick it off in Tasks.",
                            {"tab": "tasks", "assignment_id": a["id"], "line_id": l["line_id"], "event": "task_due"},
                            a["task_date"]))
    return out


BRIEF_LINE_MAX = 160


def _first_sentence(text) -> str:
    import re
    t = " ".join(str(text or "").split())
    first = re.split(r"(?<=[.!?])\s+", t, maxsplit=1)[0] if t else ""
    return first if len(first) <= BRIEF_LINE_MAX else first[:BRIEF_LINE_MAX - 1].rstrip() + "…"


def brief_line(restaurant_id, day_iso, db_path=None, memo=None):
    """(line, approved) the shift reminder carries for `day_iso` (AI-07): the
    first sentence of the brief a manager approved for that day, else the
    first line of the deterministic pre-shift items — both staff-safe by
    construction (staff_brief: approval runs the staff-safety check;
    preshift.build: relative figures only, never money). (None, False) when
    there is neither. `memo` keeps one read per restaurant and day."""
    if memo is not None and day_iso in memo:
        return memo[day_iso]
    out = (None, False)
    try:
        from datetime import date as _date
        import preshift
        import staff_brief
        d = _date.fromisoformat(str(day_iso)[:10])
        kw = {"db_path": db_path} if db_path else {}
        text = (staff_brief.approved(restaurant_id, d, **kw).get("brief_text") or "").strip()
        if text:
            out = (_first_sentence(text), True)
        else:
            built = preshift.build_cached(restaurant_id, day=d, **kw) or {}
            items = [i for i in (built.get("items") or []) if str(i.get("text") or "").strip()]
            if items:
                out = (_first_sentence(items[0]["text"]), False)
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="staff_reminders", context=f"restaurant_id={restaurant_id} brief line")
        except Exception:
            pass
        out = (None, False)
    if memo is not None:
        memo[day_iso] = out
    return out


def _with_brief(restaurant_id, body, shift_date, uid, db_path, memo) -> str:
    """The shift reminder's body with the day's brief line — the approved one
    in the person's language when they set one (staff_knowledge.translate_for
    on the login holding the device; deterministic lines stay as built)."""
    line, approved = brief_line(restaurant_id, shift_date, db_path=db_path, memo=memo)
    if not line:
        return body
    if approved and uid:
        try:
            import staff_knowledge
            conn = get_conn(db_path)
            try:
                m = conn.execute("SELECT id FROM memberships WHERE user_id=? AND restaurant_id=? AND is_active=1 "
                                 "ORDER BY id DESC LIMIT 1", (uid, restaurant_id)).fetchone()
            finally:
                conn.close()
            if m:
                line = staff_knowledge.translate_for(m["id"], line, restaurant_id=restaurant_id,
                                                     **({"db_path": db_path} if db_path else {}))
        except Exception:
            pass
    return f"{body} {line}"


def remind_restaurant(restaurant_id, now_local, db_path=None) -> dict:
    """Send this restaurant's due reminders. Returns the standard counts:
    attempted = claimed now, ok = pushed, failed = no device took it,
    skipped = no staff device, or the person muted reminders. A shift
    reminder carries the day's brief line (brief_line, AI-07)."""
    import people
    import preferences
    db = db_path or DB_PATH
    c = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
    due = _due_reminders(restaurant_id, now_local, db_path)
    if not due:
        return c
    channels = people.reach(restaurant_id, sorted({d[1] for d in due}), db_path=db)
    briefs = {}
    for kind, name, key, title, body, nav, shift_date in due:
        uid = (channels.get(name) or {}).get("push_user_id")
        if not uid or not preferences.push_allowed(uid, restaurant_id, "staff_reminder", now_local=now_local,
                                                   db_path=db):
            # Not claimed: unmuted (or the app installed) inside the window,
            # the next pass still reminds them.
            c["skipped"] += 1
            continue
        notice_id = _claim(restaurant_id, kind, key, name, title, db_path)
        if not notice_id:
            continue
        if kind == "shift_reminder":
            body = _with_brief(restaurant_id, body, shift_date, uid, db_path, briefs)
        c["attempted"] += 1
        outcome = people.tell_staff(restaurant_id, name, "reminder", title, body, nav=nav, purpose="reminder",
                                    shift_date=shift_date, channel={"push_user_id": uid},
                                    on_outcome=lambda ch, nid=notice_id: _finish(nid, ch, db_path),
                                    db_path=db)
        if outcome == "push":
            c["ok"] += 1
        else:
            c["failed"] += 1
    return c


def _restaurants_with_staff_devices(db_path) -> set:
    conn = get_conn(db_path)
    try:
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT restaurant_id FROM device_tokens WHERE tier='staff' AND disabled_reason IS NULL")}
    finally:
        conn.close()


def _prune(db_path):
    conn = get_conn(db_path)
    try:
        conn.execute("DELETE FROM staff_notices WHERE created_at < datetime('now', ?) AND state NOT IN "
                     "('held', 'sending')", (f"-{RETENTION_DAYS} days",))
        conn.commit()
    finally:
        conn.close()


def run_job(restaurants=None, db_path=None, now_utc=None) -> dict:
    """Scheduler entry (every 10 minutes): each restaurant with a staff
    device, from after its cursor, under JOB_MAX_SECONDS — then the texts
    held through the night. Returns {attempted, ok, failed, skipped,
    hit_bound} (+ reminders, released)."""
    import ops
    from strategy_jobs import _BoundedWalk, _restaurants
    from time_utils import restaurant_tz
    db = db_path or DB_PATH
    if not _allowed():
        return {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False,
                "reason": "not the production scheduler host"}
    rs = list(restaurants) if restaurants is not None else list(_restaurants(db))
    with_devices = _restaurants_with_staff_devices(db_path)
    walk = _BoundedWalk("staff_reminders", [r for r in rs if r.id in with_devices], db, JOB_MAX_SECONDS)
    total = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0}
    for r in walk:
        try:
            if now_utc is not None:
                now_local = now_utc.replace(tzinfo=timezone.utc).astimezone(restaurant_tz(r)).replace(tzinfo=None)
            else:
                now_local = datetime.now(restaurant_tz(r)).replace(tzinfo=None)
            got = remind_restaurant(r.id, now_local, db_path=db_path)
            for k in total:
                total[k] += got[k]
        except Exception as e:
            total["failed"] += 1
            ops.capture(e, job="staff_reminders", context=f"restaurant_id={r.id}")
    reminders = dict(total)
    released = release_held(now_utc=now_utc, db_path=db_path)
    for k in total:
        total[k] += released[k]
    try:
        _prune(db_path)
    except Exception as e:
        ops.capture(e, job="staff_reminders", context="prune")
    return {**total, "hit_bound": bool(walk.hit_bound or released["hit_bound"]),
            "reminders": reminders["attempted"], "released": released["ok"]}
