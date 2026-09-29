"""
offboarding.py — closing an account, as an audited checklist.

"Close my account" (web and iOS, client_api._do_request_account_deletion)
records restaurants.deletion_requested_at and notifies Will. Nothing in the
console read that flag, nothing could withdraw it, and the only delete the
console had refused anything that was not a demo — while App Store Guideline
5.1.1(v) and docs/app-store-submission.md promise the request is carried out
(#34). This module is the rest of that path:

  * checklist(rid)          — the request, its 30-day due date and the steps:
                              Stripe, DocuSign, integrations, export — then the
                              delete. A step with nothing to do (no Stripe
                              customer, no open envelope, no stored credential)
                              reads 'not_needed' without a click.
  * set_step(...)           — mark a step done or skipped (a skip needs a
                              reason); 'integrations' runs revoke_integrations.
  * revoke_integrations(...)— clears every Google, Meta, POS and other stored
                              credential and turns outbound webhooks off, with
                              an audit row (#138).
  * withdraw_deletion_request(...) — the owner changed their mind: clears the
                              flag (it was write-once, and the review fetch
                              skips a flagged restaurant).
  * delete_restaurant_now(...) — models.delete_restaurant behind the typed name
                              and a finished checklist. The step-up
                              (auth.recent_auth_required) is applied to its
                              route in the integration wave.

Every step is recorded twice on purpose: offboarding_steps is the checklist's
state, and admin_events.record_admin_action is the audit trail. Both survive
the delete (models._KEEP_ON_RESTAURANT_DELETE), so the record that an account
was wound down outlives the account.

A checklist belongs to one request ("cycle" = the deletion_requested_at it was
worked under, '' when an admin offboards without a request), so a withdrawn
request's ticks never carry into a later one.
"""
import json
from datetime import datetime, timedelta, timezone

DELETION_NOTICE_DAYS = 30

# (key, label, what the operator does). Billing and contract actions live with
# the billing workstream; until they are wired in, these two are marked by
# hand once done in Stripe / DocuSign.
STEPS = (
    ("stripe", "Cancel the Stripe subscription",
     "Cancel the subscription in Stripe (no further invoices), then mark this done."),
    ("docusign", "Void any open DocuSign envelope",
     "Void the envelope still waiting for a signature, then mark this done."),
    ("integrations", "Revoke Google, Meta, POS and webhook connections",
     "Clears every stored credential for this restaurant and turns its outbound webhooks off."),
    ("export", "Send the owner their data export",
     "Mark done with where it went, or skip with the reason (for example, they declined)."),
)
STEP_KEYS = tuple(k for k, _l, _h in STEPS)
DELETE_STEP = "delete"
STATUSES = ("pending", "done", "skipped")

_SCHEMA = """CREATE TABLE IF NOT EXISTS offboarding_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    cycle         TEXT    NOT NULL DEFAULT '',
    step          TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    actor         TEXT,
    note          TEXT,
    detail_json   TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(restaurant_id, cycle, step)
)"""

# The credentials a restaurant row holds on a client's behalf, by
# integration: the same fields each one's own disconnect clears
# (auth_routes.gmb_disconnect, mobile_api.mobile_instagram_disconnect,
# toast/square/clover/rpower_routes' admin disconnects), plus the two API
# keys with no disconnect of their own.
INTEGRATION_FIELDS = {
    "google": ("gmb_access_token", "gmb_refresh_token", "gmb_token_expires", "gmb_account_id", "gmb_location_id"),
    "meta": ("ig_token", "ig_user_id", "ig_token_expires", "fb_page_token", "fb_page_id", "fb_token_expires"),
    "toast": ("toast_client_id", "toast_client_secret", "toast_restaurant_guid", "toast_access_token",
              "toast_token_expires", "toast_last_synced", "toast_sync_error"),
    "square": ("square_access_token", "square_location_id", "square_last_synced", "square_sync_error"),
    "clover": ("clover_merchant_id", "clover_api_token", "clover_last_synced", "clover_sync_error"),
    "rpower": ("rpower_token", "rpower_cg", "rpower_store_mid", "rpower_store_name", "rpower_verified_at",
               "rpower_sync_error", "rpower_last_synced"),
    "backoffice": ("backoffice_api_key",),
    "reservations": ("reservation_api_key",),
}
# The field(s) whose presence means "connected" — never their values.
_CREDENTIAL_OF = {
    "google": ("gmb_refresh_token", "gmb_access_token"),
    "meta": ("ig_token", "fb_page_token"),
    "toast": ("toast_client_secret", "toast_access_token", "toast_client_id"),
    "square": ("square_access_token",),
    "clover": ("clover_api_token",),
    "rpower": ("rpower_token",),
    "backoffice": ("backoffice_api_key",),
    "reservations": ("reservation_api_key",),
}


def init_offboarding(db_path=None):
    """Boot-time schema (called by models.init_db)."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        conn.execute(_SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_offboarding_steps_rest ON offboarding_steps(restaurant_id, cycle)")
        conn.commit()
    finally:
        conn.close()


def _conn(db_path=None):
    from models import get_conn, DB_PATH
    return get_conn(db_path or DB_PATH)


def _raw_restaurant(restaurant_id, db_path=None):
    """The raw row (credentials still as stored: only their presence is read
    here), or None. deletion_requested_at is not a dataclass field."""
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM restaurants WHERE id=?", (int(restaurant_id),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _webhook_active(restaurant_id, db_path=None) -> bool:
    conn = _conn(db_path)
    try:
        return conn.execute("SELECT 1 FROM webhooks WHERE restaurant_id=? AND is_active=1 LIMIT 1",
                            (int(restaurant_id),)).fetchone() is not None
    except Exception:
        return False
    finally:
        conn.close()


def connected_integrations(row, restaurant_id=None, db_path=None) -> dict:
    """{integration: bool} from a raw restaurant row, plus 'webhooks'."""
    out = {name: any(row.get(c) not in (None, "") for c in cols) for name, cols in _CREDENTIAL_OF.items()}
    out["webhooks"] = _webhook_active(restaurant_id or row.get("id"), db_path=db_path)
    return out


def _due(requested_at, now=None):
    """(requested, due, whole days left) for a request stamp — aware UTC
    datetimes — or (None, None, None). Days left is floored, so it turns
    negative the moment the notice period has run out."""
    try:
        from time_utils import parse_stamp
        dt = parse_stamp(requested_at, naive_tz="UTC")
    except Exception:
        dt = None
    if dt is None:
        return None, None, None
    due = dt + timedelta(days=DELETION_NOTICE_DAYS)
    return dt, due, (due - (now or datetime.now(timezone.utc))).days


def deletion_status(restaurant_id, db_path=None):
    """The open deletion request and its due date, or None when there is
    none. For the console's issue (workstream C) and the client page."""
    row = _raw_restaurant(restaurant_id, db_path=db_path)
    if not row or not row.get("deletion_requested_at"):
        return None
    return _request_view(row)


def _request_view(row):
    """The request as the console shows it: stamps in UTC, dates as M/D/YY
    on the restaurant's own clock (an evening request in the Americas is
    already tomorrow in UTC)."""
    from time_utils import mdy, restaurant_tz
    requested_at = row.get("deletion_requested_at")
    requested, due, left = _due(requested_at)
    try:
        tz = restaurant_tz(row.get("timezone") or "America/Chicago")
    except Exception:
        tz = timezone.utc
    return {"deletion_requested_at": requested_at,
            "requested_on": mdy(requested.astimezone(tz)) if requested else "",
            "due_at": due.strftime("%Y-%m-%d %H:%M:%S") if due else None,
            "due_on": mdy(due.astimezone(tz)) if due else "",
            "days_left": left,
            "overdue": bool(left is not None and left < 0)}


def _not_needed(step, row, connected):
    """Why this step has nothing to do on this account, or None."""
    status = (row.get("billing_status") or "").lower()
    if step == "stripe":
        if not (row.get("stripe_customer_id") or "").strip():
            return "No Stripe customer on this account."
        if status in ("churned", "canceled"):
            return "Stripe already reported the subscription ended."
    if step == "docusign" and (row.get("contract_status") or "") != "sent":
        return "No envelope is waiting for a signature."
    if step == "integrations" and not any(connected.values()):
        return "No integration credentials or active webhooks are stored."
    if step == "export" and int(row.get("is_demo") or 0) == 1:
        return "Demo account: there is no owner data to export."
    return None


def _stored_steps(restaurant_id, cycle, db_path=None):
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM offboarding_steps WHERE restaurant_id=? AND cycle=?",
                            (int(restaurant_id), cycle)).fetchall()
        return {r["step"]: dict(r) for r in rows}
    finally:
        conn.close()


def checklist(restaurant_id, db_path=None):
    """The offboarding checklist for one restaurant, or None if there is no
    such restaurant. ready_to_delete is True once every step is done,
    skipped or not needed."""
    row = _raw_restaurant(restaurant_id, db_path=db_path)
    if not row:
        return None
    cycle = row.get("deletion_requested_at") or ""
    stored = _stored_steps(restaurant_id, cycle, db_path=db_path)
    connected = connected_integrations(row, restaurant_id, db_path=db_path)
    steps = []
    for key, label, hint in STEPS:
        s = stored.get(key)
        item = {"step": key, "label": label, "hint": hint, "status": "pending", "actor": None,
                "note": None, "updated_at": None, "detail": None}
        if s:
            item.update(status=s["status"], actor=s["actor"], note=s["note"], updated_at=s["updated_at"],
                        detail=json.loads(s["detail_json"]) if s.get("detail_json") else None)
        if item["status"] == "pending":
            why = _not_needed(key, row, connected)
            if why:
                item.update(status="not_needed", note=why)
        steps.append(item)
    outstanding = [s["step"] for s in steps if s["status"] not in ("done", "skipped", "not_needed")]
    out = {"ok": True, "restaurant_id": int(restaurant_id), "name": row.get("name"),
           "is_demo": int(row.get("is_demo") or 0), "billing_status": row.get("billing_status"),
           "request": _request_view(row) if cycle else None,
           "connected": connected, "steps": steps, "outstanding": outstanding,
           "ready_to_delete": not outstanding}
    return out


def _upsert_step(restaurant_id, cycle, step, status, actor_name, note=None, detail=None, db_path=None):
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO offboarding_steps (restaurant_id, cycle, step, status, actor, note, detail_json) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT(restaurant_id, cycle, step) DO UPDATE SET "
            "status=excluded.status, actor=excluded.actor, note=excluded.note, "
            "detail_json=excluded.detail_json, updated_at=datetime('now')",
            (int(restaurant_id), cycle, step, status, actor_name, (note or "")[:500] or None,
             json.dumps(detail, default=str)[:4000] if detail is not None else None))
        conn.commit()
    finally:
        conn.close()


def _actor_name(actor):
    if isinstance(actor, dict):
        return actor.get("username") or actor.get("email") or f"user:{actor.get('id')}"
    return str(actor or "admin")[:120]


def set_step(restaurant_id, step, status, actor, note=None, db_path=None):
    """Mark one step. Returns (payload, http_status). 'integrations' marked
    done runs revoke_integrations first and stores what it cleared; a skip
    needs a reason."""
    if step not in STEP_KEYS:
        return {"ok": False, "error": "Unknown step."}, 400
    if status not in STATUSES:
        return {"ok": False, "error": "Status must be done, skipped or pending."}, 400
    note = (note or "").strip()
    if status == "skipped" and not note:
        return {"ok": False, "error": "Say why this step is being skipped."}, 400
    row = _raw_restaurant(restaurant_id, db_path=db_path)
    if not row:
        return {"ok": False, "error": "Restaurant not found."}, 404
    cycle = row.get("deletion_requested_at") or ""
    before = (_stored_steps(restaurant_id, cycle, db_path=db_path).get(step) or {}).get("status") or "pending"
    detail = None
    if step == "integrations" and status == "done":
        detail = revoke_integrations(restaurant_id, actor, db_path=db_path)
        if not detail.get("ok"):
            return {"ok": False, "error": detail.get("error") or "The integrations could not be revoked."}, 500
    _upsert_step(restaurant_id, cycle, step, status, _actor_name(actor), note=note, detail=detail, db_path=db_path)
    import admin_events
    admin_events.record_admin_action(actor, f"offboarding.{step}", restaurant_id=int(restaurant_id),
                                     target=f"offboarding:{step}", before={"status": before},
                                     after={"status": status, "note": note or None}, db_path=db_path)
    payload = checklist(restaurant_id, db_path=db_path)
    if detail is not None:
        payload["revoked"] = detail
    return payload, 200


def revoke_integrations(restaurant_id, actor, db_path=None):
    """Clear every stored integration credential for this restaurant and
    turn its outbound webhooks off, with an audit row. Idempotent.

    Only our copy is cleared. The provider-side grant is deliberately NOT
    revoked from here: a multi-location owner's Google or Meta grant is one
    grant across their locations, so revoking it at Google for the location
    that is leaving would disconnect the ones that are staying. The owner
    removes Cavnar AI's access in their Google/Meta account settings when
    the whole brand leaves.

    Returns {ok, was_connected, cleared: {integration: [fields]}, webhooks_disabled}."""
    row = _raw_restaurant(restaurant_id, db_path=db_path)
    if not row:
        return {"ok": False, "error": "Restaurant not found."}
    connected = connected_integrations(row, restaurant_id, db_path=db_path)
    updates, cleared = {}, {}
    for name, fields in INTEGRATION_FIELDS.items():
        hit = [f for f in fields if f in row and row.get(f) not in (None, "")]
        if hit:
            cleared[name] = hit
            for f in hit:
                updates[f] = None
    import models
    if updates:
        # Through update_restaurant: the encrypted fields, the per-request
        # cache and row_version all follow the one write path.
        models.update_restaurant(int(restaurant_id), updates, db_path=db_path or models.DB_PATH)
    webhooks_disabled = False
    if connected.get("webhooks"):
        conn = _conn(db_path)
        try:
            conn.execute("UPDATE webhooks SET is_active=0, disabled_reason=? WHERE restaurant_id=? AND is_active=1",
                         ("offboarding: integrations revoked", int(restaurant_id)))
            conn.commit()
            webhooks_disabled = True
        finally:
            conn.close()
    result = {"ok": True, "was_connected": sorted(k for k, v in connected.items() if v),
              "cleared": cleared, "webhooks_disabled": webhooks_disabled}
    import admin_events
    admin_events.record_admin_action(
        actor, "integrations.revoked", restaurant_id=int(restaurant_id), target=f"restaurant:{int(restaurant_id)}",
        before={"connected": connected},
        after={"cleared": {k: len(v) for k, v in cleared.items()}, "webhooks_disabled": webhooks_disabled},
        summary=f"{_actor_name(actor)} revoked integrations: "
                + (", ".join(sorted(cleared)) or "nothing stored")
                + (" + webhooks off" if webhooks_disabled else ""),
        db_path=db_path)
    return result


def withdraw_deletion_request(restaurant_id, actor, note=None, db_path=None):
    """Clear an open deletion request. Returns (payload, http_status).
    Compare-and-set on the stamp, so a withdraw can't clear a newer request
    that arrived after the admin loaded the page."""
    row = _raw_restaurant(restaurant_id, db_path=db_path)
    if not row:
        return {"ok": False, "error": "Restaurant not found."}, 404
    requested_at = row.get("deletion_requested_at")
    if not requested_at:
        return {"ok": False, "error": "There is no open deletion request for this restaurant."}, 409
    conn = _conn(db_path)
    try:
        cur = conn.execute("UPDATE restaurants SET deletion_requested_at=NULL, row_version=COALESCE(row_version,0)+1 "
                           "WHERE id=? AND deletion_requested_at=?", (int(restaurant_id), requested_at))
        conn.commit()
        changed = cur.rowcount == 1
    finally:
        conn.close()
    if not changed:
        return {"ok": False, "error": "The request changed while you were looking at it. Reload and try again."}, 409
    import models
    models._invalidate_request_cache(int(restaurant_id))
    try:
        models.log_event(int(restaurant_id), "deletion_withdrawn",
                         {"actor": _actor_name(actor), "requested_at": requested_at, "note": (note or "")[:300] or None},
                         db_path=db_path or models.DB_PATH)
    except Exception:
        pass
    import admin_events
    admin_events.record_admin_action(actor, "deletion_request.withdrawn", restaurant_id=int(restaurant_id),
                                     target=f"restaurant:{int(restaurant_id)}",
                                     before={"deletion_requested_at": requested_at},
                                     after={"deletion_requested_at": None, "note": (note or "").strip() or None},
                                     db_path=db_path)
    return {"ok": True, "withdrawn": requested_at,
            "message": "The deletion request is withdrawn. Review fetching and the account carry on as before."}, 200


def admin_homes(restaurant_id, db_path=None) -> int:
    """How many admin logins call this restaurant home. Deleting it would
    delete them (models.delete_restaurant removes its users rows)."""
    conn = _conn(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM users WHERE restaurant_id=? AND is_admin=1",
                            (int(restaurant_id),)).fetchone()[0]
    finally:
        conn.close()


def delete_restaurant_now(restaurant_id, actor, confirm_name, db_path=None):
    """Delete a restaurant — real or demo — once its checklist is finished.
    Returns (payload, http_status). Gates, all server-side: the exact name,
    no admin login lives on it, every checklist step done/skipped/not needed.
    The route adds admin-only and (integration wave) the step-up."""
    row = _raw_restaurant(restaurant_id, db_path=db_path)
    if not row:
        return {"ok": False, "error": "Restaurant not found."}, 404
    if (confirm_name or "").strip() != (row.get("name") or "").strip():
        return {"ok": False, "error": "The name did not match. Nothing was deleted."}, 400
    if admin_homes(restaurant_id, db_path=db_path):
        return {"ok": False, "error": "An admin login lives on this restaurant; deleting it would delete that "
                                      "login. Move the admin first. Nothing was deleted."}, 409
    state = checklist(restaurant_id, db_path=db_path)
    if state["outstanding"]:
        labels = {k: l for k, l, _h in STEPS}
        return {"ok": False, "outstanding": state["outstanding"],
                "error": "Finish the offboarding checklist first: "
                         + "; ".join(labels[k] for k in state["outstanding"]) + ". Nothing was deleted."}, 409
    cycle = row.get("deletion_requested_at") or ""
    before = {"name": row.get("name"), "billing_status": row.get("billing_status"), "is_demo": int(row.get("is_demo") or 0),
              "deletion_requested_at": row.get("deletion_requested_at"), "created_at": row.get("created_at"),
              "steps": {s["step"]: s["status"] for s in state["steps"]}}
    import models
    deleted = models.delete_restaurant(int(restaurant_id), db_path=db_path or models.DB_PATH)
    rows = sum(deleted.values())
    _upsert_step(restaurant_id, cycle, DELETE_STEP, "done", _actor_name(actor),
                 note=f"{rows} rows deleted", detail={"tables": deleted}, db_path=db_path)
    import admin_events
    admin_events.record_admin_action(actor, "restaurant.deleted", restaurant_id=int(restaurant_id),
                                     target=f"restaurant:{int(restaurant_id)}", before=before,
                                     after={"rows": rows, "tables": deleted},
                                     summary=f"{_actor_name(actor)} deleted {row.get('name')} (#{int(restaurant_id)}), "
                                             f"{rows} rows", db_path=db_path)
    return {"ok": True, "restaurant_id": int(restaurant_id), "rows": rows, "tables": deleted}, 200
