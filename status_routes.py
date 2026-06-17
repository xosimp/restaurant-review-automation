import json
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, render_template, request, abort
from status_manager import (
    get_all_statuses, update_service_status, get_open_incidents,
    get_recent_incidents, get_incident_updates, create_incident,
    update_incident, overall_status, seed_default_services, SERVICES,
)

status_bp = Blueprint("status", __name__)


# ── Public status page ────────────────────────────────────────────────────────

@status_bp.route("/status")
def status_page():
    seed_default_services()
    statuses  = get_all_statuses()
    incidents = get_recent_incidents(limit=30)
    for inc in incidents:
        try:
            inc["affected_keys"] = json.loads(inc["affected_keys"] or "[]")
        except Exception:
            inc["affected_keys"] = []
        inc["updates"] = get_incident_updates(inc["id"])

    # Group incidents by date (YYYY-MM-DD of created_at)
    from collections import OrderedDict
    grouped = OrderedDict()
    for inc in incidents:
        day = inc["created_at"][:10]
        grouped.setdefault(day, []).append(inc)

    banner = overall_status(statuses)
    # Convert current time to CT (UTC-5 standard / UTC-6 daylight — approximate with fixed offset)
    try:
        from zoneinfo import ZoneInfo
        ct_now = datetime.now(ZoneInfo("America/Chicago"))
    except Exception:
        ct_now = datetime.now(timezone(timedelta(hours=-5)))
    last_checked = "{}/{}/{} {}:{:02d} {}".format(
        ct_now.month, ct_now.day, str(ct_now.year)[2:],
        ct_now.strftime("%-I"), ct_now.minute,
        ct_now.strftime("%p") + " CT"
    )
    return render_template("status.html",
                           statuses=statuses,
                           grouped_incidents=grouped,
                           banner=banner,
                           last_checked=last_checked)


# ── Public JSON API (used by status page auto-refresh) ────────────────────────

@status_bp.route("/api/status")
def api_status():
    statuses  = get_all_statuses()
    incidents = get_open_incidents()
    for inc in incidents:
        try:
            inc["affected_keys"] = json.loads(inc["affected_keys"] or "[]")
        except Exception:
            inc["affected_keys"] = []
    return jsonify({"overall": overall_status(statuses), "services": statuses, "incidents": incidents})


# ── Admin endpoints (require login) ──────────────────────────────────────────

def _require_admin():
    from auth import get_session_user
    token = request.cookies.get("session_token")
    user = get_session_user(token) if token else None
    if not user or not user.get("is_admin"):
        abort(403)


@status_bp.route("/admin/status/update", methods=["POST"])
def admin_update_status():
    _require_admin()
    data    = request.get_json(force=True)
    key     = data.get("service_key", "").strip()
    status  = data.get("status", "operational")
    message = data.get("message", "").strip() or None
    if not key:
        return jsonify({"ok": False, "error": "missing service_key"}), 400
    valid = {"operational", "degraded", "outage", "maintenance"}
    if status not in valid:
        return jsonify({"ok": False, "error": "invalid status"}), 400
    update_service_status(key, status, message)
    return jsonify({"ok": True})


@status_bp.route("/admin/status/incident", methods=["POST"])
def admin_create_incident():
    _require_admin()
    data     = request.get_json(force=True)
    title    = data.get("title", "").strip()
    body     = data.get("body", "").strip()
    keys     = data.get("affected_keys", [])
    severity = data.get("severity", "degraded")
    status   = data.get("status", "investigating")
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400
    inc_id = create_incident(title, body, keys, severity, status)
    return jsonify({"ok": True, "id": inc_id})


@status_bp.route("/admin/status/incident/<int:inc_id>/update", methods=["POST"])
def admin_update_incident(inc_id):
    _require_admin()
    data    = request.get_json(force=True)
    message = data.get("message", "").strip()
    status  = data.get("status", "monitoring")
    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400
    update_incident(inc_id, message, status)
    return jsonify({"ok": True})


@status_bp.route("/admin/status/services")
def admin_list_services():
    _require_admin()
    return jsonify({"services": SERVICES, "statuses": get_all_statuses()})
