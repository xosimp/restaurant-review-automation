"""
rpower_routes.py — Flask routes for the RPOWER POS integration
Blueprint: rpower_bp
Registered in hosted_dashboard.py alongside the other blueprints.

Admin endpoints (admin_required, restaurant_id in URL):
  POST /admin/rpower/save/<restaurant_id>        save + verify a token
  POST /admin/rpower/bootstrap/<restaurant_id>   resolve which store this is
  POST /admin/rpower/sync/<restaurant_id>        kick a background sync
  GET  /admin/rpower/status/<restaurant_id>
  POST /admin/rpower/disconnect/<restaurant_id>

Client endpoints (login_required, scoped to the session's restaurant):
  GET  /api/rpower/status

Connecting RPOWER is deliberately admin-only and has no client-facing save.
The token is issued by RPOWER to Cavnar as an integrator, not to the
restaurant — an owner has nothing to paste, and offering them a field would
imply otherwise.
"""
from flask import Blueprint, request, jsonify
from auth import admin_required, login_required
from models import update_restaurant

rpower_bp = Blueprint("rpower", __name__)


@rpower_bp.route("/admin/rpower/save/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_rpower_token(restaurant_id, current_user):
    """Save a token, after checking it actually works.

    The token is verified against /store/get before anything is written, so a
    typo fails here rather than at 5am in a sync job nobody is watching. When
    the token sees exactly one store the binding is completed in the same
    call; when it sees several the stores are returned for the admin to pick
    from, because binding a restaurant to the wrong store produces a module
    full of somebody else's numbers that looks entirely normal.
    """
    import rpower
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token:
        return jsonify(ok=False, error="A token is required."), 400

    probe = rpower.test_token(token)
    if not probe["ok"]:
        return jsonify(ok=False, error=probe["error"]), 400

    update_restaurant(restaurant_id, {
        "rpower_token": token, "rpower_sync_error": None, "pos_system": "RPOWER"})
    stores = probe["stores"]
    if len(stores) == 1:
        result = rpower.bootstrap(restaurant_id, store_mid=stores[0]["store_mid"])
        if not result.get("ok"):
            return jsonify(ok=False, error=result.get("error"), stores=stores), 400
        return jsonify(ok=True, connected=True, store=result["store"],
                       applied=result.get("applied", []),
                       message=f"Connected to {stores[0].get('name') or 'RPOWER'}.")
    return jsonify(ok=True, connected=False, needs_choice=True, stores=stores,
                   message=f"Token saved. It can see {len(stores)} stores — pick one.")


@rpower_bp.route("/admin/rpower/bootstrap/<int:restaurant_id>", methods=["POST"])
@admin_required
def bootstrap_rpower(restaurant_id, current_user):
    """Bind this restaurant to one of the stores the token can see."""
    import rpower
    data = request.get_json(silent=True) or {}
    store_mid = (data.get("store_mid") or "").strip() or None
    result = rpower.bootstrap(restaurant_id, store_mid=store_mid)
    return jsonify(**result), (200 if result.get("ok") else 400)


@rpower_bp.route("/admin/rpower/sync/<int:restaurant_id>", methods=["POST"])
@admin_required
def sync_rpower(restaurant_id, current_user):
    """Kick a sync in the background so the admin screen doesn't hang on it."""
    import rpower
    if not rpower.is_connected(restaurant_id):
        return jsonify(ok=False, error="RPOWER isn't fully connected for this restaurant."), 400

    import threading

    def _run():
        try:
            rpower.sync_to_db(restaurant_id)
        except Exception as e:
            import ops
            ops.capture(e, job="rpower_sync", context=f"restaurant_id={restaurant_id}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify(ok=True, message="Sync started.")


@rpower_bp.route("/admin/rpower/status/<int:restaurant_id>")
@admin_required
def rpower_status_admin(restaurant_id, current_user):
    import rpower
    return jsonify(**rpower.get_connection_status(restaurant_id))


@rpower_bp.route("/admin/rpower/disconnect/<int:restaurant_id>", methods=["POST"])
@admin_required
def disconnect_rpower(restaurant_id, current_user):
    """Clear every RPOWER field.

    The store binding goes with the token deliberately: a cg and store_mid
    left behind after a token is removed is a half-connected state that
    is_connected would keep reporting as live.
    """
    update_restaurant(restaurant_id, {
        "rpower_token": None, "rpower_cg": None, "rpower_store_mid": None,
        "rpower_store_name": None, "rpower_verified_at": None,
        "rpower_sync_error": None})
    return jsonify(ok=True, message="RPOWER disconnected.")


@rpower_bp.route("/api/rpower/status")
@login_required
def rpower_status_client(current_user):
    """Read-only for the owner's own account screen — no credential fields."""
    import rpower
    status = rpower.get_connection_status(current_user["restaurant_id"])
    # The owner sees whether it is working and when it last ran, never the
    # token or the internal identifiers.
    return jsonify(connected=status.get("connected", False),
                   state=status.get("state"),
                   store_name=status.get("store_name"),
                   last_synced=status.get("last_synced"),
                   error=status.get("error"))
