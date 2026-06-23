"""
square_routes.py — Flask routes for Square POS integration
Blueprint: square_bp
"""
from flask import Blueprint, request, jsonify
from auth import admin_required, login_required
from models import update_restaurant

square_bp = Blueprint("square", __name__)


# ── Admin routes ───────────────────────────────────────────────────────────────

@square_bp.route("/admin/square/save/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_save_square(restaurant_id, current_user):
    data         = request.get_json(force=True) or {}
    access_token = (data.get("access_token") or "").strip()
    location_id  = (data.get("location_id") or "").strip()
    if not access_token or not location_id:
        return jsonify(ok=False, error="Access token and location ID are required")
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
    return jsonify(ok=True, message="Square credentials saved")


@square_bp.route("/admin/square/sync/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_sync_square(restaurant_id, current_user):
    from square import is_connected, sync_to_db
    if not is_connected(restaurant_id):
        return jsonify(ok=False, error="Square not connected for this restaurant")
    import threading
    def _run():
        try:
            sync_to_db(restaurant_id)
        except Exception as e:
            print(f"[square_routes] sync error rid={restaurant_id}: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, message="Sync started")


@square_bp.route("/admin/square/disconnect/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_disconnect_square(restaurant_id, current_user):
    update_restaurant(restaurant_id, {
        "square_access_token": None,
        "square_location_id":  None,
        "square_last_synced":  None,
        "square_sync_error":   None,
    })
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
    data         = request.get_json(force=True) or {}
    access_token = (data.get("access_token") or "").strip()
    location_id  = (data.get("location_id") or "").strip()
    if not access_token or not location_id:
        return jsonify(ok=False, error="Access token and location ID are required.")
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
    from square import is_connected, sync_to_db
    rid = current_user["restaurant_id"]
    if not is_connected(rid):
        return jsonify(ok=False, error="Square is not connected yet.")
    import threading
    def _run():
        try:
            sync_to_db(rid)
        except Exception as e:
            print(f"[square_routes] client sync error rid={rid}: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, message="Sync started — labor data refreshes in ~30 seconds")


@square_bp.route("/api/square/disconnect", methods=["POST"])
@login_required
def client_disconnect_square(current_user):
    update_restaurant(current_user["restaurant_id"], {
        "square_access_token": None,
        "square_location_id":  None,
        "square_last_synced":  None,
        "square_sync_error":   None,
    })
    return jsonify(ok=True)
