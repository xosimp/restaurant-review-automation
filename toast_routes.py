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
from auth import admin_required, login_required
from models import update_restaurant, get_restaurant

toast_bp = Blueprint("toast", __name__)


@toast_bp.route("/admin/toast/save/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_toast_credentials(restaurant_id, current_user):
    data          = request.get_json(force=True) or {}
    client_id     = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    guid          = (data.get("restaurant_guid") or "").strip()
    run_test      = data.get("test", True)

    if not client_id or not client_secret or not guid:
        return jsonify(ok=False, error="client_id, client_secret, and restaurant_guid are all required")

    # Optionally validate credentials against the Toast API before saving
    if run_test:
        from toast import test_credentials
        result = test_credentials(client_id, client_secret, guid)
        if not result["ok"]:
            return jsonify(ok=False, error=result["error"])

    update_restaurant(restaurant_id, {
        "toast_client_id":       client_id,
        "toast_client_secret":   client_secret,
        "toast_restaurant_guid": guid,
        "toast_access_token":    None,   # force re-auth on next call
        "toast_token_expires":   None,
        "toast_sync_error":      None,
        "pos_system":            "Toast",
    })
    return jsonify(ok=True, message="Toast credentials saved")


@toast_bp.route("/admin/toast/sync/<int:restaurant_id>", methods=["POST"])
@admin_required
def sync_toast(restaurant_id, current_user):
    """
    Kick off a background sync so the admin doesn't have to wait ~30s.
    Returns immediately with ok=True; the sync runs in a daemon thread.
    """
    from toast import is_connected
    if not is_connected(restaurant_id):
        return jsonify(ok=False, error="Toast not connected for this restaurant")

    import threading
    from toast import sync_to_db

    def _run():
        try:
            result = sync_to_db(restaurant_id)
            print(f"[toast_routes] sync complete for restaurant {restaurant_id}: {result}")
        except Exception as e:
            print(f"[toast_routes] sync error for restaurant {restaurant_id}: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, message="Sync started — data will appear in the Labor module in ~30 seconds")


@toast_bp.route("/admin/toast/status/<int:restaurant_id>", methods=["GET"])
@admin_required
def toast_status(restaurant_id, current_user):
    from toast import get_connection_status
    return jsonify(get_connection_status(restaurant_id))


@toast_bp.route("/admin/toast/disconnect/<int:restaurant_id>", methods=["POST"])
@admin_required
def disconnect_toast(restaurant_id, current_user):
    update_restaurant(restaurant_id, {
        "toast_client_id":       None,
        "toast_client_secret":   None,
        "toast_restaurant_guid": None,
        "toast_access_token":    None,
        "toast_token_expires":   None,
        "toast_last_synced":     None,
        "toast_sync_error":      None,
    })
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
    data          = request.get_json(force=True) or {}
    client_id     = (data.get("client_id") or "").strip()
    client_secret = (data.get("client_secret") or "").strip()
    guid          = (data.get("restaurant_guid") or "").strip()

    if not client_id or not client_secret or not guid:
        return jsonify(ok=False, error="All three fields are required.")

    from toast import test_credentials
    result = test_credentials(client_id, client_secret, guid)
    if not result["ok"]:
        return jsonify(ok=False, error=result["error"])

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

    import threading
    from toast import sync_to_db

    def _run():
        try:
            sync_to_db(rid)
        except Exception as e:
            print(f"[toast_routes] client sync error for restaurant {rid}: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, message="Sync started — labor data refreshes in ~30 seconds")


@toast_bp.route("/api/toast/disconnect", methods=["POST"])
@login_required
def client_disconnect_toast(current_user):
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
