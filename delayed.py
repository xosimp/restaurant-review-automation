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


def pending(restaurant_id, db_path=DB_PATH, sees_food=True):
    """The queued actions, soonest first. `sees_food=False` (a login
    without FOOD_COST_VIEW) reads a supplier order's label without its
    dollar total — the order's cost is food-cost money (for_viewer)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM delayed_actions WHERE restaurant_id=? AND status='pending' ORDER BY execute_at ASC",
            (restaurant_id,)).fetchall()
        return [for_viewer(_row(r), sees_food) for r in rows]
    finally:
        conn.close()


import re as _re

# "Sending the Sysco order ($1,234, 5 items)" — the dollar part of the label
# client_api.send_supplier_orders writes for a queued order.
_ORDER_DOLLARS_RE = _re.compile(r"\(\s*-?\$[\d,]+(?:\.\d+)?\s*,\s*")
_ANY_DOLLARS_RE = _re.compile(r"\s*\(?\s*-?\$[\d,]+(?:\.\d+)?\s*\)?")


def for_viewer(action, sees_food=True):
    """`action` as a login may read it. A supplier order's total is what
    the restaurant pays for food — a manager without food cost reads "the
    Sysco order (5 items)", never the dollars (the margins rule)."""
    if sees_food or not action or action.get("kind") != "order_send":
        return action
    out = dict(action)
    label = out.get("label") or ""
    if label:
        label = _ORDER_DOLLARS_RE.sub("(", label)
        out["label"] = _ANY_DOLLARS_RE.sub("", label).strip()
    return out


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


# A row still 'running' this long after it was claimed belongs to a process
# that died mid-handler (a deploy's SIGTERM kills the scheduler's daemon
# thread without its finally blocks). Longer than any handler takes.
RUNNING_STALE_MINUTES = 30

# One run_due pass stops taking new rows after this long, so it cannot hold
# the scheduler past its tick; what is left is still due, oldest first, on
# the next pass.
RUN_DUE_MAX_SECONDS = 240
_BATCH = 50


def reap_interrupted(db_path=DB_PATH):
    """Mark actions left 'running' by a dead process as failed, with a
    reason, and report them. They used to stay 'running' forever: no reaper,
    no failure recorded, gone from the owner's feed (DATA-19). Not re-run:
    a publish or an order killed mid-send may have partly gone out, and
    sending it again blind is worse than asking (marketing_publish.
    reap_stuck_publishes is the same stance). Returns how many."""
    cutoff = _utc(datetime.now(timezone.utc) - timedelta(minutes=RUNNING_STALE_MINUTES))
    message = ("Interrupted by a restart while it was running, so it may have partly gone out. "
               "Check before doing it again — it was not retried automatically.")
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, restaurant_id, kind FROM delayed_actions WHERE status='running' "
            "AND COALESCE(executed_at, execute_at) < ?", (cutoff,)).fetchall()
        reaped = []
        for r in rows:
            cur = conn.execute(
                "UPDATE delayed_actions SET status='failed', executed_at=?, result_json=? "
                "WHERE id=? AND status='running'",
                (_utc(), json.dumps({"ok": False, "error": message, "interrupted": True}), r["id"]))
            if cur.rowcount == 1:
                reaped.append(dict(r))
        conn.commit()
    finally:
        conn.close()
    if reaped:
        import ops
        for r in reaped:
            ops.capture(RuntimeError("delayed action interrupted mid-run"), job=f"delayed:{r['kind']}",
                        context=f"restaurant_id={r['restaurant_id']} id={r['id']}")
    return len(reaped)


def _out_of_service(restaurant_id, db_path):
    """True when the account was cancelled, paused or asked to be deleted
    inside the undo window: its queued publish or supplier order must not
    go out in its name (DATA-51)."""
    import models
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT billing_status, deletion_requested_at FROM restaurants WHERE id=?",
                           (restaurant_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return True
    return (not models.in_service(row)) or bool(row["deletion_requested_at"])


def run_due(db_path=DB_PATH, now=None, limit=None):
    """Execute every pending row whose time has come, oldest first. Each row
    is claimed with an atomic status flip, so two runners cannot both send
    it. Drains the whole backlog in batches, bounded by RUN_DUE_MAX_SECONDS
    (or `limit` rows): 20 a tick was 240 an hour, so a Friday-11am burst of
    auto-publishes went out over hours (MOD-PERF-4). Rows a dead process
    left 'running' are reaped first (reap_interrupted)."""
    import time as _time
    now_s = _utc(now)
    try:
        reap_interrupted(db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="delayed_reap")
    started = _time.monotonic()
    ran = failed = skipped = taken = 0
    while True:
        if limit is not None and taken >= limit:
            break
        if _time.monotonic() - started > RUN_DUE_MAX_SECONDS:
            break
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT id FROM delayed_actions WHERE status='pending' AND execute_at <= ? "
                "ORDER BY execute_at ASC, id ASC LIMIT ?", (now_s, _BATCH)).fetchall()
            ids = [r["id"] for r in rows]
        finally:
            conn.close()
        if not ids:
            break
        for aid in ids:
            if limit is not None and taken >= limit:
                break
            taken += 1
            outcome = _run_one(aid, db_path)
            ran += outcome == "done"
            failed += outcome == "failed"
            skipped += outcome == "cancelled"
    out = {"ran": ran, "failed": failed}
    if skipped:
        out["cancelled"] = skipped
    return out


def _run_one(aid, db_path):
    """Claim and execute one row. Returns its final status, or None if
    another runner had it."""
    conn = get_conn(db_path)
    try:
        # executed_at is the claim time while 'running' (reap_interrupted);
        # it is overwritten with the finish time below.
        cur = conn.execute("UPDATE delayed_actions SET status='running', executed_at=? "
                           "WHERE id=? AND status='pending'", (_utc(), aid))
        conn.commit()
        claimed = cur.rowcount == 1
        row = conn.execute("SELECT * FROM delayed_actions WHERE id=?", (aid,)).fetchone()
    finally:
        conn.close()
    if not claimed or not row:
        return None
    action = _row(row)
    if _out_of_service(action["restaurant_id"], db_path):
        result, status = {"ok": False, "error": "The account is no longer active, so this was not sent."}, "cancelled"
    else:
        result, status = _execute(action, row, aid, db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE delayed_actions SET status=?, executed_at=?, result_json=? WHERE id=?",
                     (status, _utc(), json.dumps(result, default=str)[:4000], aid))
        conn.commit()
    finally:
        conn.close()
    return status


def _execute(action, row, aid, db_path):
    """Run the row's handler. Returns (result, status)."""
    try:
        # When it was queued rides alongside (not in the payload): a
        # handler that promises "unchanged since it was queued" needs it.
        _token = _queued_at.set(row["created_at"])
        try:
            result = HANDLERS[action["kind"]](action["restaurant_id"], action["payload"], db_path)
        finally:
            _queued_at.reset(_token)
        ok = bool((result or {}).get("ok", True))
        return result, ("done" if ok else "failed")
    except Exception as e:
        import ops
        ops.capture(e, job=f"delayed:{action['kind']}", context=f"restaurant_id={action['restaurant_id']} id={aid}")
        return {"ok": False, "error": str(e)[:300]}, "failed"


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
    # The gate runs again now, against today's data (NS5 H3). A person's
    # queued publish carries the blocker keys they acknowledged, and only
    # those; an older row carrying acknowledge=True is read the same way it
    # was queued. Auto-publish acknowledged nothing, and the soft flags a
    # person decides (HOLD_UNATTENDED) hold it too.
    ack = payload.get("acknowledge")
    if isinstance(ack, list):
        acknowledge = ack
    elif "acknowledge" in payload or payload.get("manual"):
        acknowledge = True if ack else []      # a person's publish, queued before keys were stored
    else:
        acknowledge = False                    # auto-publish: nothing acknowledged, unattended
    out, _status = _publish_schedule(restaurant_id, payload.get("schedule_id"), AUTOMATION_ACTOR,
                                     acknowledge=acknowledge)
    if out.get("needs_ack"):
        _tell_owner_schedule_held(restaurant_id, payload, out.get("new_blockers") or out.get("blockers") or [], db_path)
        out = dict(out, ok=False, error="The week was not sent: " + "; ".join((out.get("new_blockers")
                                                                             or out.get("blockers") or [])[:3]) + ".")
    return out


def _run_schedule_changes_send(restaurant_id, payload, db_path):
    """Tell the people a saved change moved on a week staff already have
    (client_api.send_schedule_changes) — through the owner's undo window,
    like the first send (F2-2). A week edited again inside the window is
    not sent: the people and shifts it would tell are no longer the ones
    the manager saw when they pressed Send."""
    from client_api import send_schedule_changes
    if payload.get("schedule_id") and payload.get("version") is not None:
        # By version, not by clock: a save in the same second as the press
        # is the edit the person was sending, not one made after it.
        conn = get_conn(db_path)
        try:
            r = conn.execute("SELECT MAX(version) AS v FROM schedule_versions WHERE history_id=? AND restaurant_id=?",
                             (payload["schedule_id"], restaurant_id)).fetchone()
        finally:
            conn.close()
        if r and r["v"] is not None and int(r["v"]) != int(payload["version"]):
            return {"ok": False, "error": "The week was changed again after it was queued, so the changes were "
                                          "not sent. Send them from the Labor tab when it's ready."}
    ack = payload.get("acknowledge")
    out, _status = send_schedule_changes(restaurant_id, payload.get("schedule_id"), AUTOMATION_ACTOR,
                                         acknowledge=ack if isinstance(ack, list) else [])
    if out.get("needs_ack"):
        held = out.get("new_blockers") or out.get("blockers") or []
        _tell_owner_schedule_held(restaurant_id, payload, held, db_path,
                                  title="Your schedule changes were not sent")
        out = dict(out, ok=False, error="The changes were not sent: " + "; ".join(held[:3]) + ".")
    return out


def _tell_owner_schedule_held(restaurant_id, payload, blockers, db_path, title=None):
    """A queued publish the gate held at send time. The owner was told
    "goes to staff at 11am"; without this the week simply never arrived."""
    title = title or "Next week's schedule was not sent"
    body = ("It was held when its send time came: " + "; ".join(blockers[:3])
            + ". Review it on the Labor tab and send it yourself.")
    try:
        import strategy_jobs
        strategy_jobs._reach(restaurant_id, "schedule_publish_held", title, body,
                             {"schedule_id": payload.get("schedule_id")}, db_path,
                             subject=title)
    except Exception as e:
        import ops
        ops.capture(e, job="schedule_publish_held", context=f"restaurant_id={restaurant_id}")


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
    # The owner's edited quantities (friction audit U2-15), applied only
    # after the draft they edited proved unchanged above.
    drafts = None
    if payload.get("lines") and len(groups) == 1:
        from inventory import apply_order_edits
        try:
            edited = apply_order_edits(groups[0], payload["lines"])
        except ValueError:
            edited = None
        if not edited or not edited["items"]:
            out = {"ok": False, "error": "The edited order had nothing left to send."}
            _tell_owner_order_not_sent(restaurant_id, payload, out["error"], db_path)
            return out
        drafts = {groups[0]["supplier_email"].lower(): groups[0]["items"]}
        groups = [edited]
    # An order the trusted-supplier rule queued is 'automatic'; one the
    # owner sent through their own undo window is theirs (ordering.supplier_trust).
    sent, failed = _send_supplier_orders(restaurant_id, get_restaurant(restaurant_id), groups, AUTOMATION_ACTOR,
                                         resend=bool(payload.get("resend")),
                                         source="automatic" if payload.get("automatic") else "owner",
                                         drafts=drafts)
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
    "schedule_changes_send": _run_schedule_changes_send,
    "order_send": _run_order_send,
}
