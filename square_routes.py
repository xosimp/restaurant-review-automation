"""
square_routes.py — Flask routes for Square POS integration
Blueprint: square_bp
"""
from flask import Blueprint, request, jsonify
from auth import admin_required, login_required, recent_auth_required
from models import update_restaurant

square_bp = Blueprint("square", __name__)
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(square_bp)
# Its /admin writes (credentials, sync, disconnect) are in the audit trail (#126).
import admin_events as _admin_events
_admin_events.register_audit(square_bp)


# ── Admin routes ───────────────────────────────────────────────────────────────

@square_bp.route("/admin/square/save/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_save_square(restaurant_id, current_user):
    data         = request.get_json(force=True) or {}
    access_token = (data.get("access_token") or "").strip()
    location_id  = (data.get("location_id") or "").strip()
    if not access_token or not location_id:
        return jsonify(ok=False, error="Access token and location ID are required")
    # One Square location, one restaurant (fix round #143, as for Toast).
    from models import get_restaurant, pos_binding_conflict
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    if not int(getattr(r, "is_demo", 0) or 0):
        clash = pos_binding_conflict("square", location_id, exclude_id=restaurant_id)
        if clash:
            return jsonify(ok=False, error=f"That Square location is already connected to {clash}. "
                                           f"One Square location can feed only one restaurant."), 400
    from square import test_credentials
    result = test_credentials(access_token, location_id)
    if not result["ok"]:
        return jsonify(ok=False, error=result["error"])
    update_restaurant(restaurant_id, {
        "square_access_token": access_token,
        "square_location_id":  location_id,
        "square_sync_error":   None,
        "pos_system":          "Square",
    })
    # Booleans only: never a credential value in the audit trail.
    _admin_events.record_admin_action(
        current_user, "pos.square.saved", restaurant_id=restaurant_id, target="integration:square",
        before={"connected": bool(getattr(r, "square_access_token", None))}, after={"connected": True})
    return jsonify(ok=True, message="Square credentials saved")


@square_bp.route("/admin/square/sync/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_sync_square(restaurant_id, current_user):
    """A background sync through pos.sync_restaurant on the bounded admin
    pool (scheduler.start_manual_pos_sync), as Toast's and RPOWER's are
    (#65): a direct sync_to_db on an unbounded thread per click skipped the
    Data Health ledger, so the issue it was pressed to clear never cleared.
    Returns a job id to poll (GET /admin/api/tasks/<job_id>)."""
    from square import is_connected
    if not is_connected(restaurant_id):
        return jsonify(ok=False, error="Square not connected for this restaurant")
    import scheduler
    job_id, _joined = scheduler.start_manual_pos_sync(restaurant_id, current_user.get("username") or "admin")
    return jsonify(ok=True, job_id=job_id, message="Sync started")


@square_bp.route("/admin/square/disconnect/<int:restaurant_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_disconnect_square(restaurant_id, current_user):
    from models import get_restaurant
    was = get_restaurant(restaurant_id)
    update_restaurant(restaurant_id, {
        "square_access_token": None,
        "square_location_id":  None,
        "square_last_synced":  None,
        "square_sync_error":   None,
    })
    _admin_events.record_admin_action(
        current_user, "pos.square.disconnected", restaurant_id=restaurant_id, target="integration:square",
        before={"connected": bool(getattr(was, "square_access_token", None))}, after={"connected": False})
    return jsonify(ok=True, message="Square disconnected")


# ── Client routes ──────────────────────────────────────────────────────────────

@square_bp.route("/api/square/status", methods=["GET"])
@login_required
def client_square_status(current_user):
    from square import is_connected
    from models import get_restaurant
    rid = current_user["restaurant_id"]
    r = get_restaurant(rid)
    return jsonify(
        connected=is_connected(rid),
        last_synced=getattr(r, "square_last_synced", None),
        sync_error=getattr(r, "square_sync_error", None),
    )


@square_bp.route("/api/square/save", methods=["POST"])
@login_required
def client_save_square(current_user):
    # POS credentials are the owner's, not any console role's (SEC-24).
    from permissions import principal_only
    denied = principal_only(current_user, "the Square connection")
    if denied:
        return denied
    data         = request.get_json(force=True) or {}
    access_token = (data.get("access_token") or "").strip()
    location_id  = (data.get("location_id") or "").strip()
    if not access_token or not location_id:
        return jsonify(ok=False, error="Access token and location ID are required.")
    from models import owner_pos_binding_refusal
    refusal = owner_pos_binding_refusal("square", location_id, current_user["restaurant_id"])
    if refusal:
        return jsonify(ok=False, error=refusal), 409
    from square import test_credentials
    result = test_credentials(access_token, location_id)
    if not result["ok"]:
        return jsonify(ok=False, error=result["error"])
    update_restaurant(current_user["restaurant_id"], {
        "square_access_token": access_token,
        "square_location_id":  location_id,
        "square_sync_error":   None,
        "pos_system":          "Square",
    })
    return jsonify(ok=True, message="Square connected", location_name=result.get("location_name",""))


@square_bp.route("/api/square/sync", methods=["POST"])
@login_required
def client_sync_square(current_user):
    from square import is_connected
    rid = current_user["restaurant_id"]
    if not is_connected(rid):
        return jsonify(ok=False, error="Square is not connected yet.")
    # The same path as the console's button and the nightly sync (#65).
    import scheduler
    job_id, _joined = scheduler.start_manual_pos_sync(rid, current_user.get("username") or "owner")
    return jsonify(ok=True, job_id=job_id, message="Sync started — labor data refreshes in ~30 seconds")


@square_bp.route("/api/square/disconnect", methods=["POST"])
@login_required
def client_disconnect_square(current_user):
    # POS credentials are the owner's, not any console role's (SEC-24).
    from permissions import principal_only
    denied = principal_only(current_user, "the Square connection")
    if denied:
        return denied
    update_restaurant(current_user["restaurant_id"], {
        "square_access_token": None,
        "square_location_id":  None,
        "square_last_synced":  None,
        "square_sync_error":   None,
    })
    return jsonify(ok=True)
