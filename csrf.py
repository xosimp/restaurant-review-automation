"""
csrf.py — double-submit CSRF protection for every browser-facing blueprint.

The login/2FA/password-reset forms got per-form CSRF tokens during the
security audit, but the 90+ state-changing fetch() endpoints behind login
(admin actions, approvals, settings saves, POS connects) had nothing beyond
SameSite=Lax on the session cookie. This closes that: a JS-readable cookie is
issued with the page, a fetch wrapper echoes it back as an X-CSRF header, and
a before_request hook on each protected blueprint requires the two to match.

Deliberately NOT applied to: webhook_bp (external callers verified by HMAC
signature), auth_bp (has its own form-token flow), status_bp (public GETs).
"""
import hmac
import os
import secrets

from flask import request, jsonify

CSRF_COOKIE = "csrf_js"          # readable by JS on purpose — that's the double-submit design
CSRF_HEADER = "X-CSRF"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))


def ensure_csrf_cookie(resp):
    """after_request: make sure every browser session carries the token.
    Idempotent — only sets when absent so the value stays stable across
    open tabs."""
    try:
        if not request.cookies.get(CSRF_COOKIE):
            resp.set_cookie(
                CSRF_COOKIE, secrets.token_urlsafe(32),
                max_age=30 * 24 * 3600,
                httponly=False, secure=_on_railway, samesite="Lax",
            )
    except Exception:
        pass
    return resp


def _token_from_request():
    tok = request.headers.get(CSRF_HEADER, "")
    if tok:
        return tok
    # Fallbacks for any future non-fetch caller
    if request.is_json:
        try:
            return (request.get_json(silent=True) or {}).get("csrf_token", "") or ""
        except Exception:
            return ""
    return request.form.get("csrf_token", "")


def csrf_protect(blueprint):
    """Attach enforcement to a blueprint. State-changing requests must echo
    the csrf_js cookie back in the X-CSRF header (or csrf_token field)."""

    @blueprint.before_request
    def _check_csrf():
        if request.method in _SAFE_METHODS:
            return None
        cookie_tok = request.cookies.get(CSRF_COOKIE, "")
        sent_tok = _token_from_request()
        if cookie_tok and sent_tok and hmac.compare_digest(cookie_tok, sent_tok):
            return None
        return jsonify(ok=False, error="Request blocked (CSRF). Refresh the page and try again."), 403

    return blueprint
