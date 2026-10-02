"""staff_device_routes — the staff app's push registration and its own
notification switches, on staff_bp (employee audit C4/H3, 10/1/26).

Staff never registered a device: the only route was the console's
/mobile/api/device-tokens, which refuses a PIN session, so the push channel
people.tell prefers did not exist for an employee (COM-01, PERF-02, LG-06).
These routes file the phone under the session's own login and restaurant
with tier='staff' — push.fire_push reaches a staff device only when a
notice names that login, and only with a staff alert type (COM-03).

Like every staff route, nothing here reads a restaurant or a person from
the request: both come from the session (staff_login_required).

Imported at the end of staff_routes, so the routes exist before any app
registers staff_bp (Flask refuses a route on a registered blueprint).
"""
from flask import jsonify, request

from auth import staff_login_required
from staff_routes import staff_bp

_ENVIRONMENTS = ("sandbox", "production")


def _info(user_id, rid):
    """What the app needs to know about staff notifications here."""
    import people
    import preferences
    import push
    return {
        "push_registered": user_id in push.staff_device_users(rid, [user_id]),
        "reminders": "staff_reminder" not in preferences.staff_muted(user_id, rid),
        # The server sends the shift-start and task-due reminders
        # (staff_reminders): the app must not schedule its own for them.
        "reminders_server_side": True,
        "quiet_hours": {"start": f"{people.STAFF_QUIET_START_HOUR:02d}:00",
                        "end": f"{people.STAFF_QUIET_END_HOUR:02d}:00"},
        "alert_types": sorted(push.STAFF_ALERT_TYPES),
        "tabs": list(push.STAFF_TABS),
    }


def _token_from_request():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        data = {}
    return str(data.get("apns_token") or data.get("token") or request.args.get("apns_token") or "").strip(), data


@staff_bp.route("/api/device-tokens", methods=["POST"])
@staff_login_required
def staff_register_device_token(current_user):
    """{apns_token, environment: sandbox|production} → files this phone
    for the signed-in employee here. Call after every sign-in and when iOS
    hands the app a new token."""
    import push
    token, data = _token_from_request()
    environment = str(data.get("environment") or "production").strip()
    if not token or len(token) > 400:
        return jsonify(ok=False, error="apns_token required"), 400
    if environment not in _ENVIRONMENTS:
        return jsonify(ok=False, error="environment must be 'sandbox' or 'production'"), 400
    rid = current_user["restaurant_id"]
    push.register_device_token(current_user["id"], rid, token, environment, tier=push.TIER_STAFF)
    return jsonify(ok=True, **_info(current_user["id"], rid))


@staff_bp.route("/api/device-tokens", methods=["DELETE"])
@staff_bp.route("/api/device-tokens/<apns_token>", methods=["DELETE"])
@staff_login_required
def staff_delete_device_token(current_user, apns_token=None):
    """Stop pushes to this phone (sign-out). Only this employee's own
    staff-app row here is touched; answers 200 either way, so sign-out
    never stalls on it. Without a token, every staff device of this login
    here goes."""
    import push
    token = (apns_token or "").strip() or _token_from_request()[0]
    n = push.unregister_staff_devices(current_user["id"], current_user["restaurant_id"], apns_token=token or None)
    return jsonify(ok=True, removed=n)


@staff_bp.route("/api/notifications")
@staff_login_required
def staff_notification_settings(current_user):
    return jsonify(ok=True, **_info(current_user["id"], current_user["restaurant_id"]))


@staff_bp.route("/api/notifications", methods=["POST"])
@staff_login_required
def staff_notification_settings_save(current_user):
    """{reminders: bool} — the employee's own switch for the shift-start and
    task-due reminders on their phone."""
    import preferences
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or "reminders" not in data:
        return jsonify(ok=False, error="Send {reminders: true|false}."), 400
    rid = current_user["restaurant_id"]
    preferences.set_staff_muted(current_user["id"], rid, "staff_reminder", not bool(data["reminders"]))
    return jsonify(ok=True, **_info(current_user["id"], rid))
