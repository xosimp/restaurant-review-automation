"""
toast_routes.py — Flask routes for Toast POS integration
Blueprint: toast_bp
Registered in hosted_dashboard.py alongside the other blueprints.

Admin endpoints (admin_required, restaurant_id in URL):
  POST /admin/toast/save/<restaurant_id>
  POST /admin/toast/sync/<restaurant_id>
  GET  /admin/toast/status/<restaurant_id>
  POST /admin/toast/disconnect/<restaurant_id>

Client endpoints (login_required, scoped to session restaurant):
  GET  /api/toast/status
  POST /api/toast/save
  POST /api/toast/sync
  POST /api/toast/disconnect
"""
from flask import Blueprint, request, jsonify
from auth import admin_required, login_required, recent_auth_required
from models import update_restaurant

toast_bp = Blueprint("toast", __name__)
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(toast_bp)
# Its /admin writes (credentials, sync, disconnect) are in the audit trail (#126).
import admin_events as _admin_events
_admin_events.register_audit(toast_bp)


@toast_bp.route("/admin/toast/save/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def save_toast_credentials(restaurant_id, current_user):
    data          = request.get_json(force=True) or {}
    client_id     = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    guid          = (data.get("restaurant_guid") or "").strip()
    run_test      = data.get("test", True)

    if not client_id or not client_secret or not guid:
        return jsonify(ok=False, error="client_id, client_secret, and restaurant_guid are all required")

    from toast import test_credentials, demo_allowed, _DEMO_ID
    from models import get_restaurant, pos_binding_conflict
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    is_demo_row = demo_allowed(restaurant_id)
    # Synthetic "demo" data only on a restaurant flagged is_demo (CA3 F8) —
    # whether or not the credential test runs: {"test": false} used to skip
    # this and save "demo" credentials onto a real restaurant (fix round #143).
    if _DEMO_ID in (client_id, client_secret, guid) and not is_demo_row:
        return jsonify(ok=False, error="Demo credentials are only accepted on a restaurant flagged as a demo"), 400
    # One Toast restaurant, one Cavnar AI restaurant: a GUID bound to two
    # syncs its sales and labor into both (fix round #143). A demo may
    # mirror a live one, as it may a Google listing.
    if not is_demo_row:
        clash = pos_binding_conflict("toast", guid, exclude_id=restaurant_id)
        if clash:
            return jsonify(ok=False, error=f"That Toast restaurant GUID is already connected to {clash}. "
                                           f"One Toast location can feed only one restaurant."), 400

    # Optionally validate credentials against the Toast API before saving
    if run_test:
        result = test_credentials(client_id, client_secret, guid)
        if not result["ok"]:
            return jsonify(ok=False, error=result["error"])

    was = get_restaurant(restaurant_id)
    update_restaurant(restaurant_id, {
        "toast_client_id":       client_id,
        "toast_client_secret":   client_secret,
        "toast_restaurant_guid": guid,
        "toast_access_token":    None,   # force re-auth on next call
        "toast_token_expires":   None,
        "toast_sync_error":      None,
        "pos_system":            "Toast",
    })
    # Booleans only: never a credential value in the audit trail.
    _admin_events.record_admin_action(
        current_user, "pos.toast.saved", restaurant_id=restaurant_id, target="integration:toast",
        before={"connected": bool(getattr(was, "toast_client_secret", None))},
        after={"connected": True, "verified_with_toast": bool(run_test)})
    return jsonify(ok=True, message="Toast credentials saved")


@toast_bp.route("/admin/toast/sync/<int:restaurant_id>", methods=["POST"])
@admin_required
def sync_toast(restaurant_id, current_user):
    """
    Kick off a background sync so the admin doesn't have to wait ~30s.
    Through pos.sync_restaurant on the bounded admin pool
    (scheduler.start_manual_pos_sync, #65): a direct sync_to_db on a daemon
    thread skipped the Data Health ledger, so the issue it was pressed to
    clear never cleared. Returns a job id to poll.
    """
    from toast import is_connected
    if not is_connected(restaurant_id):
        return jsonify(ok=False, error="Toast not connected for this restaurant")
    import scheduler
    job_id, _joined = scheduler.start_manual_pos_sync(restaurant_id, current_user.get("username") or "admin")
    return jsonify(ok=True, job_id=job_id,
                   message="Sync started — data will appear in the Labor module in ~30 seconds")


@toast_bp.route("/admin/toast/status/<int:restaurant_id>", methods=["GET"])
@admin_required
def toast_status(restaurant_id, current_user):
    from toast import get_connection_status
    return jsonify(get_connection_status(restaurant_id))


@toast_bp.route("/admin/toast/disconnect/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def disconnect_toast(restaurant_id, current_user):
    from models import get_restaurant
    was = get_restaurant(restaurant_id)
    update_restaurant(restaurant_id, {
        "toast_client_id":       None,
        "toast_client_secret":   None,
        "toast_restaurant_guid": None,
        "toast_access_token":    None,
        "toast_token_expires":   None,
        "toast_last_synced":     None,
        "toast_sync_error":      None,
    })
    _admin_events.record_admin_action(
        current_user, "pos.toast.disconnected", restaurant_id=restaurant_id, target="integration:toast",
        before={"connected": bool(getattr(was, "toast_client_secret", None))}, after={"connected": False})
    return jsonify(ok=True, message="Toast disconnected")


# ── Client-facing routes (scoped to session user's restaurant) ─────────────────

@toast_bp.route("/api/toast/status", methods=["GET"])
@login_required
def client_toast_status(current_user):
    from toast import get_connection_status
    return jsonify(get_connection_status(current_user["restaurant_id"]))


@toast_bp.route("/api/toast/save", methods=["POST"])
@login_required
def client_save_toast(current_user):
    # POS credentials are the owner's, not any console role's (SEC-24).
    from permissions import principal_only
    denied = principal_only(current_user, "the Toast connection")
    if denied:
        return denied
    data          = request.get_json(force=True) or {}
    client_id     = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    guid          = (data.get("restaurant_guid") or "").strip()

    if not client_id or not client_secret or not guid:
        return jsonify(ok=False, error="All three fields are required.")

    from toast import test_credentials, demo_allowed
    from models import owner_pos_binding_refusal
    refusal = owner_pos_binding_refusal("toast", guid, current_user["restaurant_id"])
    if refusal:
        return jsonify(ok=False, error=refusal), 409
    result = test_credentials(client_id, client_secret, guid)
    if not result["ok"]:
        return jsonify(ok=False, error=result["error"])
    # "demo" loads synthetic shifts and sales — never into a real restaurant
    # (CA3 F8).
    if result.get("demo") and not demo_allowed(current_user["restaurant_id"]):
        return jsonify(ok=False, error="Those are not Toast credentials.")

    update_restaurant(current_user["restaurant_id"], {
        "toast_client_id":       client_id,
        "toast_client_secret":   client_secret,
        "toast_restaurant_guid": guid,
        "toast_access_token":    None,
        "toast_token_expires":   None,
        "toast_sync_error":      None,
        "pos_system":            "Toast",
    })
    return jsonify(ok=True, message="Toast credentials saved")


@toast_bp.route("/api/toast/sync", methods=["POST"])
@login_required
def client_sync_toast(current_user):
    from toast import is_connected
    rid = current_user["restaurant_id"]
    if not is_connected(rid):
        return jsonify(ok=False, error="Toast is not connected yet.")
    # The same path as the console's button and the nightly sync (#65).
    import scheduler
    scheduler.start_manual_pos_sync(rid, current_user.get("username") or "owner")
    return jsonify(ok=True, message="Sync started — labor data refreshes in ~30 seconds")


@toast_bp.route("/api/toast/disconnect", methods=["POST"])
@login_required
def client_disconnect_toast(current_user):
    # POS credentials are the owner's, not any console role's (SEC-24).
    from permissions import principal_only
    denied = principal_only(current_user, "the Toast connection")
    if denied:
        return denied
    update_restaurant(current_user["restaurant_id"], {
        "toast_client_id":       None,
        "toast_client_secret":   None,
        "toast_restaurant_guid": None,
        "toast_access_token":    None,
        "toast_token_expires":   None,
        "toast_last_synced":     None,
        "toast_sync_error":      None,
        "pos_system":            None,
    })
    return jsonify(ok=True, message="Toast disconnected")
