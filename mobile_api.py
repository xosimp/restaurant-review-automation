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

mobile_bp = Blueprint('mobile_api', __name__, url_prefix='/mobile/api')


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
    """Account activity log (Account -> Security -> Account activity)."""
    try:
        from models import log_event
        data = {"detail": detail}
        if current_user:
            data["actor"] = current_user.get("username")
        log_event(restaurant_id, event_type, data)
    except Exception:
        pass


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
        return jsonify(ok=False, error="No account found for that Apple ID. Contact will@cavnar.ai."), 401

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

        modules_out.append({"key": key, "label": m["label"], "icon": key, "status": m["status"], "kpi": kpi})

    # "Needs attention" — same three checks and thresholds as the web Home
    # tab's card list (templates/dashboard.html, id="home-attention-list").
    # `module` names the module key a tap should navigate into — the mobile
    # app no longer has one tab per module, so this is a key into the
    # Modules grid/registry, not a literal tab name.
    needs_attention = []
    if "reviews" in active_keys and rstats.get("awaiting_approval", 0) > 0:
        n = rstats["awaiting_approval"]
        needs_attention.append({
            "type": "reviews_awaiting_approval", "module": "reviews",
            "title": f"{n} review{'' if n == 1 else 's'} awaiting approval",
            "detail": "AI responses drafted — publish in one click",
        })
    if "labor" in active_keys and labor and labor.get("overtime_risk"):
        ot_count = sum(1 for o in labor["overtime_risk"] if o.get("status") == "overtime")
        if ot_count > 0:
            needs_attention.append({
                "type": "labor_overtime", "module": "labor",
                "title": f"{ot_count} staff member{'' if ot_count == 1 else 's'} in overtime",
                "detail": f"Est. ${ot_count * 38}+ extra in OT wages this week",
            })
    if "reviews" in active_keys and rstats.get("total", 0) > 0 and rstats.get("response_rate", 0) < 50:
        needs_attention.append({
            "type": "low_response_rate", "module": "reviews",
            "title": f"Response rate at {rstats['response_rate']}%",
            "detail": "Restaurants at 80%+ get 2x more new guests",
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
        return jsonify(ok=False, error=str(e)), 500


@mobile_bp.route("/reviews/platform-breakdown")
@mobile_login_required
def mobile_platform_breakdown(current_user):
    from models import get_platform_breakdown
    try:
        data = get_platform_breakdown(current_user["restaurant_id"])
        return jsonify(ok=True, data=data)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


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
        return jsonify(ok=False, error=str(e)), 500


@mobile_bp.route("/reviews/sentiment-trend")
@mobile_login_required
def mobile_sentiment_trend(current_user):
    from models import get_sentiment_trend
    try:
        data = get_sentiment_trend(current_user["restaurant_id"], weeks=8)
        return jsonify(ok=True, weeks=data)
    except Exception as e:
        return jsonify(ok=False, weeks=[], error=str(e)), 500


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
        current_user["restaurant_id"], data.get("question"), history=data.get("history")
    )
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
        items, _is_live = load_inventory_for_restaurant(rid)
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
                restaurant_id=rid, items=items,
            )
            _capi._cache_set("mobile-inv-insight:" + str(rid), insight)
        return jsonify(
            ok=True,
            insight=insight,
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
        return jsonify(ok=False, error=str(e), insight="Analysis unavailable — check back shortly.",
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
        return jsonify(ok=False, weeks=[], error=str(e)), 500


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


@mobile_bp.route("/device-tokens/<apns_token>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_device_token(apns_token, current_user):
    from push import get_device_tokens, remove_device_token
    # apns_tokens are long random hex strings issued by Apple, not sequential
    # ids — effectively unguessable — but scope the delete to the caller's
    # own restaurant anyway rather than trusting any authenticated bearer.
    owned = any(t["apns_token"] == apns_token for t in get_device_tokens(current_user["restaurant_id"]))
    if not owned:
        return jsonify(ok=False, error="Device token not found"), 404
    remove_device_token(apns_token)
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


def _do_mobile_labor(restaurant_id):
    from labor import analyse_shifts_for_restaurant

    restaurant = get_restaurant(restaurant_id)
    target = float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0
    hourly_rate = float(restaurant.hourly_rate or 26.0) if restaurant else 26.0
    try:
        analysis = analyse_shifts_for_restaurant(restaurant_id)
    except Exception:
        analysis = {}

    overall_pct = analysis.get("overall_labor_pct", 0)

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
    ot_premium = 0.0
    for o in analysis.get("overtime_risk", []):
        if o.get("status") == "overtime":
            ot_premium += max(0, o.get("hours", 0) - 40) * hourly_rate * 0.5

    date_range = analysis.get("date_range") or {}
    period_days = 14
    try:
        if date_range.get("start") and date_range.get("end"):
            _start = datetime.strptime(date_range["start"], "%Y-%m-%d")
            _end = datetime.strptime(date_range["end"], "%Y-%m-%d")
            period_days = max((_end - _start).days + 1, 1)
    except Exception:
        pass
    total_sales = analysis.get("total_sales", 0)
    monthly_sales_est = (total_sales / period_days * 30) if period_days else 0
    potential_savings = analysis.get("potential_savings", 0)
    labor_monthly = round(potential_savings * 4.33)
    # 0.345 = midpoint of the 33-36% full-service industry range (NRA 2024
    # Restaurant Operations Data Abstract) — was 0.32, a leftover from the
    # stale pre-pandemic 28-32% benchmark already corrected everywhere else
    # this figure appears (labor.py's AI prompt, the web dashboard, iOS's
    # own benchmark band).
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
        "on_track": overall_pct <= target,
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
        return jsonify(ok=False, weeks=[], error=str(e)), 500


@mobile_bp.route("/labor/gap")
@mobile_login_required
def mobile_labor_gap(current_user):
    from labor import analyse_shifts_for_restaurant, calculate_monthly_gap
    try:
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        gap = calculate_monthly_gap(analysis)
        return jsonify(ok=True, **gap)
    except Exception as e:
        return jsonify(ok=False, error=str(e), over_target=False, monthly_gap=0,
                       current_pct=0, target_pct=30), 500


def _insight_json(insight_text):
    """Structured {intro, recommendations, forecast} fields for a raw AI
    insight string — the same parsing client_api.format_insight_html() uses
    to build the web's HTML, just handed back as JSON so the iOS app can
    render its own native equivalent instead of a plain text blob."""
    intro, recs, forecast = _capi.parse_insight_sections(insight_text)
    return {"insight_intro": intro, "insight_recommendations": recs, "insight_forecast": forecast}


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
                       insight_recommendations=[], insight_forecast=None, error=str(e)), 500


@mobile_bp.route("/labor/generate-schedule", methods=["POST"])
@mobile_login_required
def mobile_generate_schedule(current_user):
    """Reuses client_api.py's existing background-job machinery
    (_run_schedule_job / _schedule_jobs) rather than building a second job
    system — the same async-generate-then-poll pattern the web Labor tab
    already relies on."""
    import threading
    import uuid
    from ai_utils import ai_rate_limited

    rid = current_user["restaurant_id"]
    if ai_rate_limited(f"schedule:{rid}", max_calls=3, window_secs=60):
        return jsonify(ok=False, error="Too many schedule generations — please wait a moment and try again."), 429
    job_id = str(uuid.uuid4())
    _capi._schedule_jobs[job_id] = {"status": "pending", "result": None}
    t = threading.Thread(target=_capi._run_schedule_job, args=(job_id, rid), daemon=True)
    t.start()
    return jsonify(ok=True, job_id=job_id)


@mobile_bp.route("/labor/schedule-status/<job_id>")
@mobile_login_required
def mobile_schedule_status(job_id, current_user):
    """Mirrors client_api.py's schedule_status() route body, reading the
    same shared _schedule_jobs dict — unlike the web route this one does
    require auth (mobile_login_required), a small deliberate hardening over
    the web version's job-id-is-unguessable-so-no-login-needed approach,
    since bearer auth costs nothing extra to check here."""
    job = _capi._schedule_jobs.get(job_id)
    if not job:
        return jsonify(ok=False, status="error", error="Job not found"), 404
    if job["status"] == "pending":
        return jsonify(ok=True, status="pending")
    try:
        result = dict(job["result"])
        result["status"] = job["status"]
        _capi._schedule_jobs.pop(job_id, None)
        return jsonify(**result)
    except Exception as e:
        return jsonify(ok=False, status="error", error=str(e)), 500


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
        return jsonify(ok=False, history=[], error=str(e)), 500


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


def _do_mobile_marketing(restaurant_id):
    from marketing import get_content_calendar_ideas
    stats = _do_mobile_marketing_stats(restaurant_id)
    try:
        calendar = get_content_calendar_ideas(restaurant_id=restaurant_id)
    except Exception:
        calendar = []
    return {"ok": True, "stats": stats, "calendar": calendar}, 200


@mobile_bp.route("/marketing")
@mobile_login_required
def mobile_marketing(current_user):
    payload, status = _do_mobile_marketing(current_user["restaurant_id"])
    return jsonify(**payload), status


def _do_mobile_generate_content(restaurant_id, content_type, topic):
    from marketing import generate_content
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"gencontent:{restaurant_id}", max_calls=8, window_secs=60):
        return {"ok": False, "error": "Too many requests — please wait a moment and try again."}, 429
    try:
        result = generate_content(content_type or "instagram_post", topic or "", restaurant_id=restaurant_id)
        return {"ok": True, "content": result}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@mobile_bp.route("/marketing/generate-content", methods=["POST"])
@mobile_login_required
def mobile_generate_content(current_user):
    data = request.get_json() or {}
    payload, status = _do_mobile_generate_content(
        current_user["restaurant_id"], data.get("type"), data.get("topic")
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
        result = send_campaign(rid, message)
        return jsonify(ok=True, **result)
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
        return jsonify(ok=False, error=str(e)), 500


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
    return jsonify(ok=True, join_url=join_url)


# ── Intel ─────────────────────────────────────────────────────────────────

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

        own_rating = None
        if restaurant.module_reviews:
            from models import get_review_stats
            rstats = get_review_stats(restaurant_id)
            if rstats and rstats.get("avg_rating"):
                own_rating = rstats["avg_rating"]

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
                }
                for c in blob.get("competitors", [])
            ],
            "updated_at": restaurant.competitor_updated_at,
            "own_rating": own_rating,
        }, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@mobile_bp.route("/intel")
@mobile_login_required
def mobile_intel(current_user):
    payload, status = _do_mobile_intel(current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/intel/refresh-competitors", methods=["POST"])
@mobile_login_required
def mobile_refresh_competitors(current_user):
    """Same async job pattern as /labor/generate-schedule — reuses
    admin_routes.py's existing _run_competitor_job/_competitor_jobs rather
    than building a second job system. (admin_routes.py despite its
    filename: this specific route is @login_required, not @admin_required —
    any logged-in owner can trigger it, matching the web dashboard's own
    "Refresh" button.)"""
    import threading
    import uuid
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not (restaurant and restaurant.module_reviews and restaurant.module_labor
            and restaurant.module_inventory and restaurant.module_marketing):
        return jsonify(ok=False, error="Competitor intelligence is available on the Full System plan only."), 403
    import admin_routes as _admin
    job_id = str(uuid.uuid4())
    _admin._competitor_jobs[job_id] = {"status": "pending", "result": None}
    t = threading.Thread(target=_admin._run_competitor_job, args=(job_id, rid), daemon=True)
    t.start()
    return jsonify(ok=True, job_id=job_id)


@mobile_bp.route("/intel/refresh-status/<job_id>")
@mobile_login_required
def mobile_refresh_competitors_status(job_id, current_user):
    """Mirrors mobile_schedule_status's body/shape, reading admin_routes.py's
    shared _competitor_jobs dict."""
    import admin_routes as _admin
    job = _admin._competitor_jobs.get(job_id)
    if not job:
        return jsonify(ok=False, status="error", error="Job not found"), 404
    if job["status"] == "pending":
        return jsonify(ok=True, status="pending")
    try:
        result = dict(job["result"])
        result["status"] = job["status"]
        _admin._competitor_jobs.pop(job_id, None)
        return jsonify(**result)
    except Exception as e:
        return jsonify(ok=False, status="error", error=str(e)), 500


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

    profile = {
        "restaurant_name": restaurant.name,
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
    connections = {
        "google_business": {
            "connected": bool(getattr(restaurant, "gmb_refresh_token", None)),
        },
        "instagram": {
            "connected": bool(getattr(restaurant, "ig_token", None)),
        },
        "toast": {
            "connected": bool(getattr(restaurant, "toast_restaurant_guid", None)),
            "last_synced": getattr(restaurant, "toast_last_synced", None),
        },
        "square": {
            "connected": bool(getattr(restaurant, "square_location_id", None)),
            "last_synced": getattr(restaurant, "square_last_synced", None),
        },
        "clover": {
            "connected": bool(getattr(restaurant, "clover_merchant_id", None)),
            "last_synced": getattr(restaurant, "clover_last_synced", None),
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
    data = request.get_json() or {}
    update_restaurant(current_user["restaurant_id"], {"login_notify": int(bool(data.get("enabled")))})
    _log_account_event(current_user["restaurant_id"], "login_notify_changed", current_user,
                       detail="on" if data.get("enabled") else "off")
    return jsonify(ok=True)


@mobile_bp.route("/account/marketing-opt-out", methods=["POST"])
@mobile_login_required
def mobile_toggle_marketing_opt_out(current_user):
    """Gates only the non-critical automated sends (onboarding drip,
    monthly summary) at their scheduler.py call sites — security/
    transactional email (2FA, login notify, password/email-changed,
    welcome) is never affected by this flag."""
    data = request.get_json() or {}
    update_restaurant(current_user["restaurant_id"], {"marketing_emails_opt_out": int(bool(data.get("opted_out")))})
    _log_account_event(current_user["restaurant_id"], "marketing_emails_changed", current_user,
                       detail="off" if data.get("opted_out") else "on")
    return jsonify(ok=True)


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
            "html": html,
        })
        try:
            log_email(rid, "digest", to_email, f"[Preview] Weekly digest — {restaurant.name}")
        except Exception: pass
        return jsonify(ok=True, email=to_email)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


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
            "html": "<p>Attached: " + ", ".join(labels) + ".</p>",
            "attachments": attachments,
        })
        try:
            log_email(rid, "data_export", to_email, f"Data export — {restaurant.name}")
        except Exception: pass
        _log_account_event(rid, "data_exported", current_user, detail=", ".join(scopes))
        return jsonify(ok=True, email=to_email, scopes=scopes)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


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


def _do_mobile_billing(restaurant_id):
    import os as _os
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
    """The one rule: drafted 5-star responses get approved (and posted, when
    Google is connected) without waiting — capped per day, with a kill
    switch. Runs inside the daily fetch (scheduler.auto_approve_five_stars)."""
    data = request.get_json() or {}
    cap = data.get("daily_cap", 5)
    try:
        cap = max(1, min(50, int(cap)))
    except Exception:
        cap = 5
    rid = current_user["restaurant_id"]
    update_restaurant(rid, {
        "auto_approve_5star": int(bool(data.get("enabled"))),
        "auto_approve_daily_cap": cap,
        "auto_approve_paused": int(bool(data.get("paused"))),
    })
    _log_account_event(rid, "auto_approve_changed", current_user,
                       detail=("on" if data.get("enabled") else "off") + (", paused" if data.get("paused") else "") + f", cap {cap}/day")
    return jsonify(ok=True)


_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

@mobile_bp.route("/account/hours", methods=["POST"])
@mobile_login_required
def mobile_hours(current_user):
    """Open/close time per day plus closure dates. close_times_json already
    drove schedule generation (shift_end hard cap); open_times_json is new."""
    import json as _json_h
    data = request.get_json() or {}
    def _clean_times(raw):
        out = {}
        for day in _DAYS:
            v = (raw or {}).get(day)
            if isinstance(v, str) and v.strip():
                out[day] = v.strip()[:12]
        return _json_h.dumps(out) if out else None
    closures = data.get("closures") or []
    if not isinstance(closures, list):
        closures = []
    closures = sorted({str(c).strip()[:10] for c in closures if str(c).strip()})[:60]
    rid = current_user["restaurant_id"]
    update_restaurant(rid, {
        "open_times_json": _clean_times(data.get("open")),
        "close_times_json": _clean_times(data.get("close")),
        "skip_holidays": ",".join(closures) or None,
    })
    _log_account_event(rid, "hours_changed", current_user)
    return jsonify(ok=True)


@mobile_bp.route("/account/data-retention", methods=["POST"])
@mobile_login_required
def mobile_data_retention(current_user):
    """0 = keep everything; otherwise reviews older than N months are
    soft-deleted by the nightly job (models.purge_expired_reviews)."""
    data = request.get_json() or {}
    try:
        months = int(data.get("months", 0))
    except Exception:
        months = 0
    if months not in (0, 6, 12, 24, 36):
        return jsonify(ok=False, error="Choose keep everything, or 6, 12, 24 or 36 months."), 400
    rid = current_user["restaurant_id"]
    update_restaurant(rid, {"data_retention_months": months})
    _log_account_event(rid, "data_retention_changed", current_user, detail=f"{months} months" if months else "keep everything")
    return jsonify(ok=True)


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
        return jsonify(ok=False, error=str(e)), 500


@mobile_bp.route("/labor/daily")
@mobile_login_required
def mobile_labor_daily(current_user):
    from models import get_labor_daily
    try:
        return jsonify(ok=True, days=get_labor_daily(current_user["restaurant_id"], days=14))
    except Exception as e:
        return jsonify(ok=False, days=[], error=str(e)), 500


@mobile_bp.route("/intel/ai-visibility/history")
@mobile_login_required
def mobile_ai_visibility_history(current_user):
    from models import get_ai_visibility_history
    try:
        return jsonify(ok=True, runs=get_ai_visibility_history(current_user["restaurant_id"], limit=10))
    except Exception as e:
        return jsonify(ok=False, runs=[], error=str(e)), 500
