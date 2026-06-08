"""
auth_routes.py — Login, logout, password reset, 2FA, Google auth, session management
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import Blueprint, request, jsonify, make_response, redirect, url_for, render_template
import os
import time

from models import get_conn, get_restaurant, update_restaurant
from auth import (verify_password, create_session, delete_session,
                  get_user_by_restaurant_id, get_sessions_for_user,
                  revoke_other_sessions, update_password, login_required)

auth_bp = Blueprint('auth', __name__)

# ── Login rate limiting ────────────────────────────────────────────────────────
# Tracks failed login attempts per IP: {ip: [timestamp, timestamp, ...]}
_login_attempts = {}
_MAX_ATTEMPTS   = 5      # max failures before lockout
_LOCKOUT_SECS   = 300    # 5 minute lockout

def _get_client_ip():
    """Get real client IP, respecting Railway's proxy headers."""
    return (request.headers.get("X-Forwarded-For","").split(",")[0].strip()
            or request.remote_addr or "unknown")

def _is_rate_limited(ip):
    """Return True if IP has exceeded failed login attempts."""
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    recent = [t for t in attempts if now - t < _LOCKOUT_SECS]
    _login_attempts[ip] = recent
    return len(recent) >= _MAX_ATTEMPTS

def _record_failed_attempt(ip):
    _login_attempts.setdefault(ip, []).append(time.time())

def _clear_attempts(ip):
    _login_attempts.pop(ip, None)

@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        sent = request.args.get("sent")
        return render_template('forgot_password.html', sent=sent)

    # POST — send reset email
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return render_template('rate_limited.html'), 429
    _record_failed_attempt(ip)  # Count each forgot-password POST

    email = request.form.get("email", "").strip().lower()
    if email:
        try:
            from models import create_reset_token
            import resend as _resend
            token = create_reset_token(email)
            if token:
                reset_url = f"https://dashboard.cavnar.ai/reset-password/{token}"
                _resend.api_key = os.getenv("RESEND_API_KEY", "")
                _resend.Emails.send({
                    "from": f"Cavnar AI <{os.getenv('FROM_EMAIL', 'will@cavnar.ai')}>",
                    "to": [email],
                    "subject": "Reset your Cavnar AI password",
                    "html": f"""
                    <div style="font-family:'DM Sans',sans-serif;max-width:480px;margin:0 auto;padding:32px 24px">
                      <div style="font-size:20px;font-weight:600;margin-bottom:24px">Cavnar <em style="color:#c84b2f;font-style:italic">AI</em></div>
                      <h2 style="font-size:18px;font-weight:600;margin-bottom:12px;color:#0e0c0a">Reset your password</h2>
                      <p style="font-size:14px;color:#4a4540;line-height:1.6;margin-bottom:24px">
                        Click the button below to reset your password. This link expires in 1 hour.
                      </p>
                      <a href="{reset_url}" style="display:inline-block;background:#c84b2f;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600">Reset password →</a>
                      <p style="font-size:12px;color:#7a736a;margin-top:24px">If you didn't request this, ignore this email — your password won't change.</p>
                      <hr style="border:none;border-top:1px solid #e5e0db;margin:24px 0">
                      <p style="font-size:11px;color:#9ca3af">Cavnar AI · will@cavnar.ai · cavnar.ai</p>
                    </div>"""
                })
        except Exception as e:
            print(f"[forgot-password] error: {e}")
    # Always redirect to sent page (don't reveal if email exists)
    from flask import redirect as _redir
    return _redir("/forgot-password?sent=1")


@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    from models import validate_reset_token, consume_reset_token
    valid = validate_reset_token(token)

    if request.method == "GET":
        return render_template('reset_password.html', valid=valid)

    # POST — set new password
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        from flask import redirect as _redir
        return _redir("/forgot-password?sent=1"), 429

    if not valid:
        from flask import redirect as _redir
        return _redir("/forgot-password")

    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")

    if len(password) < 8 or password != confirm:
        from flask import redirect as _redir
        return _redir(f"/reset-password/{token}")

    success = consume_reset_token(token, password)
    if success:
        from flask import redirect as _redir
        return _redir("/login?reset=1")
    from flask import redirect as _redir
    return _redir("/forgot-password")



@auth_bp.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        ip = _get_client_ip()

        if _is_rate_limited(ip):
            return render_template('login.html',
                error="Too many failed attempts. Please wait 5 minutes and try again.")
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        user = verify_password(username, password)
        if not user:
            _record_failed_attempt(ip)
            return render_template('login.html', error="Invalid username or password")
        _clear_attempts(ip)
        next_url = request.args.get("next", "/admin" if user["is_admin"] else "/")

        # Check if 2FA is enabled and device not remembered
        try:
            from models import get_restaurant
            _rid = user.get("restaurant_id")
            rest = get_restaurant(_rid) if _rid and not user.get("is_admin") else None
            _device_cookie = request.cookies.get("device_token_" + str(_rid), "")
            _2fa_on = rest and rest.two_fa_enabled and not user.get("is_admin")
            _device_ok = _device_cookie and _device_cookie == getattr(rest, "two_fa_device_token", "")
        except Exception as _e_2fa:
            _2fa_on = False
            _device_ok = False

        if _2fa_on and not _device_ok:
            # Generate and send 2FA code
            import random, datetime as _dt2
            from models import update_restaurant, get_restaurant
            code = str(random.randint(100000, 999999))
            expires = (_dt2.datetime.now() + _dt2.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            update_restaurant(_rid, {"two_fa_code": code, "two_fa_expires": expires})
            # Send email
            try:
                rest2 = get_restaurant(_rid)
                email = rest2.owner_email or ""
                if "@" in email:
                    from emails import send_2fa_code
                    owner = rest2.owner_name or None
                    send_2fa_code(email, rest2.name or "your restaurant", code, owner)
                    masked = email[:2] + "***@" + email.split("@")[-1] if "@" in email else email
                else:
                    masked = "your registered email"
            except Exception:
                masked = "your registered email"
            # Store pending token in DB so it works across Gunicorn workers
            import secrets as _sec3
            pending = _sec3.token_hex(24)
            # Encode uid into pending token: "uid:token"
            pending_signed = str(_rid) + ":" + pending
            import base64 as _b64
            pending_encoded = _b64.urlsafe_b64encode(pending_signed.encode()).decode()
            import secrets as _sec4
            csrf3 = _sec4.token_hex(16)
            resp3 = make_response(render_template('two_fa.html',
                masked_email=masked, error=None,
                pending_token=pending_encoded, next_url=next_url, csrf_token=csrf3))
            resp3.set_cookie("csrf_token", csrf3, httponly=True, samesite="Lax")
            return resp3

        _ua = request.headers.get("User-Agent", "")
        token = create_session(user["id"], ip_address=ip, user_agent=_ua)
        # Send login notification email if enabled
        try:
            from models import get_restaurant as _gr_ln
            _rest_ln = _gr_ln(user.get("restaurant_id")) if user.get("restaurant_id") else None
            if _rest_ln and getattr(_rest_ln, "login_notify", 0) and _rest_ln.owner_email:
                from emails import send_login_notification
                send_login_notification(_rest_ln.owner_email, _rest_ln.name or "", ip, _ua)
        except Exception as _ln_e:
            print(f"[LoginNotify] {_ln_e}")
        resp = make_response(redirect(next_url))
        _on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
        resp.set_cookie("session_token", token, max_age=30*24*3600,
                        httponly=True, secure=_on_railway, samesite="Lax")
        return resp
    import secrets as _sec2
    csrf2 = _sec2.token_hex(16)
    resp2 = make_response(render_template('login.html', error=None))
    resp2.set_cookie("csrf_token", csrf2, httponly=True, samesite="Lax")
    return resp2

@auth_bp.route("/verify-2fa", methods=["GET","POST"])
def verify_2fa():
    import flask as _fl3
    import datetime as _dt3
    from models import get_restaurant, update_restaurant
    if request.method == "POST":
        pending_token = request.form.get("pending_token","")
        code_entered  = request.form.get("code","").strip()
        next_url      = request.form.get("next_url", "/")
        remember      = request.form.get("remember_device","")
        # Decode uid from token
        try:
            import base64 as _b64_v
            decoded = _b64_v.urlsafe_b64decode(pending_token.encode()).decode()
            uid_str, _ = decoded.split(":", 1)
            uid = int(uid_str)
        except Exception:
            return redirect("/login")
        if not uid:
            return redirect("/login")
        rest = get_restaurant(uid)
        if not rest:
            return redirect("/login")
        # Check code
        now = _dt3.datetime.now()
        try:
            expires = _dt3.datetime.strptime(rest.two_fa_expires, "%Y-%m-%d %H:%M:%S")
        except Exception:
            expires = now
        import secrets as _sec5
        csrf4 = _sec5.token_hex(16)
        try:
            _rest_v = get_restaurant(uid)
            _email_v = _rest_v.owner_email if _rest_v else ""
            masked = _email_v[:2] + "***@" + _email_v.split("@")[-1] if "@" in _email_v else "your registered email"
        except Exception as _e_v:
            print(f"[verify_2fa] error: {_e_v}")
            masked = "your registered email"
        if rest.two_fa_code != code_entered:
            resp_err = make_response(render_template('two_fa.html',
                masked_email=masked, error="Incorrect code. Try again.",
                pending_token=pending_token, next_url=next_url, csrf_token=csrf4))
            resp_err.set_cookie("csrf_token", csrf4, httponly=True, samesite="Lax")
            return resp_err
        if now > expires:
            resp_exp = make_response(render_template('two_fa.html',
                masked_email=masked, error="Code expired. Request a new one.",
                pending_token=pending_token, next_url=next_url, csrf_token=csrf4))
            resp_exp.set_cookie("csrf_token", csrf4, httponly=True, samesite="Lax")
            return resp_exp
        # Code correct — clear it and create session
        update_restaurant(uid, {"two_fa_code": "", "two_fa_expires": ""})
        _fl3.session.pop("pending_uid", None)
        _fl3.session.pop("pending_token", None)
        _ip_2fa = _get_client_ip()
        _ua_2fa = request.headers.get("User-Agent", "")
        # uid is restaurant_id — look up the actual user_id for the session
        _user_for_session = get_user_by_restaurant_id(uid)
        if not _user_for_session:
            return redirect("/login")
        token = create_session(_user_for_session["id"], ip_address=_ip_2fa, user_agent=_ua_2fa)
        # Login notification
        try:
            _rest_ln2 = get_restaurant(uid)
            if _rest_ln2 and getattr(_rest_ln2, "login_notify", 0) and _rest_ln2.owner_email:
                from emails import send_login_notification
                send_login_notification(_rest_ln2.owner_email, _rest_ln2.name or "", _ip_2fa, _ua_2fa)
        except Exception as _ln2_e:
            print(f"[LoginNotify2FA] {_ln2_e}")
        _on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
        resp_ok = make_response(redirect(next_url or "/"))
        resp_ok.set_cookie("session_token", token, max_age=30*24*3600,
                           httponly=True, secure=_on_railway, samesite="Lax")
        if remember == "1":
            import secrets as _sec6
            dev_tok = _sec6.token_hex(32)
            update_restaurant(uid, {"two_fa_device_token": dev_tok})
            resp_ok.set_cookie("device_token_"+str(uid), dev_tok,
                               max_age=30*24*3600, httponly=True,
                               secure=_on_railway, samesite="Lax")
        return resp_ok
    return redirect("/login")

@auth_bp.route("/resend-2fa", methods=["POST"])
def resend_2fa():
    import flask as _fl4, random, datetime as _dt4
    from models import get_restaurant, update_restaurant
    data_r = request.get_json() or {}
    pending_token_r = data_r.get("pending_token", "")
    try:
        import base64 as _b64_r
        decoded_r = _b64_r.urlsafe_b64decode(pending_token_r.encode()).decode()
        uid_r, _ = decoded_r.split(":", 1)
        uid = int(uid_r)
    except Exception:
        return jsonify(ok=False, error="Session expired — please log in again")
    if not uid:
        return jsonify(ok=False, error="Session expired")
    rest = get_restaurant(uid)
    if not rest:
        return jsonify(ok=False)
    code = str(random.randint(100000, 999999))
    expires = (_dt4.datetime.now() + _dt4.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    update_restaurant(uid, {"two_fa_code": code, "two_fa_expires": expires})
    try:
        email = rest.owner_email or ""
        if "@" in email:
            from emails import send_2fa_code
            send_2fa_code(email, rest.name or "your restaurant", code, rest.owner_name)
    except Exception:
        pass
    return jsonify(ok=True)

@auth_bp.route("/logout")
def logout():
    token = request.cookies.get("session_token")
    if token:
        delete_session(token)
    resp = make_response(redirect("/login"))
    resp.delete_cookie("session_token")
    return resp


@auth_bp.route("/api/send-2fa-test", methods=["POST"])
@login_required
def send_2fa_test(current_user):
    """Send a test 2FA code to verify email before enabling."""
    import random, datetime as _dt5
    from models import get_restaurant, update_restaurant
    rest = get_restaurant(current_user["restaurant_id"])
    if not rest:
        return jsonify(ok=False, error="Restaurant not found")
    email = rest.owner_email or ""
    if not email or "@" not in email:
        return jsonify(ok=False, error="No email address found. Contact will@cavnar.ai to update your account email.")
    code = str(random.randint(100000, 999999))
    expires = (_dt5.datetime.now() + _dt5.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    update_restaurant(current_user["restaurant_id"], {"two_fa_code": code, "two_fa_expires": expires})
    try:
        from emails import send_2fa_code
        send_2fa_code(email, rest.name or "your restaurant", code, rest.owner_name)
    except Exception as e:
        return jsonify(ok=False, error=f"Failed to send email: {str(e)[:60]}")
    masked = email[:2] + "***@" + email.split("@")[-1]
    return jsonify(ok=True, masked=masked)

@auth_bp.route("/api/verify-2fa-setup", methods=["POST"])
@login_required
def verify_2fa_setup(current_user):
    """Verify the test code and enable 2FA."""
    import datetime as _dt6
    from models import get_restaurant, update_restaurant
    data = request.get_json() or {}
    code = data.get("code", "").strip()
    rest = get_restaurant(current_user["restaurant_id"])
    if not rest:
        return jsonify(ok=False, error="Not found")
    if rest.two_fa_code != code:
        return jsonify(ok=False, error="Incorrect code. Try again.")
    try:
        exp_str = (rest.two_fa_expires or "").strip()
        expired = True
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"]:
            try:
                expires = _dt6.datetime.strptime(exp_str, fmt)
                expired = _dt6.datetime.now() > expires
                break
            except Exception:
                continue
        if expired:
            return jsonify(ok=False, error="Code expired. Click resend.")
    except Exception:
        return jsonify(ok=False, error="Code expired. Click resend.")
    update_restaurant(current_user["restaurant_id"], {
        "two_fa_enabled": 1, "two_fa_code": "", "two_fa_expires": ""
    })
    return jsonify(ok=True)

@auth_bp.route("/api/toggle-2fa", methods=["POST"])
@login_required
def toggle_2fa(current_user):
    from models import update_restaurant
    data = request.get_json() or {}
    enabled = 1 if data.get("enabled") else 0
    update_restaurant(current_user["restaurant_id"], {"two_fa_enabled": enabled})
    return jsonify(ok=True)


@auth_bp.route("/api/change-password", methods=["POST"])
@login_required
def change_password(current_user):
    data = request.get_json()
    user = verify_password(current_user["username"], data.get("current",""))
    if not user:
        return jsonify(ok=False, error="Current password is incorrect")
    new_pw = data.get("new_password","")
    if len(new_pw) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters")
    update_password(current_user["id"], new_pw)
    return jsonify(ok=True)

@auth_bp.route("/api/update-email", methods=["POST"])
@login_required
def update_email_route(current_user):
    data = request.get_json()
    new_email = (data.get("new_email") or "").strip().lower()
    current_pw = data.get("current_password", "")
    if not new_email:
        return jsonify(ok=False, error="Email address is required")
    import re as _re_email
    if not _re_email.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", new_email):
        return jsonify(ok=False, error="Enter a valid email address")
    # Verify current password
    user = verify_password(current_user["username"], current_pw)
    if not user:
        return jsonify(ok=False, error="Current password is incorrect")
    # Check email not already taken by another user
    conn = get_conn()
    existing = conn.execute("SELECT id FROM users WHERE email=? AND id!=?", (new_email, current_user["id"])).fetchone()
    if existing:
        conn.close()
        return jsonify(ok=False, error="That email is already in use")
    # Update users.email
    conn.execute("UPDATE users SET email=? WHERE id=?", (new_email, current_user["id"]))
    # Update restaurant.owner_email so notifications/digest still work
    conn.execute("UPDATE restaurants SET owner_email=? WHERE id=?", (new_email, current_user["restaurant_id"]))
    conn.commit()
    conn.close()
    return jsonify(ok=True)

@auth_bp.route("/api/sessions", methods=["GET"])
@login_required
def list_sessions(current_user):
    token = request.cookies.get("session_token", "")
    sessions = get_sessions_for_user(current_user["id"], current_token=token)
    # Parse UA into a readable label
    def _parse_ua(ua):
        ua = ua or ""
        if "iPhone" in ua: return "iPhone"
        if "iPad" in ua: return "iPad"
        if "Android" in ua:
            import re as _re_ua
            m = _re_ua.search(r'Android[\s/]+([\d.]+)', ua)
            return "Android" + (" " + m.group(1) if m else "")
        if "Windows NT" in ua:
            import re as _re_ua2
            m = _re_ua2.search(r'Windows NT ([\d.]+)', ua)
            nt = {"10.0":"10","6.3":"8.1","6.2":"8","6.1":"7"}.get(m.group(1) if m else "", "")
            return "Windows" + (" " + nt if nt else "")
        if "Macintosh" in ua or "Mac OS X" in ua:
            import re as _re_ua3
            m = _re_ua3.search(r'Mac OS X ([\d_]+)', ua)
            ver = m.group(1).replace("_", ".") if m else ""
            return "Mac" + (" " + ver if ver else "")
        if "CrOS" in ua: return "Chromebook"
        if "Linux" in ua: return "Linux"
        return None  # unknown — will be handled below
    def _parse_browser(ua):
        ua = ua or ""
        import re as _re_b
        if "Edg/" in ua:
            m = _re_b.search(r'Edg/([\d.]+)', ua)
            return "Edge" + (" " + m.group(1).split(".")[0] if m else "")
        if "OPR/" in ua or "Opera/" in ua: return "Opera"
        if "Chrome/" in ua:
            m = _re_b.search(r'Chrome/([\d.]+)', ua)
            return "Chrome" + (" " + m.group(1).split(".")[0] if m else "")
        if "Firefox/" in ua:
            m = _re_b.search(r'Firefox/([\d.]+)', ua)
            return "Firefox" + (" " + m.group(1).split(".")[0] if m else "")
        if "Safari/" in ua:
            m = _re_b.search(r'Version/([\d.]+)', ua)
            return "Safari" + (" " + m.group(1).split(".")[0] if m else "")
        return None  # unknown
    def _fmt_ct(ts):
        """Convert UTC sqlite timestamp to CT M/D/YY h:MM AM/PM."""
        if not ts:
            return ""
        try:
            from datetime import datetime as _dt_s, timezone as _tz_s, timedelta as _td_s
            from zoneinfo import ZoneInfo as _ZI_s
            # SQLite stores as 'YYYY-MM-DD HH:MM:SS' UTC
            dt_utc = _dt_s.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz_s.utc)
            dt_ct = dt_utc.astimezone(_ZI_s("America/Chicago"))
            hour = dt_ct.hour % 12 or 12
            ampm = "AM" if dt_ct.hour < 12 else "PM"
            return "{}/{}/{} {}:{:02d} {} CT".format(
                dt_ct.month, dt_ct.day, str(dt_ct.year)[2:],
                hour, dt_ct.minute, ampm)
        except Exception:
            return ts[:16]
    live_ua = request.headers.get("User-Agent", "")
    live_ip = _get_client_ip()
    for s in sessions:
        ua = s.pop("user_agent", "") or ""
        # For the current session, always use the live request UA/IP
        # (handles sessions created before UA/IP tracking was added)
        if s.get("is_current"):
            ua = live_ua or ua
            if not s.get("ip_address") or s["ip_address"].lower() in ("", "unknown"):
                s["ip_address"] = live_ip
        device = _parse_ua(ua)
        browser = _parse_browser(ua)
        if device and browser:
            s["device"] = device
            s["browser"] = browser
        elif device:
            s["device"] = device
            s["browser"] = "Browser"
        elif browser:
            s["device"] = "Unknown device"
            s["browser"] = browser
        else:
            s["device"] = "Unknown device"
            s["browser"] = ""
        s["last_active"] = _fmt_ct(s.get("last_active", ""))
    return jsonify(sessions=sessions)


@auth_bp.route("/api/sessions/revoke-others", methods=["POST"])
@login_required
def revoke_other_sessions_route(current_user):
    token = request.cookies.get("session_token", "")
    revoke_other_sessions(current_user["id"], current_token=token)
    return jsonify(ok=True)


@auth_bp.route("/api/toggle-login-notify", methods=["POST"])
@login_required
def toggle_login_notify(current_user):
    from models import update_restaurant
    data = request.get_json()
    enabled = 1 if data.get("enabled") else 0
    update_restaurant(current_user["restaurant_id"], {"login_notify": enabled})
    return jsonify(ok=True)


# ── Admin routes ──────────────────────────────────────────────────────────────


@auth_bp.route("/auth/google/connect")
@login_required
def gmb_connect(current_user):
    """Start Google OAuth flow for the logged-in client."""
    from gmb import get_auth_url
    if not os.getenv("GOOGLE_CLIENT_ID"):
        return jsonify(ok=False, error="Google OAuth not configured"), 500
    url = get_auth_url(current_user["restaurant_id"])
    from flask import redirect as _redirect
    return _redirect(url)


@auth_bp.route("/auth/google/callback")
def gmb_callback():
    """Handle Google OAuth callback — exchange code, store tokens, discover location."""
    from gmb import exchange_code, get_gmb_account_id, get_gmb_location_id
    from models import update_restaurant, get_restaurant
    from datetime import datetime, timezone, timedelta

    code          = request.args.get("code")
    restaurant_id = request.args.get("state")
    error         = request.args.get("error")

    if error or not code or not restaurant_id:
        msg = error or "No code returned"
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'error',msg:'" + msg + "'},'*');"
            "window.close();"
            "</script><p>Connection failed. Close this window.</p></body></html>"
        )

    try:
        restaurant_id = int(restaurant_id)
        tokens        = exchange_code(code)
        access_token  = tokens["access_token"]
        refresh_token = tokens.get("refresh_token", "")
        expires_in    = tokens.get("expires_in", 3600)
        expires_at    = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

        account_id  = get_gmb_account_id(access_token)
        location_id = None
        if account_id:
            r = get_restaurant(restaurant_id)
            location_id = get_gmb_location_id(access_token, account_id, r.google_place_id or "")

        update_restaurant(restaurant_id, {
            "gmb_access_token":  access_token,
            "gmb_refresh_token": refresh_token,
            "gmb_token_expires": expires_at,
            "gmb_account_id":    account_id or "",
            "gmb_location_id":   location_id or "",
        })

        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'connected'},'*');"
            "window.close();"
            "</script><p>Google Business connected! Close this window.</p></body></html>"
        )

    except Exception as e:
        print(f"[GMB] OAuth callback error: {e}")
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'error',msg:'Connection error'},'*');"
            "window.close();"
            "</script><p>Connection error. Close this window.</p></body></html>"
        )


@auth_bp.route("/auth/google/disconnect", methods=["POST"])
@login_required
def gmb_disconnect(current_user):
    """Disconnect Google Business from this restaurant."""
    from models import update_restaurant
    update_restaurant(current_user["restaurant_id"], {
        "gmb_access_token":  "",
        "gmb_refresh_token": "",
        "gmb_account_id":    "",
        "gmb_location_id":   "",
        "gmb_token_expires": "",
    })
    return jsonify(ok=True)

