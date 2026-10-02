"""staff_account_routes.py — an employee's own account, on the staff portal.

The employee app audit (10/1/26) found the account half of the staff portal
missing: sign-out only forgot the token on the phone (the session lived on
for 14 hours), a forgotten PIN had no way back but a manager, an account
created in the app could not be deleted in it (App Store 5.1.1(v)), and the
Me tab had nothing to show about how the person is reached or where else they
work. These routes register on staff_routes.staff_bp (imported at the bottom
of staff_routes.py, so every registration of the blueprint carries them):

  POST /staff/api/logout                 end this session on the server
  POST /staff/api/pin/forgot/start       text a reset code to the phone on file
  POST /staff/api/pin/forgot/verify      the code → a reset token
  POST /staff/api/pin/forgot/set         the token + a new PIN → signed in
  POST /staff/api/account/delete         delete my login here (confirm: true)
  POST /staff/api/me/email               set or clear my email
  POST /staff/api/switch                 open another of my locations

The rules live in auth (one PIN policy, one OTP gate, one owner-notice path);
this module is the HTTP shape around them. As everywhere on the portal, the
restaurant and the person come from the session — never from the body —
except on the unauthenticated forgot-PIN steps, which name the restaurant by
its staff code exactly as sign-in does.
"""
import re

from flask import jsonify, make_response, request, url_for

import auth
from staff_routes import (_client_ip, _throttled, _token_for_native_app, staff_bp,
                          staff_session_token)


def _staff_cookie(resp, session_token):
    resp.set_cookie("staff_session", session_token, httponly=True, samesite="Lax",
                    max_age=auth.STAFF_SESSION_HOURS * 3600,
                    secure=auth.cookies_require_secure())
    return resp


# ── Owner notices ──────────────────────────────────────────────────────────

def notify_owner_of_claim(restaurant_id, claimed):
    """Every self-signup claim, to the owners and managers (M4 / SEC-01):
    the name and the last four digits of the phone that took it. Never
    raises — the claim has already landed."""
    try:
        name = claimed.get("employee_name") or "Someone"
        last4 = claimed.get("phone_last4") or ""
        auth.alert_staff_admins(
            restaurant_id, "staff_claim", "New staff sign-up",
            f"{name} created a staff login from the phone ending {last4.lstrip('…')}. If that isn't "
            "them, unlink it in Account → Staff.")
    except Exception as exc:
        print(f"[staff_account] claim notice failed rid={restaurant_id}: {exc}")


# ── Sign out ───────────────────────────────────────────────────────────────

@staff_bp.route("/api/logout", methods=["POST"])
def api_logout():
    """End this staff session on the server (M4 / LG-27 / WF-31).

    The app's sign-out only deleted its Keychain copy, and /staff/logout
    reads the browser cookie only, so a token captured earlier (a debug
    proxy, a device backup) kept working for the rest of its 14 hours. This
    takes the Bearer token or the cookie, deletes that session, and answers
    the same whether or not it was still live — a sign-out never fails.

    Optional body {"apns_token": "..."}: this phone's push token, so only
    this phone stops getting the person's staff pushes; without it, every
    phone of theirs registered at this restaurant does."""
    token = staff_session_token()
    data = request.get_json(silent=True) or {}
    ended = False
    if token:
        try:
            user = auth.get_session_user(token)
            ended = user is not None
            auth.delete_session(token)
            if user and user.get("membership_id"):
                auth.unregister_staff_push(user["id"], user.get("restaurant_id"),
                                           apns_token=(str(data.get("apns_token") or "").strip() or None))
        except Exception as exc:
            print(f"[staff_account] logout failed: {exc}")
    resp = make_response(jsonify(ok=True, signed_out=True, was_signed_in=ended))
    resp.delete_cookie("staff_session")
    return resp


# ── Forgot PIN (H10) ───────────────────────────────────────────────────────

@staff_bp.route("/api/pin/forgot/start", methods=["POST"])
def api_pin_forgot_start():
    """Text a reset code to the phone on file. The same answer whether or
    not the number has a login here."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    auth.record_portal_attempt(ip)
    data = request.get_json(silent=True) or {}
    rid = auth.restaurant_for_staff_code(data.get("join_code") or data.get("code") or "")
    if not rid:
        return jsonify(ok=False, error="We don't recognise that code. Check with your manager."), 404
    try:
        result = auth.start_pin_reset(rid, data.get("phone") or "")
    except auth.SignupError as se:
        return jsonify(ok=False, error=str(se)), 400
    out = {"ok": True, "message": "If that number is on file here, we've texted it a code."}
    if result.get("dev_code"):
        out["dev_code"] = result["dev_code"]
    return jsonify(**out)


@staff_bp.route("/api/pin/forgot/verify", methods=["POST"])
def api_pin_forgot_verify():
    """The texted code → a reset token, good for 15 minutes and one PIN."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    attempt = auth.record_portal_attempt(ip)
    data = request.get_json(silent=True) or {}
    try:
        found = auth.verify_pin_reset(data.get("phone") or "", data.get("code") or "")
    except auth.SignupError as se:
        return jsonify(ok=False, error=str(se)), 400
    auth.mark_portal_attempt_ok(attempt)
    from models import get_restaurant
    restaurant = get_restaurant(found["restaurant_id"])
    return jsonify(ok=True, reset_token=found["reset_token"], employee_name=found["employee_name"],
                   restaurant=(restaurant.name if restaurant else ""))


@staff_bp.route("/api/pin/forgot/set", methods=["POST"])
def api_pin_forgot_set():
    """Set the new PIN and sign in with it. Every other staff session this
    login had at the restaurant ends (set_membership_pin), and its lockout
    lifts — this is the way out of one that does not need a manager."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    attempt = auth.record_portal_attempt(ip)
    data = request.get_json(silent=True) or {}
    try:
        done = auth.complete_pin_reset((data.get("reset_token") or "").strip(), data.get("pin") or "",
                                       ip_address=ip)
    except auth.PinError as pe:
        return jsonify(ok=False, error=str(pe)), 400
    except auth.SignupError as se:
        return jsonify(ok=False, error=str(se), reset_expired=True), 400
    auth.mark_portal_attempt_ok(attempt)
    session_token = auth.create_staff_session(
        done["user_id"], done["restaurant_id"], ip_address=ip,
        user_agent=request.headers.get("User-Agent", ""),
        device_id=(data.get("device_id") or "").strip() or None)
    from models import get_restaurant
    restaurant = get_restaurant(done["restaurant_id"])
    resp = make_response(jsonify(ok=True, signed_in=True, redirect=url_for("staff.portal_home"),
                                 employee_name=done["employee_name"], membership_id=done["membership_id"],
                                 restaurant=(restaurant.name if restaurant else ""),
                                 portal_token=auth.get_or_create_staff_portal_token(done["restaurant_id"]),
                                 **_token_for_native_app(data, session_token)))
    return _staff_cookie(resp, session_token)


# ── Delete my account (C10) ────────────────────────────────────────────────

@staff_bp.route("/api/account/delete", methods=["POST"])
@auth.staff_login_required
def api_account_delete(current_user):
    """Delete the login this person holds at this restaurant — App Store
    guideline 5.1.1(v): an account created in the app can be deleted in it.

    {"confirm": true} is required, so a stray request cannot do it. The
    login ends (auth.delete_own_staff_account: PIN, texts consent and
    claimed phone cleared; sessions here ended; emailed schedule links
    expired; the identity closed when it has no other login), the owners and
    managers are told, and the restaurant's account activity records it."""
    data = request.get_json(silent=True) or {}
    if data.get("confirm") is not True:
        return jsonify(ok=False, error="Confirm that you want to delete your account."), 400
    rid = current_user["restaurant_id"]
    result = auth.delete_own_staff_account(current_user["id"], rid)
    if not result.get("ok"):
        return jsonify(ok=False, error="This isn't a staff account."), 403
    name = result.get("employee_name") or "An employee"
    last4 = (result.get("phone_last4") or "").lstrip("…")
    try:
        from models import log_event
        log_event(rid, "staff_account_deleted", {
            "detail": f"{name} deleted their own staff login" + (f" (phone ending {last4})" if last4 else ""),
            "actor": current_user.get("username"), "membership_id": result.get("membership_id"),
            "identity_closed": result.get("identity_closed")})
    except Exception as exc:
        print(f"[staff_account] deletion not recorded rid={rid}: {exc}")
    try:
        auth.alert_staff_admins(rid, "staff_account_deleted", "Staff account deleted",
                                f"{name} deleted their staff login. They're still on your roster and "
                                "schedule; take them off the roster if they've left.")
    except Exception as exc:
        print(f"[staff_account] deletion notice failed rid={rid}: {exc}")
    resp = make_response(jsonify(ok=True, deleted=True, signed_out=True))
    resp.delete_cookie("staff_session")
    return resp


# ── Me (M12) ───────────────────────────────────────────────────────────────

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def _phone_on_file(current_user, membership):
    return (current_user.get("phone") or (membership or {}).get("claimed_by_phone") or "").strip()


def me_details(current_user, restaurant_id, name, membership) -> dict:
    """What GET /staff/api/me adds for the Me tab. Each part degrades to its
    empty form on its own, so the profile always loads."""
    out = {"phone_masked": "", "has_phone": False, "email": "",
           "notifications": {"push": False, "texts": False, "texts_consent": False, "email": False},
           "locations": [], "restaurant_id": restaurant_id,
           "membership_id": (membership or {}).get("id")}
    phone = _phone_on_file(current_user, membership)
    if phone:
        out["phone_masked"] = auth._mask_phone(phone)       # "(•••) •••-2233"
        out["has_phone"] = True
    try:
        import people
        channel = (people.reach(restaurant_id, [name]) or {}).get(name) or {}
        out["email"] = channel.get("email") or ""
        out["notifications"] = {"push": bool(channel.get("push_user_id")), "texts": bool(channel.get("sms")),
                                "texts_consent": bool((membership or {}).get("schedule_texts_at")),
                                "email": bool(channel.get("email"))}
    except Exception as exc:
        print(f"[staff_account] reach unavailable rid={restaurant_id}: {exc}")
    try:
        out["locations"] = [dict(loc, current=(loc["restaurant_id"] == restaurant_id))
                            for loc in auth.staff_locations_for_user(current_user["id"])]
    except Exception as exc:
        print(f"[staff_account] locations unavailable rid={restaurant_id}: {exc}")
    return out


@staff_bp.route("/api/me/email", methods=["POST"])
@auth.staff_login_required
def api_me_email(current_user):
    """Set or clear the email notices fall back to. It is the same address
    the owner sees on the person's roster card (staff_contacts), so there is
    one email for one person; "" clears it."""
    data = request.get_json(silent=True) or {}
    email = " ".join(str(data.get("email") or "").split())
    if email and (len(email) > 254 or not _EMAIL.fullmatch(email)):
        return jsonify(ok=False, error="That doesn't look like an email address."), 400
    rid = current_user["restaurant_id"]
    name = current_user.get("employee_name") or ""
    if not name:
        return jsonify(ok=False, error="This isn't a staff account."), 403
    from models import set_staff_contact
    set_staff_contact(rid, name, email, None)
    return jsonify(ok=True, email=email)


@staff_bp.route("/api/switch", methods=["POST"])
@auth.staff_login_required
def api_switch(current_user):
    """Open another location this person works at.

    Deliberately NOT a session without a PIN. Each restaurant's PIN is its
    own credential — its owner resets it, its lockout guards it, its owner
    can unlink it — and a staff session is the least trusted place a session
    lives (a shared tablet left signed in). Minting B's session from A's
    would let whoever holds A's open tablet act as this person at every
    location they work. So the answer is what B's own PIN pad needs: its
    portal token, this person's membership there, and a fresh sign-in nonce;
    the app shows B's PIN pad with their name already chosen and posts to
    /staff/r/<portal_token>/login."""
    data = request.get_json(silent=True) or {}
    try:
        target = int(data.get("restaurant_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick a location."), 400
    loc = next((loc for loc in auth.staff_locations_for_user(current_user["id"])
                if loc["restaurant_id"] == target), None)
    if not loc:
        return jsonify(ok=False, error="You don't have a login at that location."), 404
    current = target == current_user["restaurant_id"]
    return jsonify(ok=True, requires_pin=not current, current=current, restaurant_id=target,
                   restaurant=loc["restaurant"], membership_id=loc["membership_id"],
                   employee_name=loc["employee_name"], portal_token=loc["portal_token"],
                   join_code=loc["join_code"],
                   login_nonce=("" if current else auth.issue_portal_nonce(target)))
