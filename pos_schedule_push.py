"""
pos_schedule_push.py — the published week written to the POS, where its time
clock enforces it (schedule audit 10/10/26 MARKET-7, roadmap RM-16).

Cavnar AI has no clock of its own and reads punches after the fact, so the
"no overtime" rule held in the plan and not on the floor: early clock-ins and
late clock-outs — the main source of unplanned overtime — were seen the next
morning. RPOWER's Core API takes a labor schedule (rpower.push_labor_schedule,
enabled for our token 9/29/26) and enforces it at the store; nothing called it.

  * The owner's switch (floor_settings.pos_push) is OFF by default and is
    turned on from the preview: exactly what would be sent, checked against
    the POS's own employees and jobs (provider.preview_labor_schedule), with
    every shift's status — sent, skipped and why, a problem the POS would
    refuse, already started, salaried (they don't clock in).
  * A publish never waits on it: the Send enqueues the week (after_publish)
    and the `pos_schedule_push` job writes it within minutes; a failure tells
    the owner (and the operator), keeps the reason per shift, and retries
    POS-side failures (RETRY_MINUTES) — never a refusal for good (the push is
    not enabled for the token, a provider without a write).
  * Idempotent: the body for a week is hashed; the same body already at the
    POS is not sent again ("unchanged"). Shifts that already started are not
    sent. A person on the last week sent but on none of this week's shifts is
    listed `removed_unconfirmed`: whether RPOWER drops a person left out of a
    corrected push is not confirmed by RPOWER (rpower.push_labor_schedule) —
    said, never assumed.
  * A local copy of the app never writes to a POS (scheduler.scheduling_allowed:
    a laptop has production's credentials).

Toast: the preview is real (reads), the push is refused until Toast grants
Cavnar AI integration-partner write access (toast.SCHEDULE_WRITE_EXTERNAL).
Square and Clover: no schedule write in their integrations here.

overtime_drift() is the other half of the loop: minutes punched outside the
scheduled shift, by person and role, for the nightly report.
"""
import hashlib
import json
from datetime import datetime, timedelta

import models as _models_mod

STATUSES = ("queued", "sending", "sent", "partial", "unchanged", "failed", "refused", "held_local")
SHIFT_STATUSES = ("sent", "would_send", "skipped", "problem", "started", "salaried", "unchanged",
                  "failed", "removed_unconfirmed")
RETRY_MINUTES = (10, 30, 120)
RUN_MAX_SECONDS = 4 * 60
# A push still `sending` this long after its claim belongs to a process that
# died (_reap_stale): a run is bounded by RUN_MAX_SECONDS.
SENDING_LEASE_MINUTES = 15
# Minutes either side of a scheduled shift a punch may land before it is drift.
DRIFT_GRACE_MINUTES = 7


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports)."""
    return _models_mod.get_conn(db_path) if db_path else _models_mod.get_conn()


def init_pos_schedule_push(db_path=None):
    """Boot DDL (models.init_db) — never on a request path."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS pos_schedule_pushes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            history_id    INTEGER NOT NULL,
            week_start    TEXT NOT NULL,
            provider      TEXT,
            status        TEXT NOT NULL DEFAULT 'queued',
            reason        TEXT,
            payload_hash  TEXT,
            shifts_total  INTEGER,
            shifts_sent   INTEGER,
            error         TEXT,
            attempts      INTEGER NOT NULL DEFAULT 0,
            next_try_at   TEXT,
            actor         TEXT,
            people_json   TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            sent_at       TEXT,
            updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_pushes_week ON pos_schedule_pushes "
                     "(restaurant_id, week_start, id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_pushes_queue ON pos_schedule_pushes (status, next_try_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS pos_schedule_push_shifts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            push_id       INTEGER NOT NULL,
            restaurant_id INTEGER NOT NULL,
            shift_key     TEXT NOT NULL,
            employee      TEXT,
            role          TEXT,
            start_at      TEXT,
            end_at        TEXT,
            status        TEXT NOT NULL,
            reason        TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_push_shifts ON pos_schedule_push_shifts (push_id)")
        # The retention deletes' columns (ops._RETENTION_COLUMN).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_pushes_created ON pos_schedule_pushes (created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pos_push_shifts_created ON pos_schedule_push_shifts (created_at)")
        conn.commit()
    finally:
        conn.close()


# ── the week ────────────────────────────────────────────────────────────────

def _hist(restaurant_id, history_id, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT id, week_start, week_end, published_at FROM schedule_history "
                         "WHERE id=? AND restaurant_id=?", (int(history_id), restaurant_id)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def published_rows(restaurant_id, week_start, db_path=None) -> list:
    """The PUBLISHED copy of the week staff are on — the one "week in force"
    read (models.live_week_for), its rows from the shift table
    (schedule_shifts.live_week_rows, each with its shift_id — schedule
    re-audit 10/11/26 ARCH-7). [] when none is live."""
    import schedule_shifts
    return list(schedule_shifts.live_week_rows(restaurant_id, str(week_start)[:10], db_path=db_path) or [])


def shift_key(row) -> str:
    """The shift's persistent id (D1's shift_id) where the row carries one,
    else person|date|start."""
    sid = str(row.get("shift_id") or "").strip()
    if sid:
        return sid
    import staff_settings as _ss
    return f"{_ss.name_key(row.get('employee'))}|{row.get('date')}|{row.get('shift_start')}"


def week_shifts(restaurant, rows, now_local=None) -> tuple:
    """(to_send, held): each row of the published week as a timed shift
    {key, employee, role, start, end} on local wall clock (schedule_time.
    shift_span: a 12:30am start is the night's, a close past midnight ends
    the next day). `held` are the rows not sent and why: already started,
    salaried (they don't clock in), open, or times unreadable."""
    import attendance
    import schedule_time as _st
    from time_utils import restaurant_now
    now_local = now_local or restaurant_now(restaurant, naive=True)
    sal = attendance.salaried_keys(restaurant)
    from models import salaried_name_key
    send, held = [], []
    for r in rows or []:
        name = (r.get("employee") or "").strip()
        base = {"key": shift_key(r), "employee": name, "role": (r.get("role") or "").strip()}
        a, z = _st.shift_span(r)
        if a is None or z is None:
            held.append(dict(base, start=None, end=None, status="skipped", reason="times not readable"))
            continue
        base.update(start=a.strftime("%Y-%m-%dT%H:%M:%S"), end=z.strftime("%Y-%m-%dT%H:%M:%S"))
        if not name or name.lower().startswith("open"):
            held.append(dict(base, status="skipped", reason="open shift — nobody to schedule at the POS"))
        elif salaried_name_key(name) in sal:
            held.append(dict(base, status="salaried", reason="salaried — doesn't clock in"))
        elif a <= now_local:
            held.append(dict(base, status="started", reason="already started"))
        else:
            send.append(base)
    send.sort(key=lambda s: (s["start"], s["employee"].lower()))
    return send, held


def payload_hash(shifts) -> str:
    body = sorted((s["employee"].lower(), s["role"].lower(), s["start"], s["end"]) for s in shifts)
    return hashlib.sha256(json.dumps(body).encode()).hexdigest()


def _provider(restaurant_id):
    """(name, module) of the connected POS when it can at least preview a
    schedule, else (name or None, None)."""
    import pos
    name, mod = pos.connected_provider(restaurant_id)
    if mod is None or not callable(getattr(mod, "preview_labor_schedule", None)):
        return name, None
    return name, mod


def _label(name):
    return {"rpower": "RPOWER", None: "your POS"}.get(name, (name or "POS").title())


def _last_sent(restaurant_id, week_start, provider, db_path=None):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM pos_schedule_pushes WHERE restaurant_id=? AND week_start=? AND provider=? "
                         "AND status IN ('sent','partial') ORDER BY id DESC LIMIT 1",
                         (restaurant_id, week_start, provider)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def _shift_status_from_preview(send, prev):
    """Each shift to send marked from the provider's preview: a person it
    skipped (no POS id, no job) and the shift its problems name."""
    import staff_settings as _ss
    skipped = {}
    for s in prev.get("skipped") or []:
        skipped.setdefault(_ss.name_key(s.get("employee")), s.get("why") or "skipped")
    problems = {}
    for p in prev.get("problems") or []:
        sh = p.get("shift") or {}
        if sh.get("inTime"):
            problems[(_ss.name_key(p.get("employee")), str(sh.get("inTime"))[:19])] = p.get("problem")
        else:
            problems[(_ss.name_key(p.get("employee")), None)] = p.get("problem")
    out = []
    for s in send:
        k = _ss.name_key(s["employee"])
        why = problems.get((k, s["start"])) or problems.get((k, None))
        if k in skipped:
            out.append(dict(s, status="skipped", reason=skipped[k]))
        elif why:
            out.append(dict(s, status="problem", reason=why))
        else:
            out.append(dict(s, status="would_send", reason=None))
    return out


def preview(restaurant_id, history_id, now_local=None, db_path=None) -> dict:
    """What a push of this published week would send, nothing sent:
    {ok, provider, label, supported, write_enabled, external, enabled,
    week_start, shifts: [{key, employee, role, start, end, status, reason}],
    counts: {status: n}, unchanged, last_push}. `supported` False (and why)
    for a POS with no schedule write."""
    import floor_settings
    from models import get_restaurant
    r = get_restaurant(restaurant_id, db_path) if db_path else get_restaurant(restaurant_id)
    hist = _hist(restaurant_id, history_id, db_path)
    if not hist:
        return {"ok": False, "error": "That schedule wasn't found."}
    if not hist.get("published_at"):
        return {"ok": False, "error": "Only a week that has gone to staff can go to the POS — send it first."}
    name, mod = _provider(restaurant_id)
    out = {"ok": True, "provider": name, "label": _label(name), "week_start": str(hist["week_start"])[:10],
           "enabled": floor_settings.get(r)["pos_push"], "history_id": int(history_id),
           "last_push": last_push(restaurant_id, str(hist["week_start"])[:10], db_path=db_path)}
    if mod is None:
        out.update(supported=False, shifts=[], counts={},
                   reason=(f"{_label(name)} has no schedule write Cavnar AI can use — the week stays in "
                           "Cavnar AI and the staff app." if name else "No POS is connected."))
        return out
    rows = published_rows(restaurant_id, out["week_start"], db_path)
    send, held = week_shifts(r, rows, now_local=now_local)
    prev = mod.preview_labor_schedule(restaurant_id, [dict(s) for s in send]) if send else {"skipped": [],
                                                                                            "problems": []}
    marked = _shift_status_from_preview(send, prev)
    sendable = [s for s in marked if s["status"] == "would_send"]
    last = _last_sent(restaurant_id, out["week_start"], name, db_path)
    unchanged = bool(last and sendable and last.get("payload_hash") == payload_hash(sendable))
    removed = _removed(last, sendable)
    shifts = marked + held + [dict(key=f"removed|{p}", employee=p, role="", start=None, end=None,
                                   status="removed_unconfirmed",
                                   reason=f"on the last week sent to {_label(name)}, on no shift now — "
                                          f"{_label(name)} may still hold their earlier shifts; check the POS")
                              for p in removed]
    counts = {}
    for s in shifts:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    out.update(supported=callable(getattr(mod, "push_labor_schedule", None)),
               write_enabled=prev.get("write_enabled", True), external=prev.get("external"),
               shifts=shifts, counts=counts, unchanged=unchanged, sendable=len(sendable))
    return out


def _removed(last, sendable) -> list:
    """People on the last sent week who are on none of these shifts."""
    if not last:
        return []
    try:
        before = set(json.loads(last.get("people_json") or "[]"))
    except (TypeError, ValueError):
        before = set()
    now = {s["employee"] for s in sendable}
    import staff_settings as _ss
    now_keys = {_ss.name_key(n) for n in now}
    return sorted(p for p in before if _ss.name_key(p) not in now_keys)


def last_push(restaurant_id, week_start, db_path=None) -> dict:
    """The newest push of the week with its shifts' statuses, or None."""
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT * FROM pos_schedule_pushes WHERE restaurant_id=? AND week_start=? "
                         "ORDER BY id DESC LIMIT 1", (restaurant_id, str(week_start)[:10])).fetchone()
        if not r:
            return None
        shifts = [dict(x) for x in conn.execute(
            "SELECT shift_key, employee, role, start_at, end_at, status, reason FROM pos_schedule_push_shifts "
            "WHERE push_id=? ORDER BY start_at, employee", (r["id"],)).fetchall()]
    finally:
        conn.close()
    from time_utils import mdy
    d = dict(r)
    d.pop("people_json", None)
    d["shifts"] = shifts
    d["sent_on"] = mdy(str(d.get("sent_at") or "")[:10]) if d.get("sent_at") else None
    return d


# ── the queue ───────────────────────────────────────────────────────────────

def enqueue(restaurant_id, history_id, actor=None, reason="publish", db_path=None):
    """Queue the week for the push job. A week already queued is re-pointed
    at this copy (one queued row per week). Returns the push id, or None
    when the switch is off, the POS cannot take a schedule, or the copy is
    not published."""
    import floor_settings
    from models import get_restaurant
    r = get_restaurant(restaurant_id, db_path) if db_path else get_restaurant(restaurant_id)
    if r is None or not floor_settings.get(r)["pos_push"]:
        return None
    name, mod = _provider(restaurant_id)
    if mod is None or not callable(getattr(mod, "push_labor_schedule", None)):
        return None
    hist = _hist(restaurant_id, history_id, db_path)
    if not hist or not hist.get("published_at"):
        return None
    ws = str(hist["week_start"])[:10]
    who = ((actor.get("username") or actor.get("email")) if isinstance(actor, dict) else actor) or "automation"
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        q = conn.execute("SELECT id FROM pos_schedule_pushes WHERE restaurant_id=? AND week_start=? "
                         "AND status='queued'", (restaurant_id, ws)).fetchone()
        if q:
            conn.execute("UPDATE pos_schedule_pushes SET history_id=?, reason=?, actor=?, next_try_at=NULL, "
                         "updated_at=datetime('now') WHERE id=?", (int(history_id), reason, str(who)[:120], q["id"]))
            pid = q["id"]
        else:
            pid = conn.execute("INSERT INTO pos_schedule_pushes (restaurant_id, history_id, week_start, provider, "
                               "status, reason, actor) VALUES (?,?,?,?, 'queued', ?, ?)",
                               (restaurant_id, int(history_id), ws, name, reason, str(who)[:120])).lastrowid
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    return pid


def after_publish(restaurant_id, history_id, actor=None, db_path=None):
    """The Send's one call: queue the week when the owner turned the push on.
    Never raises and never waits on the POS — a publish is never held by it."""
    try:
        return enqueue(restaurant_id, history_id, actor=actor, reason="publish", db_path=db_path)
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="pos_schedule_push", context=f"restaurant_id={restaurant_id} enqueue")
        except Exception:
            pass
        return None


def _reap_stale(db_path=None) -> int:
    """Pushes a dead process left in `sending` (claimed longer ago than
    SENDING_LEASE_MINUTES — a push takes seconds, a run at most
    RUN_MAX_SECONDS) go back to `failed`, due now while their tries last and
    left failed after the last (the owner's "push again" reaches them): a
    deploy mid-push left the week off the POS for good, never retried and
    never said (scale audit 10/19/26 A5-F15). Returns rows reaped."""
    now = datetime.utcnow()
    cutoff = (now - timedelta(minutes=SENDING_LEASE_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        n = conn.execute(
            "UPDATE pos_schedule_pushes SET status='failed', "
            "error='The push was interrupted (a restart) before the POS answered.', "
            "next_try_at=CASE WHEN attempts <= ? THEN ? ELSE NULL END, updated_at=datetime('now') "
            "WHERE status='sending' AND updated_at < ?", (len(RETRY_MINUTES), stamp, cutoff)).rowcount
        conn.commit()
        return int(n or 0)
    finally:
        conn.close()


def _claim(push_id, db_path=None) -> bool:
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE pos_schedule_pushes SET status='sending', attempts=attempts+1, "
                           "updated_at=datetime('now') WHERE id=? AND status IN ('queued','failed')", (push_id,))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _finish(push_id, status, shifts, error=None, payload=None, people=None, retry=False, attempts=0, db_path=None):
    nxt = None
    if retry and attempts <= len(RETRY_MINUTES):
        nxt = (datetime.utcnow() + timedelta(minutes=RETRY_MINUTES[min(attempts, len(RETRY_MINUTES)) - 1])
               ).strftime("%Y-%m-%d %H:%M:%S")
    sent = sum(1 for s in shifts if s["status"] == "sent")
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE pos_schedule_pushes SET status=?, error=?, payload_hash=COALESCE(?, payload_hash), "
                     "shifts_total=?, shifts_sent=?, next_try_at=?, people_json=COALESCE(?, people_json), "
                     "sent_at=CASE WHEN ? IN ('sent','partial') THEN datetime('now') ELSE sent_at END, "
                     "updated_at=datetime('now') WHERE id=?",
                     (status, (error or None) and str(error)[:500], payload, len(shifts), sent, nxt,
                      json.dumps(sorted(people)) if people is not None else None, status, push_id))
        row = conn.execute("SELECT restaurant_id FROM pos_schedule_pushes WHERE id=?", (push_id,)).fetchone()
        conn.execute("DELETE FROM pos_schedule_push_shifts WHERE push_id=?", (push_id,))
        conn.executemany("INSERT INTO pos_schedule_push_shifts (push_id, restaurant_id, shift_key, employee, role, "
                         "start_at, end_at, status, reason) VALUES (?,?,?,?,?,?,?,?,?)",
                         [(push_id, row["restaurant_id"], s.get("key") or "", s.get("employee"), s.get("role"),
                           s.get("start"), s.get("end"), s["status"], (s.get("reason") or None) and
                           str(s["reason"])[:300]) for s in shifts])
        conn.commit()
    finally:
        conn.close()


def run_push(push_id, now_local=None, db_path=None) -> dict:
    """Send one queued push: preview first (problem shifts held back with
    their reason), the same body as the last push not sent again, the rest
    to the POS. {status, sent, error}. Never raises."""
    import scheduler
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM pos_schedule_pushes WHERE id=?", (push_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"status": None, "error": "no such push"}
    row = dict(row)
    rid = row["restaurant_id"]
    if not scheduler.scheduling_allowed():
        # A laptop holds production's POS tokens: it never writes to a POS.
        _finish(push_id, "held_local", [], error="a local copy of Cavnar AI never writes to a POS", db_path=db_path)
        return {"status": "held_local", "error": "This copy of Cavnar AI can't write to a POS."}
    if not _claim(push_id, db_path):
        return {"status": row["status"], "error": "already being sent"}
    attempts = int(row.get("attempts") or 0) + 1
    try:
        pv = preview(rid, row["history_id"], now_local=now_local, db_path=db_path)
    except Exception as e:
        _finish(push_id, "failed", [], error=f"preview failed: {e}", retry=True, attempts=attempts, db_path=db_path)
        _tell_failure(rid, row, f"the POS couldn't be read ({e})", db_path)
        return {"status": "failed", "error": str(e)}
    if not pv.get("ok") or not pv.get("supported"):
        _finish(push_id, "refused", [], error=pv.get("error") or pv.get("reason"), db_path=db_path)
        return {"status": "refused", "error": pv.get("error") or pv.get("reason")}
    shifts = pv["shifts"]
    sendable = [s for s in shifts if s["status"] == "would_send"]
    if not sendable:
        _finish(push_id, "refused", shifts, error="nothing on the week can go to the POS", db_path=db_path)
        return {"status": "refused", "error": "nothing on the week can go to the POS"}
    h = payload_hash(sendable)
    people = {s["employee"] for s in sendable}
    if pv.get("unchanged"):
        _finish(push_id, "unchanged", [dict(s, status="unchanged" if s["status"] == "would_send" else s["status"])
                                       for s in shifts], payload=h, people=people, db_path=db_path)
        return {"status": "unchanged", "sent": 0}
    _name, mod = _provider(rid)
    try:
        res = mod.push_labor_schedule(rid, [dict(s) for s in sendable]) or {}
    except Exception as e:
        res = {"ok": False, "error": f"Couldn't reach the POS: {e}"}
    if res.get("external"):
        _finish(push_id, "refused", [dict(s, status="failed", reason=res.get("error")) if s["status"] == "would_send"
                                     else s for s in shifts], error=res.get("error"), db_path=db_path)
        _tell_failure(rid, row, res.get("error"), db_path)
        return {"status": "refused", "error": res.get("error")}
    import staff_settings as _ss
    failed_keys = {(_ss.name_key(f.get("employee")), str(f.get("start") or "")[:19]) for f in res.get("failed") or []}
    skipped_people = {_ss.name_key(s.get("employee")): s.get("why") for s in res.get("skipped") or []}
    out_shifts = []
    for s in shifts:
        if s["status"] != "would_send":
            out_shifts.append(s)
            continue
        k = _ss.name_key(s["employee"])
        if not res.get("ok") and not res.get("failed"):
            out_shifts.append(dict(s, status="failed", reason=res.get("error")))
        elif (k, s["start"]) in failed_keys:
            out_shifts.append(dict(s, status="failed", reason=next(
                (f.get("why") for f in res["failed"] if _ss.name_key(f.get("employee")) == k), None)))
        elif k in skipped_people:
            out_shifts.append(dict(s, status="skipped", reason=skipped_people[k]))
        else:
            out_shifts.append(dict(s, status="sent", reason=None))
    n_sent = sum(1 for s in out_shifts if s["status"] == "sent")
    n_failed = sum(1 for s in out_shifts if s["status"] == "failed")
    if n_sent and not n_failed:
        status = "sent"
    elif n_sent:
        status = "partial"
    else:
        status = "failed"
    auth = "refused the schedule push" in str(res.get("error") or "")
    _finish(push_id, status, out_shifts, error=res.get("error"), payload=h if n_sent else None,
            people=people if n_sent else None, retry=(status == "failed" and not auth), attempts=attempts,
            db_path=db_path)
    if status in ("failed", "partial"):
        _tell_failure(rid, row, res.get("error") or f"{n_failed} shift(s) didn't go through", db_path)
    try:
        from models import log_event
        log_event(rid, "pos_schedule_pushed", {"week_start": row["week_start"], "status": status, "sent": n_sent,
                                               "provider": row.get("provider"),
                                               "detail": f"{n_sent} shift(s) to {_label(row.get('provider'))}"})
    except Exception:
        pass
    return {"status": status, "sent": n_sent, "error": res.get("error")}


def send_now(restaurant_id, push_id) -> bool:
    """Hand a queued push to the job queue so it is sent within seconds, off
    the web request thread (blind re-audit 10/20/26 R3-07: the owner's
    "Send this week now" ran the preview and a 30-second POST on one of the
    four request threads). True when queued; a push not handed over still
    goes with the `pos_schedule_push` job's next 10-minute run. Never
    raises."""
    try:
        import time as _t
        import job_queue
        _qid, _created = job_queue.enqueue(
            "pos_schedule_push", {"push_id": int(push_id)}, restaurant_id=int(restaurant_id),
            priority_class="interactive",
            # A double press in the same minute is one send.
            idempotency_key=f"pos_schedule_push_now:{int(push_id)}:{int(_t.time() // 60)}")
        return True
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="pos_schedule_push", context=f"restaurant_id={restaurant_id} push={push_id} hand-off")
        except Exception:
            pass
        return False


def run_queued_push(job):
    """job_queue's handler for `pos_schedule_push`: one push, sent
    (run_push — it claims the row, so the 10-minute job never sends it
    twice)."""
    pid = (job.payload or {}).get("push_id")
    if not pid:
        return {"skipped": "no push"}
    out = run_push(int(pid))
    return {"status": out.get("status"), "sent": out.get("sent")}


def _tell_failure(restaurant_id, row, why, db_path=None):
    """The owner hears a push that didn't land (the week is with staff; the
    clock won't enforce it), and the operator gets the error. Never raises."""
    from time_utils import mdy
    label = _label(row.get("provider"))
    text = (f"The week of {mdy(str(row['week_start'])[:10])} went to staff, but not to {label}'s time clock: "
            f"{why}. Open the schedule to see each shift and push again.")
    try:
        import ops
        ops.capture(RuntimeError(f"POS schedule push: {why}"), job="pos_schedule_push",
                    context=f"restaurant_id={restaurant_id} push={row.get('id')}")
    except Exception:
        pass
    try:
        from models import log_event
        log_event(restaurant_id, "pos_schedule_push_failed", {"week_start": row["week_start"], "detail": text})
    except Exception:
        pass
    try:
        import strategy_jobs
        strategy_jobs._reach(restaurant_id, "schedule_publish_held", f"Schedule not on {label}'s clock", text,
                             {"tab": "labor"}, db_path or _models_mod.DB_PATH, lines=[text])
    except Exception as e:
        print(f"[pos_schedule_push] owner not told rid={restaurant_id}: {e!r}")


def run_queue(db_path=None, now=None) -> dict:
    """The `pos_schedule_push` job: every queued push, and every failed one
    whose retry is due, oldest first, under RUN_MAX_SECONDS — the queue rows
    are the cursor. Standard counts."""
    import time as _t
    t0 = _t.monotonic()
    try:
        _reap_stale(db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="pos_schedule_push", context="stale sending rows")
    stamp = (now or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn(db_path)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM pos_schedule_pushes WHERE status='queued' OR (status='failed' AND next_try_at IS NOT NULL "
            "AND next_try_at <= ?) ORDER BY id LIMIT 200", (stamp,)).fetchall()]
    finally:
        conn.close()
    c = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False}
    for pid in ids:
        if _t.monotonic() - t0 > RUN_MAX_SECONDS:
            c["hit_bound"] = True
            break
        c["attempted"] += 1
        out = run_push(pid, db_path=db_path)
        if out.get("status") in ("sent", "unchanged"):
            c["ok"] += 1
        elif out.get("status") in ("refused", "held_local", None):
            c["skipped"] += 1
        else:
            c["failed"] += 1
    return c


# ── overtime drift: the floor against the plan ──────────────────────────────

def overtime_drift(restaurant_id, day, rows=None, punches=None, db_path=None) -> dict:
    """Minutes punched outside the scheduled shift on business date `day`:
    clocked in more than DRIFT_GRACE_MINUTES before the scheduled start
    (early) or out more than that after the scheduled end (late), by person
    and role, priced at the punch's own wage else the person's or role's
    rate (labor._shift_rate_source). Punches from the ticket archive
    (pos_punches); the schedule is the published week. Salaried people are
    left out (they don't clock in). {available, reason?, people: [{employee,
    role, early, late, dollars}], early, late, minutes, dollars,
    unscheduled}."""
    import attendance
    import labor as _lab
    import schedule_time as _st
    import staff_settings as _ss
    from models import get_restaurant, get_role_rates, salaried_name_key
    iso = day.isoformat() if hasattr(day, "isoformat") else str(day)[:10]
    r = get_restaurant(restaurant_id, db_path) if db_path else get_restaurant(restaurant_id)
    if rows is None:
        import intraday
        rows = intraday.published_rows(restaurant_id, datetime.strptime(iso, "%Y-%m-%d").date(),
                                       **({"db_path": db_path} if db_path else {}))
        rows = [dict(x, date=iso) for x in rows]
    if not rows:
        return {"available": False, "reason": "no published schedule for the day"}
    if punches is None:
        conn = get_conn(db_path)
        try:
            punches = [dict(p) for p in conn.execute(
                "SELECT employee_id, employee_name, role, clock_in, clock_out, reg_rate FROM pos_punches "
                "WHERE restaurant_id=? AND business_date=? AND COALESCE(is_station,0)=0", (restaurant_id, iso))]
        finally:
            conn.close()
    if not punches:
        return {"available": False, "reason": "no punches archived for the day"}
    sal = attendance.salaried_keys(r)
    try:
        import people as _people
        canon = _people.canonical_names(restaurant_id, [p.get("employee_name") for p in punches if p.get("employee_name")]
                                        + [x.get("employee") for x in rows], **({"db_path": db_path} if db_path else {}))
    except Exception:
        canon = {}
    by_person = {}
    for x in rows:
        n = canon.get(x.get("employee"), x.get("employee")) or ""
        a, z = _st.shift_span(dict(x, date=iso))
        if a is not None and n:
            by_person.setdefault(_ss.name_key(n), []).append((a, z, x))
    rates = get_role_rates(restaurant_id, **({"db_path": db_path} if db_path else {})) or {}
    try:
        ppl, typ = _lab.person_rate_book(restaurant_id)
    except Exception:
        ppl, typ = {}, {}
    grace = timedelta(minutes=DRIFT_GRACE_MINUTES)
    out, unscheduled = {}, 0
    for p in punches:
        name = canon.get(p.get("employee_name"), p.get("employee_name")) or ""
        if not name or salaried_name_key(name) in sal:
            continue
        try:
            cin = datetime.strptime(str(p.get("clock_in") or "")[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        try:
            cout = datetime.strptime(str(p.get("clock_out") or "")[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            cout = None
        shifts = by_person.get(_ss.name_key(name)) or []
        if not shifts:
            if cout:
                unscheduled += int((cout - cin).total_seconds() // 60)
            continue
        a, z, x = min(shifts, key=lambda s: abs((s[0] - cin).total_seconds()))
        early = max(0, int((a - cin).total_seconds() // 60)) if cin < a - grace else 0
        late = max(0, int((cout - z).total_seconds() // 60)) if (cout and cout > z + grace) else 0
        if not (early or late):
            continue
        rate, _src = _lab._shift_rate_source({"employee": name, "role": x.get("role") or p.get("role"),
                                              "pay_rate": p.get("reg_rate")}, rates,
                                             float(rates.get("_default") or getattr(r, "hourly_rate", 0) or 0),
                                             ppl, typ)
        e = out.setdefault(_ss.name_key(name), {"employee": name, "role": x.get("role") or p.get("role") or "",
                                                 "early": 0, "late": 0, "dollars": 0.0})
        e["early"] += early
        e["late"] += late
        e["dollars"] = round(e["dollars"] + (early + late) / 60.0 * float(rate or 0), 2)
    people = sorted(out.values(), key=lambda e: -(e["early"] + e["late"]))
    return {"available": True, "people": people, "early": sum(e["early"] for e in people),
            "late": sum(e["late"] for e in people), "minutes": sum(e["early"] + e["late"] for e in people),
            "dollars": round(sum(e["dollars"] for e in people), 2), "unscheduled": unscheduled,
            "grace_minutes": DRIFT_GRACE_MINUTES}
