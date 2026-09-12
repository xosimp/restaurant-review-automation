"""
mobile_api.py — JSON API for the Cavnar AI iOS app.

A separate blueprint from client_api.py's client_bp, for two concrete
reasons:

1. CSRF. client_bp is wrapped in csrf_protect() (see csrf.py), a cookie-based
   double-submit scheme a bearer-token client has no cookie jar to carry.
   This blueprint is never passed to csrf_protect() — the same exemption
   pattern webhook_bp already uses for its own HMAC-verified callers.
2. Auth. login_required (auth.py) only reads a session cookie, and its
   redirect-vs-JSON branch hardcodes an "/api/" path prefix that a
   "/mobile/api/" route wouldn't match. Rather than teach a decorator three
   other blueprints depend on to also handle bearer tokens, this blueprint
   gets its own: auth.mobile_login_required.

Business logic is NOT duplicated: every route below that has a web
equivalent calls the exact same _do_*() helper client_api.py's own routes
call, so there's one implementation behind both the dashboard and the app.
"""
import base64
import hmac
import os
import random
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

from auth import (
    verify_password, create_session, delete_session,
    revoke_other_sessions, mobile_login_required, get_user_by_restaurant_id,
    update_last_login,
)
from auth_routes import _is_rate_limited, _record_failed_attempt, _clear_attempts, _get_client_ip
from models import get_restaurant, update_restaurant, get_conn

import client_api as _capi


def _html_doc(fragment, bg="#f7f4ef"):
    """Wrap a bare fragment in a real HTML document so its background fills
    the mail client's viewport instead of stopping at the content's height
    (the half-cut-off look). Imported lazily: emails.py reads RESEND_API_KEY
    at module scope, and a module-level import here could bind it before
    load_dotenv() runs. See emails._html_document."""
    from emails import html_document
    return html_document(fragment, bg)



mobile_bp = Blueprint('mobile_api', __name__, url_prefix='/mobile/api')

# Exception text handed to a client, with credentials stripped — a
# requests error carries the failing URL, and a Places URL carries key=.
from ai_guard import safe_error as _safe_err


# Same one-liner duplication as admin_routes.py/scheduler.py/audit_app.py —
# only needed here for the two routes (test-digest preview, data export)
# that send a dynamically-built report/attachment rather than one of
# emails.py's fixed templates, so they can't go through emails.py's own
# senders like every other email in this file does.
def _resend_key(): return os.getenv("RESEND_API_KEY", "")
def _from_email(): return os.getenv("FROM_EMAIL", "will@cavnar.ai")


def _public_user(user):
    """Fields safe to hand to the client — never the password hash."""
    return {
        "id": user["id"], "username": user["username"], "email": user["email"],
        "restaurant_id": user["restaurant_id"], "role": user.get("role") or "client",
        "is_admin": bool(user.get("is_admin")),
        # Lets both clients hide the editing controls rather than letting a
        # teammate discover the restriction by being refused.
        "can_manage_team": True if user.get("can_manage_team") is None
                           else bool(user.get("can_manage_team")),
    }


def _bearer_token():
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def _send_login_notification(user, ip, user_agent):
    try:
        rid = user.get("restaurant_id")
        rest = get_restaurant(rid) if rid else None
        if rest and getattr(rest, "login_notify", 0) and rest.owner_email:
            from notify import send_login_alert
            send_login_alert(rid, rest.name or "", rest.owner_email, ip, user_agent,
                             report_url=_login_report_url(user["id"]))
    except Exception as e:
        print(f"[LoginNotify-mobile] {e}")


def _login_report_url(user_id: int) -> str:
    """One-time link for the login email's 'This wasn't me' button — see
    auth.consume_login_report for what it does."""
    from auth import create_login_report
    base = os.getenv("BASE_URL", "https://dashboard.cavnar.ai").rstrip("/")
    return f"{base}/auth/not-me/{create_login_report(user_id, None)}"


def _log_account_event(restaurant_id, event_type, current_user=None, detail=None):
    """Account activity log (Account -> Security -> Account activity).
    Thin delegate — the implementation is shared with the web routes so a
    change made in either surface is recorded identically."""
    _capi.log_account_event(restaurant_id, event_type, current_user, detail)


_APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_apple_jwk_client = None


def _verify_apple_identity_token(identity_token, bundle_id):
    """Verifies the JWT ASAuthorizationController hands the app against
    Apple's own public signing keys — no shared secret involved, unlike
    Google's OAuth code exchange. Raises on any failure (bad signature,
    wrong audience/issuer, expired); callers turn that into a 401."""
    import jwt as _pyjwt
    global _apple_jwk_client
    if _apple_jwk_client is None:
        _apple_jwk_client = _pyjwt.PyJWKClient(_APPLE_JWKS_URL)
    signing_key = _apple_jwk_client.get_signing_key_from_jwt(identity_token)
    return _pyjwt.decode(
        identity_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=bundle_id,
        issuer="https://appleid.apple.com",
    )


@mobile_bp.route("/apple-signin", methods=["POST"])
def mobile_apple_signin():
    """Native Sign In with Apple — unlike Google's web-redirect OAuth flow,
    ASAuthorizationController hands the app a signed identity token directly
    on-device, so there's no callback/deep-link involved: the app just POSTs
    the token here. Matches by apple_user_id (Apple's stable per-app user
    identifier) first, falling back to email — Apple only includes the
    user's email on their very first-ever Sign In with Apple for this app,
    so apple_user_id is the only reliable match on every login after that."""
    data = request.get_json() or {}
    device_id = (data.get("device_id") or "").strip() or None
    identity_token = data.get("identity_token") or ""
    if not identity_token:
        return jsonify(ok=False, error="Missing Apple identity token"), 400

    bundle_id = os.getenv("APNS_BUNDLE_ID", "ai.cavnar.CavnarAI")
    try:
        payload = _verify_apple_identity_token(identity_token, bundle_id)
    except Exception:
        return jsonify(ok=False, error="Couldn't verify Sign in with Apple. Try again."), 401

    apple_user_id = payload.get("sub", "")
    email = (payload.get("email") or "").lower().strip()
    if not apple_user_id:
        return jsonify(ok=False, error="Apple didn't return a valid identity."), 401

    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE apple_user_id=? AND is_active=1 LIMIT 1", (apple_user_id,)
    ).fetchone()
    if not row and email:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(email)=? AND is_active=1 LIMIT 1", (email,)
        ).fetchone()
    if row and not row["apple_user_id"]:
        conn.execute("UPDATE users SET apple_user_id=? WHERE id=?", (apple_user_id, row["id"]))
        conn.commit()
    conn.close()

    if not row:
        # Reads as a dead end otherwise, which is exactly what an App Store
        # reviewer sees if they tap "Continue with Apple" before reading the
        # review notes. Say what this app is and how an account is obtained.
        return jsonify(ok=False, error=(
            "Cavnar AI accounts are set up for a restaurant by Will directly — there is no "
            "self-serve signup. If you already have one, sign in with the username and password "
            "you were given. Otherwise email will@cavnar.ai."
        )), 401

    user = dict(row)
    ip = _get_client_ip()
    ua = request.headers.get("User-Agent", "Cavnar-iOS")
    token = create_session(user["id"], ip_address=ip, user_agent=ua, device_type="ios", device_id=device_id, restaurant_id=user["restaurant_id"])
    update_last_login(user["id"])
    _send_login_notification(user, ip, ua)
    return jsonify(ok=True, token=token, user=_public_user(user))


# ── Auth ──────────────────────────────────────────────────────────────────

@mobile_bp.route("/login", methods=["POST"])
def mobile_login():
    """Mirrors auth_routes.login()'s POST branch, minus the HTML/redirect/CSRF
    parts a native client has no use for. Same IP rate limiting (shared with
    the web login's in-memory limiter — one brute-force counter, not two),
    same 2FA pending-token scheme, same create_session() — just JSON in,
    JSON out, and device_type='ios' on the resulting session."""
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return jsonify(ok=False, error="Too many failed attempts. Please wait 5 minutes and try again."), 429
    data = request.get_json() or {}
    device_id = (data.get("device_id") or "").strip() or None
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = verify_password(username, password)
    if not user:
        _record_failed_attempt(ip)
        return jsonify(ok=False, error="Invalid username or password"), 401
    _clear_attempts(ip)
    # A sign-in reported as "not me" burns the password until it's reset —
    # see auth.consume_login_report.
    if user.get("must_reset_password"):
        return jsonify(ok=False, error="This account needs a password reset before signing in — use Forgot password.",
                       password_reset_required=True), 403

    rid = user.get("restaurant_id")
    rest = get_restaurant(rid) if rid and not user.get("is_admin") else None
    # iOS persists the "remember this device" value in Keychain (rather than
    # the web's device_token_<rid> cookie) and resends it here.
    device_token = (data.get("device_token") or "").strip()
    two_fa_on = bool(rest and rest.two_fa_enabled and not user.get("is_admin"))
    from auth import trusted_device_ok
    device_ok = bool(device_token) and trusted_device_ok(rid, device_token)

    if two_fa_on and not device_ok:
        code = str(random.randint(100000, 999999))
        expires = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        import secrets as _secrets
        pending = _secrets.token_hex(24)
        update_restaurant(rid, {"two_fa_code": code, "two_fa_expires": expires, "two_fa_pending": pending})
        masked = "your registered email"
        try:
            if rest.two_fa_method == "sms" and rest.owner_phone:
                from notify import send_2fa_sms
                send_2fa_sms(rest.owner_phone, rest.name or "your restaurant", code)
                masked = "(•••) •••-" + "".join(c for c in rest.owner_phone if c.isdigit())[-4:]
            else:
                email = rest.owner_email or ""
                if "@" in email:
                    from emails import send_2fa_code
                    send_2fa_code(email, rest.name or "your restaurant", code, rest.owner_name)
                    masked = email[:2] + "***@" + email.split("@")[-1]
        except Exception:
            pass
        pending_encoded = base64.urlsafe_b64encode(f"{rid}:{pending}".encode()).decode()
        return jsonify(ok=True, requires_2fa=True, pending_token=pending_encoded, masked_email=masked)

    ua = request.headers.get("User-Agent", "Cavnar-iOS")
    token = create_session(user["id"], ip_address=ip, user_agent=ua, device_type="ios", device_id=device_id, restaurant_id=user["restaurant_id"])
    _send_login_notification(user, ip, ua)
    return jsonify(ok=True, requires_2fa=False, token=token, user=_public_user(user))


@mobile_bp.route("/forgot-password", methods=["POST"])
def mobile_forgot_password():
    """The app's Forgot Password sheet. Same reset-token + 1-hour emailed
    link the web /forgot-password form sends (the link itself still opens
    the web reset page — that's fine, it's a one-time flow). Always
    answers ok=True whether or not the email exists, same as the web,
    so this can't be used to enumerate accounts. Rate-limited on the
    shared in-memory counter the login routes use."""
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return jsonify(ok=False, error="Too many requests — please wait a few minutes and try again."), 429
    _record_failed_attempt(ip)
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify(ok=False, error="Enter the email address on your account."), 400
    try:
        # In-app flow: a 6-digit code the sheet asks for, not the web's
        # emailed link. Stored in the same reset_token/expires columns the
        # web flow uses, so the two can't both be live for one user at once
        # — whichever was requested last is the one that works.
        import random as _rnd
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        code = f"{_rnd.randint(0, 999999):06d}"
        conn = get_conn()
        row = conn.execute("SELECT id FROM users WHERE LOWER(email)=? AND is_active=1", (email,)).fetchone()
        if row:
            expires = (_dt.now(_tz.utc) + _td(hours=1)).isoformat()
            conn.execute("UPDATE users SET reset_token=?, reset_token_expires=? WHERE id=?", (code, expires, row["id"]))
            conn.commit()
        conn.close()
        if row:
            from emails import send_password_reset_code_email
            send_password_reset_code_email(email, code)
    except Exception as e:
        print(f"[forgot-password-mobile] {e}")
    return jsonify(ok=True)


@mobile_bp.route("/reset-password", methods=["POST"])
def mobile_reset_password():
    """Second half of the in-app reset: {email, code, new_password}. The
    code is only ever checked against the row for THAT email (never looked
    up on its own — a bare 6-digit lookup would let anyone reset any
    account by guessing), compared constant-time, and rejected past its
    1-hour expiry. Goes through auth.update_password so the change is
    stamped like any other (password_changed_at/strength). Rate-limited on
    the shared login counter so the code can't be brute-forced."""
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return jsonify(ok=False, error="Too many attempts — please wait a few minutes and try again."), 429
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    code = (data.get("code") or "").strip()
    new_password = data.get("new_password") or ""
    if len(new_password) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters."), 400
    if len(code) != 6 or not code.isdigit():
        _record_failed_attempt(ip)
        return jsonify(ok=False, error="That code isn't right. Check the email and try again."), 400

    import hmac as _hmac
    from datetime import datetime as _dt, timezone as _tz
    conn = get_conn()
    row = conn.execute(
        "SELECT id, reset_token, reset_token_expires FROM users WHERE LOWER(email)=? AND is_active=1", (email,)
    ).fetchone()
    conn.close()
    stored = (row["reset_token"] if row else "") or ""
    if not row or not stored or not _hmac.compare_digest(stored, code):
        _record_failed_attempt(ip)
        return jsonify(ok=False, error="That code isn't right. Check the email and try again."), 400
    try:
        exp = _dt.fromisoformat((row["reset_token_expires"] or "").replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=_tz.utc)
        if _dt.now(_tz.utc) > exp:
            return jsonify(ok=False, error="That code has expired — request a new one."), 400
    except Exception:
        return jsonify(ok=False, error="That code has expired — request a new one."), 400

    from auth import update_password
    update_password(row["id"], new_password)
    conn = get_conn()
    conn.execute("UPDATE users SET reset_token=NULL, reset_token_expires=NULL WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    _clear_attempts(ip)
    try:
        restaurant = get_restaurant(get_user_by_email_rid(email))
        if restaurant and restaurant.owner_email:
            from emails import send_password_changed_email
            send_password_changed_email(restaurant.owner_email, restaurant.name or "your restaurant", restaurant.owner_name)
    except Exception:
        pass
    return jsonify(ok=True)


def get_user_by_email_rid(email):
    conn = get_conn()
    row = conn.execute("SELECT restaurant_id FROM users WHERE LOWER(email)=? AND is_active=1", (email,)).fetchone()
    conn.close()
    return row["restaurant_id"] if row else None


# Self-serve signup is CLOSED by default (audit #5 P0).
#
# The route creates a restaurant on the Restaurant dataclass's own defaults —
# all four modules on, billing_status 'trial' — and 'trial' is in
# ACTIVE_BILLING_STATES, so the account had full entitlement including Intel.
# Nothing in this codebase expires a trial, so it kept that access forever,
# drawing on the same global AI ceiling paying clients draw on. Hiding the
# Sign Up button would have left the endpoint answering on the open internet,
# which is where the exposure actually was.
#
# Every client today is onboarded by hand. When self-serve is wanted, set
# ALLOW_PUBLIC_SIGNUP=1 — and fix the entitlement defaults first: a signup
# should not arrive full-tier, and unpaid accounts need their own AI budget
# (see ai_utils.AI_UNPAID_* ).
def public_signup_open() -> bool:
    return (os.getenv("ALLOW_PUBLIC_SIGNUP", "") or "").strip().lower() in ("1", "true", "yes", "on")


@mobile_bp.route("/register", methods=["POST"])
def mobile_register():
    """Self-serve signup from the app's Sign Up screen — the first public
    registration path; every account before this was created by hand in
    the admin panel (admin_routes.create_client). Mirrors that route's
    restaurant + user creation with the Restaurant dataclass's own
    defaults (trial tier, all four modules on), then signs the new user
    straight in — same {token, user} shape /login returns — so the app
    lands on Home instead of bouncing back to the login form. Will gets a
    heads-up email so a signup nobody set up isn't discovered later."""
    if not public_signup_open():
        return jsonify(ok=False,
                       error="Cavnar AI accounts are set up with you directly. Email will@cavnar.ai to get started."), 403

    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return jsonify(ok=False, error="Too many attempts — please wait a few minutes and try again."), 429
    data = request.get_json(silent=True) or {}
    device_id = (data.get("device_id") or "").strip() or None

    restaurant_name = (data.get("restaurant_name") or "").strip()
    owner_name = (data.get("owner_name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    phone = (data.get("phone") or "").strip() or None

    import re as _re
    if len(restaurant_name) < 2:
        return jsonify(ok=False, error="Enter your restaurant's name."), 400
    if not _re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return jsonify(ok=False, error="Enter a valid email address."), 400
    if not _re.match(r"^[a-z0-9._-]{3,30}$", username):
        return jsonify(ok=False, error="Username must be 3–30 characters — letters, numbers, dots, dashes, or underscores."), 400
    if len(password) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters."), 400

    conn = get_conn()
    existing = conn.execute(
        "SELECT id FROM users WHERE LOWER(email)=? OR LOWER(username)=?", (email, username)
    ).fetchone()
    conn.close()
    if existing:
        _record_failed_attempt(ip)
        return jsonify(ok=False, error="An account with that email or username already exists. Try signing in instead."), 409

    from models import create_restaurant, Restaurant
    from auth import create_user
    rid = create_restaurant(Restaurant(
        name=restaurant_name,
        owner_email=email,
        owner_name=owner_name or None,
        owner_phone=phone,
        sign_off_name=restaurant_name,
    ))
    uid = create_user(restaurant_id=rid, username=username, email=email, password=password)
    _clear_attempts(ip)

    try:
        from emails import send_signup_welcome_email, send_signup_admin_alert
        send_signup_welcome_email(email, restaurant_name, owner_name or None)
        send_signup_admin_alert(restaurant_name, owner_name, email, phone)
    except Exception as e:
        print(f"[register] welcome/admin email error: {e}")

    from auth import get_user_by_username
    user = get_user_by_username(username)
    ua = request.headers.get("User-Agent", "Cavnar-iOS")
    token = create_session(uid, ip_address=ip, user_agent=ua, device_type="ios", device_id=device_id, restaurant_id=rid)
    return jsonify(ok=True, token=token, user=_public_user(user)), 201


@mobile_bp.route("/verify-2fa", methods=["POST"])
def mobile_verify_2fa():
    """Mirrors auth_routes.verify_2fa()'s POST branch — same pending-token
    decode, same HMAC-compared pending secret (proves this code was actually
    issued by OUR /login call for this restaurant, not just a guessed id),
    same rate limiting, same single-use code clearing. On success, creates an
    ios-tagged session and, if remember_device is set, mints the same
    two_fa_device_token value the web flow does."""
    ip = _get_client_ip()
    if _is_rate_limited("2fa:" + ip):
        return jsonify(ok=False, error="Too many attempts. Please wait 5 minutes and try again."), 429
    data = request.get_json() or {}
    device_id = (data.get("device_id") or "").strip() or None
    pending_token = data.get("pending_token", "")
    code_entered = (data.get("code") or "").strip()
    remember = bool(data.get("remember_device"))

    try:
        decoded = base64.urlsafe_b64decode(pending_token.encode()).decode()
        rid_str, pending_secret = decoded.split(":", 1)
        rid = int(rid_str)
    except Exception:
        return jsonify(ok=False, error="Session expired — please log in again."), 401
    rest = get_restaurant(rid) if rid else None
    if not rest:
        return jsonify(ok=False, error="Session expired — please log in again."), 401

    stored_pending = rest.two_fa_pending or ""
    if not stored_pending or not hmac.compare_digest(stored_pending, pending_secret):
        _record_failed_attempt("2fa:" + ip)
        return jsonify(ok=False, error="Session expired — please log in again."), 401

    otp_matches = rest.two_fa_code and hmac.compare_digest(rest.two_fa_code, code_entered)
    if not otp_matches:
        # Not the emailed/texted code — try a 2FA backup code before
        # failing outright (unlike the OTP, backup codes have no expiry
        # window; a stolen phone with no email/SMS access is exactly the
        # scenario recovery codes exist for).
        from models import verify_and_consume_backup_code
        if not verify_and_consume_backup_code(rid, code_entered):
            _record_failed_attempt("2fa:" + ip)
            return jsonify(ok=False, error="Incorrect code. Try again."), 401
    else:
        try:
            expires = datetime.strptime(rest.two_fa_expires, "%Y-%m-%d %H:%M:%S")
        except Exception:
            expires = datetime.now()
        if datetime.now() > expires:
            return jsonify(ok=False, error="Code expired. Request a new one."), 401

    _clear_attempts("2fa:" + ip)
    update_restaurant(rid, {"two_fa_code": "", "two_fa_expires": "", "two_fa_pending": ""})
    user = get_user_by_restaurant_id(rid)
    if not user:
        return jsonify(ok=False, error="Session expired — please log in again."), 401

    ua = request.headers.get("User-Agent", "Cavnar-iOS")
    token = create_session(user["id"], ip_address=ip, user_agent=ua, device_type="ios", device_id=device_id, restaurant_id=user["restaurant_id"])
    _send_login_notification(user, ip, ua)

    device_token = None
    if remember:
        from auth import create_trusted_device, describe_user_agent
        device_token = create_trusted_device(rid, user["id"], describe_user_agent(ua) + " · app")

    return jsonify(ok=True, token=token, device_token=device_token, user=_public_user(user))


@mobile_bp.route("/me")
@mobile_login_required
def mobile_me(current_user):
    """Resolves a bearer token to its User — used after Google Sign-In,
    where the token arrives via a cavnarai:// deep link rather than the
    /login response body, so the app has no `user` object yet to complete
    the session with."""
    return jsonify(ok=True, user=_public_user(current_user))


@mobile_bp.route("/logout", methods=["POST"])
@mobile_login_required
def mobile_logout(current_user):
    # Unregister this device's APNs token first, while the bearer token is
    # still valid. Signing out used to leave the device_tokens row behind, so
    # the phone kept receiving that restaurant's review alerts and daily
    # digests indefinitely — a real problem on a shared back-office iPad or a
    # departing manager's phone. Best-effort: a push-registry failure must
    # never block the sign-out itself.
    apns_token = ((request.get_json(silent=True) or {}).get("apns_token") or "").strip()
    if apns_token:
        try:
            _unregister_device_token(current_user["restaurant_id"], apns_token)
        except Exception as e:
            import ops
            ops.capture(e, job="mobile_logout", context=f"restaurant_id={current_user['restaurant_id']}")
    token = _bearer_token()
    if token:
        delete_session(token)
    return jsonify(ok=True)


@mobile_bp.route("/sessions/revoke-others", methods=["POST"])
@mobile_login_required
def mobile_revoke_other_sessions(current_user):
    revoke_other_sessions(current_user["id"], _bearer_token())
    _log_account_event(current_user["restaurant_id"], "sessions_revoked_others", current_user)
    return jsonify(ok=True)


# ── Home ──────────────────────────────────────────────────────────────────
# No JSON equivalent of this exists on the web side — index() (hosted_
# dashboard.py) computes a much larger set of desktop-oriented figures
# (agency-equivalent marketing value, full savings-breakdown ledger, the
# onboarding checklist, holiday/event banners) and renders them straight into
# Jinja. This is a deliberately trimmed aggregate for the phone: the KPI
# numbers an owner actually glances at, plus the same "needs attention" list
# the web Home tab shows, built from the exact same underlying data
# (get_review_stats, analyse_shifts_for_restaurant, analyse_inventory) so the
# two surfaces never disagree — just less of it, and no marketing copy.

def _intel_home_kpi(restaurant):
    """No stored numeric 'average competitor rating' exists — competitor_intel
    is a JSON blob ({"competitors": [...], "insight": <narrative text>,
    "generated_at": ...}), not structured rating data. Rather than fabricate
    a number, the Home tile surfaces how many recommendations are ready to
    read (the full Intel screen does the real side-by-side comparison).
    Uses extract_recs (not parse_competitor_intel's own recommendations) to
    match the count web's stat row shows — the two parsers deliberately
    disagree on edge cases (see extract_recs's docstring), so mixing them
    made the Home tile and the web "Action items" tile show different
    counts for the same restaurant."""
    if not getattr(restaurant, "competitor_intel", None):
        return {"value": "—", "sublabel": "no data yet"}
    try:
        import json as _json
        from competitor_intel_format import extract_recs
        insight = _json.loads(restaurant.competitor_intel).get("insight", "")
        n = len(extract_recs(insight))
        return {"value": str(n), "sublabel": f"recommendation{'' if n == 1 else 's'} ready"}
    except Exception:
        return {"value": "—", "sublabel": "no data yet"}


def _home_tz(restaurant):
    from zoneinfo import ZoneInfo
    try:
        return ZoneInfo(getattr(restaurant, "timezone", None) or "America/Chicago")
    except Exception:
        return ZoneInfo("America/Chicago")


def _home_pulse(key, kpi, rstats, labor, restaurant, inv):
    """One chip for Home's pulse strip per active module: the KPI value, a
    short label, and a semantic tone ("good"/"warn"/None) that colours the
    chip's breathing dot. Computed here, not on the phone, so the same
    thresholds drive the chip, the Modules tile sublabel and the alerts."""
    if not kpi:
        return None
    value = kpi.get("value", "—")
    if key == "reviews":
        rate = int(rstats.get("response_rate", 0) or 0)
        tone = "good" if rate >= 80 else ("warn" if rstats.get("total", 0) and rate < 50 else None)
        return {"value": value, "label": f"replies · {rate}%", "tone": tone}
    if key == "labor":
        target = float(restaurant.labor_target_pct or 30.0)
        pct = (labor or {}).get("overall_labor_pct", 0) or 0
        on_track = pct <= target
        return {"value": value, "label": "labor · on target" if on_track else f"labor · over {int(target)}%",
                "tone": "good" if on_track else "warn"}
    if key == "inventory":
        recoverable = int((inv or {}).get("recoverable_monthly", 0) or 0)
        return {"value": value, "label": "recoverable", "tone": "warn" if recoverable > 0 else "good"}
    if key == "marketing":
        return {"value": value, "label": "pieces this month", "tone": None}
    if key == "intel":
        return {"value": value, "label": kpi.get("sublabel", "").replace("recommendations ready", "recommendations").replace("recommendation ready", "recommendation") or "intel", "tone": None}
    return {"value": value, "label": kpi.get("sublabel", ""), "tone": None}


def _home_query(sql, params):
    """One row, or None on any error — every Home helper query is independent,
    so a missing optional table (alert_log/marketing_content_log on a fresh
    install) costs that one number, never the whole screen."""
    try:
        conn = get_conn()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()
    except Exception:
        return None


def _home_overnight(rid):
    """What Cavnar did while the owner wasn't looking — drafts written and
    alerts fired in the last 24 hours. A rolling window rather than a literal
    'since 6pm' so the line is true whenever the app is opened; the phone
    picks 'Overnight' vs 'Since yesterday' from the clock."""
    answered = _home_query("""
        SELECT COUNT(*) AS n FROM reviews
        WHERE restaurant_id=? AND processed=1 AND deleted_at IS NULL
          AND response_status IN ('drafted','approved','posted')
          AND draft_response IS NOT NULL
          AND julianday(fetched_at) >= julianday('now','-1 day')
    """, (rid,))
    flagged = _home_query("""
        SELECT COUNT(*) AS n FROM alert_log
        WHERE restaurant_id=? AND julianday(fired_at) >= julianday('now','-1 day')
    """, (rid,))
    return {
        "answered": int((answered["n"] if answered else 0) or 0),
        "flagged": int((flagged["n"] if flagged else 0) or 0),
        "window_hours": 24,
    }


def _home_weekly_receipts(rid, active_keys, inv):
    """The 'This week — what Cavnar did for you' receipt: up to three real,
    dated things since Monday. Each is only included when its number is
    non-zero — an empty list means the phone hides the section, never
    pads it with placeholders."""
    receipts = []
    week_start = "date('now','-6 days','weekday 1')"
    if "reviews" in active_keys:
        row = _home_query(f"""
            SELECT COUNT(*) AS posted,
                   SUM(CASE WHEN review_date IS NOT NULL
                             AND (julianday(posted_at) - julianday(review_date)) * 24 <= 24 THEN 1 ELSE 0 END) AS fast
            FROM reviews
            WHERE restaurant_id=? AND deleted_at IS NULL AND response_status='posted'
              AND julianday(posted_at) >= julianday({week_start})
        """, (rid,))
        posted = int((row["posted"] if row else 0) or 0)
        if posted:
            pct = int(round(100 * ((row["fast"] or 0) / posted)))
            receipts.append({"module": "reviews",
                             "emphasis": f"{posted} {'reply' if posted == 1 else 'replies'}",
                             "text": f"published to Google — {pct}% within 24h"})
        else:
            row = _home_query(f"""
                SELECT COUNT(*) AS n FROM reviews
                WHERE restaurant_id=? AND deleted_at IS NULL AND response_status='approved'
                  AND julianday(approved_at) >= julianday({week_start})
            """, (rid,))
            approved = int((row["n"] if row else 0) or 0)
            if approved:
                receipts.append({"module": "reviews",
                                 "emphasis": f"{approved} {'reply' if approved == 1 else 'replies'}",
                                 "text": "approved in your voice"})
    if "labor" in active_keys:
        sched = _home_query(f"""
            SELECT week_start, hours_scheduled, hours_budget FROM schedule_history
            WHERE restaurant_id=? AND julianday(generated_at) >= julianday({week_start})
            ORDER BY id DESC LIMIT 1
        """, (rid,))
        if sched:
            hs = float(sched["hours_scheduled"] or 0)
            hb = float(sched["hours_budget"] or 0)
            if hb and hs and hs < hb:
                receipts.append({"module": "labor", "emphasis": "Next week's schedule",
                                 "text": f"built {int(round(hb - hs))} hrs under budget"})
            elif hs:
                receipts.append({"module": "labor", "emphasis": "Next week's schedule",
                                 "text": f"built — {int(round(hs))} hrs scheduled"})
    if "inventory" in active_keys:
        top = ((inv or {}).get("waste_items") or [None])[0]
        try:
            waste_cost = float((top or {}).get("waste_cost", 0) or 0)
        except (TypeError, ValueError):
            waste_cost = 0
        if top and waste_cost > 0:
            receipts.append({"module": "inventory",
                             "emphasis": f"${int(round(waste_cost))} of {top.get('item', 'waste')} waste",
                             "text": "flagged before your next order"})
    if "marketing" in active_keys:
        row = _home_query(f"""
            SELECT COUNT(*) AS n FROM marketing_content_log
            WHERE restaurant_id=? AND julianday(created_at) >= julianday({week_start})
        """, (rid,))
        n = int((row["n"] if row else 0) or 0)
        if n:
            receipts.append({"module": "marketing", "emphasis": f"{n} marketing {'piece' if n == 1 else 'pieces'}",
                             "text": "drafted and ready to post"})
    row = _home_query(f"""
        SELECT COUNT(*) AS n FROM alert_log
        WHERE restaurant_id=? AND julianday(fired_at) >= julianday({week_start})
    """, (rid,))
    alerts = int((row["n"] if row else 0) or 0)
    if alerts:
        receipts.append({"module": "alerts", "emphasis": f"{alerts} {'alert' if alerts == 1 else 'alerts'}",
                         "text": "sent to you before they became problems"})
    return receipts[:3]


def _setup_checklist(restaurant, rstats, labor, active_keys):
    """Retired (Sep 2026): onboarding checklists are gone from web and iOS.
    Kept as an always-empty list so older app builds still decode Home."""
    return []
    if getattr(restaurant, "onboarding_dismissed", 0):
        return []
    steps = [
        {"key": "reviews", "label": "Connect your Google reviews",
         "sub": "Live reviews flow in automatically once connected",
         "done": bool(restaurant.gmb_refresh_token or getattr(restaurant, "reviews_live", 0)),
         "module": "account"},
        {"key": "voice", "label": "Set your brand voice",
         "sub": "Teaches the AI how you talk to guests",
         "done": bool(restaurant.voice_notes), "module": "account"},
        {"key": "respond", "label": "Approve your first review response",
         "sub": "Review the AI draft, tweak it, hit approve",
         "done": (rstats.get("responded", 0) or 0) > 0, "module": "reviews"},
    ]
    if "labor" in active_keys:
        steps.append({"key": "labor", "label": "Generate your first schedule",
                      "sub": "Unlocks labor cost analysis and AI scheduling",
                      "done": bool((labor or {}).get("is_live")), "module": "labor"})
    if "marketing" in active_keys:
        try:
            from marketing import get_recent_content
            done = bool(get_recent_content(restaurant.id, limit=1))
        except Exception:
            done = False
        steps.append({"key": "marketing", "label": "Generate your first post",
                      "sub": "One tap — the AI writes it in your voice",
                      "done": done, "module": "marketing"})
    return [] if all(st["done"] for st in steps) else steps


def _do_mobile_home(current_user):
    from models import get_review_stats, get_active_modules
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404

    active_modules = get_active_modules(restaurant)
    active_keys = {m["key"] for m in active_modules}

    rstats = get_review_stats(rid) if "reviews" in active_keys else {}
    labor = None
    if "labor" in active_keys:
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(rid)
        except Exception:
            labor = None

    inv = {}
    inv_live = False
    modules_out = []
    for m in active_modules:
        key = m["key"]
        kpi = None
        if key == "reviews":
            kpi = {
                "value": f"{rstats.get('responded', 0)}/{rstats.get('total', 0)}",
                "sublabel": f"{rstats.get('response_rate', 0)}% response rate",
            }
        elif key == "labor":
            labor_target = float(restaurant.labor_target_pct or 30.0)
            overall_pct = (labor or {}).get("overall_labor_pct", 0)
            on_track = overall_pct <= labor_target
            kpi = {
                "value": f"{round(overall_pct, 1)}%",
                "sublabel": "on track" if on_track else f"over {int(labor_target)}% target",
            }
        elif key == "inventory":
            try:
                from inventory import load_inventory_for_restaurant, analyse_inventory
                _items, _live = load_inventory_for_restaurant(rid)
                inv = analyse_inventory(_items)
                # Only a restaurant with its own live inventory gets a
                # waste line on the weekly receipt — the sample items a
                # fresh account is analysed against aren't its own waste.
                inv_live = bool(_live)
            except Exception:
                inv = {}
            kpi = {
                "value": f"${int(inv.get('recoverable_monthly', 0))}",
                "sublabel": "recoverable / mo",
            }
        elif key == "marketing":
            try:
                conn = get_conn()
                conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
                    content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
                    created_at TEXT DEFAULT (datetime('now')))""")
                this_month = conn.execute(
                    "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')",
                    (rid,)
                ).fetchone()[0] or 0
                conn.close()
            except Exception:
                this_month = 0
            kpi = {"value": str(this_month), "sublabel": "pieces this month"}
        elif key == "intel":
            kpi = _intel_home_kpi(restaurant)

        modules_out.append({"key": key, "label": m["label"], "icon": key, "status": m["status"], "kpi": kpi,
                            "pulse": _home_pulse(key, kpi, rstats, labor, restaurant, inv)})

    # "Needs attention" — same three checks and thresholds as the web Home
    # tab's card list (templates/dashboard.html, id="home-attention-list").
    # `module` names the module key a tap should navigate into — the mobile
    # app no longer has one tab per module, so this is a key into the
    # Modules grid/registry, not a literal tab name.
    needs_attention = []
    urgent = int(rstats.get("urgent", 0) or 0) if "reviews" in active_keys else 0
    if urgent > 0:
        needs_attention.append({
            "type": "urgent_reviews", "module": "reviews",
            "title": f"{urgent} urgent review{'' if urgent == 1 else 's'} unanswered",
            "detail": "Negative reviews are still waiting on a reply",
            "cta": "Reply now", "secondary": None, "action": "open_module",
        })
    if "reviews" in active_keys and rstats.get("awaiting_approval", 0) > 0:
        n = rstats["awaiting_approval"]
        needs_attention.append({
            "type": "reviews_awaiting_approval", "module": "reviews",
            "title": f"{n} review{'' if n == 1 else 's'} awaiting approval",
            "detail": "AI responses drafted — publish in one tap",
            # The deck's one-tap publish is capped per tap (see
            # mobile_approve_all_reviews); the label promises only what
            # one tap will actually do.
            "cta": f"Publish {min(n, 25)} {'reply' if n == 1 else 'replies'}",
            "secondary": "Read them first", "action": "publish_replies",
        })
    if "labor" in active_keys and labor and labor.get("overtime_risk"):
        ot_count = sum(1 for o in labor["overtime_risk"] if o.get("status") == "overtime")
        if ot_count > 0:
            needs_attention.append({
                "type": "labor_overtime", "module": "labor",
                "title": f"{ot_count} staff member{'' if ot_count == 1 else 's'} in overtime",
                "detail": f"Est. ${ot_count * 38}+ extra in OT wages this week",
                "cta": "Open schedule", "secondary": None, "action": "open_module",
            })
    if "reviews" in active_keys and rstats.get("total", 0) > 0 and rstats.get("response_rate", 0) < 50:
        needs_attention.append({
            "type": "low_response_rate", "module": "reviews",
            "title": f"Response rate at {rstats['response_rate']}%",
            "detail": "Restaurants at 80%+ get 2x more new guests",
            "cta": "Answer reviews", "secondary": None, "action": "open_module",
        })

    # Total value delivered — the Home tab's chart card. Snapshot recorded
    # opportunistically right here (upsert-on-conflict, so a second load
    # the same day is a no-op) rather than via a separate scheduled job —
    # see value_delivered.py.
    from value_delivered import compute_total_value_delivered, record_value_snapshot, get_value_history
    total_value = compute_total_value_delivered(rid)
    try:
        record_value_snapshot(rid, total_value)
    except Exception:
        pass  # the chart just has one fewer data point — never worth failing Home over
    value_history = get_value_history(rid, days=365)

    # For Home's quiet-hours badge — reuses the exact same check
    # notify.py's own alert dispatch gates on, so "is it actually silenced
    # right now" can never disagree between what fires an alert and what
    # the badge claims.
    from models import is_in_quiet_hours
    quiet_hours_active = is_in_quiet_hours(rid)

    return {
        "ok": True,
        "username": current_user.get("username"),
        "setup_checklist": _setup_checklist(restaurant, rstats, labor, active_keys),
        "restaurant_name": restaurant.name,
        "location_name": restaurant.location_name or None,
        "brand_color": restaurant.brand_color or None,
        "reviews_awaiting_approval": rstats.get("awaiting_approval", 0),
        "modules": modules_out,
        "needs_attention": needs_attention,
        "total_value_delivered": total_value,
        "value_history": value_history,
        "quiet_hours_active": quiet_hours_active,
        "alert_quiet_end": restaurant.alert_quiet_end or None,
        # Home's hero subline and its closing receipt — see the helpers.
        "overnight": _home_overnight(rid),
        "weekly_receipts": _home_weekly_receipts(rid, active_keys, inv if inv_live else {}),
    }, 200


@mobile_bp.route("/home")
@mobile_login_required
def mobile_home(current_user):
    payload, status = _do_mobile_home(current_user)
    return jsonify(**payload), status


# ── Reviews ───────────────────────────────────────────────────────────────

def _do_mobile_reviews(restaurant_id, filter_by="all", search="", category=None, platform=None):
    from models import get_reviews_data
    reviews = get_reviews_data(restaurant_id, filter_by, search, category=category, platform=platform)
    return {"ok": True, "reviews": reviews}, 200


@mobile_bp.route("/reviews")
@mobile_login_required
def mobile_reviews(current_user):
    payload, status = _do_mobile_reviews(
        current_user["restaurant_id"],
        request.args.get("filter", "all"),
        request.args.get("search", ""),
        request.args.get("category") or None,
        request.args.get("platform") or None,
    )
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/approve", methods=["POST"])
@mobile_login_required
def mobile_approve_review(review_id, current_user):
    payload, status = _capi._do_approve(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/approve-all", methods=["POST"])
@mobile_login_required
def mobile_approve_all_reviews(current_user):
    """Home's action deck: publish every drafted reply in one tap.
    Delegates to client_api._do_approve_all so the web route, this one and
    Ask Cavnar's proposal all run the identical bulk-approve path."""
    data = request.get_json(silent=True) or {}
    payload, status = _capi._do_approve_all(current_user["restaurant_id"], data.get("limit", 25))
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/skip", methods=["POST"])
@mobile_login_required
def mobile_skip_review(review_id, current_user):
    payload, status = _capi._do_skip(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/undo", methods=["POST"])
@mobile_login_required
def mobile_undo_review(review_id, current_user):
    payload, status = _capi._do_undo(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/retract", methods=["POST"])
@mobile_login_required
def mobile_retract_review(review_id, current_user):
    payload, status = _capi._do_retract(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/regenerate-draft", methods=["POST"])
@mobile_login_required
def mobile_regenerate_draft(review_id, current_user):
    payload, status = _capi._do_regenerate_draft(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/save-draft", methods=["POST"])
@mobile_login_required
def mobile_save_draft(review_id, current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_save_draft(review_id, current_user["restaurant_id"], data.get("draft", ""))
    return jsonify(**payload), status


@mobile_bp.route("/review-stats")
@mobile_login_required
def mobile_review_stats(current_user):
    payload, status = _capi._do_review_stats(current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/delete", methods=["POST"])
@mobile_login_required
def mobile_delete_review(review_id, current_user):
    payload, status = _capi._do_delete_review(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


# ── Reviews Analytics ────────────────────────────────────────────────────
# Mirrors the web Reviews tab's Analytics sub-tab (dashboard.html's
# rv-panel-analytics): response performance, rate-vs-benchmark (computed
# client-side from review-stats, already available), topic sentiment
# heatmap, 8-week sentiment trend, and the AI one-line insight.

@mobile_bp.route("/reviews/response-performance")
@mobile_login_required
def mobile_response_performance(current_user):
    from models import get_response_performance
    days = int(request.args.get("days", 90))
    if days not in (30, 60, 90, 180):
        days = 90
    try:
        data = get_response_performance(current_user["restaurant_id"], days=days)
        return jsonify(ok=True, data=data)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/reviews/topic-heatmap")
@mobile_login_required
def mobile_topic_heatmap(current_user):
    from models import get_topic_heatmap
    days = int(request.args.get("days", 90))
    if days not in (30, 60, 90, 180):
        days = 90
    try:
        data = get_topic_heatmap(current_user["restaurant_id"], days=days)
        return jsonify(ok=True, data=data)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/reviews/sentiment-trend")
@mobile_login_required
def mobile_sentiment_trend(current_user):
    from models import get_sentiment_trend
    try:
        data = get_sentiment_trend(current_user["restaurant_id"], weeks=8)
        return jsonify(ok=True, weeks=data)
    except Exception as e:
        return jsonify(ok=False, weeks=[], error=_safe_err(e)), 500


@mobile_bp.route("/reviews/insight")
@mobile_login_required
def mobile_review_insight(current_user):
    payload, status = _capi._do_review_insight(current_user["restaurant_id"])
    return jsonify(ok=True, **payload), status


# ── Response templates ───────────────────────────────────────────────────

@mobile_bp.route("/templates")
@mobile_login_required
def mobile_list_templates(current_user):
    from models import get_response_templates
    return jsonify(ok=True, templates=get_response_templates(current_user["restaurant_id"]))


@mobile_bp.route("/templates", methods=["POST"])
@mobile_login_required
def mobile_create_template(current_user):
    from models import create_response_template
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    if not title or not body:
        return jsonify(ok=False, error="Title and body required"), 400
    if len(title) > 120:
        return jsonify(ok=False, error="Title too long (120 chars max)"), 400
    category = data.get("category", "general")
    if category not in ("general", "positive", "negative", "neutral"):
        category = "general"
    tid = create_response_template(current_user["restaurant_id"], title, body, category)
    return jsonify(ok=True, id=tid)


@mobile_bp.route("/templates/<int:tid>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_template(tid, current_user):
    from models import delete_response_template
    delete_response_template(tid, current_user["restaurant_id"])
    return jsonify(ok=True)


@mobile_bp.route("/templates/<int:tid>/use", methods=["POST"])
@mobile_login_required
def mobile_use_template(tid, current_user):
    from models import increment_template_use
    increment_template_use(tid, restaurant_id=current_user["restaurant_id"])
    return jsonify(ok=True)


# ── Send review request ──────────────────────────────────────────────────

@mobile_bp.route("/send-review-request", methods=["POST"])
@mobile_login_required
def mobile_send_review_request(current_user):
    payload, status = _capi._do_send_review_request(current_user["restaurant_id"], request.get_json() or {})
    return jsonify(**payload), status


@mobile_bp.route("/review-request-stats")
@mobile_login_required
def mobile_review_request_stats(current_user):
    from models import get_review_request_stats
    return jsonify(ok=True, **get_review_request_stats(current_user["restaurant_id"]))


# ── Notifications ─────────────────────────────────────────────────────────

@mobile_bp.route("/notifications")
@mobile_login_required
def mobile_notifications(current_user):
    import datetime as _dt
    payload, status = _capi._do_get_notifications(current_user["restaurant_id"])
    # Mark as seen — same stamp-on-read behavior as Changelog (see
    # mobile_changelog below), so the bell's unread badge clears once the
    # client has actually opened the list, not before.
    update_restaurant(current_user["restaurant_id"], {
        "notifications_seen_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    })
    return jsonify(**payload), status


@mobile_bp.route("/notifications/unread-count")
@mobile_login_required
def mobile_notifications_unread_count(current_user):
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    since = restaurant.notifications_seen_at if restaurant else None
    conn = get_conn()
    if since:
        count = conn.execute(
            "SELECT COUNT(*) FROM alert_log WHERE restaurant_id=? AND fired_at > ?",
            (rid, since)
        ).fetchone()[0]
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM alert_log WHERE restaurant_id=?", (rid,)
        ).fetchone()[0]
    conn.close()
    return jsonify(ok=True, count=count)


# ── Changelog ─────────────────────────────────────────────────────────────

@mobile_bp.route("/changelog")
@mobile_login_required
def mobile_changelog(current_user):
    from models import get_changelog
    import datetime as _dt
    entries = get_changelog()
    # Mark as seen — same stamp-on-read behavior as the web route, so the
    # unread badge clears once the owner has actually opened the list.
    update_restaurant(current_user["restaurant_id"], {
        "changelog_seen_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    })
    return jsonify(ok=True, entries=entries)


@mobile_bp.route("/changelog/unread-count")
@mobile_login_required
def mobile_changelog_unread_count(current_user):
    from models import get_changelog
    restaurant = get_restaurant(current_user["restaurant_id"])
    since = restaurant.changelog_seen_at if restaurant else None
    unread = get_changelog(since=since) if since else get_changelog()
    return jsonify(ok=True, count=len(unread))


# ── Ask Cavnar ────────────────────────────────────────────────────────────

@mobile_bp.route("/ask-cavnar", methods=["POST"])
@mobile_login_required
def mobile_ask_cavnar(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_ask_cavnar(
        current_user["restaurant_id"], data.get("question"), history=data.get("history"),
        user_id=current_user.get("id"),
        conversation_id=_capi._parse_conversation_id(data.get("conversation_id")),
        new_conversation=bool(data.get("new_conversation")),
    )
    return jsonify(**payload), status


@mobile_bp.route("/ask-cavnar/stream", methods=["POST"])
@mobile_login_required
def mobile_ask_cavnar_stream(current_user):
    """SSE twin of client_api's stream route — same progress events, same
    tool loop, so the app can show "Reading your reviews" instead of a bare
    spinner for however long a multi-tool answer takes.

    Bearer-authed like every other /mobile/api/ route: no cookie jar on a
    native client, so the token comes from the Authorization header that
    @mobile_login_required already validated to resolve current_user.
    """
    data = request.get_json(silent=True) or {}
    return _capi._ask_cavnar_stream_response(
        current_user["restaurant_id"], current_user.get("id"), data.get("question"),
        conversation_id=_capi._parse_conversation_id(data.get("conversation_id")),
        new_conversation=bool(data.get("new_conversation")))


@mobile_bp.route("/ask-cavnar/history")
@mobile_login_required
def mobile_ask_cavnar_history(current_user):
    from models import get_ask_history
    return jsonify(ok=True, messages=get_ask_history(current_user["restaurant_id"],
                                                     viewer_id=current_user.get("id")))


@mobile_bp.route("/ask-cavnar/history", methods=["DELETE"])
@mobile_login_required
def mobile_ask_cavnar_clear_history(current_user):
    from models import clear_ask_history
    clear_ask_history(current_user["restaurant_id"])
    return jsonify(ok=True)


# Chat history — one row per conversation. Bodies live in client_api so the
# web and the app can't drift; every one is restaurant-scoped in models.

@mobile_bp.route("/ask-cavnar/conversations")
@mobile_login_required
def mobile_ask_cavnar_conversations(current_user):
    payload, status = _capi._do_list_ask_conversations(current_user["restaurant_id"],
                                                       viewer_id=current_user.get("id"))
    return jsonify(**payload), status


@mobile_bp.route("/ask-cavnar/conversations", methods=["POST"])
@mobile_login_required
def mobile_ask_cavnar_new_conversation(current_user):
    payload, status = _capi._do_create_ask_conversation(current_user["restaurant_id"], current_user.get("id"))
    return jsonify(**payload), status


@mobile_bp.route("/ask-cavnar/conversations/<int:conversation_id>")
@mobile_login_required
def mobile_ask_cavnar_conversation(current_user, conversation_id):
    payload, status = _capi._do_get_ask_conversation(current_user["restaurant_id"], conversation_id,
                                                     viewer_id=current_user.get("id"))
    return jsonify(**payload), status


@mobile_bp.route("/ask-cavnar/conversations/<int:conversation_id>", methods=["DELETE"])
@mobile_login_required
def mobile_ask_cavnar_delete_conversation(current_user, conversation_id):
    payload, status = _capi._do_delete_ask_conversation(current_user["restaurant_id"], conversation_id,
                                                        viewer_id=current_user.get("id"))
    return jsonify(**payload), status


@mobile_bp.route("/ask-cavnar/action", methods=["POST"])
@mobile_login_required
def mobile_ask_cavnar_record_action(current_user):
    """Audit line only — the confirmed action is executed by the app calling
    the same route its own button uses. See client_api's shared body."""
    data = request.get_json(silent=True) or {}
    payload, status = _capi._do_record_ask_action(
        current_user["restaurant_id"], current_user.get("id"), data)
    return jsonify(**payload), status


# ── Food cost quick-entry ─────────────────────────────────────────────────

@mobile_bp.route("/food-cost/quickcount", methods=["POST"])
@mobile_login_required
def mobile_food_cost_quickcount(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_food_cost_quickcount(current_user["restaurant_id"], data.get("items", []))
    return jsonify(**payload), status


@mobile_bp.route("/food-cost/custom-item", methods=["POST"])
@mobile_login_required
def mobile_save_food_cost_custom_item(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_save_food_cost_custom_item(current_user["restaurant_id"], data.get("name"), data.get("unit"))
    return jsonify(**payload), status


@mobile_bp.route("/food-cost/custom-item", methods=["DELETE"])
@mobile_login_required
def mobile_delete_food_cost_custom_item(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_delete_food_cost_custom_item(current_user["restaurant_id"], data.get("name"))
    return jsonify(**payload), status


@mobile_bp.route("/marketing/google-post", methods=["POST"])
@mobile_login_required
def mobile_create_google_post(current_user):
    """Publish generated copy to the connected Google Business Profile.

    Marketing already writes `google_promo` copy — this posts it. Logged to
    marketing_content_log on success so it counts toward "pieces this
    month" exactly like a published Instagram post does."""
    import gmb as _gmb
    data = request.get_json(silent=True) or {}
    rid = current_user["restaurant_id"]

    if not _gmb.is_connected(rid):
        return jsonify(ok=False, error="Connect Google Business first — Account → Connections."), 400

    summary  = (data.get("summary") or "").strip()
    cta_type = (data.get("cta_type") or "").strip() or None
    cta_url  = (data.get("cta_url") or "").strip() or None
    if not summary:
        return jsonify(ok=False, error="Post text is required"), 400

    result = _gmb.create_local_post(rid, summary, cta_type=cta_type, cta_url=cta_url)
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error") or "Google rejected the post")

    try:
        from marketing import log_content
        log_content(rid, "google_promo", summary[:80], post_id=result.get("name") or None,
                    post_platform="google")
    except Exception:
        pass
    return jsonify(ok=True, name=result.get("name") or "")


@mobile_bp.route("/food-cost/ingredient-supplier", methods=["POST"])
@mobile_login_required
def mobile_set_ingredient_supplier(current_user):
    """Assign (or clear) the supplier an ingredient is ordered from.
    Addressed by ingredient NAME to match how the rest of the food-cost
    surface identifies items, and scoped to this restaurant so one
    restaurant can never rewrite another's rows."""
    # silent=True: a body-less POST should be a clean 400 about the
    # missing field, not Flask's 415 about the missing content type.
    data = request.get_json(silent=True) or {}
    rid = current_user["restaurant_id"]
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Ingredient name is required"), 400
    supplier_name  = (data.get("supplier_name") or "").strip()
    supplier_email = (data.get("supplier_email") or "").strip()
    if supplier_email and "@" not in supplier_email:
        return jsonify(ok=False, error="That doesn't look like an email address"), 400

    conn = get_conn()
    try:
        cur = conn.execute("""
            UPDATE ingredients SET supplier_name=?, supplier_email=?, updated_at=datetime('now')
            WHERE restaurant_id=? AND name=? AND is_active=1
        """, (supplier_name or None, supplier_email or None, rid, name))
        conn.commit()
        updated = cur.rowcount
    finally:
        conn.close()
    if not updated:
        return jsonify(ok=False, error=f"No active ingredient named \"{name}\""), 404
    return jsonify(ok=True, name=name, supplier_name=supplier_name, supplier_email=supplier_email)


@mobile_bp.route("/labor/staff-contacts")
@mobile_login_required
def mobile_get_staff_contacts(current_user):
    """Everyone named in the latest generated schedule, paired with however
    we can reach them. Names come from the schedule itself (which comes from
    POS shift data), so the list is always the people actually being
    scheduled rather than a roster that has to be kept in sync by hand."""
    from models import get_staff_contacts, get_conn as _gc
    from labor import employees_in_schedule
    rid = current_user["restaurant_id"]

    conn = _gc()
    try:
        row = conn.execute(
            "SELECT id, week_start, week_end, schedule_csv FROM schedule_history "
            "WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (rid,)
        ).fetchone()
    finally:
        conn.close()

    names = employees_in_schedule(row["schedule_csv"]) if row else []
    saved = {c["employee_name"].lower(): c for c in get_staff_contacts(rid)}
    contacts = [{
        "employee_name": n,
        "email": (saved.get(n.lower()) or {}).get("email", ""),
        "phone": (saved.get(n.lower()) or {}).get("phone", ""),
    } for n in names]
    return jsonify(ok=True, contacts=contacts,
                   schedule_id=(row["id"] if row else None),
                   week_start=(row["week_start"] if row else None),
                   week_end=(row["week_end"] if row else None),
                   reachable=sum(1 for c in contacts if c["email"]))


@mobile_bp.route("/labor/staff-contacts", methods=["POST"])
@mobile_login_required
def mobile_set_staff_contact(current_user):
    from models import set_staff_contact
    data = request.get_json(silent=True) or {}
    name = (data.get("employee_name") or "").strip()
    email = (data.get("email") or "").strip()
    if not name:
        return jsonify(ok=False, error="Which member of staff?"), 400
    if email and "@" not in email:
        return jsonify(ok=False, error="That doesn't look like an email address"), 400
    if not set_staff_contact(current_user["restaurant_id"], name, email, (data.get("phone") or "").strip()):
        return jsonify(ok=False, error="Couldn't save that contact."), 400
    return jsonify(ok=True)


@mobile_bp.route("/labor/publish-schedule", methods=["POST"])
@mobile_login_required
def mobile_publish_schedule(current_user):
    """Send each member of staff their own shifts.

    Every employee gets a private tokenised link to their own schedule
    only, and a copy of their shifts in the email body so it's readable
    without tapping anything. Sends are per-employee, so one bad address
    can't stop the rest.

    Email only for now: SMS would be the better channel for floor staff,
    but it needs Twilio credentials this deployment doesn't have — rather
    than pretend, staff without an email address come back in `unreachable`
    so the manager knows exactly who still needs telling.
    """
    from models import get_conn as _gc, get_staff_contacts, create_schedule_share, get_schedule_share_status
    from labor import employees_in_schedule, employee_shifts_from_csv

    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404

    data = request.get_json(silent=True) or {}
    conn = _gc()
    try:
        if data.get("schedule_id"):
            row = conn.execute(
                "SELECT id, week_start, week_end, schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                (int(data["schedule_id"]), rid)).fetchone()
        else:
            row = conn.execute(
                "SELECT id, week_start, week_end, schedule_csv FROM schedule_history "
                "WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Which schedule?"), 400
    finally:
        conn.close()

    if not row or not (row["schedule_csv"] or "").strip():
        return jsonify(ok=False, error="Generate a schedule first — there's nothing to send yet."), 400

    schedule_id = row["id"]
    week_label = row["week_start"] or ""
    if row["week_end"]:
        week_label = f"{row['week_start']} – {row['week_end']}"

    contacts = {c["employee_name"].lower(): c for c in get_staff_contacts(rid)}
    base_url = (os.getenv("BASE_URL") or "https://dashboard.cavnar.ai").rstrip("/")

    sent, unreachable, failed = [], [], []
    for name in employees_in_schedule(row["schedule_csv"]):
        contact = contacts.get(name.lower()) or {}
        email = (contact.get("email") or "").strip()
        if not email:
            unreachable.append({"employee_name": name, "reason": "no email address on file"})
            continue

        token = create_schedule_share(rid, schedule_id, name, sent_to=email)
        link = f"{base_url}/s/{token}"
        shifts = employee_shifts_from_csv(row["schedule_csv"], name)
        try:
            from emails import send_staff_schedule_email
            send_staff_schedule_email(
                to_email=email, employee_name=name, restaurant_name=restaurant.name,
                week_label=week_label, link=link, shifts=shifts,
                reply_to=restaurant.owner_email or None)
        except Exception as e:
            failed.append({"employee_name": name, "error": str(e)})
            continue

        try:
            from models import log_email as _log_email
            _log_email(rid, "staff_schedule", email, f"Your schedule — {week_label}")
        except Exception:
            pass
        sent.append({"employee_name": name, "sent_to": email, "shifts": len(shifts)})

    if sent:
        _log_account_event(rid, "schedule_published", current_user,
                           detail=f"{len(sent)} to staff")
    return jsonify(ok=bool(sent), schedule_id=schedule_id, week_label=week_label,
                   sent=sent, unreachable=unreachable, failed=failed,
                   status=get_schedule_share_status(rid, schedule_id),
                   error=None if sent else "Nobody has an email address on file yet.")


@mobile_bp.route("/labor/schedule-share-status")
@mobile_login_required
def mobile_schedule_share_status(current_user):
    """Who has actually opened the schedule — the difference between "I sent
    it" and "the closing server has seen it"."""
    from models import get_schedule_share_status, get_conn as _gc
    rid = current_user["restaurant_id"]
    schedule_id = request.args.get("schedule_id", type=int)
    if not schedule_id:
        conn = _gc()
        try:
            row = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=? ORDER BY id DESC LIMIT 1",
                               (rid,)).fetchone()
        finally:
            conn.close()
        schedule_id = row["id"] if row else None
    if not schedule_id:
        return jsonify(ok=True, status=[])
    return jsonify(ok=True, schedule_id=schedule_id,
                   status=get_schedule_share_status(rid, schedule_id))


@mobile_bp.route("/food-cost/menu-profitability")
@mobile_login_required
def mobile_menu_profitability(current_user):
    """Plate cost and margin per dish — see
    inventory_ledger.menu_profitability. Items are grouped by what can
    honestly be said about them, so the UI never implies a margin it
    can't compute."""
    import inventory_ledger as _il
    try:
        return jsonify(ok=True, **_il.menu_profitability(current_user["restaurant_id"]))
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't work out menu margins: {e}"), 500


@mobile_bp.route("/food-cost/menu-item-price", methods=["POST"])
@mobile_login_required
def mobile_set_menu_item_price(current_user):
    """Set (or clear, with 0/null) what a dish sells for."""
    import inventory_ledger as _il
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get("menu_item_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Which menu item?"), 400
    if not _il.set_menu_item_price(current_user["restaurant_id"], item_id, data.get("sell_price")):
        return jsonify(ok=False, error="Couldn't set that price — check the item and the amount."), 400
    return jsonify(ok=True)


@mobile_bp.route("/food-cost/order-draft")
@mobile_login_required
def mobile_food_cost_order_draft(current_user):
    """What would be sent, grouped by supplier, without sending anything.
    The order quantities are the same ones the Food Cost order list already
    shows — see inventory.build_supplier_orders."""
    from inventory import build_supplier_orders
    try:
        draft = build_supplier_orders(current_user["restaurant_id"])
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't build the order: {e}"), 500
    return jsonify(ok=True, **draft)


@mobile_bp.route("/food-cost/send-order", methods=["POST"])
@mobile_login_required
def mobile_send_supplier_order(current_user):
    """Email the suggested order to each supplier and record a PO per
    supplier. Optional `supplier_email` in the body sends to just that one
    supplier; omitted, every group goes.

    Each supplier is its own PO and its own send, so one bad address can't
    stop the rest — failures come back per-supplier rather than as a single
    all-or-nothing error."""
    from inventory import build_supplier_orders
    from models import next_po_number, record_purchase_order

    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404

    # The body is optional here — no body means "send every supplier".
    data = request.get_json(silent=True) or {}
    only = (data.get("supplier_email") or "").strip().lower()

    try:
        draft = build_supplier_orders(rid)
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't build the order: {e}"), 500

    groups = draft.get("groups") or []
    if only:
        groups = [g for g in groups if (g.get("supplier_email") or "").lower() == only]
    if not groups:
        return jsonify(ok=False, error="Nothing to order — no items with a supplier assigned."), 400

    sent, failed = [], []
    for group in groups:
        po_number = next_po_number(rid)
        try:
            from emails import send_supplier_order_email
            send_supplier_order_email(
                to_email=group["supplier_email"],
                supplier_name=group.get("supplier_name") or "",
                restaurant_name=restaurant.name,
                po_number=po_number,
                items=group["items"],
                total_cost=group.get("total_cost") or 0,
                reply_to=restaurant.owner_email or None,
            )
        except Exception as e:
            failed.append({"supplier_email": group["supplier_email"], "error": str(e)})
            continue

        # Only recorded once the send actually succeeded — a PO in the
        # ledger means a supplier really has it, so receiving can trust it.
        record_purchase_order(rid, po_number, group.get("supplier_name") or "",
                              group["supplier_email"], group["items"], group.get("total_cost") or 0)
        try:
            from models import log_email as _log_email
            _log_email(rid, "supplier_order", group["supplier_email"],
                       f"Order {po_number} — {restaurant.name}")
        except Exception:
            pass
        sent.append({"po_number": po_number, "supplier_email": group["supplier_email"],
                     "supplier_name": group.get("supplier_name") or "",
                     "item_count": len(group["items"]), "total_cost": group.get("total_cost") or 0})

    if sent:
        _log_account_event(rid, "supplier_order_sent", current_user,
                           detail=f"{len(sent)} order{'' if len(sent) == 1 else 's'}")
    return jsonify(ok=bool(sent), sent=sent, failed=failed,
                   error=None if sent else "Couldn't send the order — check the supplier addresses.")


@mobile_bp.route("/food-cost/purchase-orders")
@mobile_login_required
def mobile_purchase_orders(current_user):
    from models import get_purchase_orders
    status = request.args.get("status") or None
    return jsonify(ok=True, orders=get_purchase_orders(current_user["restaurant_id"], status=status))


@mobile_bp.route("/food-cost/purchase-orders/<int:po_id>/received", methods=["POST"])
@mobile_login_required
def mobile_receive_purchase_order(current_user, po_id):
    """Close a PO. The stored line items are what receiving pre-fills from,
    so the count is confirmed against what was ordered rather than retyped."""
    from models import mark_purchase_order_received
    if not mark_purchase_order_received(current_user["restaurant_id"], po_id):
        return jsonify(ok=False, error="That order is already received, or isn't yours."), 404
    return jsonify(ok=True)


@mobile_bp.route("/food-cost/analytics")
@mobile_login_required
def mobile_food_cost_analytics(current_user):
    """Mirrors the web Food Cost tab's Analytics sub-tab: AI insight plus the
    same waste_items/overstock breakdowns dashboard.html bakes into its donut
    charts at render time — both already computed by analyse_inventory(),
    just not previously exposed as JSON."""
    from inventory import (load_inventory_for_restaurant, analyse_inventory, get_claude_insights,
                          compute_item_trends, build_price_watch)
    from marketing import get_upcoming_holidays
    rid = current_user["restaurant_id"]
    try:
        restaurant = get_restaurant(rid)
        items, is_live = load_inventory_for_restaurant(rid)
        analysis = analyse_inventory(
            items,
            delivery_days=restaurant.delivery_days if restaurant else None,
            upcoming_holidays=get_upcoming_holidays(),
        )
        try:
            price_watch = build_price_watch(compute_item_trends(rid, items))
        except Exception:
            price_watch = []
        cached = _capi._cache_get("mobile-inv-insight:" + str(rid))
        if cached:
            insight = cached
        else:
            insight = get_claude_insights(
                analysis, owner_name=restaurant.owner_name if restaurant else None,
                restaurant_name=restaurant.name if restaurant else None,
                restaurant_id=rid, items=items, is_live=is_live,
            )
            _capi._cache_set("mobile-inv-insight:" + str(rid), insight)
        return jsonify(
            ok=True,
            insight=insight,
            # Example data must never read as the owner's own numbers.
            is_live=bool(is_live),
            **_insight_json(insight),
            waste_items=analysis.get("waste_items", []),
            overstock=analysis.get("overstock", []),
            critical_low=analysis.get("critical_low", []),
            reorder_soon=analysis.get("reorder_soon", []),
            order_reduction=analysis.get("order_reduction", []),
            price_watch=price_watch,
            recoverable_monthly=analysis.get("recoverable_monthly", 0),
            annual_recoverable=analysis.get("annual_recoverable", 0),
            total_waste_cost_week=analysis.get("total_waste_cost_week", 0),
            monthly_waste_projection=analysis.get("monthly_waste_projection", 0),
            annual_waste_projection=analysis.get("annual_waste_projection", 0),
            waste_rate_pct=analysis.get("waste_rate_pct", 0),
            benchmark_label=analysis.get("benchmark_label", "—"),
            benchmark_detail=analysis.get("benchmark_detail", ""),
            total_stock_value=analysis.get("total_stock_value", 0),
            total_items=analysis.get("total_items", 0),
            week_start=analysis.get("week_start", ""),
            week_end=analysis.get("week_end", ""),
            last_updated=analysis.get("last_updated", ""),
        )
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e), insight="Analysis unavailable — check back shortly.",
                       insight_intro="Analysis unavailable — check back shortly.",
                       insight_recommendations=[], insight_forecast=None,
                       waste_items=[], overstock=[], critical_low=[], reorder_soon=[],
                       order_reduction=[], price_watch=[]), 500


@mobile_bp.route("/food-cost/trend")
@mobile_login_required
def mobile_food_cost_trend(current_user):
    """Weekly waste-cost history for the Analytics tab's trend chart — same
    inventory_history table admin_routes.py's own /api/inv-trend (desktop-
    only) reads, just mobile-auth'd and re-exposed here. 8 weeks (not that
    route's 6) to match LaborPerformanceChart's own "8-Week Trend" window,
    so the two modules' trend charts read as the same convention."""
    from models import get_conn
    import json as _json
    import datetime as _dt
    rid = current_user["restaurant_id"]
    try:
        conn = get_conn()
        # Not part of init_db()'s base schema — inventory.py's own
        # get_claude_insights() creates this lazily on first real analytics
        # fetch. A restaurant that's never viewed Analytics yet (fresh
        # install, or hitting this trend route before that one) would 500
        # on "no such table" otherwise, which reads as an error when it's
        # really just "no history yet" — same schema as that lazy creation.
        conn.execute("""CREATE TABLE IF NOT EXISTS inventory_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            waste_json TEXT,
            week_end    TEXT,
            items_json  TEXT,
            saved_at    TEXT DEFAULT (datetime('now'))
        )""")
        rows = conn.execute("""
            SELECT week_end, waste_json FROM inventory_history
            WHERE restaurant_id=? AND week_end IS NOT NULL
            ORDER BY week_end DESC LIMIT 8
        """, (rid,)).fetchall()
        conn.close()

        weeks = []
        for row in reversed(rows):  # oldest first, left-to-right on the chart
            try:
                data = _json.loads(row["waste_json"])
                waste = round(float(data.get("total_waste_cost", 0)), 2)
                we = _dt.date.fromisoformat(row["week_end"])
                ws = we - _dt.timedelta(days=6)
                weeks.append({
                    "label": f"{we.month}/{we.day}",
                    "start": ws.isoformat(),
                    "end": row["week_end"],
                    "waste": waste,
                })
            except Exception:
                continue

        return jsonify(ok=True, weeks=weeks)
    except Exception as e:
        return jsonify(ok=False, weeks=[], error=_safe_err(e)), 500


# ── Restaurant switcher ───────────────────────────────────────────────────

@mobile_bp.route("/switch-location", methods=["POST"])
@mobile_login_required
def mobile_switch_location(current_user):
    data = request.get_json() or {}
    target_id = int(data.get("restaurant_id", 0))
    payload, status = _capi._do_switch_location(current_user, target_id, _bearer_token())
    return jsonify(**payload), status


@mobile_bp.route("/group-locations")
@mobile_login_required
def mobile_group_locations(current_user):
    payload, status = _capi._do_group_locations(current_user)
    return jsonify(**payload), status


# ── Push device-token registration ─────────────────────────────────────────

@mobile_bp.route("/device-tokens", methods=["POST"])
@mobile_login_required
def mobile_register_device_token(current_user):
    from push import register_device_token
    data = request.get_json() or {}
    apns_token = (data.get("apns_token") or "").strip()
    environment = data.get("environment") or "production"
    if not apns_token:
        return jsonify(ok=False, error="apns_token required"), 400
    if environment not in ("sandbox", "production"):
        return jsonify(ok=False, error="environment must be 'sandbox' or 'production'"), 400
    register_device_token(current_user["id"], current_user["restaurant_id"], apns_token, environment)
    return jsonify(ok=True)


def _unregister_device_token(restaurant_id, apns_token):
    """Delete one APNs token, scoped to the restaurant that owns it. Returns
    False when the token belongs to someone else (or no longer exists), so
    the caller decides between 404 and silence."""
    from push import get_device_tokens, remove_device_token
    # apns_tokens are long random hex strings issued by Apple, not sequential
    # ids — effectively unguessable — but scope the delete to the caller's
    # own restaurant anyway rather than trusting any authenticated bearer.
    if not any(t["apns_token"] == apns_token for t in get_device_tokens(restaurant_id)):
        return False
    remove_device_token(apns_token)
    return True


@mobile_bp.route("/device-tokens/<apns_token>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_device_token(apns_token, current_user):
    if not _unregister_device_token(current_user["restaurant_id"], apns_token):
        return jsonify(ok=False, error="Device token not found"), 404
    return jsonify(ok=True)


# ── Labor ─────────────────────────────────────────────────────────────────

def _staff_constraints_index(restaurant_id):
    """Fuzzy name → constraint-note lookup (full name, first name, first+
    last-initial, with/without trailing period) — same indexing
    hosted_dashboard.py's web Labor tab builds, so "OT allowed" detection
    matches exactly between web and mobile regardless of how a name is
    spelled in the shifts CSV vs. the staff note."""
    from models import get_staff_notes
    index = {}
    try:
        for n in (get_staff_notes(restaurant_id) or []):
            name = (n.get("employee_name") or "").lower().strip().rstrip(".")
            if not name:
                continue
            index[name] = n.get("notes")
            parts = name.split()
            if parts:
                index[parts[0]] = n.get("notes")
            if len(parts) >= 2:
                index[parts[0] + " " + parts[1].rstrip(".")] = n.get("notes")
                index[parts[0] + " " + parts[1].rstrip(".") + "."] = n.get("notes")
    except Exception:
        pass
    return index


def _has_ot_allowance(employee, constraints_index):
    key = (employee or "").lower().strip().rstrip(".")
    note = constraints_index.get(key) or constraints_index.get(key.split(" ")[0], "")
    note_l = (note or "").lower()
    return bool(note) and (
        "overtime" in note_l or "extra hours" in note_l
        or " ot " in (" " + note_l + " ") or note_l.startswith("ot ") or note_l.endswith(" ot")
    )


def _labor_data_caveat(analysis_failed, hours_estimated, sales_missing,
                       days_missing, period_short, conflicting, duplicates) -> str:
    """One plain sentence naming what is incomplete about these numbers.

    labor.py has always known when a period was partial; nothing carried it
    to a screen, so a percentage covering four of fourteen days looked
    exactly like one covering all fourteen.
    """
    if analysis_failed:
        return "We couldn't finish reading your shift data — these figures aren't reliable yet."
    parts = []
    if sales_missing:
        parts.append("no sales figures were found, so there's no labor percentage to show")
    elif days_missing:
        n = len(days_missing)
        parts.append(f"{n} day{'s' if n != 1 else ''} in this period {'have' if n != 1 else 'has'} "
                     "shifts but no sales, so the percentage covers only part of it")
    if hours_estimated:
        parts.append("these are scheduled hours, not clocked hours")
    if conflicting:
        n = len(conflicting)
        parts.append(f"{n} day{'s' if n != 1 else ''} had two different sales figures; the larger was used")
    if duplicates:
        parts.append(f"{duplicates} duplicate row{'s' if duplicates != 1 else ''} ignored")
    if period_short:
        parts.append("the period is under a week, so there's no monthly figure")
    if not parts:
        return ""
    return parts[0][:1].upper() + parts[0][1:] + ("; " + "; ".join(parts[1:]) if len(parts) > 1 else "") + "."


def _do_mobile_labor(restaurant_id):
    from labor import analyse_shifts_for_restaurant

    restaurant = get_restaurant(restaurant_id)
    target = float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0
    hourly_rate = float(restaurant.hourly_rate or 26.0) if restaurant else 26.0
    analysis_failed = False
    try:
        analysis = analyse_shifts_for_restaurant(restaurant_id)
    except Exception:
        # An empty dict defaults every figure below to zero, and zero percent
        # labor reads as "comfortably under target" rather than as "we could
        # not work this out". The flag travels so the client can say so.
        analysis = {}
        analysis_failed = True

    overall_pct = analysis.get("overall_labor_pct", 0)
    # Every reason the numbers below are not a clean measured actual.
    hours_are_estimated = bool(analysis.get("hours_are_estimated"))
    days_missing_sales = analysis.get("days_missing_sales") or []
    sales_data_missing = bool(analysis.get("sales_data_missing"))
    period_too_short = bool(analysis.get("period_too_short_to_project"))
    data_complete = not (analysis_failed or hours_are_estimated or days_missing_sales
                         or sales_data_missing or analysis.get("days_with_conflicting_sales"))

    constraints_index = _staff_constraints_index(restaurant_id)
    employee_hours = {
        emp: round(d.get("actual", 0), 1)
        for emp, d in (analysis.get("employee_hours") or {}).items()
    }
    overtime_risk = [
        {
            "employee": o.get("employee"), "hours": o.get("hours"),
            "week": o.get("week"), "status": o.get("status"),
            "total_hours": employee_hours.get(o.get("employee"), o.get("hours", 0)),
            "ot_allowed": _has_ot_allowance(o.get("employee"), constraints_index),
        }
        for o in analysis.get("overtime_risk", [])
    ]
    role_summary = sorted(
        (
            {
                "role": role, "hours": d.get("hours", 0), "labor_cost": d.get("labor_cost", 0),
                "headcount": d.get("headcount", 0), "labor_pct": d.get("labor_pct", 0),
            }
            for role, d in (analysis.get("role_summary") or {}).items()
        ),
        key=lambda r: r["labor_pct"], reverse=True,
    )

    # Overtime premium — 0.5x blended rate on hours over 40/week, same
    # formula hosted_dashboard.py's web dashboard uses for its "overtime
    # premium" savings tile.
    # Computed in labor.py now, where it is also added to the labor cost the
    # percentage is derived from. This loop summed only the first flagged
    # week per employee and never fed the cost model, so an employee with
    # three overtime weeks contributed one, and the labor percentage the
    # owner read excluded the premium entirely.
    ot_premium = float(analysis.get("overtime_premium") or 0.0)

    date_range = analysis.get("date_range") or {}
    # Calendar days the synced shifts cover — computed once in labor.py.
    period_days = int(analysis.get("period_days") or 0)
    total_sales = analysis.get("total_sales", 0)
    monthly_sales_est = (total_sales / period_days * 30) if period_days >= 7 else 0
    potential_savings = analysis.get("potential_savings", 0)
    labor_monthly = round(analysis.get("potential_savings_monthly", 0) or 0)
    # 0.345 = midpoint of the 33-36% full-service industry range (NRA 2024
    # Restaurant Operations Data Abstract) — was 0.32, a leftover from the
    # stale pre-pandemic 28-32% benchmark already corrected everywhere else
    # this figure appears (labor.py's AI prompt, the web dashboard, iOS's
    # own benchmark band).
    # A benchmark comparison is only honest against a measured actual over a
    # real period. When hours were estimated, the labor percentage was
    # understated, so the gap to the industry midpoint was overstated by
    # exactly as much — a CSV missing one column produced $62,100/month of
    # claimed savings. A sub-week period has no monthly rate to compare at
    # all. Both now yield no claim rather than a large one.
    if analysis_failed or hours_are_estimated or sales_data_missing or period_too_short or not overall_pct:
        labor_vs_industry_monthly = 0
    else:
        labor_vs_industry_monthly = max(0, round((0.345 - overall_pct / 100) * monthly_sales_est))

    savings_breakdown = {
        "labor_monthly": labor_monthly,
        "labor_annual": labor_monthly * 12,
        "labor_overtime": round(ot_premium),
        "labor_vs_industry_monthly": labor_vs_industry_monthly,
        "labor_vs_industry_annual": labor_vs_industry_monthly * 12,
    }

    # Upcoming holiday/event scheduling forecast — same 21-day window and
    # holiday-string parsing client_api.py's schedule builder and
    # hosted_dashboard.py's web Labor tab both already use.
    labor_upcoming = []
    try:
        import re
        from marketing import get_upcoming_holidays
        from time_utils import restaurant_now
        now = restaurant_now(restaurant, naive=True)
        hol_str = get_upcoming_holidays(now)
        if hol_str:
            for chunk in hol_str.split(", "):
                m = re.search(r'\((\w+ \d+)\)$', chunk)
                if not m:
                    continue
                try:
                    hdate = datetime.strptime(m.group(1) + " " + str(now.year), "%b %d %Y")
                    if hdate < now:
                        hdate = hdate.replace(year=now.year + 1)
                    days_away = (hdate - now).days
                    if 0 <= days_away <= 21:
                        labor_upcoming.append({
                            "name": chunk[:chunk.rfind("(")].strip(),
                            "date_str": hdate.strftime("%B %-d"),
                            "days_away": days_away,
                        })
                except Exception:
                    pass
    except Exception:
        pass

    return {
        "ok": True,
        "is_live": bool(analysis.get("is_live")),
        "overall_labor_pct": overall_pct,
        "target": target,
        # "On track" is a claim about a measured number. Without one there is
        # nothing to be on track against, and saying so beats a green badge.
        "on_track": (overall_pct <= target) if data_complete else False,
        "data_complete": data_complete,
        "analysis_failed": analysis_failed,
        # labor.py has computed these since the savings-formula work and
        # nothing has ever returned them, so no client could tell a partial
        # period from a whole one.
        "hours_are_estimated": hours_are_estimated,
        "sales_data_missing": sales_data_missing,
        "days_missing_sales": days_missing_sales,
        "days_with_conflicting_sales": analysis.get("days_with_conflicting_sales") or [],
        "duplicate_rows_ignored": int(analysis.get("duplicate_rows_ignored") or 0),
        "period_too_short_to_project": period_too_short,
        "period_days": period_days,
        "overtime_hours": analysis.get("overtime_hours", 0),
        "data_caveat": _labor_data_caveat(analysis_failed, hours_are_estimated,
                                          sales_data_missing, days_missing_sales,
                                          period_too_short,
                                          analysis.get("days_with_conflicting_sales") or [],
                                          int(analysis.get("duplicate_rows_ignored") or 0)),
        "potential_savings": potential_savings,
        "overtime_risk": overtime_risk,
        "role_summary": role_summary,
        "date_range": date_range,
        "overstaffed_days": analysis.get("overstaffed_days") or [],
        "understaffed_days": analysis.get("understaffed_days") or [],
        "dow_summary": analysis.get("dow_summary") or {},
        "savings_breakdown": savings_breakdown,
        "labor_upcoming": labor_upcoming,
    }, 200


@mobile_bp.route("/labor")
@mobile_login_required
def mobile_labor(current_user):
    payload, status = _do_mobile_labor(current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/labor/trend")
@mobile_login_required
def mobile_labor_trend(current_user):
    """Mirrors client_api.py's labor-trend — same 8-week labor-% history the
    web Labor tab's Analytics sub-tab charts."""
    from models import get_labor_history
    from datetime import datetime as _dt
    try:
        history = get_labor_history(current_user["restaurant_id"], limit=8)
        weeks = []
        for h in history[::-1]:  # oldest first = left to right
            try:
                start = _dt.strptime(h["period_start"], "%Y-%m-%d")
                label = start.strftime("%-m/%-d")
            except Exception:
                label = h.get("period_start", "")[:5]
            weeks.append({
                "label": label,
                "pct": round(h["labor_pct"], 1),
                "labor": h["total_labor"],
                "sales": h["total_sales"],
                "start": h["period_start"],
                "end": h["period_end"],
            })
        return jsonify(ok=True, weeks=weeks)
    except Exception as e:
        return jsonify(ok=False, weeks=[], error=_safe_err(e)), 500


@mobile_bp.route("/labor/gap")
@mobile_login_required
def mobile_labor_gap(current_user):
    from labor import analyse_shifts_for_restaurant, calculate_monthly_gap
    try:
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        gap = calculate_monthly_gap(analysis)
        return jsonify(ok=True, **gap)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e), over_target=False, monthly_gap=0,
                       current_pct=0, target_pct=30), 500


def _insight_json(insight_text):
    """Structured {intro, recommendations, forecast} fields for a raw AI
    insight string — the same parsing client_api.format_insight_html() uses
    to build the web's HTML, just handed back as JSON so the iOS app can
    render its own native equivalent instead of a plain text blob."""
    intro, recs, forecast, unverified = _capi.parse_insight_sections(insight_text)
    # What KIND of claim each part is. A measured fact, the model's read of
    # it, a guess about next week and a suggestion all rendered as the same
    # prose in the same weight, so a reader had no way to tell "your 30-day
    # rating is 4.2" from "next week should be busier". The split already
    # existed structurally — this names it so the clients can stop showing
    # the three at equal authority.
    return {
        "insight_intro": intro,
        "insight_recommendations": recs,
        "insight_forecast": forecast,
        "insight_unverified": unverified,
        "claim_kinds": {
            "insight_intro": "inferred",
            "insight_recommendations": "suggestion",
            "insight_forecast": "forecast",
            "insight_unverified": "unverified",
        },
    }


@mobile_bp.route("/labor/insight")
@mobile_login_required
def mobile_labor_insight(current_user):
    """Same AI insight the web Labor tab shows, structured into
    intro/recommendations/forecast fields so the app can render the same
    numbered-circle layout the web dashboard uses."""
    from labor import analyse_shifts_for_restaurant, get_claude_insights
    from models import get_restaurant, get_staff_notes
    rid = current_user["restaurant_id"]
    cached = _capi._cache_get("mobile-labor-insight:" + str(rid))
    if cached:
        return jsonify(ok=True, insight=cached, **_insight_json(cached))
    try:
        restaurant = get_restaurant(rid)
        name = restaurant.name if restaurant else "your restaurant"
        owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
        analysis = analyse_shifts_for_restaurant(rid)
        staff_notes = get_staff_notes(rid)
        insight = get_claude_insights(
            analysis, restaurant_name=name, owner_name=owner, restaurant_id=rid,
            staff_notes=staff_notes if staff_notes else None,
        )
        _capi._cache_set("mobile-labor-insight:" + str(rid), insight)
        return jsonify(ok=True, insight=insight, **_insight_json(insight))
    except Exception as e:
        return jsonify(ok=False, insight="Analysis unavailable — check back shortly.",
                       insight_intro="Analysis unavailable — check back shortly.",
                       insight_recommendations=[], insight_forecast=None, error=_safe_err(e)), 500


@mobile_bp.route("/labor/generate-schedule", methods=["POST"])
@mobile_login_required
def mobile_generate_schedule(current_user):
    """Reuses client_api.py's existing background-job machinery
    (_run_schedule_job plus ops.async_jobs) rather than building a second job
    system — the same async-generate-then-poll pattern the web Labor tab
    already relies on."""
    import threading
    import uuid
    from ai_utils import ai_rate_limited

    rid = current_user["restaurant_id"]
    if ai_rate_limited(f"schedule:{rid}", max_calls=3, window_secs=60):
        return jsonify(ok=False, error="Too many schedule generations — please wait a moment and try again."), 429
    job_id = str(uuid.uuid4())
    import ops as _ops
    _ops.start_async_job(job_id, "schedule", rid)
    t = threading.Thread(target=_capi._run_schedule_job, args=(job_id, rid), daemon=True)
    t.start()
    return jsonify(ok=True, job_id=job_id)


@mobile_bp.route("/labor/schedule-status/<job_id>")
@mobile_login_required
def mobile_schedule_status(job_id, current_user):
    """Mirrors client_api.py's schedule_status(), reading the same
    ops.async_jobs table — the web route generates the job the phone may end
    up polling and vice versa, so they have to share one store."""
    import ops as _ops
    job = _ops.read_async_job(job_id, restaurant_id=current_user["restaurant_id"])
    if not job:
        return jsonify(ok=False, status="error", error="Job not found"), 404
    if job["status"] == "pending":
        return jsonify(ok=True, status="pending")
    try:
        result = dict(job["result"])
        result["status"] = job["status"]
        return jsonify(**result)
    except Exception as e:
        return jsonify(ok=False, status="error", error=_safe_err(e)), 500


@mobile_bp.route("/labor/schedule-history")
@mobile_login_required
def mobile_schedule_history(current_user):
    """Every schedule this restaurant has ever generated, newest first —
    a durable server-side record on the Account tab, independent of
    whatever the Labor tab's own client-side caching does."""
    from models import get_schedule_history
    try:
        history = get_schedule_history(current_user["restaurant_id"])
        return jsonify(ok=True, history=history)
    except Exception as e:
        return jsonify(ok=False, history=[], error=_safe_err(e)), 500


@mobile_bp.route("/labor/schedule-history/<int:history_id>")
@mobile_login_required
def mobile_schedule_history_detail(history_id, current_user):
    """Full record for one past generation, including the CSV parsed into
    preview_rows the same shape the Labor tab's own schedule result uses
    (so the iOS detail screen can reuse the same row-rendering component).
    Scoped to current_user's restaurant_id — get_schedule_history_detail()
    returns None for an id that belongs to a different tenant, same as a
    genuinely missing id, rather than confirming which ids exist."""
    from models import get_schedule_history_detail
    detail = get_schedule_history_detail(history_id, current_user["restaurant_id"])
    if not detail:
        return jsonify(ok=False, error="Not found"), 404

    _COLS = ["date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes"]
    preview_rows = []
    for _line in (detail.get("schedule_csv") or "").split("\n")[1:]:
        _line = _line.strip()
        if not _line:
            continue
        _parts = _line.split(",", 7)
        if len(_parts) < 6:
            continue
        preview_rows.append({_COLS[i]: _parts[i].strip() for i in range(min(len(_parts), 8))})

    return jsonify(ok=True, **detail, preview_rows=preview_rows)


@mobile_bp.route("/labor/schedule-history/<int:history_id>", methods=["DELETE"])
@mobile_login_required
def mobile_schedule_history_delete(history_id, current_user):
    """The only deletion path for schedule_history rows -- nothing in this
    codebase ever removes one automatically. Scoped to current_user's
    restaurant_id the same way the detail route is."""
    from models import delete_schedule_history
    deleted = delete_schedule_history(history_id, current_user["restaurant_id"])
    if not deleted:
        return jsonify(ok=False, error="Not found"), 404
    return jsonify(ok=True)


@mobile_bp.route("/labor/availability")
@mobile_login_required
def mobile_labor_availability(current_user):
    """Client-scoped counterpart to admin_routes.py's /admin/staff-
    availability/<id> — same models.py CRUD, gated by the restaurant's own
    mobile session instead of internal admin auth. Feeds the same AI
    scheduler input client_api.py's _build_schedule_result() already reads
    (staff_availability=...), so entries saved here are respected by the
    next "Generate schedule" run with no extra wiring."""
    from models import get_staff_availability, init_staff_availability
    import json as _json
    init_staff_availability()
    rows = get_staff_availability(current_user["restaurant_id"]) or []
    entries = [
        {
            "employee_name": r.get("employee_name"),
            "available_days": _json.loads(r.get("available_days") or "[]"),
            "unavailable_days": _json.loads(r.get("unavailable_days") or "[]") if r.get("unavailable_days") else [],
            "notes": r.get("notes"),
        }
        for r in rows
    ]
    return jsonify(ok=True, availability=entries)


@mobile_bp.route("/labor/availability", methods=["POST"])
@mobile_login_required
def mobile_labor_availability_save(current_user):
    from models import save_staff_availability, init_staff_availability
    data = request.get_json(silent=True) or {}
    name = (data.get("employee_name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Employee name is required."), 400
    init_staff_availability()
    save_staff_availability(
        current_user["restaurant_id"], name,
        available_days=data.get("available_days") or [],
        unavailable_days=data.get("unavailable_days") or [],
        notes=(data.get("notes") or "").strip() or None,
    )
    return jsonify(ok=True)


@mobile_bp.route("/labor/availability/delete", methods=["POST"])
@mobile_login_required
def mobile_labor_availability_delete(current_user):
    from models import delete_staff_availability
    data = request.get_json(silent=True) or {}
    name = (data.get("employee_name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Employee name is required."), 400
    delete_staff_availability(current_user["restaurant_id"], name)
    return jsonify(ok=True)


# ── Marketing ─────────────────────────────────────────────────────────────

def _do_mobile_marketing_stats(restaurant_id):
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now')))""")
        generated = conn.execute(
            "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (restaurant_id,)
        ).fetchone()[0] or 0
        published = conn.execute(
            "SELECT COUNT(DISTINCT topic) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL",
            (restaurant_id,)
        ).fetchone()[0] or 0
        this_month = conn.execute(
            "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')",
            (restaurant_id,)
        ).fetchone()[0] or 0
        conn.close()
        return {"generated": generated, "published": published, "this_month": this_month}
    except Exception:
        return {"generated": 0, "published": 0, "this_month": 0}


def _marketing_channels(restaurant_id):
    """Which publish destinations are actually usable right now.

    The app was showing Post to Instagram and Post to Facebook
    unconditionally, so a restaurant with neither connected got a button that
    could only fail — and Instagram/Facebook has no connect flow in the app,
    so there was nowhere to go to fix it. The web tab has always hidden these
    (see _igConnected/_fbConnected in dashboard.html); this is that same
    truth, served instead of templated, plus Google, which mobile can post to
    and web currently can't.
    """
    r = get_restaurant(restaurant_id)
    return {
        "instagram": bool(r and getattr(r, "ig_token", None) and getattr(r, "ig_user_id", None)),
        "facebook": bool(r and getattr(r, "fb_page_token", None) and getattr(r, "fb_page_id", None)),
        "google": bool(r and getattr(r, "gmb_refresh_token", None)
                       and getattr(r, "gmb_account_id", None)
                       and getattr(r, "gmb_location_id", None)),
    }


def _annotate_written(restaurant_id, ideas):
    """Mark which of this week's ideas have already been written from.

    The week rail shows a written day in green. mark_calendar_idea_used has
    logged every calendar-sourced generation as content_type "calendar_<type>"
    since the web tab existed; matching those rows' topics against this
    week's angles is what turns that log into a state the phone can show,
    instead of the app forgetting every time it relaunches."""
    if not ideas:
        return ideas
    try:
        from marketing import _week_start
        since = _week_start(restaurant_id).strftime("%Y-%m-%d")
        conn = get_conn()
        rows = conn.execute(
            "SELECT DISTINCT topic FROM marketing_content_log "
            "WHERE restaurant_id=? AND content_type LIKE 'calendar_%' AND created_at >= ?",
            (restaurant_id, since)).fetchall()
        conn.close()
        done = {(r["topic"] or "").strip() for r in rows}
        for idea in ideas:
            idea["written"] = (idea.get("angle") or "").strip() in done
    except Exception:
        for idea in ideas:
            idea.setdefault("written", False)
    return ideas


def _guest_textable_count(restaurant_id):
    """Consented, not unsubscribed — the number the Text Club tile shows."""
    try:
        conn = get_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM guest_contacts WHERE restaurant_id=? AND consent=1 AND unsubscribed=0",
            (restaurant_id,)).fetchone()[0]
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def _do_mobile_marketing(restaurant_id):
    # Cached read only. This used to call get_content_calendar_ideas()
    # unconditionally, so every open of the Marketing tab fired a Sonnet
    # generation, blocked the tab on it, and produced a different "this week"
    # each time. Generating is now an explicit act (/marketing/calendar).
    from marketing import get_cached_calendar, CONTENT_TYPES
    stats = _do_mobile_marketing_stats(restaurant_id)
    return {
        "ok": True,
        "stats": stats,
        "calendar": _annotate_written(restaurant_id, get_cached_calendar(restaurant_id) or []),
        "guest_textable": _guest_textable_count(restaurant_id),
        # Served rather than hardcoded in the app, which had drifted to five
        # types with different labels and no descriptions.
        "content_types": CONTENT_TYPES,
        "channels": _marketing_channels(restaurant_id),
    }, 200


@mobile_bp.route("/marketing")
@mobile_login_required
def mobile_marketing(current_user):
    payload, status = _do_mobile_marketing(current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/marketing/calendar", methods=["POST"])
@mobile_login_required
def mobile_generate_calendar(current_user):
    """The "Generate week" action the app never had — the web tab's own
    button, which is the only place a calendar draw should be paid for."""
    import ops
    from marketing import (get_content_calendar_ideas, get_cached_calendar,
                           RECENT_CALENDAR_SECONDS)
    from ai_utils import ai_rate_limited
    rid = current_user["restaurant_id"]

    # Answer a retry before the rate limiter sees it. The limiter counts
    # attempts, not generations, so tapping again after the client gave up
    # waiting burned a token for work that had already been done — three
    # impatient taps locked the button for five minutes having generated once.
    just_made = get_cached_calendar(rid, max_age_seconds=RECENT_CALENDAR_SECONDS)
    if just_made:
        return jsonify(ok=True, calendar=_annotate_written(rid, just_made)), 200

    if ai_rate_limited(f"calendar:{rid}", max_calls=4, window_secs=300):
        return jsonify(ok=False, error="Too many calendar regenerations — try again in a few minutes."), 429
    try:
        ideas = get_content_calendar_ideas(restaurant_id=rid, force=True)
    except Exception as e:
        ops.capture(e, job="content_calendar", context=f"restaurant_id={rid}")
        ideas = []
    if not ideas:
        # Falling back to whatever this restaurant already has beats handing
        # back an error and an empty screen.
        existing = get_cached_calendar(rid)
        if existing:
            return jsonify(ok=True, calendar=_annotate_written(rid, existing), stale=True), 200
        return jsonify(ok=False,
                       error="Couldn't build a calendar right now — try again in a moment."), 200
    return jsonify(ok=True, calendar=_annotate_written(rid, ideas)), 200


def _do_mobile_generate_content(restaurant_id, content_type, topic, from_calendar=False):
    from marketing import generate_content, mark_calendar_idea_used
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"gencontent:{restaurant_id}", max_calls=8, window_secs=60):
        return {"ok": False, "error": "Too many requests — please wait a moment and try again."}, 429
    content_type = content_type or "instagram_post"
    topic = topic or ""
    try:
        result = generate_content(content_type, topic, restaurant_id=restaurant_id)
    except Exception as e:
        return {"ok": False, "error": _safe_err(e)}, 500
    # The web route has always logged this; mobile never did, so a calendar
    # idea generated on the phone never fed the "avoid repeating these"
    # signal the next calendar draw reads.
    if from_calendar:
        try:
            mark_calendar_idea_used(restaurant_id, content_type, topic)
        except Exception:
            pass
    return {"ok": True, "content": result}, 200


@mobile_bp.route("/marketing/generate-content", methods=["POST"])
@mobile_login_required
def mobile_generate_content(current_user):
    data = request.get_json() or {}
    payload, status = _do_mobile_generate_content(
        current_user["restaurant_id"], data.get("type"), data.get("topic"),
        from_calendar=bool(data.get("from_calendar")),
    )
    return jsonify(**payload), status


# ── Guest Text Club ───────────────────────────────────────────────────────
# Mirrors the web Marketing tab's Guest Text Club section (guest contacts,
# SMS campaign draft/send, join link). Gated on the same marketing-module
# check client_api.py's own routes use, via the same helpers.

@mobile_bp.route("/guest-contacts")
@mobile_login_required
def mobile_guest_contacts(current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import get_guest_contacts
    return jsonify(ok=True, contacts=get_guest_contacts(rid))


@mobile_bp.route("/guest-contacts", methods=["POST"])
@mobile_login_required
def mobile_add_guest_contact(current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import add_guest_contact_manual
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    if not phone:
        return jsonify(ok=False, error="Phone number required"), 400
    contact_id = add_guest_contact_manual(rid, phone, name=name)
    return jsonify(ok=True, id=contact_id)


@mobile_bp.route("/guest-contacts/<int:contact_id>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_guest_contact(contact_id, current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import delete_guest_contact
    delete_guest_contact(contact_id, rid)
    return jsonify(ok=True)


@mobile_bp.route("/guest-contacts/<int:contact_id>/mark-visit", methods=["POST"])
@mobile_login_required
def mobile_mark_guest_visit(contact_id, current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import mark_guest_visit
    mark_guest_visit(contact_id, rid)
    return jsonify(ok=True)


@mobile_bp.route("/guest-campaign/draft", methods=["POST"])
@mobile_login_required
def mobile_guest_campaign_draft(current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"guestcampaign:{rid}", max_calls=8, window_secs=60):
        return jsonify(ok=False, error="Too many requests — please wait a moment and try again."), 429
    data = request.get_json() or {}
    try:
        from guest_marketing import draft_campaign_message
        restaurant = get_restaurant(rid)
        message = draft_campaign_message(restaurant, campaign_type=data.get("type", "general"), topic=data.get("topic", ""))
        return jsonify(ok=True, message=message)
    except Exception as e:
        return jsonify(ok=False, error="Couldn't draft a message right now — try again in a moment."), 500


@mobile_bp.route("/guest-campaign/send", methods=["POST"])
@mobile_login_required
def mobile_guest_campaign_send(current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from ai_utils import ai_rate_limited
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="Message required"), 400
    if ai_rate_limited(f"guestcampaignsend:{rid}", max_calls=3, window_secs=300):
        return jsonify(ok=False, error="Too many campaigns sent recently — please wait a few minutes."), 429
    try:
        from guest_marketing import send_campaign
        # A campaign now goes to a segment, not to everyone consented — and
        # can carry a tracked link, which SMS could never carry at all.
        link_token = None
        target = (data.get("link_url") or "").strip()
        if target:
            import marketing_links as _ml
            made = _ml.create_link(rid, target, source="sms",
                                   campaign=(data.get("type") or "campaign"))
            if made.get("ok"):
                link_token = made["token"]
        result = send_campaign(rid, message, segment=data.get("segment") or "all",
                               link_token=link_token)
        # send_campaign reports its own ok — it refuses outside the guest-text
        # quiet-hours window rather than sending a marketing text at midnight.
        return jsonify(**result), 200
    except Exception as e:
        return jsonify(ok=False, error="Couldn't send the campaign — try again in a moment."), 500


@mobile_bp.route("/marketing/performance")
@mobile_login_required
def mobile_marketing_performance(current_user):
    """Mirrors client_api.py's mkt-performance — summarizes real Meta post
    metrics already stored by refresh_post_metrics(), never calls Meta
    itself."""
    rid = current_user["restaurant_id"]
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now')))""")
        for col in ("reach", "impressions", "engaged", "likes", "comments", "shares"):
            try:
                conn.execute(f"ALTER TABLE marketing_content_log ADD COLUMN {col} INTEGER DEFAULT 0")
            except Exception:
                pass
        conn.commit()

        published = conn.execute(
            "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL",
            (rid,)
        ).fetchone()[0] or 0

        totals = conn.execute("""
            SELECT COALESCE(SUM(reach),0) as reach, COALESCE(SUM(impressions),0) as impressions,
                   COALESCE(SUM(likes),0) as likes, COALESCE(SUM(comments),0) as comments,
                   COALESCE(SUM(shares),0) as shares
            FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL
        """, (rid,)).fetchone()

        rows = conn.execute("""
            SELECT topic, post_platform, reach, impressions, likes, comments, shares
            FROM marketing_content_log
            WHERE restaurant_id=? AND post_id IS NOT NULL
              AND (reach > 0 OR impressions > 0 OR likes > 0 OR comments > 0)
        """, (rid,)).fetchall()
        conn.close()

        top_post = None
        if rows:
            best = max(rows, key=lambda r: (r["reach"] or 0) + (r["impressions"] or 0))
            top_post = {
                "topic": best["topic"], "platform": best["post_platform"],
                "reach": best["reach"] or 0, "likes": best["likes"] or 0,
                "comments": best["comments"] or 0, "shares": best["shares"] or 0,
            }

        total_engagement = (totals["likes"] or 0) + (totals["comments"] or 0) + (totals["shares"] or 0)
        return jsonify(
            ok=True,
            published=published,
            has_data=bool(rows),
            total_reach=(totals["reach"] or 0) + (totals["impressions"] or 0),
            total_engagement=total_engagement,
            top_post=top_post,
        )
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/marketing/recent-topics")
@mobile_login_required
def mobile_recent_topics(current_user):
    """What was generated lately and what it did. The web Analytics tab has
    always shown these as chips with live reach/likes/comments; the app showed
    three all-time totals and nothing per piece."""
    return jsonify(ok=True, **_capi._do_recent_topics(current_user["restaurant_id"]))


@mobile_bp.route("/marketing/refresh-metrics", methods=["POST"])
@mobile_login_required
def mobile_refresh_metrics(current_user):
    """Pull fresh numbers from Meta for this restaurant's posts.

    The web tab polls this every 60s while it is open. The app never did, so
    a post's metrics sat at whatever the nightly scheduler last wrote — the
    numbers an owner saw on their phone the evening they posted were always
    zero. Rate limited because it is a real Meta round trip per post."""
    from ai_utils import ai_rate_limited
    rid = current_user["restaurant_id"]
    if ai_rate_limited(f"mktmetrics:{rid}", max_calls=4, window_secs=120):
        return jsonify(ok=True, refreshed=0, throttled=True), 200
    try:
        from social_routes import refresh_post_metrics
        result = refresh_post_metrics(rid) or {}
        return jsonify(ok=True, refreshed=len(result.get("posts") or [])), 200
    except Exception:
        return jsonify(ok=True, refreshed=0), 200


# ── Photos ────────────────────────────────────────────────────────────────

@mobile_bp.route("/marketing/media", methods=["POST"])
@mobile_login_required
def mobile_upload_media(current_user):
    """Upload a photo from the camera roll.

    Instagram requires an image and the only way to give it one was a text
    field asking for a public URL — on a phone, where the photo has no URL.
    Accepts a multipart file or a base64 body, because the app has a picker
    and the web tab has a file input."""
    import base64
    from marketing_media import store_image, MediaError, media_url
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403

    raw, mime = None, ""
    upload = request.files.get("file") if request.files else None
    if upload:
        raw, mime = upload.read(), (upload.mimetype or "")
    else:
        data = request.get_json(silent=True) or {}
        encoded = data.get("image_base64") or ""
        if encoded:
            if "," in encoded[:64] and encoded.strip().startswith("data:"):
                header, encoded = encoded.split(",", 1)
                mime = header.split(":", 1)[-1].split(";")[0]
            try:
                raw = base64.b64decode(encoded, validate=False)
            except Exception:
                return jsonify(ok=False, error="That photo didn't decode."), 400
    if not raw:
        return jsonify(ok=False, error="No photo was attached."), 400

    try:
        stored = store_image(rid, raw, mime)
    except MediaError as e:
        return jsonify(ok=False, error=_safe_err(e)), 400
    except Exception:
        return jsonify(ok=False, error="Couldn't process that photo."), 500

    return jsonify(ok=True, media_id=stored["id"], token=stored["token"],
                   url=media_url(request.url_root, stored["token"]),
                   width=stored["width"], height=stored["height"])


@mobile_bp.route("/marketing/media")
@mobile_login_required
def mobile_list_media(current_user):
    from marketing_media import list_media, media_url
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    items = list_media(rid)
    for item in items:
        item["url"] = media_url(request.url_root, item["token"])
    return jsonify(ok=True, media=items)


@mobile_bp.route("/marketing/media/<int:media_id>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_media(media_id, current_user):
    from marketing_media import delete_media
    rid = current_user["restaurant_id"]
    delete_media(media_id, rid)
    return jsonify(ok=True)


# ── Scheduling ────────────────────────────────────────────────────────────

@mobile_bp.route("/marketing/schedule", methods=["GET", "POST"])
@mobile_login_required
def mobile_schedule(current_user):
    """The queue. Everything this module did was generate-now/post-now, and an
    owner does admin at 11pm for a post that belongs on Tuesday at lunch."""
    import marketing_publish as _mp
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403

    if request.method == "GET":
        return jsonify(ok=True, posts=_mp.list_scheduled(rid))

    data = request.get_json() or {}
    result = _mp.schedule_post(
        rid, data.get("platform"), data.get("body"), data.get("scheduled_for"),
        topic=data.get("topic") or "", content_type=data.get("content_type"),
        media_id=data.get("media_id"), cta_type=data.get("cta_type"),
        cta_url=data.get("cta_url"),
    )
    return jsonify(**result), (200 if result.get("ok") else 400)


@mobile_bp.route("/marketing/schedule/<int:post_id>", methods=["DELETE"])
@mobile_login_required
def mobile_cancel_scheduled(post_id, current_user):
    import marketing_publish as _mp
    result = _mp.cancel_scheduled(post_id, current_user["restaurant_id"])
    return jsonify(**result), (200 if result.get("ok") else 400)


# ── Drafts and approval ───────────────────────────────────────────────────

@mobile_bp.route("/marketing/drafts", methods=["GET", "POST"])
@mobile_login_required
def mobile_drafts(current_user):
    """Generated copy used to survive exactly as long as the screen it was on.
    A draft is the saved version; approving it is the separate act that says
    it may go out, which is what lets a GM write and an owner release."""
    import marketing_drafts as _md
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    if request.method == "GET":
        return jsonify(ok=True, drafts=_md.list_drafts(rid))
    data = request.get_json() or {}
    result = _md.save_draft(rid, data.get("body"), content_type=data.get("content_type"),
                            topic=data.get("topic"), media_id=data.get("media_id"),
                            draft_id=data.get("id"), user_id=current_user.get("id"))
    return jsonify(**result), (200 if result.get("ok") else 400)


@mobile_bp.route("/marketing/drafts/<int:draft_id>/approve", methods=["POST"])
@mobile_login_required
def mobile_approve_draft(draft_id, current_user):
    import marketing_drafts as _md
    result = _md.approve_draft(draft_id, current_user["restaurant_id"],
                               user_id=current_user.get("id"),
                               role=current_user.get("role"))
    return jsonify(**result), (200 if result.get("ok") else 403)


@mobile_bp.route("/marketing/drafts/<int:draft_id>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_draft(draft_id, current_user):
    import marketing_drafts as _md
    return jsonify(**_md.delete_draft(draft_id, current_user["restaurant_id"]))


# ── Analytics, attribution, links ─────────────────────────────────────────

@mobile_bp.route("/marketing/performance-window")
@mobile_login_required
def mobile_performance_window(current_user):
    """Windowed and compared, unlike the all-time totals this replaced."""
    from marketing_signals import performance_window
    try:
        days = max(7, min(int(request.args.get("days", 30)), 365))
    except (TypeError, ValueError):
        days = 30
    return jsonify(ok=True, **performance_window(current_user["restaurant_id"], days=days))


@mobile_bp.route("/marketing/attribution")
@mobile_login_required
def mobile_attribution(current_user):
    from marketing_signals import attribution_summary
    return jsonify(**attribution_summary(current_user["restaurant_id"]))


@mobile_bp.route("/marketing/links", methods=["GET", "POST"])
@mobile_login_required
def mobile_links(current_user):
    import marketing_links as _ml
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    if request.method == "GET":
        return jsonify(ok=True, links=_ml.link_stats(rid))
    data = request.get_json() or {}
    result = _ml.create_link(rid, data.get("target_url"), source=data.get("source") or "sms",
                             campaign=data.get("campaign") or "", label=data.get("label") or "")
    if result.get("ok"):
        result["short_url"] = request.url_root.rstrip("/") + "/g/" + result["token"]
    return jsonify(**result), (200 if result.get("ok") else 400)


# ── Guest text club: segments, history, compliance ────────────────────────

@mobile_bp.route("/guest-segments")
@mobile_login_required
def mobile_guest_segments(current_user):
    """Who a campaign would actually reach, before it is sent."""
    from guest_marketing import SEGMENTS, segment_counts, CAMPAIGN_DEFAULT_SEGMENT
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    counts = segment_counts(rid)
    return jsonify(ok=True, defaults=CAMPAIGN_DEFAULT_SEGMENT, segments=[
        {"key": k, "label": v["label"], "help": v["help"], "count": counts.get(k, 0)}
        for k, v in SEGMENTS.items()
    ])


@mobile_bp.route("/guest-campaigns")
@mobile_login_required
def mobile_guest_campaign_history(current_user):
    from guest_marketing import campaign_history, consent_ledger
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    return jsonify(ok=True, campaigns=campaign_history(rid), ledger=consent_ledger(rid))


# ── Newsletter ────────────────────────────────────────────────────────────

@mobile_bp.route("/guest-newsletter", methods=["GET", "POST"])
@mobile_login_required
def mobile_guest_newsletter(current_user):
    """`weekly_email` has generated newsletters since this product existed
    with no list to send them to and no way to send one."""
    import guest_email as _ge
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    if request.method == "GET":
        return jsonify(ok=True, subscribers=_ge.subscriber_count(rid))
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"newsletter:{rid}", max_calls=2, window_secs=600):
        return jsonify(ok=False, error="Too many newsletters sent recently — wait a few minutes."), 429
    data = request.get_json() or {}
    result = _ge.send_newsletter(rid, data.get("body") or "", subject=data.get("subject"))
    return jsonify(**result), (200 if result.get("ok") else 400)


@mobile_bp.route("/marketing/preview", methods=["POST"])
@mobile_login_required
def mobile_marketing_preview(current_user):
    """What the post will look like where it lands, and whether it will be
    accepted — computed server-side so the two platforms can't disagree."""
    import marketing_publish as _mp
    from marketing_media import get_media_token
    data = request.get_json() or {}
    token = None
    if data.get("media_id"):
        token = get_media_token(data["media_id"], current_user["restaurant_id"])
    return jsonify(ok=True, **_mp.preview(
        data.get("platform"), data.get("body") or "",
        media_token=token, cta_type=data.get("cta_type"),
        base_url=request.url_root))


@mobile_bp.route("/marketing/insight")
@mobile_login_required
def mobile_marketing_insight(current_user):
    payload, status = _capi._do_mkt_insight(current_user["restaurant_id"], raw=True)
    extra = _insight_json(payload.get("insight", "")) if payload.get("insight") else {}
    return jsonify(ok=True, **payload, **extra), status


@mobile_bp.route("/marketing/post-to-instagram", methods=["POST"])
@mobile_login_required
def mobile_post_to_instagram(current_user):
    from social_routes import _do_post_to_instagram
    data = request.get_json() or {}
    payload, status = _do_post_to_instagram(
        current_user["restaurant_id"], data.get("caption", ""), data.get("image_url", ""), data.get("topic", "")
    )
    return jsonify(**payload), status


@mobile_bp.route("/marketing/post-to-facebook", methods=["POST"])
@mobile_login_required
def mobile_post_to_facebook(current_user):
    from social_routes import _do_post_to_facebook
    data = request.get_json() or {}
    payload, status = _do_post_to_facebook(
        current_user["restaurant_id"], data.get("caption", ""), data.get("topic", "")
    )
    return jsonify(**payload), status


@mobile_bp.route("/guest-join-link")
@mobile_login_required
def mobile_guest_join_link(current_user):
    """The web's /api/guest-qr renders a downloadable PNG — mobile just
    returns the join URL and lets the app render its own QR code (CoreImage
    has a QR filter built in) rather than round-tripping an image."""
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    join_url = request.url_root.rstrip("/") + f"/join/{rid}"
    # The web tab prints POS-specific instructions for where this link belongs
    # (dashboard.html's "Add this to your receipts"), which is the step that
    # actually gets guests into the club. Served here so the app shows the
    # same guidance instead of hardcoding a second copy that drifts.
    r = get_restaurant(rid)
    pos = (getattr(r, "pos_system", "") or "").lower()
    if "square" in pos:
        hint = ("Square lets you add a custom receipt footer under Square Dashboard "
                "→ Settings → Checkout → Receipts. Paste the link in there — Square's "
                "footer editor takes text, not an image, so use the link rather than the QR.")
    elif "toast" in pos:
        hint = ("Toast Web has a custom footer message field under your restaurant's "
                "receipt/order settings — paste the link there. If you don't see it, "
                "Toast support can usually enable it.")
    elif "clover" in pos:
        hint = ("Clover Dashboard → Setup → Receipts has a custom message option. "
                "If your device doesn't have one, the Clover App Market has "
                "receipt-customization apps that add it.")
    else:
        hint = ("Most POS systems let you add a custom line to the receipt footer — "
                "that's where this link belongs. If yours doesn't, the QR code on a "
                "table tent or by the register works just as well.")
    return jsonify(ok=True, join_url=join_url, pos_system=getattr(r, "pos_system", None) or "",
                   receipt_hint=hint)


# ── Intel ─────────────────────────────────────────────────────────────────

def _market_rating(competitors: list) -> dict:
    """The competitor set's rating, weighted by review volume.

    A straight mean over competitor ratings treats a twelve-review venue as
    equal to a three-thousand-review one. Each competitor's own rating is
    volume-weighted internally by Google; averaging them flat throws that
    away. Returns both so a client can show the honest one and still say
    how many restaurants it covers.
    """
    # Provisional ratings are excluded from the market figure. A four-review
    # venue at 5.0 would otherwise pull the market average the owner is
    # measured against.
    rated = [(float(c.get("rating") or 0), int(c.get("review_count") or 0))
             for c in competitors
             if c.get("rating") and not c.get("rating_is_provisional")]
    if not rated:
        return {"market_rating": None, "market_rating_reviews": 0, "market_rating_n": 0}
    total_reviews = sum(n for _r, n in rated)
    if total_reviews > 0:
        weighted = sum(r * n for r, n in rated) / total_reviews
    else:
        weighted = sum(r for r, _n in rated) / len(rated)
    return {"market_rating": round(weighted, 1),
            "market_rating_reviews": total_reviews,
            "market_rating_n": len(rated)}


def _do_mobile_intel(restaurant_id):
    """Read-only for the narrative + competitor list; refreshing is its own
    async job (see mobile_refresh_competitors below), the same job-id/poll
    pattern already used for schedule generation.

    restaurant.competitor_intel is a JSON blob — {"competitors": [...],
    "insight": <narrative text>, "generated_at": ...} — written by
    competitor.py's run_competitor_analysis(). The narrative text has to be
    pulled out of that blob BEFORE handing it to parse_competitor_intel/
    extract_recs (both are plain regex text parsers); passing the raw JSON
    string straight through, as this used to do, fed them an escaped JSON
    fragment instead of real text and silently produced garbage intro/
    sections/recommendations for every restaurant with real competitor data."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404
    empty = {"ok": True, "has_data": False, "intro": None, "recommendations": [], "sections": [],
             "competitors": [], "updated_at": None, "own_rating": None,
             "restaurant_name": restaurant.name, "owner_name": restaurant.owner_name}
    if not getattr(restaurant, "competitor_intel", None):
        return empty, 200
    try:
        import json as _json
        from competitor_intel_format import parse_competitor_intel, extract_recs
        blob = _json.loads(restaurant.competitor_intel)
        insight = blob.get("insight", "")
        parsed = parse_competitor_intel(insight)

        # Google's own all-time rating for this restaurant, which is the
        # only figure comparable to the competitor ratings beside it.
        #
        # This used to be get_review_stats()["avg_rating"] — an average over
        # the reviews Cavnar AI happens to hold, at most fifty per Business
        # Profile fetch and at most five on a Places-only restaurant. It was
        # rendered next to competitors' all-time Google averages over
        # thousands of reviews each, and coloured green or red by the
        # comparison. A restaurant with a genuine 4.6 and a rough month in
        # the window we imported read as losing to its market.
        #
        # gbp_rating has been in the database all along, written by
        # fetch_location_rating. When it is absent the sample is shown, and
        # own_rating_basis says which it is so no surface can present the
        # two as the same kind of number.
        own_rating = None
        own_rating_basis = None
        own_rating_count = None
        _gbp = getattr(restaurant, "gbp_rating", None)
        if _gbp:
            own_rating = round(float(_gbp), 1)
            own_rating_basis = "google_all_time"
            own_rating_count = getattr(restaurant, "gbp_review_count", None)
        elif restaurant.module_reviews:
            from models import get_review_stats
            rstats = get_review_stats(restaurant_id)
            if rstats and rstats.get("avg_rating"):
                own_rating = rstats["avg_rating"]
                own_rating_basis = "imported_sample"
                own_rating_count = rstats.get("total")

        return {
            "ok": True,
            "has_data": True,
            "restaurant_name": restaurant.name,
            "owner_name": restaurant.owner_name,
            "intro": parsed.get("intro"),
            "recommendations": extract_recs(insight),
            "sections": [{"name": name, "bullets": bullets} for name, bullets in parsed.get("sections", [])],
            "competitors": [
                {
                    "name": c.get("name", ""),
                    "rating": c.get("rating", 0),
                    "review_count": c.get("review_count", 0),
                    "vicinity": c.get("vicinity", ""),
                    "reviews": c.get("reviews", []),
                    # place_id/custom weren't passed through before — the
                    # client had no way to identify which competitor to
                    # remove, or to know which ones were owner-added at all
                    # (competitor.py's run_competitor_analysis already
                    # tags custom ones with "custom": True, this just
                    # surfaces both fields instead of dropping them here).
                    "place_id": c.get("place_id", ""),
                    "custom": c.get("custom", False),
                    # How this one was selected. Four relaxation passes run,
                    # widening to 8km with no cuisine or price match, and a
                    # wildcard five miles away used to arrive in the same
                    # shape as a direct match across the street.
                    "match_basis": c.get("match_basis"),
                    "distance_m": c.get("distance_m"),
                    "price_level": c.get("price_level"),
                    # A rating on a handful of reviews is not a reputation.
                    "rating_is_provisional": bool(c.get("rating_is_provisional")),
                }
                for c in blob.get("competitors", [])
            ],
            "own_rating_basis": own_rating_basis,
            "own_rating_count": own_rating_count,
            # The market figure, weighted by how many reviews each
            # competitor's rating rests on. An unweighted mean let a
            # twelve-review venue count as much as a three-thousand-review
            # one, which is an average of averages, not a market average.
            **_market_rating(blob.get("competitors") or []),
            # Which claims here are measured and which are the model's read
            # of five Google-selected reviews. Same convention Reviews ships.
            "claim_kinds": {
                "own_rating": own_rating_basis or "unavailable",
                "market_rating": "measured",
                "competitor_ratings": "measured",
                "sections": "inferred",
                "recommendations": "suggestion",
            },
            "updated_at": restaurant.competitor_updated_at,
            **{k: v for k, v in __import__("ai_guard").freshness(
                restaurant.competitor_updated_at).items() if k in ("as_of", "age_days", "stale")},
            "own_rating": own_rating,
        }, 200
    except Exception as e:
        return {"ok": False, "error": _safe_err(e)}, 500


@mobile_bp.route("/labor/team")
@mobile_login_required
def mobile_labor_team(current_user):
    """Everyone on the roster with their Operational Score.

    The roster comes from the shift data, because that is where employees
    exist in this product — there is no separate staff table, and the three
    other staff features (availability, notes, contacts) are keyed the same
    way.
    """
    from models import (get_capabilities, capability_coverage, CAPABILITY_ATTRIBUTES,
                        SCORE_LABELS, SCORE_MIN, SCORE_MAX,
                        get_role_strength_thresholds, get_shift_leader_rules)
    from labor import load_shifts_for_restaurant, analyse_shifts_for_restaurant
    rid = current_user["restaurant_id"]
    try:
        analysis = analyse_shifts_for_restaurant(rid)
        if not analysis.get("is_live"):
            return jsonify(ok=True, is_live=False, team=[], coverage=None,
                           thresholds={}, leader_rules=[],
                           note="Upload your shifts CSV under Account and your team will "
                                "appear here to rate."), 200
        shifts = load_shifts_for_restaurant(rid)
        # Most recent role each person worked, and how many shifts — enough
        # to order the list usefully without inventing a roster.
        seen = {}
        for sh in shifts:
            n = (sh.get("employee") or "").strip()
            if not n:
                continue
            e = seen.setdefault(n, {"name": n, "role": None, "shifts": 0, "last": ""})
            e["shifts"] += 1
            d = sh.get("date") or ""
            if d >= e["last"]:
                e["last"] = d
                e["role"] = (sh.get("role") or "").strip() or e["role"]

        caps = get_capabilities(rid)
        team = []
        for n, e in seen.items():
            c = (caps.get(n) or {}).get("overall") or {}
            closer = (caps.get(n) or {}).get("can_close") or {}
            team.append({
                "name": n, "role": e["role"], "shifts": e["shifts"],
                "score": c.get("score"),
                # Authorised to close. A fact about a person that owes
                # nothing to their rating, and the only way a leadership
                # rule can be satisfied by somebody the owner trusts to
                # lock up but would not call a 5.
                "can_close": bool(closer.get("flag")),
                "score_label": SCORE_LABELS.get(c.get("score")) if c.get("score") else None,
                "notes": c.get("notes"),
                "updated_by": c.get("updated_by"),
                "updated_at": c.get("updated_at"),
            })
        # Unrated first — that is the work in front of the owner.
        team.sort(key=lambda t: (t["score"] is not None, -t["shifts"], t["name"]))
        return jsonify(
            ok=True, is_live=True, team=team,
            coverage=capability_coverage(rid, [t["name"] for t in team]),
            thresholds=get_role_strength_thresholds(rid),
            leader_rules=get_shift_leader_rules(rid),
            scale={"min": SCORE_MIN, "max": SCORE_MAX, "labels": SCORE_LABELS},
            capability_version=__import__("models").capability_version(rid),
            attributes={k: v for k, v in CAPABILITY_ATTRIBUTES.items() if v.get("v1")},
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e), team=[]), 500


@mobile_bp.route("/labor/team/rating", methods=["POST"])
@mobile_login_required
def mobile_set_rating(current_user):
    """Set or clear one employee's Operational Score."""
    if not _may_manage_team(current_user):
        return _refuse_team_write()
    from models import (set_capability, CapabilityError, get_capabilities,
                        record_capability_change)
    data = request.get_json(silent=True) or {}
    rid = current_user["restaurant_id"]
    who = current_user.get("username") or current_user.get("email")
    name = data.get("employee_name") or data.get("name") or ""
    attribute = data.get("attribute") or "overall"
    try:
        before = (get_capabilities(rid).get(name) or {}).get(attribute)
        out = set_capability(
            rid,
            employee_name=name,
            attribute=attribute,
            score=data.get("score"),
            flag=data.get("flag"),
            notes=data.get("notes"),
            updated_by=who,
        )
        record_capability_change(rid, "rating", subject=f"{name} · {attribute}",
                                 before=before, after=out, changed_by=who)
        return jsonify(ok=True, **out), 200
    except CapabilityError as ce:
        return jsonify(ok=False, error=str(ce)), 400
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/labor/team/thresholds", methods=["POST"])
@mobile_login_required
def mobile_set_thresholds(current_user):
    """Minimum combined score per role, and shift leader rules."""
    if not _may_manage_team(current_user):
        return _refuse_team_write()
    import json as _j
    from models import (update_restaurant, validate_strength_thresholds,
                        get_operational_scores, load_shifts_for_restaurant_roles)
    rid = current_user["restaurant_id"]
    data = request.get_json(silent=True) or {}
    raw = data.get("thresholds")
    if raw is None:
        return jsonify(ok=False, error="thresholds required"), 400
    try:
        cleaned = {}
        for role, v in (raw or {}).items():
            if v in (None, ""):
                continue
            n = float(v)
            if n < 0:
                return jsonify(ok=False, error=f"{role}: a threshold cannot be negative"), 400
            cleaned[str(role).strip()] = n
        warnings = validate_strength_thresholds(
            cleaned, get_operational_scores(rid), load_shifts_for_restaurant_roles(rid))
        fields = {"role_strength_json": _j.dumps(cleaned)}
        if "leader_rules" in data:
            rules = data.get("leader_rules") or []
            if not isinstance(rules, list):
                return jsonify(ok=False, error="leader_rules must be a list"), 400
            fields["shift_leader_rules_json"] = _j.dumps(rules)
        from models import record_capability_change, get_role_strength_thresholds
        _before = get_role_strength_thresholds(rid)
        update_restaurant(rid, fields)
        record_capability_change(
            rid, "threshold", subject="per-role targets", before=_before, after=cleaned,
            changed_by=current_user.get("username") or current_user.get("email"))
        # Unreachable targets are saved and warned about rather than
        # refused — an owner may be describing the team they intend to have.
        return jsonify(ok=True, thresholds=cleaned, warnings=warnings), 200
    except (TypeError, ValueError) as ve:
        return jsonify(ok=False, error=f"Could not read those thresholds: {ve}"), 400
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/intel/movement")
@mobile_login_required
def mobile_intel_movement(current_user):
    """How the competitor set has moved, and who joined or left it.

    competitor_snapshots has been written on every weekly run since the
    history was added, and competitor_movement read it — but nothing called
    competitor_movement, so the data accumulated and could not be seen.
    """
    from models import competitor_movement, competitor_roster_changes
    rid = current_user["restaurant_id"]
    try:
        days = min(int(request.args.get("days", 90) or 90), 365)
    except (TypeError, ValueError):
        days = 90
    try:
        moves = competitor_movement(rid, days=days)
        changes = competitor_roster_changes(rid)
        return jsonify(
            ok=True,
            days=days,
            movement=moves,
            # Only the moves that clear the noise floor, for a client that
            # wants the short list rather than everything.
            significant=[m for m in moves if m.get("significant")],
            arrived=changes.get("arrived", []),
            gone=changes.get("gone", []),
            compared_from=changes.get("compared_from"),
            compared_to=changes.get("compared_to"),
            # A rating move is only meaningful against the volume behind it.
            # confidence_z is how far past that noise floor each one sits.
            claim_kinds={"movement": "measured", "significant": "measured",
                         "arrived": "measured", "gone": "measured"},
        )
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e), movement=[], significant=[],
                       arrived=[], gone=[]), 500


@mobile_bp.route("/intel")
@mobile_login_required
def mobile_intel(current_user):
    payload, status = _do_mobile_intel(current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/intel/refresh-competitors", methods=["POST"])
@mobile_login_required
def mobile_refresh_competitors(current_user):
    """Same async job pattern as /labor/generate-schedule — reuses
    admin_routes.py's existing _run_competitor_job rather than building a
    second job system. (admin_routes.py despite its filename: this specific
    route is @login_required, not @admin_required — any logged-in owner can
    trigger it, matching the web dashboard's own "Refresh" button.)"""
    import threading
    import uuid
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not (restaurant and restaurant.module_reviews and restaurant.module_labor
            and restaurant.module_inventory and restaurant.module_marketing):
        return jsonify(ok=False, error="Competitor intelligence is available on the Full System plan only."), 403
    import admin_routes as _admin
    job_id = str(uuid.uuid4())
    import ops as _ops
    _ops.start_async_job(job_id, "competitor_intel", current_user["restaurant_id"])
    t = threading.Thread(target=_admin._run_competitor_job, args=(job_id, rid), daemon=True)
    t.start()
    return jsonify(ok=True, job_id=job_id)


@mobile_bp.route("/intel/refresh-status/<job_id>")
@mobile_login_required
def mobile_refresh_competitors_status(job_id, current_user):
    """Mirrors mobile_schedule_status's body/shape, reading the same
    ops.async_jobs table the web Intel tab's poll reads."""
    import ops as _ops
    job = _ops.read_async_job(job_id, restaurant_id=current_user["restaurant_id"])
    if not job:
        return jsonify(ok=False, status="error", error="Job not found"), 404
    if job["status"] == "pending":
        return jsonify(ok=True, status="pending")
    try:
        result = dict(job["result"])
        result["status"] = job["status"]
        return jsonify(**result)
    except Exception as e:
        return jsonify(ok=False, status="error", error=_safe_err(e)), 500


@mobile_bp.route("/intel/search-places")
@mobile_login_required
def mobile_search_places(current_user):
    """Owner-facing competitor search — Google Places Text Search biased
    toward this restaurant's own location. Lets an owner add a real
    competitor the automatic type-filtered nearby search wouldn't surface
    on its own (see competitor.py's search_places_near docstring), by
    typing a name and picking the right result rather than needing a raw
    Google Place ID (that's what the admin-only custom_competitors field
    already handles)."""
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(ok=True, results=[])
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not (restaurant and restaurant.module_reviews and restaurant.module_labor
            and restaurant.module_inventory and restaurant.module_marketing):
        return jsonify(ok=False, error="Competitor intelligence is available on the Full System plan only."), 403
    from competitor import search_places_near
    results = search_places_near(q, restaurant.latitude, restaurant.longitude)
    return jsonify(ok=True, results=results)


@mobile_bp.route("/intel/add-competitor", methods=["POST"])
@mobile_login_required
def mobile_add_competitor(current_user):
    """Appends a Place ID to custom_competitors (same comma-separated field
    the admin panel's own competitor text field writes to — this is just a
    client-facing, search-driven way to write the same field) so the next
    refresh-competitors run picks it up permanently, same as any admin-
    added one already does."""
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not (restaurant and restaurant.module_reviews and restaurant.module_labor
            and restaurant.module_inventory and restaurant.module_marketing):
        return jsonify(ok=False, error="Competitor intelligence is available on the Full System plan only."), 403
    data = request.get_json(silent=True) or {}
    place_id = (data.get("place_id") or "").strip()
    if not place_id:
        return jsonify(ok=False, error="Missing place_id"), 400
    existing = [pid.strip() for pid in (restaurant.custom_competitors or "").split(",") if pid.strip()]
    if place_id not in existing:
        existing.append(place_id)
        update_restaurant(rid, {"custom_competitors": ",".join(existing)})
    return jsonify(ok=True)


@mobile_bp.route("/intel/remove-competitor", methods=["POST"])
@mobile_login_required
def mobile_remove_competitor(current_user):
    """Only ever removes from custom_competitors — an auto-discovered
    competitor (get_nearby_competitors' own results) was never in this
    field to begin with, so there's nothing here for the client to
    accidentally remove that it didn't add itself."""
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    data = request.get_json(silent=True) or {}
    place_id = (data.get("place_id") or "").strip()
    if not place_id:
        return jsonify(ok=False, error="Missing place_id"), 400
    existing = [pid.strip() for pid in (restaurant.custom_competitors or "").split(",") if pid.strip()]
    existing = [pid for pid in existing if pid != place_id]
    update_restaurant(rid, {"custom_competitors": ",".join(existing)})
    # Also drop it from the already-cached analysis blob directly — the
    # client used to have to trigger a full refresh-competitors job (same
    # 20-40s Google Places + Claude pipeline add-competitor uses) just to
    # make a removal show up, which is why deleting visibly lagged for
    # several seconds. custom_competitors above only affects the NEXT full
    # analysis run; this is what makes the removal appear immediately on
    # the very next plain GET /intel.
    from competitor import remove_competitor_from_cache
    remove_competitor_from_cache(rid, place_id)
    return jsonify(ok=True)


@mobile_bp.route("/intel/ai-visibility")
@mobile_login_required
def mobile_ai_visibility(current_user):
    """Mirrors the web Intel tab's AI Visibility sub-tab: how often the
    restaurant appears in real Perplexity answers to "where should I eat"
    style queries, plus a 10-item GBP/profile completeness checklist. Shares
    the same 3-call/60s rate limit as the web route since each call fires
    real, billable Perplexity queries."""
    payload, status = _capi._do_ai_visibility(current_user["restaurant_id"])
    return jsonify(**payload), status


# ── Account / Settings ──────────────────────────────────────────────────────
# The web dashboard's Account tab in one place: profile, security, POS/social
# connection status, alert contacts, billing. Connecting a POS/social account
# is an OAuth redirect flow that belongs on desktop (see dashboard.html's
# gmbConnect/igConnect/openToastClientModal etc.) — mobile only reads
# connection status here; the "connect" action, if ever added, would open
# the same web OAuth URL in a system browser rather than reimplement OAuth.

def _session_label(session):
    if session.get("device_type") == "ios":
        return "iPhone (Cavnar AI app)"
    ua = session.get("user_agent") or ""
    if "iPhone" in ua:
        return "iPhone (browser)"
    if "iPad" in ua:
        return "iPad (browser)"
    if "Android" in ua:
        return "Android"
    if "Macintosh" in ua:
        return "Mac"
    if "Windows" in ua:
        return "Windows"
    return "Web browser"


def _do_mobile_account(current_user):
    from notify import get_alert_contacts
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404

    from models import get_deletion_requested_at
    profile = {
        "restaurant_name": restaurant.name,
        "deletion_requested_at": get_deletion_requested_at(rid),
        "location_name": restaurant.location_name or None,
        "owner_name": restaurant.owner_name or None,
        "owner_email": restaurant.owner_email or None,
        "owner_phone": restaurant.owner_phone or None,
        "neighborhood": restaurant.neighborhood or None,
        "vibe": restaurant.vibe or None,
        "known_for": restaurant.known_for or None,
        "voice_notes": restaurant.voice_notes or None,
        "never_say": restaurant.never_say or None,
        "menu_notes": restaurant.menu_notes or None,
        "timezone": restaurant.timezone or "America/Chicago",
        "sign_off_name": getattr(restaurant, "sign_off_name", None) or None,
        "response_language": getattr(restaurant, "response_language", None) or None,
        "tone_preset": getattr(restaurant, "tone_preset", None) or None,
        "open_times_json": getattr(restaurant, "open_times_json", None) or None,
        "close_times_json": getattr(restaurant, "close_times_json", None) or None,
        "skip_holidays": getattr(restaurant, "skip_holidays", None) or None,
    }
    # Where a 2FA code actually goes, masked, for the Security sheet's
    # status tile ("Text · •••-0142" / "Email · ma***@giamia.com").
    _method = getattr(restaurant, "two_fa_method", "email") or "email"
    if _method == "sms" and restaurant.owner_phone:
        _digits = "".join(c for c in restaurant.owner_phone if c.isdigit())
        _masked = "•••-" + _digits[-4:] if len(_digits) >= 4 else "your phone"
    else:
        _method = "email"
        _em = restaurant.owner_email or current_user.get("email") or ""
        _masked = (_em[:2] + "***@" + _em.split("@")[-1]) if "@" in _em else "your email"
    account = {
        "username": current_user["username"],
        "email": current_user["email"],
        "two_fa_enabled": bool(restaurant.two_fa_enabled),
        "two_fa_method": _method,
        "two_fa_contact_masked": _masked,
        "login_notify": bool(getattr(restaurant, "login_notify", 0)),
        "marketing_emails_opt_out": bool(getattr(restaurant, "marketing_emails_opt_out", 0)),
        "recovery_email": current_user.get("recovery_email"),
        "recovery_email_pending": current_user.get("recovery_email_pending"),
        "last_login": current_user.get("last_login"),
        "password_changed_at": current_user.get("password_changed_at"),
        "password_strength": current_user.get("password_strength"),
    }
    # A credential-pair POS (Toast/Square/Clover) can have its id fields set
    # and still not be working: Toast's connect route saves the three fields
    # BEFORE verifying them (so a nightly retry can pick up a transient
    # failure), and all three write sync_error whenever a LATER sync fails —
    # a revoked token, an expired secret. Reporting "connected" off the id
    # field alone means a typo'd Toast connect, or a token Toast revokes
    # six months in, shows a permanent green "Connected" with a Disconnect
    # button while no data has synced since. sync_error is the same signal
    # admin.html already reads for exactly this reason (see its "Last error"
    # line) — this brings the client-facing surfaces in line with it.
    connections = {
        "google_business": {
            "connected": bool(getattr(restaurant, "gmb_refresh_token", None)),
        },
        "instagram": {
            "connected": bool(getattr(restaurant, "ig_token", None)),
        },
        "toast": {
            "connected": bool(getattr(restaurant, "toast_restaurant_guid", None))
                        and not getattr(restaurant, "toast_sync_error", None),
            "last_synced": getattr(restaurant, "toast_last_synced", None),
            "error": getattr(restaurant, "toast_sync_error", None),
        },
        "square": {
            "connected": bool(getattr(restaurant, "square_location_id", None))
                        and not getattr(restaurant, "square_sync_error", None),
            "last_synced": getattr(restaurant, "square_last_synced", None),
            "error": getattr(restaurant, "square_sync_error", None),
        },
        "clover": {
            "connected": bool(getattr(restaurant, "clover_merchant_id", None))
                        and not getattr(restaurant, "clover_sync_error", None),
            "last_synced": getattr(restaurant, "clover_last_synced", None),
            "error": getattr(restaurant, "clover_sync_error", None),
        },
    }
    alerts = {
        "contacts": get_alert_contacts(rid),
        "settings": {
            "alert_1star": bool(getattr(restaurant, "alert_1star", 0)),
            "alert_2star": bool(getattr(restaurant, "alert_2star", 0)),
            "alert_health": bool(getattr(restaurant, "alert_health", 0)),
            "alert_neg_spike": bool(getattr(restaurant, "alert_neg_spike", 0)),
            "alert_negative_trend": bool(getattr(restaurant, "alert_negative_trend", 0)),
            "alert_no_response": bool(getattr(restaurant, "alert_no_response", 0)),
            "alert_5star": bool(getattr(restaurant, "alert_5star", 0)),
            "alert_labor_over": bool(getattr(restaurant, "alert_labor_over", 0)),
            "urgent_via_sms": bool(getattr(restaurant, "urgent_via_sms", 0)),
            "urgent_via_email": bool(getattr(restaurant, "urgent_via_email", 0)),
            "digest_enabled": bool(getattr(restaurant, "digest_enabled", 1)),
            "digest_day": getattr(restaurant, "digest_day", "monday"),
            # Backend has fully supported this since notify.py's own
            # is_in_quiet_hours() — restaurants could never actually set it
            # themselves from either client. "HH:MM" 24h strings or None.
            "alert_quiet_start": getattr(restaurant, "alert_quiet_start", None),
            "alert_quiet_end": getattr(restaurant, "alert_quiet_end", None),
            # Per-category push — notify.py's blast()/check_no_response_alerts()
            # have read these for real routing since the al_* matrix was added;
            # this is the first client UI to expose them for editing.
            "al_1star_push": bool(getattr(restaurant, "al_1star_push", 1)),
            "al_2star_push": bool(getattr(restaurant, "al_2star_push", 1)),
            "al_5star_push": bool(getattr(restaurant, "al_5star_push", 1)),
            "al_health_push": bool(getattr(restaurant, "al_health_push", 1)),
            "al_spike_push": bool(getattr(restaurant, "al_spike_push", 1)),
            "al_unres_push": bool(getattr(restaurant, "al_unres_push", 1)),
            "alert_health_bypass_quiet": bool(getattr(restaurant, "alert_health_bypass_quiet", 0)),
            "alert_food_waste": bool(getattr(restaurant, "alert_food_waste", 0)),
            "alert_ai_visibility_drop": bool(getattr(restaurant, "alert_ai_visibility_drop", 0)),
            "alert_extra_emails": getattr(restaurant, "alert_extra_emails", None) or "",
            "push_sound": bool(getattr(restaurant, "push_sound", 1) if getattr(restaurant, "push_sound", 1) is not None else 1),
        },
    }
    from models import count_auto_approved_today
    reviews_block = {
        "auto_approve_5star": bool(getattr(restaurant, "auto_approve_5star", 0)),
        "auto_approve_daily_cap": int(getattr(restaurant, "auto_approve_daily_cap", 5) or 0),
        "auto_approve_paused": bool(getattr(restaurant, "auto_approve_paused", 0)),
        "auto_approved_today": count_auto_approved_today(rid),
    }
    data_block = {"data_retention_months": int(getattr(restaurant, "data_retention_months", 0) or 0)}

    return {
        "ok": True,
        "profile": profile,
        "account": account,
        "connections": connections,
        "alerts": alerts,
        "reviews": reviews_block,
        "data": data_block,
    }, 200


@mobile_bp.route("/account")
@mobile_login_required
def mobile_account(current_user):
    payload, status = _do_mobile_account(current_user)
    return jsonify(**payload), status


@mobile_bp.route("/account/sessions")
@mobile_login_required
def mobile_account_sessions(current_user):
    from auth import get_sessions_for_user
    sessions = get_sessions_for_user(current_user["id"], current_token=_bearer_token())
    for s in sessions:
        s["label"] = _session_label(s)
    return jsonify(ok=True, sessions=sessions)


@mobile_bp.route("/account/login-history")
@mobile_login_required
def mobile_login_history(current_user):
    """Distinct from /account/sessions above: that's only currently-live
    sessions, this is every past login (including ones whose session has
    since expired, been revoked, or been deduped by device_id) — see
    auth.get_login_history()'s doc comment."""
    from auth import get_login_history
    history = get_login_history(current_user["id"])
    for h in history:
        h["label"] = _session_label(h)
    return jsonify(ok=True, history=history)


@mobile_bp.route("/account/change-password", methods=["POST"])
@mobile_login_required
def mobile_change_password(current_user):
    from auth import update_password
    data = request.get_json() or {}
    user = verify_password(current_user["username"], data.get("current", ""))
    if not user:
        return jsonify(ok=False, error="Current password is incorrect"), 400
    new_pw = data.get("new_password", "")
    if len(new_pw) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters"), 400
    update_password(current_user["id"], new_pw)
    _log_account_event(current_user["restaurant_id"], "password_changed", current_user)
    try:
        restaurant = get_restaurant(current_user["restaurant_id"])
        if restaurant and restaurant.owner_email:
            from emails import send_password_changed_email
            send_password_changed_email(restaurant.owner_email, restaurant.name or "your restaurant", restaurant.owner_name)
    except Exception:
        pass  # the password change itself already succeeded — a failed confirmation email isn't worth failing the request over
    return jsonify(ok=True)


@mobile_bp.route("/account/update-email", methods=["POST"])
@mobile_login_required
def mobile_update_email(current_user):
    """Mirrors auth_routes.py's own /api/update-email exactly (verify
    current password, validate format, check uniqueness, dual-update
    users.email + restaurants.owner_email so notifications/digest keep
    working) — that route is @login_required (web session cookie), which
    the app has no way to authenticate as, so this is the same logic under
    mobile_login_required (bearer token) instead."""
    data = request.get_json() or {}
    new_email = (data.get("new_email") or "").strip().lower()
    current_pw = data.get("current_password", "")
    if not new_email:
        return jsonify(ok=False, error="Email address is required"), 400
    import re as _re_email
    if not _re_email.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", new_email):
        return jsonify(ok=False, error="Enter a valid email address"), 400
    user = verify_password(current_user["username"], current_pw)
    if not user:
        return jsonify(ok=False, error="Current password is incorrect"), 400
    conn = get_conn()
    existing = conn.execute("SELECT id FROM users WHERE email=? AND id!=?", (new_email, current_user["id"])).fetchone()
    if existing:
        conn.close()
        return jsonify(ok=False, error="That email is already in use"), 400
    old_email_row = conn.execute("SELECT email FROM users WHERE id=?", (current_user["id"],)).fetchone()
    old_email = old_email_row["email"] if old_email_row else None
    conn.execute("UPDATE users SET email=? WHERE id=?", (new_email, current_user["id"]))
    conn.execute("UPDATE restaurants SET owner_email=? WHERE id=?", (new_email, current_user["restaurant_id"]))
    conn.commit()
    conn.close()
    _log_account_event(current_user["restaurant_id"], "email_changed", current_user, detail=new_email)
    if old_email and old_email != new_email:
        try:
            restaurant = get_restaurant(current_user["restaurant_id"])
            from emails import send_email_changed_email
            send_email_changed_email(old_email, restaurant.name if restaurant else "your restaurant", new_email, restaurant.owner_name if restaurant else None)
        except Exception:
            pass  # the email change itself already succeeded
    return jsonify(ok=True)


@mobile_bp.route("/account/update-profile", methods=["POST"])
@mobile_login_required
def mobile_update_profile(current_user):
    """Self-service edit for the profile fields that are pure contact info
    or freeform AI-voice notes, with no dependency on exact string
    matching elsewhere. restaurant_name/location_name/neighborhood/vibe/
    known_for are deliberately NOT here — those feed string-matching in
    client_api.py's AI query construction and competitor lookups, so they
    stay admin-set-only (see AccountProfileDetailView for why those are
    shown grayed out instead of editable)."""
    import re as _re_profile

    def _clean(value, max_len=1000):
        if not value:
            return None
        value = _re_profile.sub(r'<[^>]+>', '', str(value))
        value = _re_profile.sub(r'(?i)javascript\s*:', '', value)
        return value[:max_len].strip() or None

    data = request.get_json() or {}
    updates = {
        "owner_name":  _clean(data.get("owner_name"), 200),
        "owner_phone": (data.get("owner_phone") or "").strip()[:30] or None,
        "voice_notes": _clean(data.get("voice_notes"), 1000),
        "never_say":   _clean(data.get("never_say"), 1000),
        "menu_notes":  _clean(data.get("menu_notes"), 2000),
        "sign_off_name": _clean(data.get("sign_off_name"), 80),
    }
    # Fixed sets — these are dropped straight into the drafting prompt.
    lang = (data.get("response_language") or "").strip().lower()
    updates["response_language"] = lang if lang in ("en", "es", "fr", "it", "pt", "de") else None
    tone = (data.get("tone_preset") or "").strip().lower()
    updates["tone_preset"] = tone if tone in ("warm", "professional", "playful", "concise") else None
    # Same 7-zone list as the admin Client Settings page (templates/
    # client_settings.html) — only accept a value from that fixed set so a
    # bad string can't silently break "today"/trend math elsewhere.
    _valid_timezones = {
        "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
        "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu",
    }
    tz = (data.get("timezone") or "").strip()
    if tz in _valid_timezones:
        updates["timezone"] = tz
    update_restaurant(current_user["restaurant_id"], updates)
    _log_account_event(current_user["restaurant_id"], "profile_updated", current_user)
    return jsonify(ok=True)


# ── Connections ──────────────────────────────────────────────────────────

@mobile_bp.route("/connections/toast", methods=["POST"])
@mobile_login_required
def mobile_connect_toast(current_user):
    """Saves Toast API credentials and immediately tries a token fetch so
    a typo shows up now instead of at the next sync — same 3 fields the
    admin panel sets, just self-service."""
    import toast as _toast
    data = request.get_json() or {}
    rid = current_user["restaurant_id"]
    client_id     = (data.get("toast_client_id") or "").strip()
    client_secret = (data.get("toast_client_secret") or "").strip()
    restaurant_guid = (data.get("toast_restaurant_guid") or "").strip()
    if not client_id or not client_secret or not restaurant_guid:
        return jsonify(ok=False, error="All three fields are required"), 400

    update_restaurant(rid, {
        "toast_client_id": client_id,
        "toast_client_secret": client_secret,
        "toast_restaurant_guid": restaurant_guid,
        "toast_sync_error": None,
    })
    try:
        _toast.get_toast_token(rid)
    except Exception as e:
        update_restaurant(rid, {"toast_sync_error": str(e)})
        return jsonify(ok=False, error=f"Saved, but couldn't connect: {e}")
    return jsonify(ok=True)


@mobile_bp.route("/connections/toast", methods=["DELETE"])
@mobile_login_required
def mobile_disconnect_toast(current_user):
    update_restaurant(current_user["restaurant_id"], {
        "toast_client_id": None, "toast_client_secret": None,
        "toast_restaurant_guid": None, "toast_access_token": None,
        "toast_token_expires": None, "toast_sync_error": None,
    })
    return jsonify(ok=True)


@mobile_bp.route("/connections/square", methods=["POST"])
@mobile_login_required
def mobile_connect_square(current_user):
    """Self-service Square connect — the same two fields (access token +
    location id) the admin panel and the web dashboard already set, now
    reachable from the app. Credentials are verified against Square before
    they're stored, so a typo fails here rather than silently at the next
    nightly sync. The web routes in square_routes.py are session-auth only
    (@login_required), which is why the app couldn't reach them and the
    Connections row was a status-only stub."""
    import square as _square
    data = request.get_json(silent=True) or {}
    rid = current_user["restaurant_id"]
    access_token = (data.get("square_access_token") or "").strip()
    location_id  = (data.get("square_location_id") or "").strip()
    if not access_token or not location_id:
        return jsonify(ok=False, error="Access token and location ID are both required"), 400

    result = _square.test_credentials(access_token, location_id)
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error") or "Square rejected those credentials")

    update_restaurant(rid, {
        "square_access_token": access_token,
        "square_location_id": location_id,
        "square_sync_error": None,
        "pos_system": "Square",
    })
    _log_account_event(rid, "pos_connected", current_user, detail="Square")
    return jsonify(ok=True, location_name=result.get("location_name") or "")


@mobile_bp.route("/connections/square", methods=["DELETE"])
@mobile_login_required
def mobile_disconnect_square(current_user):
    update_restaurant(current_user["restaurant_id"], {
        "square_access_token": None, "square_location_id": None,
        "square_last_synced": None, "square_sync_error": None,
    })
    _log_account_event(current_user["restaurant_id"], "pos_disconnected", current_user, detail="Square")
    return jsonify(ok=True)


@mobile_bp.route("/connections/clover", methods=["POST"])
@mobile_login_required
def mobile_connect_clover(current_user):
    """Self-service Clover connect — merchant ID + API token, verified
    against Clover before storing. See mobile_connect_square for why these
    mobile routes exist alongside clover_routes.py's session-auth ones."""
    import clover as _clover
    data = request.get_json(silent=True) or {}
    rid = current_user["restaurant_id"]
    merchant_id = (data.get("clover_merchant_id") or "").strip()
    api_token   = (data.get("clover_api_token") or "").strip()
    if not merchant_id or not api_token:
        return jsonify(ok=False, error="Merchant ID and API token are both required"), 400

    result = _clover.test_credentials(merchant_id, api_token)
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error") or "Clover rejected those credentials")

    update_restaurant(rid, {
        "clover_merchant_id": merchant_id,
        "clover_api_token": api_token,
        "clover_sync_error": None,
        "pos_system": "Clover",
    })
    _log_account_event(rid, "pos_connected", current_user, detail="Clover")
    return jsonify(ok=True, merchant_name=result.get("merchant_name") or "")


@mobile_bp.route("/connections/clover", methods=["DELETE"])
@mobile_login_required
def mobile_disconnect_clover(current_user):
    update_restaurant(current_user["restaurant_id"], {
        "clover_merchant_id": None, "clover_api_token": None,
        "clover_last_synced": None, "clover_sync_error": None,
    })
    _log_account_event(current_user["restaurant_id"], "pos_disconnected", current_user, detail="Clover")
    return jsonify(ok=True)


@mobile_bp.route("/connections/google/authorize", methods=["GET"])
@mobile_login_required
def mobile_google_authorize(current_user):
    """Returns a Google OAuth URL for the app to open in an
    ASWebAuthenticationSession. See gmb.get_mobile_auth_url/
    verify_mobile_state for why mobile signs its own state instead of
    reusing the web flow's cookie-bound nonce, and auth_routes.py's
    google_mobile_callback for the other half of this flow."""
    from gmb import get_mobile_auth_url
    if not os.getenv("GOOGLE_CLIENT_ID"):
        return jsonify(ok=False, error="Google OAuth not configured"), 500
    return jsonify(ok=True, url=get_mobile_auth_url(current_user["restaurant_id"]))


@mobile_bp.route("/connections/google", methods=["DELETE"])
@mobile_login_required
def mobile_disconnect_google(current_user):
    """Mirrors auth_routes.py's /auth/google/disconnect under bearer auth."""
    update_restaurant(current_user["restaurant_id"], {
        "gmb_access_token": "", "gmb_refresh_token": "",
        "gmb_account_id": "", "gmb_location_id": "",
    })
    return jsonify(ok=True)


@mobile_bp.route("/account/2fa/send-test", methods=["POST"])
@mobile_login_required
def mobile_send_2fa_test(current_user):
    import random as _random
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    email = restaurant.owner_email or ""
    method = (request.get_json(silent=True) or {}).get("method") or "email"
    if method == "sms" and not restaurant.owner_phone:
        return jsonify(ok=False, error="No phone number found. Add one in Profile & Details, or send by email instead."), 400
    if method != "sms" and (not email or "@" not in email):
        return jsonify(ok=False, error="No email address found. Contact will@cavnar.ai to update your account email."), 400
    code = str(_random.randint(100000, 999999))
    expires = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    update_restaurant(rid, {"two_fa_code": code, "two_fa_expires": expires})
    if method == "sms":
        phone = restaurant.owner_phone
        try:
            from notify import send_2fa_sms
            sent = send_2fa_sms(phone, restaurant.name or "your restaurant", code)
        except Exception as e:
            return jsonify(ok=False, error=f"Failed to send text: {str(e)[:60]}"), 500
        if not sent:
            return jsonify(ok=False, error="Couldn't send the code — text delivery failed. Try again in a moment."), 502
        masked = "(•••) •••-" + "".join(c for c in phone if c.isdigit())[-4:]
        return jsonify(ok=True, masked=masked, method="sms")
    try:
        from emails import send_2fa_code
        sent = send_2fa_code(email, restaurant.name or "your restaurant", code, restaurant.owner_name)
    except Exception as e:
        return jsonify(ok=False, error=f"Failed to send email: {str(e)[:60]}"), 500
    if not sent:
        # send_2fa_code swallows its own failures (missing RESEND_API_KEY,
        # a non-200 from Resend) and just returns False rather than raising
        # — without this check the route reported ok=True regardless, so
        # the app showed "Code sent" even when nothing went out.
        return jsonify(ok=False, error="Couldn't send the code — email delivery failed. Try again in a moment."), 502
    masked = email[:2] + "***@" + email.split("@")[-1]
    return jsonify(ok=True, masked=masked, method="email")


@mobile_bp.route("/account/2fa/verify", methods=["POST"])
@mobile_login_required
def mobile_verify_2fa_setup(current_user):
    rid = current_user["restaurant_id"]
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Not found"), 404
    if restaurant.two_fa_code != code:
        return jsonify(ok=False, error="Incorrect code. Try again."), 400
    expired = True
    exp_str = (restaurant.two_fa_expires or "").strip()
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"]:
        try:
            expires = datetime.strptime(exp_str, fmt)
            expired = datetime.now() > expires
            break
        except Exception:
            continue
    if expired:
        return jsonify(ok=False, error="Code expired. Try again."), 400
    method = data.get("method") if data.get("method") in ("email", "sms") else "email"
    update_restaurant(rid, {"two_fa_enabled": 1, "two_fa_code": "", "two_fa_expires": "", "two_fa_method": method})
    from models import generate_backup_codes
    codes = generate_backup_codes(rid)
    _log_account_event(rid, "two_fa_enabled", current_user, detail=method)
    return jsonify(ok=True, backup_codes=codes)


@mobile_bp.route("/account/2fa/disable", methods=["POST"])
@mobile_login_required
def mobile_disable_2fa(current_user):
    update_restaurant(current_user["restaurant_id"], {"two_fa_enabled": 0})
    _log_account_event(current_user["restaurant_id"], "two_fa_disabled", current_user)
    return jsonify(ok=True)


@mobile_bp.route("/account/2fa/backup-codes")
@mobile_login_required
def mobile_backup_codes_status(current_user):
    from models import count_unused_backup_codes
    return jsonify(ok=True, remaining=count_unused_backup_codes(current_user["restaurant_id"]))


@mobile_bp.route("/account/2fa/backup-codes", methods=["POST"])
@mobile_login_required
def mobile_regenerate_backup_codes(current_user):
    """Invalidates every previously-issued code and mints a fresh set —
    shown once here, same as at initial 2FA setup."""
    from models import generate_backup_codes
    codes = generate_backup_codes(current_user["restaurant_id"])
    _log_account_event(current_user["restaurant_id"], "backup_codes_regenerated", current_user)
    return jsonify(ok=True, backup_codes=codes)


@mobile_bp.route("/account/login-notify", methods=["POST"])
@mobile_login_required
def mobile_toggle_login_notify(current_user):
    """See client_api._do_login_notify."""
    payload, status = _capi._do_login_notify(current_user["restaurant_id"],
                                             request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@mobile_bp.route("/account/marketing-opt-out", methods=["POST"])
@mobile_login_required
def mobile_toggle_marketing_opt_out(current_user):
    """See client_api._do_marketing_opt_out."""
    payload, status = _capi._do_marketing_opt_out(current_user["restaurant_id"],
                                                  request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@mobile_bp.route("/account/team")
@mobile_login_required
def mobile_get_team(current_user):
    from auth import get_team_members
    members = get_team_members(current_user["restaurant_id"])
    for m in members:
        m["is_you"] = (m["id"] == current_user["id"])
    return jsonify(ok=True, members=members)


@mobile_bp.route("/account/team/invite", methods=["POST"])
@mobile_login_required
def mobile_invite_team_member(current_user):
    """Primary-login only — an invited teammate (role='member', see
    auth.invite_team_member) can't add/remove other logins on the same
    restaurant. Every other role ('client', the default for a restaurant's
    own login, and 'owner', Will's multi-restaurant login) counts as the
    account owner here. There's no finer-grained permission tier today; a
    teammate just doesn't get this row in the UI, and the route
    double-checks it server-side regardless of what the client shows."""
    if current_user.get("role") == "member":
        return jsonify(ok=False, error="Only the account owner can invite team members."), 403
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    if not name or "@" not in email:
        return jsonify(ok=False, error="Enter a name and a valid email."), 400
    from auth import invite_team_member
    result = invite_team_member(current_user["restaurant_id"], name, email)
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error", "Couldn't add that teammate.")), 400
    try:
        from emails import send_team_invite_email
        from models import log_email, get_restaurant as _gr
        restaurant = _gr(current_user["restaurant_id"])
        send_team_invite_email(email, restaurant.name, result["username"], result["temp_password"],
                               inviter_name=current_user.get("username"))
        log_email(current_user["restaurant_id"], "team_invite", email, f"You've been added to {restaurant.name}")
    except Exception:
        pass
    _log_account_event(current_user["restaurant_id"], "team_member_invited", current_user, detail=email)
    return jsonify(ok=True, user_id=result["user_id"], username=result["username"])


@mobile_bp.route("/account/team/<int:user_id>/revoke", methods=["POST"])
@mobile_login_required
def mobile_revoke_team_member(current_user, user_id):
    if current_user.get("role") == "member":
        return jsonify(ok=False, error="Only the account owner can remove team members."), 403
    from auth import revoke_team_member
    result = revoke_team_member(current_user["restaurant_id"], user_id, current_user["id"])
    if not result.get("ok"):
        return jsonify(ok=False, error=result.get("error", "Couldn't remove that teammate.")), 400
    _log_account_event(current_user["restaurant_id"], "team_member_revoked", current_user, detail=str(user_id))
    return jsonify(ok=True)


@mobile_bp.route("/account/send-test-digest", methods=["POST"])
@mobile_login_required
def mobile_send_test_digest(current_user):
    """Self-serve version of admin_routes.py's test_digest() — same
    build/render, but scoped to the caller's own restaurant and sent to
    whoever's actually logged in (so a team member previews it addressed
    to themselves, not always the primary owner)."""
    from models import log_email
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    to_email = current_user.get("email")
    if not to_email:
        return jsonify(ok=False, error="No email on file for your account."), 400
    try:
        from reporter import build_report_from_db, render_html
        import resend as _resend
        report = build_report_from_db(rid, restaurant.name, days=7)
        html = render_html(report, restaurant.name, owner_name=restaurant.owner_name, restaurant_id=rid)
        _resend.api_key = _resend_key()
        _resend.Emails.send({
            "from": f"Cavnar AI <{_from_email()}>",
            "to": [to_email],
            "subject": f"[Preview] Your weekly review digest — {restaurant.name}",
            "html": _html_doc(html),
        })
        try:
            log_email(rid, "digest", to_email, f"[Preview] Weekly digest — {restaurant.name}")
        except Exception: pass
        return jsonify(ok=True, email=to_email)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/account/email-history")
@mobile_login_required
def mobile_email_history(current_user):
    """Twin of client_api's /api/email-history."""
    from models import get_email_log_for_client
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50
    return jsonify(ok=True, emails=get_email_log_for_client(current_user["restaurant_id"], limit=limit))


@mobile_bp.route("/account/export-data", methods=["POST"])
@mobile_login_required
def mobile_export_data(current_user):
    """Emails the caller their own data. `scopes` picks what's attached —
    any of reviews / labor / food_cost / settings (default: reviews, the
    original behaviour). Each scope is its own attachment."""
    import base64 as _b64
    from models import (build_reviews_export_csv, build_labor_export_csv,
                        build_food_cost_export_csv, build_settings_export_json, log_email)
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    to_email = current_user.get("email")
    if not to_email:
        return jsonify(ok=False, error="No email on file for your account."), 400
    data = request.get_json(silent=True) or {}
    scopes = [s for s in (data.get("scopes") or ["reviews"]) if s in ("reviews", "labor", "food_cost", "settings")]
    if not scopes:
        return jsonify(ok=False, error="Pick at least one thing to export."), 400
    safe_name = "".join(c for c in (restaurant.name or "cavnar") if c.isalnum() or c in " -_").strip() or "cavnar"
    attachments, labels = [], []
    builders = {
        "reviews": ("reviews.csv", "reviews (date, rating, text, response status)", lambda: build_reviews_export_csv(rid)),
        "labor": ("labor.csv", "labor history", lambda: build_labor_export_csv(rid)),
        "food_cost": ("food_cost.csv", "food cost & inventory", lambda: build_food_cost_export_csv(rid)),
        "settings": ("settings.json", "account settings", lambda: build_settings_export_json(rid)),
    }
    try:
        for scope in scopes:
            fname, label, build = builders[scope]
            body = build()
            attachments.append({"filename": f"{safe_name}_{fname}",
                                "content": _b64.b64encode(body.encode("utf-8")).decode("ascii")})
            labels.append(label)
        import resend as _resend
        _resend.api_key = _resend_key()
        _resend.Emails.send({
            "from": f"Cavnar AI <{_from_email()}>",
            "to": [to_email],
            "subject": f"Your Cavnar AI data export — {restaurant.name}",
            "html": _html_doc("<p>Attached: " + ", ".join(labels) + ".</p>"),
            "attachments": attachments,
        })
        try:
            log_email(rid, "data_export", to_email, f"Data export — {restaurant.name}")
        except Exception: pass
        _log_account_event(rid, "data_exported", current_user, detail=", ".join(scopes))
        return jsonify(ok=True, email=to_email, scopes=scopes)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/account/alert-settings", methods=["POST"])
@mobile_login_required
def mobile_save_alert_settings(current_user):
    from notify import get_alert_contacts, add_alert_contact, delete_alert_contact
    data = request.get_json() or {}
    rid = current_user["restaurant_id"]

    # SMS requires real, server-verified consent — same rule as the web
    # endpoint (client_api.save_alert_settings): the client's checkbox is a
    # UX nicety, not enforcement, since anyone can call this API directly.
    sms_requested = bool(data.get("urgent_via_sms"))
    sms_consented = bool(data.get("sms_consent"))
    sms_on = sms_requested and sms_consented

    # A real error instead of silently dropping the extras — the client
    # already hides its own "+ Add" past 2, so this only fires for a
    # stale build or a direct API call, but it should say so rather than
    # quietly truncating.
    raw_contacts = data.get("contacts") or []
    if len(raw_contacts) > 2:
        return jsonify(ok=False, error="Alert contacts are limited to 2."), 400
    new_contacts = raw_contacts[:2]
    existing = get_alert_contacts(rid)
    for ec in existing:
        delete_alert_contact(ec["id"])
    for nc in new_contacts:
        phone = _capi._normalize_phone(nc.get("phone") or "")
        name = (nc.get("name") or "").strip()
        if phone:
            add_alert_contact(rid, name, phone, sms_consent=sms_on)

    update_restaurant(rid, {
        "alert_1star": int(bool(data.get("alert_1star"))),
        "alert_2star": int(bool(data.get("alert_2star"))),
        "alert_health": int(bool(data.get("alert_health"))),
        "alert_neg_spike": int(bool(data.get("alert_neg_spike"))),
        "alert_negative_trend": int(bool(data.get("alert_negative_trend"))),
        "alert_no_response": int(bool(data.get("alert_no_response"))),
        "alert_5star": int(bool(data.get("alert_5star"))),
        "alert_labor_over": int(bool(data.get("alert_labor_over"))),
        "urgent_via_sms": int(sms_on),
        "urgent_via_email": int(bool(data.get("urgent_via_email"))),
        "digest_enabled": int(bool(data.get("digest_enabled"))),
        "digest_day": data.get("digest_day", "monday"),
        # "HH:MM" 24h strings, or None to turn quiet hours off entirely —
        # is_in_quiet_hours() (models.py) already treats either field being
        # empty as "no quiet window", so an empty string from the client
        # correctly disables it rather than needing a separate flag.
        "alert_quiet_start": data.get("alert_quiet_start") or None,
        "alert_quiet_end": data.get("alert_quiet_end") or None,
        "al_1star_push": int(bool(data.get("al_1star_push"))),
        "al_2star_push": int(bool(data.get("al_2star_push"))),
        "al_5star_push": int(bool(data.get("al_5star_push"))),
        "al_health_push": int(bool(data.get("al_health_push"))),
        "al_spike_push": int(bool(data.get("al_spike_push"))),
        "al_unres_push": int(bool(data.get("al_unres_push"))),
        "alert_health_bypass_quiet": int(bool(data.get("alert_health_bypass_quiet"))),
        "alert_food_waste": int(bool(data.get("alert_food_waste"))),
        "alert_ai_visibility_drop": int(bool(data.get("alert_ai_visibility_drop"))),
        "alert_extra_emails": _clean_email_list(data.get("alert_extra_emails")),
        "push_sound": 0 if data.get("push_sound") is False else 1,
    })
    _log_account_event(rid, "alert_settings_saved", current_user)
    return jsonify(ok=True)


def _clean_email_list(raw, cap: int = 3):
    """Comma/space/newline-separated addresses -> a de-duplicated, lowercase
    comma list of at most `cap`, or None."""
    if not raw:
        return None
    import re as _re_el
    seen, out = set(), []
    for part in _re_el.split(r"[,\s]+", str(raw)):
        e = part.strip().lower()
        if e and "@" in e and "." in e.split("@")[-1] and e not in seen:
            seen.add(e); out.append(e)
        if len(out) >= cap:
            break
    return ",".join(out) or None


@mobile_bp.route("/account/digest-day", methods=["POST"])
@mobile_login_required
def mobile_update_digest_day(current_user):
    data = request.get_json() or {}
    day = (data.get("day") or "monday").lower()
    valid = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if day not in valid:
        return jsonify(ok=False, error="Invalid day"), 400
    update_restaurant(current_user["restaurant_id"], {
        "digest_day": day,
        "digest_enabled": int(data.get("enabled", 1)),
    })
    return jsonify(ok=True)


def _billing_preview(restaurant_id):
    """Sample billing for a restaurant listed in BILLING_PREVIEW_IDS.

    Billing is read live from Stripe, so a restaurant with no
    stripe_customer_id has nothing to show and the Billing screen sits
    empty — which makes that whole screen impossible to look at before a
    client is actually paying. This env-gated hook fills it with obviously
    labelled sample figures so the layout can be reviewed.

    Opt-in and off by default: unset the variable (as it is on Railway)
    and this never runs, so a real client can never be shown invented
    billing. The label says "Sample" for the same reason.
    """
    import os as _os
    raw = _os.getenv("BILLING_PREVIEW_IDS", "")
    ids = {p.strip() for p in raw.split(",") if p.strip()}
    if not ids or str(restaurant_id) not in ids:
        return None
    from datetime import timedelta as _td
    nxt = (datetime.now() + _td(days=18)).strftime("%-m/%-d/%Y")
    return {
        "ok": True,
        "status": "active",
        "next_date": nxt,
        "amount": "1,200.00",
        "payment_method": "Visa ending 4242",
        "portal_url": None,
        "message": "Sample billing — preview only, not a real subscription",
        "invoices": [
            {"date": (datetime.now() - _td(days=12)).strftime("%-m/%-d/%Y"), "amount": "1,200.00",
             "status": "paid", "url": None},
            {"date": (datetime.now() - _td(days=42)).strftime("%-m/%-d/%Y"), "amount": "1,200.00",
             "status": "paid", "url": None},
            {"date": (datetime.now() - _td(days=72)).strftime("%-m/%-d/%Y"), "amount": "2,000.00",
             "status": "paid", "url": None},
        ],
    }


def _do_mobile_billing(restaurant_id):
    import os as _os
    preview = _billing_preview(restaurant_id)
    if preview:
        return preview, 200
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not getattr(restaurant, "stripe_customer_id", None):
        return {"ok": False, "reason": "no_customer"}, 200

    stripe_key = _os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        return {"ok": False, "reason": "no_key"}, 200

    try:
        import stripe as _stripe
        _stripe.api_key = stripe_key
        subs = _stripe.Subscription.list(customer=restaurant.stripe_customer_id, status="active", limit=5)
        if not subs.data:
            subs = _stripe.Subscription.list(customer=restaurant.stripe_customer_id, status="trialing", limit=5)
        if not subs.data:
            return {"ok": True, "status": "inactive", "message": "No active subscription found"}, 200

        sub = subs.data[0]
        next_date = datetime.fromtimestamp(sub.current_period_end).strftime("%-m/%-d/%Y")
        amount = sum(i.price.unit_amount for i in sub["items"].data) / 100

        pm_desc = "Card on file"
        try:
            customer = _stripe.Customer.retrieve(
                restaurant.stripe_customer_id,
                expand=["invoice_settings.default_payment_method"],
            )
            pm = customer.invoice_settings.default_payment_method
            if pm and pm.card:
                pm_desc = f"{pm.card.brand.title()} ending {pm.card.last4}"
        except Exception:
            pass

        try:
            portal = _stripe.billing_portal.Session.create(
                customer=restaurant.stripe_customer_id,
                return_url="https://dashboard.cavnar.ai",
            )
            portal_url = portal.url
        except Exception:
            portal_url = None

        # Recent invoices — same customer/key this whole function already
        # uses, just a second Stripe call. Failing independently of the
        # subscription/portal lookups above (own try/except, same as those)
        # so a Stripe hiccup here doesn't take down next-charge/payment
        # method too; an empty list just means the client shows no history.
        invoices = []
        try:
            for inv in _stripe.Invoice.list(customer=restaurant.stripe_customer_id, limit=6).data:
                invoices.append({
                    "date": datetime.fromtimestamp(inv.created).strftime("%-m/%-d/%Y"),
                    "amount": f"${(inv.amount_paid or inv.amount_due) / 100:,.0f}",
                    "status": inv.status,
                    "pdf_url": inv.invoice_pdf,
                })
        except Exception:
            pass

        return {
            "ok": True,
            "status": sub.status,
            "next_date": next_date,
            "amount": f"${amount:,.0f}/mo",
            "payment_method": pm_desc,
            "portal_url": portal_url,
            "trial_end": datetime.fromtimestamp(sub.trial_end).strftime("%-m/%-d/%Y") if sub.trial_end else None,
            "invoices": invoices,
        }, 200
    except Exception as e:
        return {"ok": False, "reason": "stripe_error", "error": str(e)}, 200


@mobile_bp.route("/account/billing")
@mobile_login_required
def mobile_billing(current_user):
    payload, status = _do_mobile_billing(current_user["restaurant_id"])
    return jsonify(**payload), status


# ── Settings audit additions ──────────────────────────────────────────────

@mobile_bp.route("/account/activity")
@mobile_login_required
def mobile_account_activity(current_user):
    """Account-level events (password/email/2FA/team/export/etc.) — the
    user-facing slice of activity_log. Sign-ins are in /account/login-history."""
    from models import get_account_activity
    return jsonify(ok=True, events=get_account_activity(current_user["restaurant_id"]))


@mobile_bp.route("/account/2fa/trusted-devices")
@mobile_login_required
def mobile_trusted_devices(current_user):
    from auth import get_trusted_devices
    return jsonify(ok=True, devices=get_trusted_devices(current_user["restaurant_id"]))


@mobile_bp.route("/account/2fa/trusted-devices/<int:device_id>/revoke", methods=["POST"])
@mobile_login_required
def mobile_revoke_trusted_device(current_user, device_id):
    from auth import revoke_trusted_device
    if not revoke_trusted_device(current_user["restaurant_id"], device_id):
        return jsonify(ok=False, error="That device wasn't found."), 404
    _log_account_event(current_user["restaurant_id"], "trusted_device_revoked", current_user)
    return jsonify(ok=True)


@mobile_bp.route("/account/2fa/trusted-devices/revoke-all", methods=["POST"])
@mobile_login_required
def mobile_revoke_all_trusted_devices(current_user):
    from auth import revoke_all_trusted_devices
    revoke_all_trusted_devices(current_user["restaurant_id"])
    _log_account_event(current_user["restaurant_id"], "trusted_devices_cleared", current_user)
    return jsonify(ok=True)


@mobile_bp.route("/account/recovery-email", methods=["POST"])
@mobile_login_required
def mobile_set_recovery_email(current_user):
    """Step 1: send a code to the new address. Nothing changes until
    /account/recovery-email/verify confirms it."""
    import re as _re_rec
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()
    if not _re_rec.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        return jsonify(ok=False, error="Enter a valid email address"), 400
    if email == (current_user.get("email") or "").lower():
        return jsonify(ok=False, error="Use a different address than your sign-in email."), 400
    from auth import start_recovery_email
    code = start_recovery_email(current_user["id"], email)
    try:
        from emails import send_recovery_email_code
        send_recovery_email_code(email, code)
    except Exception:
        pass
    return jsonify(ok=True, pending=email)


@mobile_bp.route("/account/recovery-email/verify", methods=["POST"])
@mobile_login_required
def mobile_verify_recovery_email(current_user):
    data = request.get_json() or {}
    from auth import verify_recovery_email
    email = verify_recovery_email(current_user["id"], (data.get("code") or "").strip())
    if not email:
        return jsonify(ok=False, error="That code isn't right, or it expired. Send a new one."), 400
    _log_account_event(current_user["restaurant_id"], "recovery_email_set", current_user, detail=email)
    return jsonify(ok=True, recovery_email=email)


@mobile_bp.route("/account/recovery-email/remove", methods=["POST"])
@mobile_login_required
def mobile_remove_recovery_email(current_user):
    from auth import remove_recovery_email
    remove_recovery_email(current_user["id"])
    _log_account_event(current_user["restaurant_id"], "recovery_email_removed", current_user)
    return jsonify(ok=True)


@mobile_bp.route("/account/auto-approve", methods=["POST"])
@mobile_login_required
def mobile_auto_approve(current_user):
    """See client_api._do_auto_approve — shared with the web route so the
    rule can't mean two different things depending on where it was set."""
    payload, status = _capi._do_auto_approve(current_user["restaurant_id"],
                                             request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@mobile_bp.route("/account/hours", methods=["POST"])
@mobile_login_required
def mobile_hours(current_user):
    """See client_api._do_account_hours."""
    payload, status = _capi._do_account_hours(current_user["restaurant_id"],
                                              request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@mobile_bp.route("/account/data-retention", methods=["POST"])
@mobile_login_required
def mobile_data_retention(current_user):
    """See client_api._do_data_retention."""
    payload, status = _capi._do_data_retention(current_user["restaurant_id"],
                                               request.get_json(silent=True) or {}, current_user)
    return jsonify(**payload), status


@mobile_bp.route("/account/request-deletion", methods=["POST"])
@mobile_login_required
def mobile_request_account_deletion(current_user):
    """Account -> Close my account. Not self-serve deletion — Cavnar AI
    clients are under a service contract, so this records the request and
    notifies Will to start the 30-day wind-down, same as the process has
    always been. What changed (Apple App Store Review Guideline 5.1.1(v)):
    the user now initiates this from a real control in the app, and gets a
    real confirmation back, instead of the app just opening their email
    client and hoping they send it. Idempotent: re-tapping the button after
    a request already went through returns the original timestamp rather
    than sending a second notification."""
    from models import request_account_deletion, get_deletion_requested_at
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    already_requested = bool(get_deletion_requested_at(rid))
    requested_at = request_account_deletion(rid)
    if not already_requested:
        try:
            from emails import send_account_deletion_request_email
            send_account_deletion_request_email(restaurant.name, restaurant.owner_name,
                                                current_user.get("email"), requested_at)
        except Exception as e:
            print(f"Account deletion notice email failed: {e}")
        try:
            _log_account_event(rid, "deletion_requested", current_user)
        except Exception:
            pass
    return jsonify(ok=True, requested_at=requested_at)


@mobile_bp.route("/account/report-bug", methods=["POST"])
@mobile_login_required
def mobile_report_bug(current_user):
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if len(message) < 5:
        return jsonify(ok=False, error="Tell us a little more about what happened."), 400
    restaurant = get_restaurant(current_user["restaurant_id"])
    meta = {k: str(data.get(k))[:80] for k in ("build", "app_version", "ios_version", "device", "screen") if data.get(k)}
    meta["username"] = current_user.get("username")
    try:
        from emails import send_bug_report_email
        send_bug_report_email(restaurant.name if restaurant else "Unknown", current_user.get("email") or "", message[:4000], meta)
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't send that right now ({e})."), 500
    return jsonify(ok=True)


# ── Analytics chart feeds ────────────────────────────────────────────────

@mobile_bp.route("/reviews/topic-weeks")
@mobile_login_required
def mobile_topic_weeks(current_user):
    from models import get_topic_weeks
    try:
        return jsonify(ok=True, data=get_topic_weeks(current_user["restaurant_id"], weeks=8))
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/labor/daily")
@mobile_login_required
def mobile_labor_daily(current_user):
    from models import get_labor_daily
    try:
        return jsonify(ok=True, days=get_labor_daily(current_user["restaurant_id"], days=14))
    except Exception as e:
        return jsonify(ok=False, days=[], error=_safe_err(e)), 500


@mobile_bp.route("/intel/ai-visibility/history")
@mobile_login_required
def mobile_ai_visibility_history(current_user):
    from models import get_ai_visibility_history
    try:
        return jsonify(ok=True, runs=get_ai_visibility_history(current_user["restaurant_id"], limit=10))
    except Exception as e:
        return jsonify(ok=False, runs=[], error=_safe_err(e)), 500



# ── Web parity: the phone halves of what the dashboard had first ──────────

@mobile_bp.route("/reviews/<int:review_id>/mark-posted", methods=["POST"])
@mobile_login_required
def mobile_mark_posted(review_id, current_user):
    """A reply the owner pasted onto Yelp/Facebook themselves — same
    handler the web's "Mark as posted" button hits."""
    import admin_routes as _admin
    return _admin.mark_posted.__wrapped__(review_id, current_user=current_user)


@mobile_bp.route("/account/referral", methods=["POST"])
@mobile_login_required
def mobile_send_referral(current_user):
    import admin_routes as _admin
    return _admin.send_referral.__wrapped__(current_user=current_user)


@mobile_bp.route("/account/dismiss-onboarding", methods=["POST"])
@mobile_login_required
def mobile_dismiss_onboarding(current_user):
    return _capi.dismiss_onboarding.__wrapped__(current_user=current_user)


@mobile_bp.route("/connections/instagram/authorize")
@mobile_login_required
def mobile_instagram_authorize(current_user):
    """Same Meta OAuth dialog the web's popup opens, with a signed mobile
    state (see gmb.sign_mobile_state) so the callback can finish via the
    cavnarai://ig-callback deep link instead of window.opener."""
    import urllib.parse
    from gmb import sign_mobile_state
    from meta_api import oauth_dialog_url
    app_id = os.getenv("META_APP_ID", "")
    if not app_id:
        return jsonify(ok=False, error="Instagram isn't configured on this server yet — contact will@cavnar.ai."), 503
    redirect_uri = os.getenv("META_REDIRECT_URI", "https://dashboard.cavnar.ai/instagram/callback")
    scope = "instagram_basic,instagram_content_publish,instagram_manage_insights,pages_read_engagement,pages_manage_posts,pages_show_list,business_management,read_insights"
    params = urllib.parse.urlencode({
        "client_id": app_id, "redirect_uri": redirect_uri, "scope": scope,
        "auth_type": "rerequest", "response_type": "code",
        "state": sign_mobile_state(current_user["restaurant_id"]),
    })
    return jsonify(ok=True, url=oauth_dialog_url(params))


@mobile_bp.route("/connections/instagram", methods=["DELETE"])
@mobile_login_required
def mobile_instagram_disconnect(current_user):
    update_restaurant(current_user["restaurant_id"], {
        "ig_token": None, "ig_user_id": None, "ig_token_expires": None,
        "fb_page_token": None, "fb_page_id": None, "fb_token_expires": None,
    })
    return jsonify(ok=True)


def _may_manage_team(current_user):
    """Whether this login may change ratings, targets, profiles or weighting.

    Defaults open, because every restaurant has exactly one login today and
    locking them out of their own settings would be absurd. The column
    exists so that the moment a second login is invited, the invite can
    create it without this permission rather than handing a new teammate the
    ability to re-rate the entire staff.
    """
    if current_user.get("is_admin"):
        return True
    value = current_user.get("can_manage_team")
    return True if value is None else bool(value)


def _refuse_team_write():
    return jsonify(ok=False,
                   error="Your login can view the team but not change ratings or "
                         "targets. Ask whoever set up this account."), 403


# ── Shift Quality Engine ───────────────────────────────────────────────────

@mobile_bp.route("/labor/schedule/score", methods=["POST"])
@mobile_login_required
def mobile_score_schedule(current_user):
    """Re-score a schedule a manager has just edited.

    The whole reason the engine is a pure function: the manager drags one
    shift, this returns the new number and the new reasons, and nothing is
    regenerated. No model call, no cost, no waiting.

    Rows come from the client because the client is holding the edit. They
    are treated as untrusted input — only the eight schedule columns are
    read, and everything the score depends on beyond them (ratings,
    profiles, targets, history) is loaded server-side.
    """
    from client_api import quality_inputs_from_db, _score_schedule_quality
    rid = current_user["restaurant_id"]
    data = request.get_json(silent=True) or {}
    raw_rows = data.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return jsonify(ok=False, error="rows required"), 400
    if len(raw_rows) > 2000:
        return jsonify(ok=False, error="that is more rows than a week can hold"), 400

    _COLS = ("date", "day", "employee", "role", "shift_start", "shift_end",
             "scheduled_hours", "notes")
    rows = [{c: str(r.get(c) or "")[:200] for c in _COLS}
            for r in raw_rows if isinstance(r, dict)]
    if not rows:
        return jsonify(ok=False, error="no readable rows"), 400

    try:
        targets = {str(k): float(v) for k, v in (data.get("daily_target_hours") or {}).items()}
    except (TypeError, ValueError):
        targets = {}

    # Sample data must never reach a real evaluation. load_shifts_for_restaurant
    # substitutes a bundled fictional week when nothing has been uploaded,
    # which would judge this restaurant's tenure and typical headcount
    # against a restaurant that does not exist.
    from labor import analyse_shifts_for_restaurant
    try:
        if not (analyse_shifts_for_restaurant(rid) or {}).get("is_live"):
            return jsonify(ok=False,
                           error="Upload your shifts before scoring a schedule."), 400
    except Exception:
        pass

    try:
        inputs = quality_inputs_from_db(rid, daily_target_hours=targets)
        quality, what_if = _score_schedule_quality(rid, rows, inputs)
        saved = 0
        # The whole point of an override. Without this the edit lived in the
        # page, the score moved, and publishing read the CSV saved at
        # generation time — so staff received the week the manager had just
        # fixed, unfixed, with nothing on screen to say so.
        if data.get("save"):
            from models import update_schedule_history_rows
            saved = update_schedule_history_rows(
                rid, _rows_to_csv(rows), quality=quality,
                history_id=data.get("history_id"),
                edited_by=current_user.get("username") or current_user.get("email"))
        from models import capability_version
        return jsonify(ok=True, quality=quality, what_if=what_if, saved=bool(saved),
                       history_id=saved or None,
                       capability_version=capability_version(rid)), 200
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


_SCHEDULE_COLS = ("date", "day", "employee", "role", "shift_start", "shift_end",
                  "scheduled_hours", "notes")


def _rows_to_csv(rows: list) -> str:
    """Rebuild the stored CSV from edited rows, in the one column order the
    rest of the pipeline reads — the same writer _run_schedule_job uses, so
    a saved edit and a generated schedule are byte-compatible."""
    lines = [",".join(_SCHEDULE_COLS)]
    for r in rows:
        lines.append(",".join(str(r.get(c, "") or "").replace(",", ";")
                              for c in _SCHEDULE_COLS))
    return "\n".join(lines)


@mobile_bp.route("/labor/schedule/replacements", methods=["POST"])
@mobile_login_required
def mobile_schedule_replacements(current_user):
    """Who could take one shift instead of the person on it.

    One answer, served to both surfaces. Three implementations of this rule
    had drifted apart: the what-if pass checked availability, constraints,
    double booking and the hours ceiling; iOS checked availability only; the
    dashboard checked neither, and would happily offer somebody who had
    declared that day unavailable or was already at thirty-eight hours.
    """
    from client_api import quality_inputs_from_db
    from models import get_operational_scores, get_unavailability_map, get_staff_notes
    import shift_quality as _sq
    rid = current_user["restaurant_id"]
    data = request.get_json(silent=True) or {}
    raw_rows = data.get("rows")
    try:
        index = int(data.get("index"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="index required"), 400
    if not isinstance(raw_rows, list) or not (0 <= index < len(raw_rows)):
        return jsonify(ok=False, error="rows and a valid index required"), 400
    if len(raw_rows) > 2000:
        return jsonify(ok=False, error="that is more rows than a week can hold"), 400

    rows = [{c: str(r.get(c) or "")[:200] for c in _SCHEDULE_COLS}
            for r in raw_rows if isinstance(r, dict)]
    try:
        scores = get_operational_scores(rid)
        availability = get_unavailability_map(rid)
        try:
            constraints = {n["employee_name"]: n["notes"] for n in (get_staff_notes(rid) or [])
                           if n.get("employee_name")}
        except Exception:
            # Constraints tighten the answer; losing them must not stop a
            # manager finding out who is free. The swap check still enforces
            # availability, double booking and the hours ceiling.
            constraints = {}
        target = rows[index]

        # Everybody else already on the schedule is a candidate; the same
        # legality check the what-if pass uses decides which of them could
        # actually take this shift.
        seen, out = set(), []
        for j, row in enumerate(rows):
            name = (row.get("employee") or "").strip()
            if not name or name.lower() in seen or j == index:
                continue
            if not _sq._swap_is_legal(rows, min(index, j), max(index, j),
                                      availability, scores, constraints):
                continue
            seen.add(name.lower())
            out.append({"name": name, "role": row.get("role"),
                        "score": scores.get(name),
                        "date": row.get("date"), "day": row.get("day")})
        out.sort(key=lambda m: (-(m["score"] or 0), m["name"]))
        return jsonify(ok=True, replacements=out,
                       employee=target.get("employee"), role=target.get("role")), 200
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e), replacements=[]), 500


@mobile_bp.route("/labor/profiles")
@mobile_login_required
def mobile_shift_profiles(current_user):
    """Shift profiles, plus the defaults they would fall back to.

    Always returns a usable set: a restaurant with none configured gets the
    engine's built-ins, marked as such, so the editor has something real to
    show rather than an empty screen and a create button.
    """
    from models import (get_shift_profiles, get_quality_weights,
                        get_role_strength_thresholds, get_shift_leader_rules,
                        get_operational_scores)
    import shift_quality as _sq
    rid = current_user["restaurant_id"]
    try:
        stored = get_shift_profiles(rid)
        scores = get_operational_scores(rid)
        resolved = _sq.profiles_from_config(
            [_sq.profile_from_dict(p) for p in stored] or None,
            default_strength=get_role_strength_thresholds(rid) if scores else {},
            default_leader_rules=get_shift_leader_rules(rid) if scores else [])
        return jsonify(
            ok=True,
            using_defaults=not stored,
            profiles=[_sq.profile_to_dict(p) for p in resolved],
            weights=get_quality_weights(rid) or _sq.DEFAULT_WEIGHTS,
            default_weights=_sq.DEFAULT_WEIGHTS,
            dimensions=[{"key": k, "label": k.replace("_", " ").capitalize(),
                         "customer_facing": k in _sq.CUSTOMER_DIMENSIONS}
                        for k in _sq.DIMENSIONS],
            demand_levels=list(_sq.DEMAND_LEVELS),
        ), 200
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e), profiles=[]), 500


@mobile_bp.route("/labor/profiles", methods=["POST"])
@mobile_login_required
def mobile_save_shift_profile(current_user):
    """Create or update one shift profile."""
    if not _may_manage_team(current_user):
        return _refuse_team_write()
    from models import save_shift_profile
    import shift_quality as _sq
    rid = current_user["restaurant_id"]
    data = request.get_json(silent=True) or {}
    profile = data.get("profile") or data
    if not str(profile.get("key") or "").strip():
        return jsonify(ok=False, error="a profile needs a key"), 400
    demand = str(profile.get("demand") or "normal")
    if demand not in _sq.DEMAND_LEVELS:
        return jsonify(ok=False, error=f"{demand!r} is not a demand level"), 400
    try:
        quality = int(profile.get("min_quality") or 70)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="min_quality must be a number"), 400
    if not 0 <= quality <= 100:
        return jsonify(ok=False, error="min_quality must be between 0 and 100"), 400
    try:
        # Normalised through the dataclass so a stored profile can never
        # carry a shape the engine will not read back.
        clean = _sq.profile_to_dict(_sq.profile_from_dict(profile))
        clean["active"] = profile.get("active", True)
        from models import record_capability_change, get_shift_profiles
        who = current_user.get("username") or current_user.get("email")
        _before = next((p for p in get_shift_profiles(rid, include_inactive=True)
                        if p.get("key") == clean["key"]), None)
        saved = save_shift_profile(rid, clean, updated_by=who)
        record_capability_change(rid, "profile", subject=clean["key"],
                                 before=_before, after=saved, changed_by=who)
        return jsonify(ok=True, profile=saved), 200
    except ValueError as ve:
        return jsonify(ok=False, error=str(ve)), 400
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/labor/profiles/delete", methods=["POST"])
@mobile_login_required
def mobile_delete_shift_profile(current_user):
    if not _may_manage_team(current_user):
        return _refuse_team_write()
    from models import delete_shift_profile
    data = request.get_json(silent=True) or {}
    key = str(data.get("key") or "").strip()
    if not key:
        return jsonify(ok=False, error="key required"), 400
    try:
        return jsonify(ok=True, deleted=delete_shift_profile(current_user["restaurant_id"], key)), 200
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/labor/quality-weights", methods=["POST"])
@mobile_login_required
def mobile_save_quality_weights(current_user):
    """How much each dimension counts for this restaurant.

    An unknown dimension name is dropped rather than refused: a saved
    weight for a dimension that has since been renamed must not stop an
    owner saving the rest.
    """
    if not _may_manage_team(current_user):
        return _refuse_team_write()
    import json as _j
    from models import update_restaurant
    import shift_quality as _sq
    data = request.get_json(silent=True) or {}
    raw = data.get("weights")
    if not isinstance(raw, dict):
        return jsonify(ok=False, error="weights required"), 400
    cleaned, ignored = {}, []
    for key, value in raw.items():
        if key not in _sq.DIMENSIONS:
            ignored.append(key)
            continue
        try:
            n = float(value)
        except (TypeError, ValueError):
            return jsonify(ok=False, error=f"{key}: {value!r} is not a number"), 400
        if n < 0:
            return jsonify(ok=False, error=f"{key}: a weight cannot be negative"), 400
        if n > _sq.MAX_WEIGHT:
            return jsonify(ok=False,
                           error=f"{key}: {n:g} is beyond the {_sq.MAX_WEIGHT} ceiling — "
                                 "past that, every other dimension stops counting"), 400
        cleaned[key] = n
    if cleaned and not any(cleaned.values()):
        return jsonify(ok=False, error="at least one dimension has to count for something"), 400
    try:
        from models import record_capability_change, get_quality_weights
        rid = current_user["restaurant_id"]
        _before = get_quality_weights(rid)
        update_restaurant(rid, {"quality_weights_json": _j.dumps(cleaned) if cleaned else None})
        record_capability_change(
            rid, "weights", subject="dimension weighting", before=_before, after=cleaned,
            changed_by=current_user.get("username") or current_user.get("email"))
        return jsonify(ok=True, weights=cleaned, ignored=ignored), 200
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500


@mobile_bp.route("/labor/capability-changes")
@mobile_login_required
def mobile_capability_changes(current_user):
    """Who changed a rating, a target, a profile or the weighting, and when.

    The first question after a disputed schedule, and until now there was
    nowhere to look: the capability tables carried the current value's
    author and nothing else, and the per-role targets lived in a plain
    column with no provenance at all.
    """
    from models import get_capability_changes
    try:
        return jsonify(ok=True,
                       changes=get_capability_changes(current_user["restaurant_id"],
                                                      limit=100)), 200
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e), changes=[]), 500


@mobile_bp.route("/ask-cavnar/opening")
@mobile_login_required
def mobile_ask_opening(current_user):
    """What the assistant says before the owner types anything.

    Two things were already being computed and thrown away. home_brief
    builds situation-aware questions from real signals — reviews changed
    this month and the top issue by name, labor over or under target, food
    cost opportunities — and only the Home tab used them, while both Ask
    surfaces hardcoded the same three strings that never changed. And the
    attention items that make a real briefing were sitting in the same
    payload.

    Deliberately no model call. This is the first thing an owner sees, it
    has to be instant, and every line here is measured rather than written —
    which also means it cannot invent anything.
    """
    import home_brief
    rid = current_user["restaurant_id"]
    try:
        payload, status = home_brief.build_home_brief(current_user)
        if status != 200:
            return jsonify(ok=True, briefing=[], suggestions=_FALLBACK_ASK_SUGGESTIONS,
                           headline=None), 200

        attention = payload.get("attention") or []
        order = {"critical": 0, "important": 1, "watch": 2}
        attention = sorted(attention, key=lambda a: order.get(a.get("severity"), 3))
        briefing = [{"severity": a.get("severity"), "title": a.get("title"),
                     "detail": a.get("detail"), "module": a.get("module")}
                    for a in attention[:4]]

        # A win is worth one line when there is one. An owner who only ever
        # opens this to a list of problems stops opening it.
        wins = payload.get("wins") or []
        if wins and len(briefing) < 4:
            w = wins[0]
            briefing.append({"severity": "good", "title": w.get("title"),
                             "detail": w.get("detail"), "module": w.get("module")})

        suggestions = payload.get("ask_suggestions") or _FALLBACK_ASK_SUGGESTIONS
        changes = payload.get("changes") or {}
        return jsonify(
            ok=True,
            briefing=briefing,
            suggestions=suggestions[:5],
            headline=_opening_headline(payload, briefing),
            since_label=changes.get("since_label"),
            changes=[c.get("label") for c in (changes.get("items") or [])[:3] if c.get("label")],
            greeting_name=payload.get("greeting_name"),
            restaurant=(payload.get("context") or {}).get("restaurant_name"),
            location=(payload.get("context") or {}).get("location_name"),
        ), 200
    except Exception as e:
        # The opening must never be the reason Ask fails to open.
        try:
            import ops
            ops.capture(e, job="ask_opening", context=f"restaurant_id={rid}")
        except Exception:
            pass
        return jsonify(ok=True, briefing=[], suggestions=_FALLBACK_ASK_SUGGESTIONS,
                       headline=None), 200


# Only used when the brief cannot be built — a brand new restaurant with
# nothing to say yet, or a failure. Never the normal path.
_FALLBACK_ASK_SUGGESTIONS = [
    "What should I focus on today?",
    "How are my reviews doing?",
    "How do I get my labor cost down?",
]


def _opening_headline(payload, briefing):
    """One line naming the state of the business, in the owner's terms."""
    critical = sum(1 for b in briefing if b.get("severity") == "critical")
    important = sum(1 for b in briefing if b.get("severity") == "important")
    if critical:
        return f"{critical} thing{'' if critical == 1 else 's'} needs you today."
    if important:
        return f"Nothing urgent. {important} worth a look."
    if briefing:
        return "Quiet morning — nothing urgent."
    if (payload.get("empty_state") or {}).get("active"):
        return "Not much to go on yet — connect your data and I can be useful."
    return "All clear. Ask me anything."
