"""staff_me_routes.py — the employee's own figures, on the staff blueprint.

Routes for staff_insights (employee audit B6, 10/1/26): hours and tips per
shift, personal stats, guest recognition, the post-shift pulse and the
calendar feed. Registered on staff_routes.staff_bp (imported at the end of
staff_routes, so they exist before the blueprint is registered).

Every authenticated route takes the restaurant, the membership and the name
from the session (staff_routes._staff_context) — never from the URL, a query
string or a body — so a payload can only ever be the caller's own. The one
public route is the calendar feed, whose token is the credential (stored
only as its hash), throttled per address like the portal's other codes.
"""
from flask import Response, jsonify, request

from auth import (mark_portal_attempt_ok, portal_attempts_exceeded, record_portal_attempt,
                  staff_login_required)

import staff_insights
# Imported by staff_routes right after staff_bp is made, so only staff_bp is
# read at import time; its helpers are reached at call time.
import staff_routes as _sr

staff_bp = _sr.staff_bp


def _staff_context(current_user):
    return _sr._staff_context(current_user)


def _client_ip():
    return _sr._client_ip()


def _membership_id(current_user):
    return current_user.get("membership_id")


def _db():
    import models
    return models.DB_PATH


@staff_bp.route("/api/earnings")
@staff_login_required
def api_earnings(current_user):
    """My hours and tips per shift, as the POS reported them (H11)."""
    rid, name = _staff_context(current_user)
    return jsonify(ok=True, **staff_insights.my_earnings(rid, name, days=request.args.get("days"), db_path=_db()))


@staff_bp.route("/api/stats")
@staff_login_required
def api_stats(current_user):
    """This week's hours, the overtime heads-up, my own attendance and my
    certifications (V2). Never a reliability score."""
    rid, name = _staff_context(current_user)
    return jsonify(ok=True, **staff_insights.my_stats(rid, name, db_path=_db()))


@staff_bp.route("/api/recognition")
@staff_login_required
def api_recognition(current_user):
    """Guests who named me in a positive review the owner confirmed (V5)."""
    rid, name = _staff_context(current_user)
    return jsonify(ok=True, **staff_insights.my_recognition(rid, name, db_path=_db()))


@staff_bp.route("/api/pulse")
@staff_login_required
def api_pulse(current_user):
    """My recent pulse answers and the shift still waiting for one."""
    rid, name = _staff_context(current_user)
    mid = _membership_id(current_user)
    if not mid:
        return jsonify(ok=False, error="This isn't a staff account."), 403
    return jsonify(ok=True, **staff_insights.my_pulse(rid, mid, name, db_path=_db()))


@staff_bp.route("/api/pulse", methods=["POST"])
@staff_login_required
def api_pulse_submit(current_user):
    """{date, rating 1-5, note?} — how one of my shifts went, once (V6)."""
    rid, name = _staff_context(current_user)
    mid = _membership_id(current_user)
    if not mid:
        return jsonify(ok=False, error="This isn't a staff account."), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Send {date, rating, note}."), 400
    try:
        saved = staff_insights.record_pulse(rid, mid, name, data.get("date"), data.get("rating"),
                                            note=data.get("note"), db_path=_db())
    except staff_insights.PulseError as e:
        return jsonify(ok=False, error=str(e)), e.status
    return jsonify(ok=True, pulse=saved)


@staff_bp.route("/api/calendar-link")
@staff_login_required
def api_calendar_link(current_user):
    """My calendar feed link (M11) — the same one every time until I reset it."""
    rid, _name = _staff_context(current_user)
    mid = _membership_id(current_user)
    if not mid:
        return jsonify(ok=False, error="This isn't a staff account."), 403
    return jsonify(ok=True, **staff_insights.calendar_link(rid, mid, db_path=_db()))


@staff_bp.route("/api/calendar-link/reset", methods=["POST"])
@staff_login_required
def api_calendar_link_reset(current_user):
    """Retire my feed link and make a new one (a link shared by mistake)."""
    rid, _name = _staff_context(current_user)
    mid = _membership_id(current_user)
    if not mid:
        return jsonify(ok=False, error="This isn't a staff account."), 403
    return jsonify(ok=True, **staff_insights.calendar_link(rid, mid, rotate=True, db_path=_db()))


@staff_bp.route("/api/calendar-link/revoke", methods=["POST"])
@staff_login_required
def api_calendar_link_revoke(current_user):
    """Turn my calendar feed off."""
    rid, _name = _staff_context(current_user)
    mid = _membership_id(current_user)
    if not mid:
        return jsonify(ok=False, error="This isn't a staff account."), 403
    return jsonify(ok=True, revoked=staff_insights.revoke_calendar_links(rid, mid, db_path=_db()))


@staff_bp.route("/cal/<token>.ics")
def calendar_feed(token):
    """The subscribed calendar's fetch: no session (a calendar app has
    none); the token is the credential. Unknown → 404, revoked or holder
    deactivated → 410, both with no body worth reading."""
    ip = _client_ip()
    if portal_attempts_exceeded(ip):
        return Response("Too many requests.", status=429, mimetype="text/plain")
    attempt = record_portal_attempt(ip)
    body, status = staff_insights.calendar_feed(token, db_path=_db())
    if status == 404:
        return Response("Not found.", status=404, mimetype="text/plain")
    mark_portal_attempt_ok(attempt)       # a real link: not a guess
    if status != 200:
        return Response("This calendar link has been turned off.", status=status, mimetype="text/plain")
    resp = Response(body, status=200, mimetype="text/calendar")
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = 'inline; filename="shifts.ics"'
    resp.headers["Cache-Control"] = "private, max-age=900"
    return resp
