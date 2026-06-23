"""
clover_routes.py — Flask routes for Clover POS integration
Blueprint: clover_bp
"""
from flask import Blueprint, request, jsonify
from auth import admin_required, login_required
from models import update_restaurant

clover_bp = Blueprint("clover", __name__)


# ── Admin routes ───────────────────────────────────────────────────────────────

@clover_bp.route("/admin/clover/save/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_save_clover(restaurant_id, current_user):
    data        = request.get_json(force=True) or {}
    merchant_id = (data.get("merchant_id") or "").strip()
    api_token   = (data.get("api_token") or "").strip()
    if not merchant_id or not api_token:
        return jsonify(ok=False, error="Merchant ID and API token are required")
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
    return jsonify(ok=True, message="Clover credentials saved")


@clover_bp.route("/admin/clover/sync/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_sync_clover(restaurant_id, current_user):
    from clover import is_connected, sync_to_db
    if not is_connected(restaurant_id):
        return jsonify(ok=False, error="Clover not connected for this restaurant")
    import threading
    def _run():
        try:
            sync_to_db(restaurant_id)
        except Exception as e:
            print(f"[clover_routes] sync error rid={restaurant_id}: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, message="Sync started")


@clover_bp.route("/admin/clover/disconnect/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_disconnect_clover(restaurant_id, current_user):
    update_restaurant(restaurant_id, {
        "clover_merchant_id":  None,
        "clover_api_token":    None,
        "clover_last_synced":  None,
        "clover_sync_error":   None,
    })
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
    data        = request.get_json(force=True) or {}
    merchant_id = (data.get("merchant_id") or "").strip()
    api_token   = (data.get("api_token") or "").strip()
    if not merchant_id or not api_token:
        return jsonify(ok=False, error="Merchant ID and API token are required.")
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
    from clover import is_connected, sync_to_db
    rid = current_user["restaurant_id"]
    if not is_connected(rid):
        return jsonify(ok=False, error="Clover is not connected yet.")
    import threading
    def _run():
        try:
            sync_to_db(rid)
        except Exception as e:
            print(f"[clover_routes] client sync error rid={rid}: {e}")
    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, message="Sync started — labor data refreshes in ~30 seconds")


@clover_bp.route("/api/clover/disconnect", methods=["POST"])
@login_required
def client_disconnect_clover(current_user):
    update_restaurant(current_user["restaurant_id"], {
        "clover_merchant_id":  None,
        "clover_api_token":    None,
        "clover_last_synced":  None,
        "clover_sync_error":   None,
    })
    return jsonify(ok=True)
