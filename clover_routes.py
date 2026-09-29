"""
clover_routes.py — Flask routes for Clover POS integration
Blueprint: clover_bp
"""
from flask import Blueprint, request, jsonify
from auth import admin_required, login_required, recent_auth_required
from models import update_restaurant

clover_bp = Blueprint("clover", __name__)
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(clover_bp)
# Its /admin writes (credentials, sync, disconnect) are in the audit trail (#126).
import admin_events as _admin_events
_admin_events.register_audit(clover_bp)


# ── Admin routes ───────────────────────────────────────────────────────────────

@clover_bp.route("/admin/clover/save/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_save_clover(restaurant_id, current_user):
    data        = request.get_json(force=True) or {}
    merchant_id = (data.get("merchant_id") or "").strip()
    api_token   = (data.get("api_token") or "").strip()
    if not merchant_id or not api_token:
        return jsonify(ok=False, error="Merchant ID and API token are required")
    # One Clover merchant, one restaurant (fix round #143, as for Toast).
    from models import get_restaurant, pos_binding_conflict
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    if not int(getattr(r, "is_demo", 0) or 0):
        clash = pos_binding_conflict("clover", merchant_id, exclude_id=restaurant_id)
        if clash:
            return jsonify(ok=False, error=f"That Clover merchant is already connected to {clash}. "
                                           f"One Clover merchant can feed only one restaurant."), 400
    from clover import test_credentials
    result = test_credentials(merchant_id, api_token)
    if not result["ok"]:
        return jsonify(ok=False, error=result["error"])
    update_restaurant(restaurant_id, {
        "clover_merchant_id": merchant_id,
        "clover_api_token":   api_token,
        "clover_sync_error":  None,
        "pos_system":         "Clover",
    })
    # Booleans only: never a credential value in the audit trail.
    _admin_events.record_admin_action(
        current_user, "pos.clover.saved", restaurant_id=restaurant_id, target="integration:clover",
        before={"connected": bool(getattr(r, "clover_api_token", None))}, after={"connected": True})
    return jsonify(ok=True, message="Clover credentials saved")


@clover_bp.route("/admin/clover/sync/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_sync_clover(restaurant_id, current_user):
    """A background sync through pos.sync_restaurant on the bounded admin
    pool (scheduler.start_manual_pos_sync), as Toast's and RPOWER's are
    (#65): a direct sync_to_db on an unbounded thread per click skipped the
    Data Health ledger, so the issue it was pressed to clear never cleared.
    Returns a job id to poll (GET /admin/api/tasks/<job_id>)."""
    from clover import is_connected
    if not is_connected(restaurant_id):
        return jsonify(ok=False, error="Clover not connected for this restaurant")
    import scheduler
    job_id, _joined = scheduler.start_manual_pos_sync(restaurant_id, current_user.get("username") or "admin")
    return jsonify(ok=True, job_id=job_id, message="Sync started")


@clover_bp.route("/admin/clover/disconnect/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_disconnect_clover(restaurant_id, current_user):
    from models import get_restaurant
    was = get_restaurant(restaurant_id)
    update_restaurant(restaurant_id, {
        "clover_merchant_id":  None,
        "clover_api_token":    None,
        "clover_last_synced":  None,
        "clover_sync_error":   None,
    })
    _admin_events.record_admin_action(
        current_user, "pos.clover.disconnected", restaurant_id=restaurant_id, target="integration:clover",
        before={"connected": bool(getattr(was, "clover_api_token", None))}, after={"connected": False})
    return jsonify(ok=True, message="Clover disconnected")


# ── Client routes ──────────────────────────────────────────────────────────────

@clover_bp.route("/api/clover/status", methods=["GET"])
@login_required
def client_clover_status(current_user):
    from clover import is_connected
    from models import get_restaurant
    rid = current_user["restaurant_id"]
    r = get_restaurant(rid)
    return jsonify(
        connected=is_connected(rid),
        last_synced=getattr(r, "clover_last_synced", None),
        sync_error=getattr(r, "clover_sync_error", None),
    )


@clover_bp.route("/api/clover/save", methods=["POST"])
@login_required
def client_save_clover(current_user):
    # POS credentials are the owner's, not any console role's (SEC-24).
    from permissions import principal_only
    denied = principal_only(current_user, "the Clover connection")
    if denied:
        return denied
    data        = request.get_json(force=True) or {}
    merchant_id = (data.get("merchant_id") or "").strip()
    api_token   = (data.get("api_token") or "").strip()
    if not merchant_id or not api_token:
        return jsonify(ok=False, error="Merchant ID and API token are required.")
    from models import owner_pos_binding_refusal
    refusal = owner_pos_binding_refusal("clover", merchant_id, current_user["restaurant_id"])
    if refusal:
        return jsonify(ok=False, error=refusal), 409
    from clover import test_credentials
    result = test_credentials(merchant_id, api_token)
    if not result["ok"]:
        return jsonify(ok=False, error=result["error"])
    update_restaurant(current_user["restaurant_id"], {
        "clover_merchant_id": merchant_id,
        "clover_api_token":   api_token,
        "clover_sync_error":  None,
        "pos_system":         "Clover",
    })
    return jsonify(ok=True, message="Clover connected", merchant_name=result.get("merchant_name",""))


@clover_bp.route("/api/clover/sync", methods=["POST"])
@login_required
def client_sync_clover(current_user):
    from clover import is_connected
    rid = current_user["restaurant_id"]
    if not is_connected(rid):
        return jsonify(ok=False, error="Clover is not connected yet.")
    # The same path as the console's button and the nightly sync (#65).
    import scheduler
    job_id, _joined = scheduler.start_manual_pos_sync(rid, current_user.get("username") or "owner")
    return jsonify(ok=True, job_id=job_id, message="Sync started — labor data refreshes in ~30 seconds")


@clover_bp.route("/api/clover/disconnect", methods=["POST"])
@login_required
def client_disconnect_clover(current_user):
    # POS credentials are the owner's, not any console role's (SEC-24).
    from permissions import principal_only
    denied = principal_only(current_user, "the Clover connection")
    if denied:
        return denied
    update_restaurant(current_user["restaurant_id"], {
        "clover_merchant_id":  None,
        "clover_api_token":    None,
        "clover_last_synced":  None,
        "clover_sync_error":   None,
    })
    return jsonify(ok=True)
