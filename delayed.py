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
            result = HANDLERS[action["kind"]](action["restaurant_id"], action["payload"], db_path)
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
    from client_api import _publish_schedule
    out, _status = _publish_schedule(restaurant_id, payload.get("schedule_id"), AUTOMATION_ACTOR)
    return out


def _run_order_send(restaurant_id, payload, db_path):
    """Send the supplier order the draft proposed — but only if the draft is
    still the one the owner saw. A stock or supplier change in the window
    voids the send rather than surprising anyone."""
    from inventory import build_supplier_orders
    from client_api import _send_supplier_orders
    from models import get_restaurant
    draft = build_supplier_orders(restaurant_id)
    if payload.get("draft_hash") and draft.get("draft_hash") != payload.get("draft_hash"):
        return {"ok": False, "error": "The order changed before it was sent — nothing went out."}
    only = (payload.get("supplier_email") or "").lower()
    groups = [g for g in (draft.get("groups") or []) if not only or (g.get("supplier_email") or "").lower() == only]
    if not groups:
        return {"ok": False, "error": "Nothing left to order."}
    sent, failed = _send_supplier_orders(restaurant_id, get_restaurant(restaurant_id), groups, AUTOMATION_ACTOR)
    return {"ok": bool(sent), "sent": sent, "failed": failed}


HANDLERS = {
    "schedule_publish": _run_schedule_publish,
    "order_send": _run_order_send,
}
