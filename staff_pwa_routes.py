"""staff_pwa_routes.py — the installable portal and on-call offers, on the
staff blueprint (scheduler audit 10/10/26 MARKET-18, MARKET-26). The
organization's bench has no route of its own: its one opt-in is the travel
switch (/staff/api/travel, cross_site), which records the bench consent
(org_bench.record_travel).

Registered on staff_routes.staff_bp (imported at the end of staff_routes,
like staff_me_routes). Every authenticated route takes the restaurant, the
login and the name from the session — never from the URL or a body.

  GET  /staff/manifest.webmanifest, /staff/sw.js, /staff/offline   the shell
       (public: no personal data in any of them)
  GET  /staff/api/push/key                 {enabled, public_key}
  POST /staff/api/push/subscribe           {endpoint, keys: {p256dh, auth}}
  POST /staff/api/push/unsubscribe         {endpoint}
  GET  /staff/api/standby                  my on-call offers and slots
  POST /staff/api/standby/<id>/respond     {accept, agree}
"""
import json

from flask import Response, jsonify, request

from auth import staff_login_required

import staff_pwa
# Imported by staff_routes right after staff_bp is made, so only staff_bp is
# read at import time; its helpers are reached at call time.
import staff_routes as _sr

staff_bp = _sr.staff_bp


def _ctx(current_user):
    return _sr._staff_context(current_user)


def _bearer() -> bool:
    return not request.cookies.get("staff_session") and \
        request.headers.get("Authorization", "").startswith("Bearer ")


# ── the shell (public, nothing personal) ───────────────────────────────────

@staff_bp.route("/manifest.webmanifest")
def pwa_manifest():
    resp = Response(json.dumps(staff_pwa.manifest()), mimetype="application/manifest+json")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@staff_bp.route("/sw.js")
def pwa_service_worker():
    resp = Response(staff_pwa.service_worker(), mimetype="application/javascript")
    # Always re-checked, so a fix is never stranded in a browser.
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/staff/"
    return resp


@staff_bp.route("/offline")
def pwa_offline():
    resp = Response(staff_pwa.OFFLINE_PAGE, mimetype="text/html")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ── Web Push ───────────────────────────────────────────────────────────────

@staff_bp.route("/api/push/key")
@staff_login_required
def api_push_key(current_user):
    import web_push
    pub, _k = web_push.vapid_keys()
    return jsonify(ok=True, enabled=pub is not None, public_key=pub)


@staff_bp.route("/api/push/subscribe", methods=["POST"])
@staff_login_required
def api_push_subscribe(current_user):
    rid, _name = _ctx(current_user)
    body = request.get_json(silent=True) or {}
    try:
        staff_pwa.subscribe(current_user["id"], rid, body.get("subscription") or body,
                            platform=staff_pwa.platform_of(request.headers, False))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, subscribed=True)


@staff_bp.route("/api/push/unsubscribe", methods=["POST"])
@staff_login_required
def api_push_unsubscribe(current_user):
    body = request.get_json(silent=True) or {}
    return jsonify(ok=True, removed=staff_pwa.unsubscribe(current_user["id"], body.get("endpoint")))


# ── on call (MARKET-26) ────────────────────────────────────────────────────

@staff_bp.route("/api/standby")
@staff_login_required
def api_standby(current_user):
    rid, name = _ctx(current_user)
    import standby_insurance
    return jsonify(ok=True, slots=standby_insurance.for_staff(rid, name) if name else [])


@staff_bp.route("/api/standby/<int:slot_id>/respond", methods=["POST"])
@staff_login_required
def api_standby_respond(slot_id, current_user):
    rid, name = _ctx(current_user)
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("accept"), bool):
        return jsonify(ok=False, error="accept must be true or false."), 400
    import standby_insurance
    try:
        slot = standby_insurance.respond(rid, slot_id, name, body["accept"], agree=body.get("agree") is True,
                                         channel="app" if _bearer() else "portal")
    except standby_insurance.StandbyError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, slot={k: slot.get(k) for k in ("id", "status", "consent_at")})
