import json
from datetime import datetime, timezone, timedelta
from flask import Blueprint, jsonify, render_template, request, abort, make_response
from status_manager import (
    get_all_statuses, update_service_status, get_open_incidents,
    get_recent_incidents, get_incident_updates, create_incident,
    update_incident, overall_status, seed_default_services, SERVICES,
    effective_statuses, get_incident, INCIDENT_STATUSES, INCIDENT_SEVERITIES,
    SERVICE_STATUSES,
)
# seed_default_services is imported for the tests that prove /status never
# calls it; the rows are seeded at boot (hosted_dashboard.py).

status_bp = Blueprint("status", __name__)
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(status_bp)


# ── Public status page ────────────────────────────────────────────────────────

@status_bp.route("/status")
def status_page():
    # No seeding here: this is a public GET, and it used to write the
    # services table on every hit (SEC-38). The rows are seeded once at boot
    # (hosted_dashboard.py) and get_all_statuses() reads whatever exists.
    # An open incident holds the services it names at its severity, and sets
    # the banner, whatever the automated checks last wrote (#61).
    open_incidents = get_open_incidents()
    statuses  = effective_statuses(get_all_statuses(), open_incidents)
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

    banner = overall_status(statuses, open_incidents)
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
    incidents = get_open_incidents()
    statuses  = effective_statuses(get_all_statuses(), incidents)
    for inc in incidents:
        try:
            inc["affected_keys"] = json.loads(inc["affected_keys"] or "[]")
        except Exception:
            inc["affected_keys"] = []
    return jsonify({"overall": overall_status(statuses, incidents), "services": statuses,
                    "incidents": incidents})


# ── Admin endpoints (require login) ──────────────────────────────────────────
#
# These writes post the public outage banner, so they sit behind the same
# controls as the rest of /admin (SEC-23): the admin 2FA gate, CSRF
# (hosted_dashboard.py wires csrf_protect onto status_bp — its public pages
# are GETs, which the check never touches), an admin_events row before the
# write runs, and a real JSON body — force=True used to parse a text/plain
# cross-site form post as JSON.

import admin_events as _admin_events
status_bp.before_request(_admin_events.audit_admin_write)


def _require_admin():
    """The /admin gate, answered as auth.admin_required answers it on
    admin_bp (fix round A: #5, #87, #88): the session, the admin role —
    support may read the incidents but never post the public banner — the
    per-session request ceiling, and the internal login's second factor
    (enrol, verify or fail closed, the console's own refusals). A call, not
    the decorator, so anyone else keeps this blueprint's plain 403."""
    import auth
    from flask import g
    user = auth.get_current_user()
    is_support = bool(user) and not user.get("is_admin") and user.get("role") == "support"
    if not user or not (user.get("is_admin") or is_support):
        abort(403)
    limited = auth._admin_rate_limited(user)
    if limited:
        abort(make_response(*limited))
    if is_support and request.method not in ("GET", "HEAD", "OPTIONS"):
        abort(make_response(jsonify(ok=False, error="Support accounts are read-only."), 403))
    state = auth.admin_second_factor_state(user)
    if state != "ok":
        refusal = auth._second_factor_refusal(state)
        abort(make_response(*refusal) if isinstance(refusal, tuple) else refusal)
    try:
        g.admin_role = "admin" if user.get("is_admin") else "support"
    except Exception:
        pass
    return user


def _json_body():
    """The request's JSON object, or a 400 for anything else."""
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict):
        abort(make_response(jsonify(ok=False, error="Send a JSON object (Content-Type: application/json)."), 400))
    return data


@status_bp.route("/admin/status/update", methods=["POST"])
def admin_update_status():
    _require_admin()
    data    = _json_body()
    key     = data.get("service_key", "").strip()
    status  = data.get("status", "operational")
    message = data.get("message", "").strip() or None
    if not key:
        return jsonify({"ok": False, "error": "missing service_key"}), 400
    if key not in _SERVICE_KEYS:
        return jsonify({"ok": False, "error": "unknown service_key"}), 400
    if status not in SERVICE_STATUSES:
        return jsonify({"ok": False, "error": "invalid status"}), 400
    update_service_status(key, status, message)
    return jsonify({"ok": True})


_SERVICE_KEYS = {s["key"] for s in SERVICES}


def _text(data, key, default=""):
    v = data.get(key, default)
    return v.strip() if isinstance(v, str) else default


@status_bp.route("/admin/status/incident", methods=["POST"])
def admin_create_incident():
    _require_admin()
    data     = _json_body()
    title    = _text(data, "title")
    body     = _text(data, "body")
    keys     = data.get("affected_keys", [])
    severity = _text(data, "severity", "degraded") or "degraded"
    status   = _text(data, "status", "investigating") or "investigating"
    if not title:
        return jsonify({"ok": False, "error": "title required"}), 400
    # Validated here rather than left to the table's CHECK constraints, which
    # answered a bad value with a 500 (#61).
    if severity not in INCIDENT_SEVERITIES:
        return jsonify({"ok": False, "error": f"severity must be one of {', '.join(INCIDENT_SEVERITIES)}"}), 400
    if status not in INCIDENT_STATUSES:
        return jsonify({"ok": False, "error": f"status must be one of {', '.join(INCIDENT_STATUSES)}"}), 400
    if not isinstance(keys, list) or any(k not in _SERVICE_KEYS for k in keys):
        return jsonify({"ok": False, "error": "affected_keys must be service keys from /admin/status/services"}), 400
    inc_id = create_incident(title[:200], body[:4000], keys, severity, status)
    return jsonify({"ok": True, "id": inc_id})


@status_bp.route("/admin/status/incident/<int:inc_id>/update", methods=["POST"])
def admin_update_incident(inc_id):
    """Post an update to an incident, or resolve it (status "resolved").
    It existed with no caller and no validation: an unknown id wrote an
    update row for an incident that did not exist, and a status outside the
    table's CHECK was a 500."""
    _require_admin()
    data    = _json_body()
    message = _text(data, "message")
    status  = _text(data, "status", "monitoring") or "monitoring"
    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400
    if status not in INCIDENT_STATUSES:
        return jsonify({"ok": False, "error": f"status must be one of {', '.join(INCIDENT_STATUSES)}"}), 400
    if get_incident(inc_id) is None:
        return jsonify({"ok": False, "error": "No such incident."}), 404
    update_incident(inc_id, message[:4000], status)
    return jsonify({"ok": True, "incident": get_incident(inc_id)})


@status_bp.route("/admin/status/incident/<int:inc_id>/resolve", methods=["POST"])
def admin_resolve_incident(inc_id):
    """Resolve an incident, with an optional closing message."""
    _require_admin()
    data = _json_body()
    inc = get_incident(inc_id)
    if inc is None:
        return jsonify({"ok": False, "error": "No such incident."}), 404
    if inc.get("status") == "resolved":
        return jsonify({"ok": True, "incident": inc, "already_resolved": True})
    update_incident(inc_id, (_text(data, "message") or "Resolved.")[:4000], "resolved")
    return jsonify({"ok": True, "incident": get_incident(inc_id)})


@status_bp.route("/admin/status/incidents")
def admin_list_incidents():
    """Open incidents (with their updates), and the last few resolved — what
    the console lists with Update and Resolve."""
    _require_admin()
    open_incs = [get_incident(i["id"]) for i in get_open_incidents()]
    recent = [i for i in get_recent_incidents(limit=20) if i.get("status") == "resolved"][:10]
    for inc in recent:
        try:
            inc["affected_keys"] = json.loads(inc.get("affected_keys") or "[]")
        except (TypeError, ValueError):
            inc["affected_keys"] = []
    return jsonify({"ok": True, "open": [i for i in open_incs if i], "resolved": recent,
                    "statuses": list(INCIDENT_STATUSES), "severities": list(INCIDENT_SEVERITIES)})


@status_bp.route("/admin/status/services")
def admin_list_services():
    _require_admin()
    return jsonify({"services": SERVICES, "statuses": get_all_statuses(),
                    "effective": effective_statuses(get_all_statuses(), get_open_incidents()),
                    "service_statuses": list(SERVICE_STATUSES),
                    "incident_statuses": list(INCIDENT_STATUSES),
                    "incident_severities": list(INCIDENT_SEVERITIES)})
