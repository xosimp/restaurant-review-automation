"""Delayed actions — the undo window for anything the product does on its
own behalf.

The automation audit found three outward-facing actions with no undo: a
sent supplier order, a published schedule, a sent campaign. A delayed queue
is the mechanism: the action is written here with an execute_at a few
minutes or hours out, the owner sees "Publishing Friday's schedule at 11am
— undo" in the activity feed and on the phone, and the scheduler runs it
when the time comes unless it was cancelled. Cancel is a status change;
nothing has left the building until run_due executes the row.

Only automations queue here today (auto-publish, trusted-supplier send).
Manual sends stay immediate — an owner who pressed Send expects it sent —
and the audit's undo for those is the next step, not this one.

Every execution runs as the automation actor, so the account activity log
names Cavnar AI, not the owner, as who did it.
"""
import json
from datetime import datetime, timedelta, timezone

from models import get_conn, DB_PATH

AUTOMATION_ACTOR = {"id": None, "username": "Cavnar AI", "is_admin": False, "role": "automation"}


from time_utils import utc_stamp as _utc

def _row(r):
    d = dict(r)
    try:
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
    except Exception:
        d["payload"] = {}
    try:
        d["result"] = json.loads(d.pop("result_json") or "null")
    except Exception:
        d["result"] = None
    ex = d.get("execute_at")
    d["execute_at"] = (str(ex).replace(" ", "T") + "Z") if ex and not str(ex).endswith("Z") else ex
    return d


import contextvars as _cv
_queued_at = _cv.ContextVar("delayed_queued_at", default=None)


def schedule(restaurant_id, kind, payload, delay_minutes, label=None, actor=None, db_path=DB_PATH):
    """Queue one action. Returns the row (with `id` and `execute_at`)."""
    if kind not in HANDLERS:
        raise ValueError(f"unknown delayed action kind {kind!r}")
    execute_at = _utc(datetime.now(timezone.utc) + timedelta(minutes=max(1, int(delay_minutes))))
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO delayed_actions (restaurant_id, kind, label, payload_json, execute_at, created_by) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, kind, (label or "")[:200] or None, json.dumps(payload or {}), execute_at,
             ((actor or AUTOMATION_ACTOR).get("username") or "")[:80]))
        conn.commit()
        row = conn.execute("SELECT * FROM delayed_actions WHERE id=?", (cur.lastrowid,)).fetchone()
        return _row(row)
    finally:
        conn.close()


def pending(restaurant_id, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM delayed_actions WHERE restaurant_id=? AND status='pending' ORDER BY execute_at ASC",
            (restaurant_id,)).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def cancel(restaurant_id, action_id, actor=None, db_path=DB_PATH):
    """The undo. True when a pending row was cancelled; False when it had
    already run, was cancelled, or belongs to another restaurant."""
    conn = get_conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE delayed_actions SET status='cancelled', executed_at=?, result_json=? "
            "WHERE id=? AND restaurant_id=? AND status='pending'",
            (_utc(), json.dumps({"cancelled_by": (actor or {}).get("username")}), action_id, restaurant_id))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def run_due(db_path=DB_PATH, now=None, limit=20):
    """Execute every pending row whose time has come. Each row is claimed
    with an atomic status flip, so two runners cannot both send it."""
    now_s = _utc(now)
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM delayed_actions WHERE status='pending' AND execute_at <= ? "
            "ORDER BY execute_at ASC LIMIT ?", (now_s, limit)).fetchall()
        ids = [r["id"] for r in rows]
    finally:
        conn.close()
    ran = failed = 0
    for aid in ids:
        conn = get_conn(db_path)
        try:
            cur = conn.execute("UPDATE delayed_actions SET status='running' WHERE id=? AND status='pending'", (aid,))
            conn.commit()
            claimed = cur.rowcount == 1
            row = conn.execute("SELECT * FROM delayed_actions WHERE id=?", (aid,)).fetchone()
        finally:
            conn.close()
        if not claimed or not row:
            continue
        action = _row(row)
        try:
            # When it was queued rides alongside (not in the payload): a
            # handler that promises "unchanged since it was queued" needs it.
            _token = _queued_at.set(row["created_at"])
            try:
                result = HANDLERS[action["kind"]](action["restaurant_id"], action["payload"], db_path)
            finally:
                _queued_at.reset(_token)
            ok = bool((result or {}).get("ok", True))
            status = "done" if ok else "failed"
            ran += 1 if ok else 0
            failed += 0 if ok else 1
        except Exception as e:
            import ops
            ops.capture(e, job=f"delayed:{action['kind']}", context=f"restaurant_id={action['restaurant_id']} id={aid}")
            result, status = {"ok": False, "error": str(e)[:300]}, "failed"
            failed += 1
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE delayed_actions SET status=?, executed_at=?, result_json=? WHERE id=?",
                         (status, _utc(), json.dumps(result, default=str)[:4000], aid))
            conn.commit()
        finally:
            conn.close()
    return {"ran": ran, "failed": failed}


# ── handlers ────────────────────────────────────────────────────────────────

def _run_schedule_publish(restaurant_id, payload, db_path):
    """Publish the queued week — unless it was edited after it was queued.
    The queue-time checks (unedited, unpublished) are two hours old when
    this runs; a week the manager changed in the window goes out only when
    they publish it themselves (DATA-12, SCHED-28). An already-published
    week is refused by _publish_schedule's own claim."""
    from client_api import _publish_schedule
    queued_at = _queued_at.get()
    if payload.get("schedule_id") and queued_at:
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT edited_at FROM schedule_history WHERE id=? AND restaurant_id=?",
                             (payload["schedule_id"], restaurant_id)).fetchone()
        finally:
            conn.close()
        if r and r["edited_at"] and str(r["edited_at"]) >= str(queued_at):
            return {"ok": False, "error": "The week was changed after it was queued, so it was not sent. "
                                          "Publish it from the Labor tab when it's ready."}
    out, _status = _publish_schedule(restaurant_id, payload.get("schedule_id"), AUTOMATION_ACTOR,
                                     acknowledge=bool(payload.get("acknowledge")))
    return out


def _run_order_send(restaurant_id, payload, db_path):
    """Send the supplier order the draft proposed — but only if the draft is
    still the one the owner saw. A stock or supplier change in the window
    voids the send rather than surprising anyone."""
    from inventory import build_supplier_orders
    from client_api import _send_supplier_orders
    from models import get_restaurant
    draft = build_supplier_orders(restaurant_id)
    only = (payload.get("supplier_email") or "").lower()
    groups = [g for g in (draft.get("groups") or []) if not only or (g.get("supplier_email") or "").lower() == only]
    # The payload carries the supplier's own hash (the whole draft's, on a
    # row queued before that existed): a count moving another supplier's
    # lines no longer voids this one (MOD-FC-10).
    expected = payload.get("draft_hash")
    if expected and expected not in ({draft.get("draft_hash")} | {g.get("draft_hash") for g in groups}):
        out = {"ok": False, "error": "The order changed before it was sent — nothing went out."}
        _tell_owner_order_not_sent(restaurant_id, payload, out["error"], db_path)
        return out
    if not groups:
        out = {"ok": False, "error": "Nothing left to order."}
        _tell_owner_order_not_sent(restaurant_id, payload, out["error"], db_path)
        return out
    sent, failed = _send_supplier_orders(restaurant_id, get_restaurant(restaurant_id), groups, AUTOMATION_ACTOR,
                                         resend=bool(payload.get("resend")))
    if not sent:
        _tell_owner_order_not_sent(restaurant_id, payload,
                                   (failed[0].get("error") if failed else None) or "It could not be sent.", db_path)
    return {"ok": bool(sent), "sent": sent, "failed": failed}


def _tell_owner_order_not_sent(restaurant_id, payload, reason, db_path):
    """A queued or automatic supplier order that did not go out. The owner
    was told "goes out in an hour"; a void used to be stored as `failed` and
    nothing else, so the delivery simply never came (MOD-FC-10). The bell
    row is written whatever happens; the push/email goes the way the
    announcement did (strategy_jobs._reach)."""
    who = payload.get("supplier_email") or "your supplier"
    title = "A supplier order did not go out"
    body = f"The order for {who} was not sent. {reason} Review it in Food Cost and send it from there."
    try:
        import morning_brief, notify, strategy_jobs
        if morning_brief.recipients(restaurant_id, db_path):
            strategy_jobs._reach(restaurant_id, "order_send_voided", title, body,
                                 {"supplier_email": payload.get("supplier_email")}, db_path,
                                 subject="A supplier order did not go out")
        else:
            notify.record_notification(restaurant_id, "order_send_voided", db_path=db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="order_send_voided", context=f"restaurant_id={restaurant_id}")


HANDLERS = {
    "schedule_publish": _run_schedule_publish,
    "order_send": _run_order_send,
}
