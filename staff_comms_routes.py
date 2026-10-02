"""
staff_comms_routes.py — the HTTP surface for staff_comms: running late,
announcements with acknowledgement, and the staff ↔ manager thread.

Two halves:

  STAFF (on staff_routes.staff_bp, /staff/api/...). The restaurant and the
  employee come from the session's membership, never from the request
  (staff_routes' rule): there is no id here to tamper with.

      POST /staff/api/running-late
      GET  /staff/api/running-late
      GET  /staff/api/inbox
      POST /staff/api/announcements/<id>/ack
      GET  /staff/api/messages
      POST /staff/api/messages

  OWNER (the Team inbox under Labor). Every route exists twice, web
  (/api/labor/inbox/...) and mobile (/mobile/api/labor/inbox/...), both
  calling ONE body (current_user) -> (payload, status) — strategy_routes'
  twin pattern. The /labor prefix gates them on the Labor module
  (auth._MODULE_PREFIXES); each body also requires SCHEDULE_DRAFT, the
  permission that decides staff requests — the same logins a staff
  message or a running-late notice is pushed to.

Imported by hosted_dashboard BEFORE staff_bp is registered (the staff
routes attach to it at import).
"""
from flask import Blueprint, Response, jsonify, request

from auth import login_required, mobile_login_required, staff_login_required
from staff_routes import staff_bp

import staff_comms

staff_comms_bp = Blueprint("staff_comms", __name__)
staff_comms_mobile_bp = Blueprint("staff_comms_mobile", __name__, url_prefix="/mobile/api")

from security import json_object_guard as _json_object_guard  # noqa: E402
_json_object_guard(staff_comms_bp)
_json_object_guard(staff_comms_mobile_bp)


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ═══ staff ═════════════════════════════════════════════════════════════════

def _membership(current_user):
    """The signed-in employee's membership, from the session alone."""
    return {"id": current_user.get("membership_id"), "user_id": current_user.get("id"),
            "employee_name": current_user.get("employee_name") or ""}


def _refuse(msg, status=400):
    return jsonify(ok=False, error=msg), status


@staff_bp.route("/api/running-late", methods=["POST"])
@staff_login_required
def api_running_late(current_user):
    """{date?, shift_start, eta_minutes, note?} → this employee's own shift
    today. One report per shift; another replaces the ETA."""
    rid = current_user["restaurant_id"]
    member = _membership(current_user)
    if not member["employee_name"]:
        return _refuse("No employee name on this session.")
    if staff_comms._limited("staff_late", member["id"], staff_comms.LATE_REPORTS_PER_HOUR):
        return _refuse("That's a lot of updates — call your manager instead.", 429)
    b = _body()
    try:
        out = staff_comms.report_late(rid, member, b.get("date"), b.get("shift_start"), b.get("eta_minutes"),
                                      note=b.get("note"))
    except staff_comms.CommsError as e:
        return _refuse(str(e))
    return jsonify(ok=True, **out)


@staff_bp.route("/api/running-late")
@staff_login_required
def api_running_late_today(current_user):
    rid = current_user["restaurant_id"]
    return jsonify(ok=True, reports=staff_comms.my_late_reports(rid, current_user.get("membership_id")),
                   eta_choices=list(staff_comms.ETA_CHOICES), eta_max=staff_comms.ETA_MAX)


@staff_bp.route("/api/inbox")
@staff_login_required
def api_inbox(current_user):
    """{announcements, unread, unread_messages} — the app's inbox and badge."""
    rid = current_user["restaurant_id"]
    return jsonify(ok=True, **staff_comms.staff_inbox(rid, current_user.get("membership_id")))


@staff_bp.route("/api/announcements/<int:announcement_id>/ack", methods=["POST"])
@staff_login_required
def api_announcement_ack(announcement_id, current_user):
    rid = current_user["restaurant_id"]
    try:
        stamp = staff_comms.ack(rid, current_user.get("membership_id"), announcement_id)
    except staff_comms.NotFound as e:
        return _refuse(str(e), 404)
    return jsonify(ok=True, id=announcement_id, acked_at=stamp)


@staff_bp.route("/api/messages")
@staff_login_required
def api_messages(current_user):
    """This employee's thread with the managers, oldest first. Opening it
    reads the managers' replies, unless ?mark_read=0."""
    rid = current_user["restaurant_id"]
    mark = request.args.get("mark_read", "1") not in ("0", "false", "no")
    return jsonify(ok=True, **staff_comms.staff_thread(rid, _membership(current_user), mark_read=mark))


@staff_bp.route("/api/messages", methods=["POST"])
@staff_login_required
def api_messages_send(current_user):
    """{body, shift_date?, request_id?, request_kind?} → to the managers."""
    rid = current_user["restaurant_id"]
    member = _membership(current_user)
    if not member["employee_name"]:
        return _refuse("No employee name on this session.")
    if staff_comms._limited("staff_msg", member["id"], staff_comms.MESSAGES_PER_HOUR):
        return _refuse("That's a lot of messages in an hour — call your manager if it's urgent.", 429)
    b = _body()
    try:
        out = staff_comms.staff_post(rid, member, b.get("body"), shift_date=b.get("shift_date"),
                                     request_id=b.get("request_id"), request_kind=b.get("request_kind"))
    except staff_comms.CommsError as e:
        return _refuse(str(e))
    return jsonify(ok=True, **out)


# ═══ owner: the Team inbox (web + mobile twins) ════════════════════════════

def _rid(u):
    return u["restaurant_id"]


def _decider(u):
    from permissions import has_permission, SCHEDULE_DRAFT
    return bool(u.get("is_admin")) or has_permission(u, SCHEDULE_DRAFT)


_NOT_DECIDER = ({"ok": False, "error": "Only a manager who handles staff requests can open the team inbox."}, 403)


def _do_inbox(u):
    if not _decider(u):
        return _NOT_DECIDER
    out = staff_comms.manager_inbox(_rid(u))
    return {"ok": True, "late_today": staff_comms.late_today(_rid(u)), **out}, 200


def _do_thread(u, thread_id):
    if not _decider(u):
        return _NOT_DECIDER
    mark = request.args.get("mark_read", "1") not in ("0", "false", "no")
    try:
        return {"ok": True, **staff_comms.manager_thread(_rid(u), thread_id, u, mark_read=mark)}, 200
    except staff_comms.NotFound as e:
        return {"ok": False, "error": str(e)}, 404


def _do_reply(u, thread_id):
    if not _decider(u):
        return _NOT_DECIDER
    try:
        out = staff_comms.manager_reply(_rid(u), thread_id, u, _body().get("body"))
    except staff_comms.NotFound as e:
        return {"ok": False, "error": str(e)}, 404
    except staff_comms.CommsError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, **out}, 200


def _do_announcements(u):
    if not _decider(u):
        return _NOT_DECIDER
    return {"ok": True, "announcements": staff_comms.list_announcements(_rid(u)),
            "roles": staff_comms.roles(_rid(u)), "audiences": list(staff_comms.AUDIENCES),
            "priorities": list(staff_comms.PRIORITIES)}, 200


def _do_announce(u):
    if not _decider(u):
        return _NOT_DECIDER
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"staff_announce:{_rid(u)}", max_calls=20, window_secs=3600):
        return {"ok": False, "error": "That's a lot of announcements in an hour — wait a little."}, 429
    b = _body()
    try:
        out = staff_comms.create_announcement(_rid(u), u, b.get("title"), b.get("body"),
                                              priority=b.get("priority"), audience=b.get("audience"),
                                              audience_value=b.get("audience_value"),
                                              expires_on=b.get("expires_on"))
    except staff_comms.CommsError as e:
        return {"ok": False, "error": str(e)}, 400
    return {"ok": True, **out}, 200


def _do_announcement(u, announcement_id):
    if not _decider(u):
        return _NOT_DECIDER
    a = staff_comms.get_announcement(_rid(u), announcement_id)
    if not a:
        return {"ok": False, "error": "No such announcement."}, 404
    return {"ok": True, "announcement": a}, 200


def _do_withdraw(u, announcement_id):
    if not _decider(u):
        return _NOT_DECIDER
    try:
        return {"ok": True, "announcement": staff_comms.withdraw_announcement(_rid(u), announcement_id, u)}, 200
    except staff_comms.NotFound as e:
        return {"ok": False, "error": str(e)}, 404


_ROUTES = [
    ("/labor/inbox", ["GET"], _do_inbox, "staff_inbox"),
    ("/labor/inbox/threads/<int:thread_id>", ["GET"], _do_thread, "staff_thread"),
    ("/labor/inbox/threads/<int:thread_id>/reply", ["POST"], _do_reply, "staff_thread_reply"),
    ("/labor/inbox/announcements", ["GET"], _do_announcements, "staff_announcements"),
    ("/labor/inbox/announcements", ["POST"], _do_announce, "staff_announce"),
    ("/labor/inbox/announcements/<int:announcement_id>", ["GET"], _do_announcement, "staff_announcement"),
    ("/labor/inbox/announcements/<int:announcement_id>/withdraw", ["POST"], _do_withdraw,
     "staff_announcement_withdraw"),
]


def _wrap(body, decorator):
    def view(current_user, **kw):
        payload, status = body(current_user, **kw)
        resp = payload if isinstance(payload, Response) else jsonify(**payload)
        resp.headers["Cache-Control"] = "no-store"
        return resp, status
    view.__name__ = body.__name__
    return decorator(view)


for _path, _methods, _body_fn, _ep in _ROUTES:
    staff_comms_bp.add_url_rule("/api" + _path, endpoint=_ep, methods=_methods,
                                view_func=_wrap(_body_fn, login_required))
    staff_comms_mobile_bp.add_url_rule(_path, endpoint=_ep, methods=_methods,
                                       view_func=_wrap(_body_fn, mobile_login_required))
