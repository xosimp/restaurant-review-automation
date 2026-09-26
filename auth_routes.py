"""
auth_routes.py — Login, logout, password reset, 2FA, Google auth, session management
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from csrf import csrf_required
from flask import Blueprint, request, jsonify, make_response, redirect, render_template
import os
import config
import time

from models import get_conn, get_restaurant, update_restaurant
from auth import verify_password, create_session, delete_session, get_sessions_for_user, revoke_other_sessions, update_password, login_required


from emails import html_document as _html_doc  # one definition; emails reads its env lazily

auth_bp = Blueprint('auth', __name__)
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(auth_bp)

# ── Login rate limiting ────────────────────────────────────────────────────────
# Tracks failed login attempts per IP: {ip: [timestamp, timestamp, ...]}
_login_attempts = {}
_MAX_ATTEMPTS   = 5      # max failures before lockout
_LOCKOUT_SECS   = 300    # 5 minute lockout

def _get_client_ip():
    """The client address the proxy vouches for: ProxyFix (one hop,
    auth.install_proxy_fix) has already set remote_addr from the entry
    Railway's edge appended. The FIRST X-Forwarded-For value is whatever the
    client sent, and keying every throttle on it let an attacker reset them
    by rotating the header (SEC-3)."""
    return request.remote_addr or "unknown"

def _is_rate_limited(ip, username=None):
    """Durable, keyed by IP and account (security.py). The in-memory dict
    this replaced reset on deploy and never saw an attacker rotate
    addresses; it was also the reason gunicorn could not run two workers."""
    try:
        import security
        blocked, _ = security.login_throttled(ip, username)
        return blocked
    except Exception:
        # A limiter that cannot read its table must not fail open.
        now = time.time()
        attempts = _login_attempts.get(ip, [])
        recent = [t for t in attempts if now - t < _LOCKOUT_SECS]
        _login_attempts[ip] = recent
        return len(recent) >= _MAX_ATTEMPTS

def _record_failed_attempt(ip, username=None):
    _login_attempts.setdefault(ip, []).append(time.time())
    try:
        import security
        security.record_login_failure(ip, username)
    except Exception:
        pass

def _clear_attempts(ip, username=None, clear_key=False):
    """After a success. Clears the ACCOUNT's failures; an address's budget
    only ages out, or a successful login on the attacker's own account wiped
    the budget they were spending guessing someone else's codes (SEC-3).
    `clear_key` clears a purpose-specific key such as "2fa:<ip>"."""
    try:
        import security
        security.clear_login_failures(ip=ip if clear_key else None, username=username)
        if clear_key:
            _login_attempts.pop(ip, None)
    except Exception:
        pass

# ── CSRF validation ───────────────────────────────────────────────────────────
# These auth forms set a csrf_token cookie and render it into a hidden field;
# this confirms the submitted value actually matches the cookie set for this
# browser, instead of just checking that *a* token was generated somewhere.

def safe_next_url(value, default="/"):
    """Where to send a browser after sign-in: a path on this site, or the
    default. ?next= and the 2FA form's next_url are attacker-writable, and an
    absolute (https://evil.example) or protocol-relative (//evil.example)
    value turned the real login page into a redirect to a look-alike
    (SEC-22). Backslashes count as slashes because browsers treat /\\host
    as //host."""
    v = (value or "").strip()
    if not v or not v.startswith("/"):
        return default
    if v.startswith("//") or v.startswith("/\\") or "\\" in v[:3]:
        return default
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        return default
    from urllib.parse import urlsplit
    parts = urlsplit(v.replace("\\", "/"))
    if parts.scheme or parts.netloc:
        return default
    return v


def _csrf_ok():
    import hmac as _hmac_csrf
    cookie_val = request.cookies.get("csrf_token", "")
    form_val   = request.form.get("csrf_token", "")
    if not cookie_val or not form_val:
        return False
    return _hmac_csrf.compare_digest(cookie_val, form_val)

@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        import secrets as _sec_fp
        sent = request.args.get("sent")
        csrf_fp = _sec_fp.token_hex(16)
        resp_fp = make_response(render_template('forgot_password.html', sent=sent, csrf_token=csrf_fp))
        resp_fp.set_cookie("csrf_token", csrf_fp, httponly=True, samesite="Lax")
        return resp_fp

    # POST — send reset email
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return render_template('rate_limited.html'), 429
    if not _csrf_ok():
        return redirect("/forgot-password")
    _record_failed_attempt(ip)  # Count each forgot-password POST

    email = request.form.get("email", "").strip().lower()
    if email:
        try:
            from models import create_reset_token
            import emails as _emails_fp
            token = create_reset_token(email)
            if token:
                reset_url = f"https://dashboard.cavnar.ai/reset-password/{token}"
                # Through emails.deliver — the flood guard and email_log a
                # direct SDK send skipped (MOD-EML-4). Password reset is
                # exempt from suppression there, as it should be.
                _emails_fp.deliver_or_raise(email_type="send_password_reset_email", payload={
                    "from": _emails_fp.sender("client"),
                    "to": [email],
                    "subject": "Reset your Cavnar AI password",
                    "html": _html_doc(f"""
                    <div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
                    <div style="font-family:'DM Sans',sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:white;border-radius:12px;box-sizing:border-box">
                      <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin-bottom:24px">
                      <h2 style="font-size:18px;font-weight:600;margin-bottom:12px;color:#0e0c0a">Reset your password</h2>
                      <p style="font-size:14px;color:#4a4540;line-height:1.6;margin-bottom:24px">
                        Click the button below to reset your password. This link expires in 1 hour.
                      </p>
                      <a href="{reset_url}" style="display:inline-block;background:#c84b2f;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600">Reset password →</a>
                      <p style="font-size:12px;color:#7a736a;margin-top:24px">If you didn't request this, ignore this email — your password won't change.</p>
                      <hr style="border:none;border-top:1px solid #e5e0db;margin:24px 0">
                      <p style="font-size:11px;color:#9ca3af">Cavnar AI · will@cavnar.ai · cavnar.ai</p>
                    </div>
                    </div>"""),
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
        import secrets as _sec_rp
        error = request.args.get("error")
        csrf_rp = _sec_rp.token_hex(16)
        resp_rp = make_response(render_template('reset_password.html', valid=valid, error=error, csrf_token=csrf_rp))
        resp_rp.set_cookie("csrf_token", csrf_rp, httponly=True, samesite="Lax")
        return resp_rp

    # POST — set new password
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        from flask import redirect as _redir
        return _redir("/forgot-password?sent=1"), 429

    if not _csrf_ok():
        from flask import redirect as _redir
        return _redir(f"/reset-password/{token}")

    if not valid:
        from flask import redirect as _redir
        return _redir("/forgot-password")

    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")

    if len(password) < 8 or password != confirm:
        from flask import redirect as _redir
        return _redir(f"/reset-password/{token}?error=mismatch")
    import security as _sec
    if _sec.password_pwned(password):
        from flask import redirect as _redir
        return _redir(f"/reset-password/{token}?error=breached")

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

        _google_sso_post = bool(os.getenv("GOOGLE_SSO_CLIENT_ID"))
        _csrf_cookie = request.cookies.get('csrf_token', '')
        if _is_rate_limited(ip, request.form.get("username", "").strip()):
            return render_template('login.html', google_sso_enabled=_google_sso_post, csrf_token=_csrf_cookie,
                error="Too many failed attempts. Please wait 5 minutes and try again.")
        if not _csrf_ok():
            return render_template('login.html', google_sso_enabled=_google_sso_post, csrf_token=_csrf_cookie,
                error="Your session expired — please try again.")
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        user = verify_password(username, password)
        if not user:
            _record_failed_attempt(ip, username)
            return render_template('login.html', error="Invalid username or password", google_sso_enabled=_google_sso_post, csrf_token=_csrf_cookie)
        _clear_attempts(ip, username)
        if user.get("must_reset_password"):
            return render_template('login.html', google_sso_enabled=_google_sso_post, csrf_token=_csrf_cookie,
                error="This account needs a password reset before signing in — use Forgot password below.")
        next_url = safe_next_url(request.args.get("next"), "/admin" if user["is_admin"] else "/")

        # Check if 2FA is enabled and device not remembered
        try:
            from models import get_restaurant
            _rid = user.get("restaurant_id")
            rest = get_restaurant(_rid) if _rid and not user.get("is_admin") else None
            _device_cookie = request.cookies.get("device_token_" + str(_rid), "")
            _2fa_on = rest and rest.two_fa_enabled and not user.get("is_admin")
            from auth import remembered_device_ok as _tdo
            _device_ok = bool(_device_cookie) and _tdo(user, _device_cookie)
        except Exception as _e_2fa:
            _2fa_on = False
            _device_ok = False

        if _2fa_on and not _device_ok:
            # Generate and send 2FA code
            from models import update_restaurant, get_restaurant
            # pending is a per-login-attempt secret bound into the token AND
            # stored (hashed) server-side with this attempt's own code, so
            # verify-2fa can confirm the submitted token was actually issued by
            # this login, and a second sign-in at the same restaurant gets its
            # own challenge instead of overwriting this one (SEC-20).
            from auth import issue_two_fa_challenge as _itfc
            # To this login's own email or phone, never the owner's (SEC-20).
            from auth import two_fa_destination as _tfd, send_two_fa_code as _stfc, NO_TWO_FA_DESTINATION as _no_dest
            rest2 = get_restaurant(_rid)
            _dest = _tfd(user, rest2)
            if not _dest:
                return render_template('login.html', error=_no_dest, google_sso_enabled=_google_sso_post,
                                       csrf_token=_csrf_cookie)
            pending, code = _itfc(_rid, user["id"], "login")
            try:
                _stfc(_dest, rest2, code)
            except Exception as _e_send:
                print(f"[2fa] login code send failed for user {user['id']}: {_e_send}")
            masked = _dest["masked"]
            # Encode restaurant_id AND the user_id that actually authenticated:
            # "rid:uid:secret". The user_id used to be absent, and verify-2fa
            # then resolved the session with get_user_by_restaurant_id(), an
            # unordered "LIMIT 1" over that restaurant's logins — so with more
            # than one login on a restaurant, whoever passed the challenge got
            # a session for whichever row SQLite returned first (typically the
            # primary/owner login). One login per restaurant hid it; employee
            # accounts make several logins the norm, so it is fixed here.
            from auth import make_pending_token as _mpt
            pending_encoded = _mpt(_rid, user["id"], pending)
            import secrets as _sec4
            csrf3 = _sec4.token_hex(16)
            resp3 = make_response(render_template('two_fa.html',
                masked_email=masked, channel=_dest["kind"], error=None,
                pending_token=pending_encoded, next_url=next_url, csrf_token=csrf3))
            resp3.set_cookie("csrf_token", csrf3, httponly=True, samesite="Lax")
            return resp3

        _ua = request.headers.get("User-Agent", "")
        token = create_session(user["id"], ip_address=ip, user_agent=_ua, restaurant_id=user["restaurant_id"])
        # Send login notification (email + push + bell) if enabled
        try:
            from models import get_restaurant as _gr_ln
            _rid_ln = user.get("restaurant_id")
            _rest_ln = _gr_ln(_rid_ln) if _rid_ln else None
            if _rest_ln and getattr(_rest_ln, "login_notify", 0) and _rest_ln.owner_email:
                from notify import send_login_alert
                from auth import create_login_report as _clr
                send_login_alert(_rid_ln, _rest_ln.name or "", _rest_ln.owner_email, ip, _ua,
                                 report_url=f"https://dashboard.cavnar.ai/auth/not-me/{_clr(user['id'], token)}")
        except Exception as _ln_e:
            print(f"[LoginNotify] {_ln_e}")
        resp = make_response(redirect(next_url))
        from auth import cookies_require_secure as _crs
        _on_railway = _crs()
        resp.set_cookie("session_token", token, max_age=30*24*3600,
                        httponly=True, secure=_on_railway, samesite="Lax")
        return resp
    import secrets as _sec2
    csrf2 = _sec2.token_hex(16)
    _google_sso = bool(os.getenv("GOOGLE_SSO_CLIENT_ID"))
    resp2 = make_response(render_template('login.html', error=None, google_sso_enabled=_google_sso, csrf_token=csrf2))
    resp2.set_cookie("csrf_token", csrf2, httponly=True, samesite="Lax")
    return resp2

@auth_bp.route("/verify-2fa", methods=["GET","POST"])
def verify_2fa():
    import flask as _fl3
    import hmac as _hmac_2fa
    from models import get_restaurant, update_restaurant
    if request.method == "POST":
        ip = _get_client_ip()
        pending_token = request.form.get("pending_token","")
        code_entered  = request.form.get("code","").strip()
        next_url      = safe_next_url(request.form.get("next_url"), "/")
        remember      = request.form.get("remember_device","")
        # Rate-limit code-guessing attempts the same way /login is throttled —
        # this is the actual brute-force defense, since a 6-digit code only has
        # ~1M possibilities and the pending_token alone used to provide none.
        if _is_rate_limited("2fa:" + ip):
            return render_template('two_fa.html',
                masked_email="", channel=None, error="Too many attempts. Please wait 5 minutes and try again.",
                pending_token=pending_token, next_url=next_url, csrf_token=request.cookies.get('csrf_token',''))
        if not _csrf_ok():
            return redirect("/login")
        # Decode uid + pending secret from token
        # Signed "rid:uid:secret:sig" (auth.make_pending_token). Anything
        # unsigned or edited — the user id swapped for the owner's — is
        # refused; the worst case is re-entering a password.
        from auth import read_pending_token as _rpt
        _parsed = _rpt(pending_token)
        if not _parsed:
            _record_failed_attempt("2fa:" + ip)
            return redirect("/login")
        uid, pending_user_id, pending_secret = _parsed
        if not uid or not pending_user_id:
            return redirect("/login")
        rest = get_restaurant(uid)
        if not rest:
            return redirect("/login")
        # Confirm this token was actually issued by OUR login flow for this
        # login at this restaurant — not just a base64 blob with a guessed
        # restaurant_id — and has not been used yet.
        from auth import two_fa_challenge_exists as _tfce, check_two_fa_code as _ctfc
        if not _tfce(uid, pending_user_id, pending_secret):
            _record_failed_attempt("2fa:" + ip)
            return redirect("/login")
        import secrets as _sec5
        csrf4 = _sec5.token_hex(16)
        try:
            from auth import two_fa_destination as _tfd_v, get_user_by_id as _gubi_v
            _dest_v = _tfd_v(_gubi_v(pending_user_id), rest)
            masked = _dest_v["masked"] if _dest_v else "your registered email"
            channel_v = _dest_v["kind"] if _dest_v else None
        except Exception as _e_v:
            print(f"[verify_2fa] error: {_e_v}")
            masked = "your registered email"
            channel_v = None
        # A backup code typed in ("7f3a 92c1", any case, dash or not) keeps
        # the page in backup-code mode if it comes back with an error.
        from models import normalize_backup_code as _nbc
        _backup_mode = bool(_nbc(code_entered))
        _otp_result = _ctfc(uid, pending_user_id, code_entered, pending=pending_secret, consume=False)
        if _otp_result == "wrong":
            from models import verify_and_consume_backup_code as _vcbc
            if not _vcbc(uid, code_entered):
                _record_failed_attempt("2fa:" + ip)
                resp_err = make_response(render_template('two_fa.html',
                    masked_email=masked, channel=channel_v, backup_mode=_backup_mode, error="Incorrect code. Try again.",
                    pending_token=pending_token, next_url=next_url, csrf_token=csrf4))
                resp_err.set_cookie("csrf_token", csrf4, httponly=True, samesite="Lax")
                return resp_err
        elif _otp_result != "ok":
            resp_exp = make_response(render_template('two_fa.html',
                masked_email=masked, channel=channel_v, backup_mode=_backup_mode, error="Code expired. Request a new one.",
                pending_token=pending_token, next_url=next_url, csrf_token=csrf4))
            resp_exp.set_cookie("csrf_token", csrf4, httponly=True, samesite="Lax")
            return resp_exp
        # Code correct — clear it (and the pending secret, single-use) and create session
        _clear_attempts("2fa:" + ip, clear_key=True)
        # Single use: this sign-in's challenge ends here (and only this one).
        from auth import end_two_fa_challenge as _etfc
        _etfc(uid, pending_user_id, pending_secret)
        _fl3.session.pop("pending_uid", None)
        _fl3.session.pop("pending_token", None)
        _ip_2fa = _get_client_ip()
        _ua_2fa = request.headers.get("User-Agent", "")
        # The session belongs to the login that actually passed the password
        # step, carried through the pending token — never "some active user of
        # this restaurant". Re-checked against uid so a tampered token can't
        # name a user from another restaurant.
        from auth import get_user_by_id as _gubi_2fa
        _user_for_session = _gubi_2fa(pending_user_id)
        if (not _user_for_session or not _user_for_session.get("is_active")
                or _user_for_session.get("restaurant_id") != uid
                or _user_for_session.get("must_reset_password")):
            return redirect("/login")
        token = create_session(_user_for_session["id"], ip_address=_ip_2fa, user_agent=_ua_2fa, restaurant_id=_user_for_session["restaurant_id"])
        # Login notification (email + push + bell)
        try:
            _rest_ln2 = get_restaurant(uid)
            if _rest_ln2 and getattr(_rest_ln2, "login_notify", 0) and _rest_ln2.owner_email:
                from notify import send_login_alert
                from auth import create_login_report as _clr2
                send_login_alert(uid, _rest_ln2.name or "", _rest_ln2.owner_email, _ip_2fa, _ua_2fa,
                                 report_url=f"https://dashboard.cavnar.ai/auth/not-me/{_clr2(_user_for_session['id'], token)}")
        except Exception as _ln2_e:
            print(f"[LoginNotify2FA] {_ln2_e}")
        from auth import cookies_require_secure as _crs
        _on_railway = _crs()
        resp_ok = make_response(redirect(next_url or "/"))
        resp_ok.set_cookie("session_token", token, max_age=30*24*3600,
                           httponly=True, secure=_on_railway, samesite="Lax")
        if remember == "1":
            from auth import create_trusted_device as _ctd, describe_user_agent as _dua
            dev_tok = _ctd(uid, _user_for_session["id"], _dua(request.headers.get("User-Agent", "")) + " · web")
            resp_ok.set_cookie("device_token_"+str(uid), dev_tok,
                               max_age=30*24*3600, httponly=True,
                               secure=_on_railway, samesite="Lax")
        return resp_ok
    return redirect("/login")

def resend_two_fa(pending_token):
    """Send this sign-in a fresh code — the one body behind the web's
    /resend-2fa and the phone's /mobile/api/resend-2fa. Returns
    (payload, status).

    Throttled per address on its own key ("2fa-resend:<ip>"), every request
    counted, so it cannot be used to spray codes. Only the holder of a
    pending token that login() actually issued can trigger it, and the new
    code replaces this sign-in's code and nobody else's; it goes to the same
    login's own email or phone the first one did (SEC-20)."""
    ip = _get_client_ip()
    if _is_rate_limited("2fa-resend:" + ip):
        return {"ok": False, "error": "Too many requests. Wait a few minutes and try again."}, 429
    _record_failed_attempt("2fa-resend:" + ip)
    # The same signed token the login page issued. This used to split it in
    # two ("rid:secret"), so the secret carried the user id and never
    # matched: Resend code never worked.
    from auth import read_pending_token as _rpt_r
    _parsed_r = _rpt_r(pending_token or "")
    expired = ({"ok": False, "error": "Your sign-in timed out. Log in again."}, 401)
    if not _parsed_r:
        return expired
    uid, _pending_uid_r, pending_secret_r = _parsed_r
    if not uid:
        return expired
    from models import get_restaurant
    rest = get_restaurant(uid)
    if not rest:
        return expired
    from auth import reissue_two_fa_code as _rtfc
    code = _rtfc(uid, _pending_uid_r, pending_secret_r)
    if not code:
        return expired
    from auth import two_fa_destination as _tfd_r, send_two_fa_code as _stfc_r, get_user_by_id as _gubi_r
    _dest_r = _tfd_r(_gubi_r(_pending_uid_r), rest)
    if not _dest_r:
        return expired
    # "Sent" only when it went: this answered ok when the send raised or the
    # provider refused, so the person waited for a code that never came.
    try:
        _sent_r = bool(_stfc_r(_dest_r, rest, code))
    except Exception as _e_r:
        print(f"[2fa] resend failed for user {_pending_uid_r}: {_e_r}")
        _sent_r = False
    if not _sent_r:
        where = "text" if _dest_r["kind"] == "sms" else "email"
        return {"ok": False, "channel": _dest_r["kind"],
                "error": f"We couldn't {where} a new code just now. Wait a minute and try again."}, 502
    return {"ok": True, "channel": _dest_r["kind"], "masked": _dest_r["masked"]}, 200


@auth_bp.route("/resend-2fa", methods=["POST"])
def resend_2fa():
    data_r = request.get_json(silent=True) or {}
    payload, status = resend_two_fa(data_r.get("pending_token", ""))
    return jsonify(**payload), status

@auth_bp.route("/logout", methods=["GET", "POST"])
def logout():
    """Sign out. A GET only asks: it used to delete the session, so any page
    anywhere could sign a user out with an <img src=/logout> (SEC-34). The
    session cookie is SameSite=Lax, so the POST below is only ever
    authenticated when it comes from this site's own form."""
    if request.method != "POST":
        if not request.cookies.get("session_token"):
            return redirect("/login")
        return _SIMPLE_PAGE % ("<h1>Sign out?</h1><p>You'll need your username and password (and your "
                               "two-factor code, if it's on) to sign back in.</p><form method='post' "
                               "action='/logout'><button type='submit' class='cbtn cbtn-primary'>Sign out"
                               "</button></form><p style='margin-top:16px'><a href='/'>Back to the dashboard</a></p>")
    token = request.cookies.get("session_token")
    if token:
        delete_session(token)
    resp = make_response(redirect("/login"))
    resp.delete_cookie("session_token")
    return resp


@auth_bp.route("/api/send-2fa-test", methods=["POST"])
@login_required
def send_2fa_test(current_user):
    """Send a test 2FA code to verify email or phone before enabling."""
    from models import get_restaurant, update_restaurant
    rest = get_restaurant(current_user["restaurant_id"])
    if not rest:
        return jsonify(ok=False, error="Restaurant not found")
    data = request.get_json(silent=True) or {}
    method = "sms" if data.get("method") == "sms" else "email"
    # The code for turning 2FA on goes to whoever is turning it on, on the
    # channel they picked (SEC-20).
    from auth import two_fa_destination as _tfd_t, send_two_fa_code as _stfc_t, get_user_by_id as _gubi_t
    _me = dict(_gubi_t(current_user["id"]) or {})
    _me.update({k: current_user.get(k) for k in ("role", "is_admin", "restaurant_id") if k in current_user})
    dest = _tfd_t(_me, rest, method=method, strict=True)
    if not dest:
        if method == "sms":
            return jsonify(ok=False, error="No phone number found for your login. Add one in Profile & Details, or send by email instead.")
        return jsonify(ok=False, error="No email address found for your login. Contact will@cavnar.ai to update your account email.")
    # This login's own setup challenge: it never touches a sign-in someone
    # else has in progress at this restaurant (SEC-20).
    from auth import issue_two_fa_challenge as _itfc_t
    _pending_t, code = _itfc_t(current_user["restaurant_id"], current_user["id"], "setup")
    try:
        _stfc_t(dest, rest, code)
    except Exception as e:
        print(f"[2fa] setup code send failed for user {current_user['id']}: {e}")
        return jsonify(ok=False, error="Couldn't send the code. Try again in a moment.")
    masked = dest["masked"]
    return jsonify(ok=True, masked=masked, method=method)

@auth_bp.route("/api/verify-2fa-setup", methods=["POST"])
@login_required
def verify_2fa_setup(current_user):
    """Verify the test code and enable 2FA."""
    from models import get_restaurant, update_restaurant
    data = request.get_json() or {}
    code = data.get("code", "").strip()
    rest = get_restaurant(current_user["restaurant_id"])
    if not rest:
        return jsonify(ok=False, error="Not found")
    from auth import check_two_fa_code as _ctfc_s
    result = _ctfc_s(current_user["restaurant_id"], current_user["id"], code, purpose="setup")
    if result in ("wrong", "missing"):
        return jsonify(ok=False, error="Incorrect code. Try again.")
    if result == "expired":
        return jsonify(ok=False, error="Code expired. Click resend.")
    method = data.get("method") if data.get("method") in ("email", "sms") else "email"
    update_restaurant(current_user["restaurant_id"], {"two_fa_enabled": 1, "two_fa_method": method})
    return jsonify(ok=True)

@auth_bp.route("/api/toggle-2fa", methods=["POST"])
@csrf_required
@login_required
def toggle_2fa(current_user):
    from models import update_restaurant
    from permissions import is_principal
    if not is_principal(current_user):
        return jsonify(ok=False, error="Only the account owner can change sign-in security."), 403
    data = request.get_json() or {}
    enabled = 1 if data.get("enabled") else 0
    update_restaurant(current_user["restaurant_id"], {"two_fa_enabled": enabled})
    try:
        from client_api import log_account_event
        log_account_event(current_user["restaurant_id"], "two_fa_enabled" if enabled else "two_fa_disabled", current_user)
    except Exception:
        pass
    return jsonify(ok=True)


@auth_bp.route("/api/change-password", methods=["POST"])
@csrf_required
@login_required
def change_password(current_user):
    data = request.get_json()
    user = verify_password(current_user["username"], data.get("current",""))
    if not user:
        return jsonify(ok=False, error="Current password is incorrect")
    new_pw = data.get("new_password","")
    if len(new_pw) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters")
    import security as _sec
    if _sec.password_pwned(new_pw):
        return jsonify(ok=False, error=_sec.PWNED_MESSAGE)
    from auth import current_session_token as _cst
    update_password(current_user["id"], new_pw, keep_token=_cst())
    try:
        restaurant = get_restaurant(current_user["restaurant_id"])
        if restaurant and restaurant.owner_email:
            from emails import send_password_changed_email
            send_password_changed_email(restaurant.owner_email, restaurant.name or "your restaurant", restaurant.owner_name, tz=restaurant.timezone)
    except Exception:
        pass  # the password change itself already succeeded
    return jsonify(ok=True)

@auth_bp.route("/api/update-email", methods=["POST"])
@csrf_required
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
    old_email_row = conn.execute("SELECT email FROM users WHERE id=?", (current_user["id"],)).fetchone()
    old_email = old_email_row["email"] if old_email_row else None
    # Update users.email
    conn.execute("UPDATE users SET email=? WHERE id=?", (new_email, current_user["id"]))
    # The restaurant's contact address (2FA codes, alerts, the owner
    # match that keeps a location in its group) moves only when the account
    # holder whose address it is changes theirs. Any login's change used to
    # rewrite it (SEC-13).
    from permissions import is_principal as _is_principal_em
    _rest_em = conn.execute("SELECT owner_email FROM restaurants WHERE id=?", (current_user["restaurant_id"],)).fetchone()
    _moves_contact = (_is_principal_em(current_user) and _rest_em is not None and old_email
                      and (_rest_em["owner_email"] or "").strip().lower() == (old_email or "").strip().lower())
    # Update restaurant.owner_email so notifications/digest still work
    if _moves_contact:
        # Bumps row_version like update_restaurant does (DATA-28).
        conn.execute("UPDATE restaurants SET owner_email=?, row_version=COALESCE(row_version,0)+1 WHERE id=?",
                     (new_email, current_user["restaurant_id"]))
    conn.commit()
    conn.close()
    import models as _models_inv
    _models_inv._invalidate_request_cache(current_user["restaurant_id"])
    if old_email and old_email != new_email:
        try:
            restaurant = get_restaurant(current_user["restaurant_id"])
            from emails import send_email_changed_email
            send_email_changed_email(old_email, restaurant.name if restaurant else "your restaurant", new_email, restaurant.owner_name if restaurant else None, tz=restaurant.timezone if restaurant else None)
        except Exception:
            pass  # the email change itself already succeeded
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
            from datetime import datetime as _dt_s, timezone as _tz_s
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
@csrf_required
@login_required
def revoke_other_sessions_route(current_user):
    token = request.cookies.get("session_token", "")
    revoke_other_sessions(current_user["id"], current_token=token)
    return jsonify(ok=True)


@auth_bp.route("/api/toggle-login-notify", methods=["POST"])
@csrf_required
@login_required
def toggle_login_notify(current_user):
    from models import update_restaurant
    from permissions import is_principal
    if not is_principal(current_user):
        return jsonify(ok=False, error="Only the account owner can change sign-in alerts."), 403
    data = request.get_json()
    enabled = 1 if data.get("enabled") else 0
    update_restaurant(current_user["restaurant_id"], {"login_notify": enabled})
    return jsonify(ok=True)


@auth_bp.route("/api/toggle-staff-signin-notify", methods=["POST"])
@csrf_required
@login_required
def toggle_staff_signin_notify(current_user):
    """Alert the owner when an employee opens the staff portal."""
    from models import update_restaurant
    from permissions import is_principal
    if not is_principal(current_user):
        return jsonify(ok=False, error="Only the account owner can change sign-in alerts."), 403
    data = request.get_json() or {}
    enabled = 1 if data.get("enabled") else 0
    update_restaurant(current_user["restaurant_id"], {"staff_signin_notify": enabled})
    return jsonify(ok=True)


# ── Admin routes ──────────────────────────────────────────────────────────────


@auth_bp.route("/auth/google/connect")
@login_required
def gmb_connect(current_user):
    """Start Google OAuth flow for the logged-in client. Owner-only, like
    every other connection (Toast, Square, Clover, RPower, webhooks): the
    token it stores publishes replies under the restaurant's name."""
    from permissions import principal_only
    denied = principal_only(current_user, "the Google Business connection")
    if denied:
        return denied
    from gmb import get_auth_url
    if not os.getenv("GOOGLE_CLIENT_ID"):
        return jsonify(ok=False, error="Google OAuth not configured"), 500
    import secrets as _sec_gmb
    nonce = _sec_gmb.token_hex(16)
    url = get_auth_url(current_user["restaurant_id"], nonce)
    from flask import redirect as _redirect
    resp = make_response(_redirect(url))
    resp.set_cookie("gmb_oauth_state", nonce, httponly=True, samesite="Lax", max_age=600)
    return resp


@auth_bp.route("/auth/google/callback")
@login_required
def gmb_callback(current_user):
    """Handle Google OAuth callback — exchange code, store tokens, discover location.

    state is "<nonce>:<restaurant_id>" from gmb_connect(). The nonce must match
    the gmb_oauth_state cookie set when THIS user started the flow, and the
    tokens are always stored against the logged-in user's own restaurant_id —
    never a restaurant_id taken from the query string — so a forged state value
    can't attach an attacker's Google tokens to a victim's restaurant."""
    from gmb import exchange_code, find_gmb_location
    from html import escape as _html_escape
    from models import update_restaurant, get_restaurant
    from datetime import datetime, timezone, timedelta

    code  = request.args.get("code")
    state = request.args.get("state", "")
    error = request.args.get("error")

    if error or not code or not state:
        # ?error= is attacker-controlled and used to land inside a JS string
        # literal (SEC-9). It is never reflected now: a known Google code
        # maps to our own sentence, anything else to a generic one.
        import json as _json_cb
        _known = {"access_denied": "You declined access to Google.",
                  "invalid_request": "Google rejected the request.",
                  "server_error": "Google had a problem — try again.",
                  "temporarily_unavailable": "Google is busy — try again shortly."}
        msg = _json_cb.dumps(_known.get(str(error or ""), "No code returned" if not error else "Google sign-in failed."))
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'error',msg:" + msg + "},'*');"
            "window.close();"
            "</script><p>Connection failed. Close this window.</p></body></html>"
        )

    from permissions import is_principal as _is_principal_gmb
    if not _is_principal_gmb(current_user):
        # Owner-only at the start (gmb_connect) and here, so a flow begun by
        # an owner cannot be finished into a manager's session either.
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'error',msg:'Only the account owner can connect Google Business.'},'*');"
            "window.close();"
            "</script><p>Only the account owner can connect Google Business. Close this window.</p></body></html>"
        ), 403

    state_nonce, _, state_rid = state.partition(":")
    cookie_nonce = request.cookies.get("gmb_oauth_state", "")
    import hmac as _hmac_gmb
    if not cookie_nonce or not _hmac_gmb.compare_digest(cookie_nonce, state_nonce):
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'error',msg:'Connection expired — please try again'},'*');"
            "window.close();"
            "</script><p>Connection expired. Close this window and try again.</p></body></html>"
        )

    try:
        # Always use the logged-in user's own restaurant — state_rid is not trusted.
        restaurant_id = current_user["restaurant_id"]
        tokens        = exchange_code(code)
        access_token  = tokens["access_token"]
        refresh_token = tokens.get("refresh_token", "")
        expires_in    = tokens.get("expires_in", 3600)
        expires_at    = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

        # Resolve the ONE location whose Place ID matches this restaurant.
        # This used to take accounts[0] and locations[0], so an owner with
        # several restaurants under one Business Profile connected every one
        # of them to the same listing — same reviews, and replies published
        # under the wrong restaurant's name.
        r = get_restaurant(restaurant_id)
        match = find_gmb_location(access_token, (r.google_place_id or "") if r else "")
        if not match.get("ok"):
            _msg = _html_escape(match.get("error") or "Could not match this Google account to this restaurant.")
            return (
                "<html><body><script>"
                "window.opener&&window.opener.postMessage({gmb:'error'},'*');"
                "</script><p>Google Business not connected.</p>"
                f"<p>{_msg}</p></body></html>"
            )

        update_restaurant(restaurant_id, {
            "gmb_access_token":  access_token,
            "gmb_refresh_token": refresh_token,
            "gmb_token_expires": expires_at,
            "gmb_account_id":    match["account"],
            "gmb_location_id":   match["location"],
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


@auth_bp.route("/auth/google/mobile-callback")
def gmb_mobile_callback():
    """Mobile equivalent of gmb_callback above. No @login_required — the
    app has a bearer token, not a browser session cookie, so there's
    nothing for a decorator to check on this redirect; state itself
    (gmb.verify_mobile_state) is what proves which restaurant this
    belongs to. Finishes via the same cavnarai:// deep link
    google_sso_callback already uses for mobile login, so no new URL
    scheme registration is needed — GMBConnectCoordinator.swift just
    listens for a different path on it."""
    from gmb import exchange_code, find_gmb_location, verify_mobile_state, MOBILE_REDIRECT_URI
    from models import update_restaurant, get_restaurant
    from datetime import datetime, timezone, timedelta
    import urllib.parse

    def _finish(status, msg=None):
        query = {"status": status}
        if msg:
            query["msg"] = msg
        # quote_via=quote, not urlencode's default quote_plus — Swift's
        # URLComponents.queryItems percent-decodes %XX back to real
        # characters but never turns a literal "+" back into a space (that
        # convention is HTML form-encoding, not URI encoding), so a
        # quote_plus-encoded space survived the round trip as a literal
        # "+" and showed up verbatim in the app's error text.
        return redirect("cavnarai://gmb-callback?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote))

    code  = request.args.get("code")
    state = request.args.get("state", "")
    error = request.args.get("error")

    if error or not code or not state:
        return _finish("error", error or "No code returned")

    restaurant_id = verify_mobile_state(state)
    if restaurant_id is None:
        return _finish("error", "Connection expired — please try again")

    try:
        tokens        = exchange_code(code, redirect_uri=MOBILE_REDIRECT_URI)
        access_token  = tokens["access_token"]
        refresh_token = tokens.get("refresh_token", "")
        expires_in    = tokens.get("expires_in", 3600)
        expires_at    = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

        r = get_restaurant(restaurant_id)
        match = find_gmb_location(access_token, (r.google_place_id or "") if r else "")
        if not match.get("ok"):
            print(f"[GMB] mobile connect refused for rid={restaurant_id}: {match.get('error')}")
            return _finish("nomatch")

        update_restaurant(restaurant_id, {
            "gmb_access_token":  access_token,
            "gmb_refresh_token": refresh_token,
            "gmb_token_expires": expires_at,
            "gmb_account_id":    match["account"],
            "gmb_location_id":   match["location"],
        })
        return _finish("connected")
    except Exception as e:
        print(f"[GMB] Mobile OAuth callback error: {e}")
        return _finish("error", "Connection error")


@auth_bp.route("/auth/google-sso")
def google_sso_start():
    """Kick off Google Sign-In OAuth flow for dashboard login. ?mobile=1
    (the iOS app, via ASWebAuthenticationSession) is remembered in a
    short-lived cookie so the callback knows to hand back a bearer token
    over a custom URL scheme instead of setting a web session cookie."""
    import secrets, urllib.parse
    state = secrets.token_hex(16)
    base_url = config.base_url()
    params = urllib.parse.urlencode({
        "client_id":     os.getenv("GOOGLE_SSO_CLIENT_ID", ""),
        "redirect_uri":  base_url + "/auth/google-sso/callback",
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
        "access_type":   "online",
        "prompt":        "select_account",
    })
    resp = make_response(redirect("https://accounts.google.com/o/oauth2/v2/auth?" + params))
    resp.set_cookie("g_sso_state", state, httponly=True, samesite="Lax", max_age=300)
    # Where the sign-in was headed — a keyed link from an email (?rec=,
    # ?src=, ?ask=) — carried across Google's round trip the way the
    # password form carries ?next=. It was dropped, so a link opened through
    # Google landed on the bare dashboard and its open was never recorded
    # (re-audit C10). Only a path on this site (safe_next_url).
    nxt = safe_next_url(request.args.get("next"), "")
    if nxt:
        resp.set_cookie("g_sso_next", nxt, httponly=True, samesite="Lax", max_age=300)
    if request.args.get("mobile") == "1":
        resp.set_cookie("g_sso_mobile", "1", httponly=True, samesite="Lax", max_age=300)
        device_id = (request.args.get("device_id") or "").strip()
        if device_id:
            resp.set_cookie("g_sso_device_id", device_id, httponly=True, samesite="Lax", max_age=300)
    return resp


def _sso_web_finish(token=None, error=None):
    """The web half of Google sign-in's finish: the session cookie and a
    redirect to where the sign-in was headed (g_sso_next, set by
    google_sso_start — re-audit C10), or back to /login with the error and
    that same destination."""
    from urllib.parse import quote
    nxt = safe_next_url(request.cookies.get("g_sso_next"), "")
    if error:
        resp = make_response(redirect(f"/login?error={error}" + (f"&next={quote(nxt, safe='')}" if nxt else "")))
    else:
        resp = make_response(redirect(nxt or "/"))
        resp.set_cookie("session_token", token, httponly=True, samesite="Lax",
                        secure=True, max_age=30*24*3600)
        resp.delete_cookie("g_sso_state")
    resp.delete_cookie("g_sso_next")
    return resp


@auth_bp.route("/auth/google-sso/callback")
def google_sso_callback():
    """Handle Google's redirect after sign-in."""
    import requests as _req, urllib.parse
    from auth import create_session, get_conn as _gc

    is_mobile = request.cookies.get("g_sso_mobile") == "1"

    def _finish(token=None, error=None):
        """Web gets its existing cookie-session redirect; the iOS app (which
        has no cookie jar and speaks bearer tokens) gets the same outcome
        via a cavnarai:// deep link instead."""
        if is_mobile:
            query = {}
            if token:
                query["token"] = token
            if error:
                query["error"] = error
            # See gmb_mobile_callback's _finish for why quote_via=quote,
            # not urlencode's default quote_plus, is required here too.
            resp = make_response(redirect("cavnarai://auth-callback?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)))
            resp.delete_cookie("g_sso_state")
            resp.delete_cookie("g_sso_mobile")
            resp.delete_cookie("g_sso_device_id")
            return resp
        return _sso_web_finish(token, error)

    error = request.args.get("error")
    if error:
        return _finish(error="google_denied")

    # CSRF check
    state_param  = request.args.get("state", "")
    state_cookie = request.cookies.get("g_sso_state", "")
    if not state_param or state_param != state_cookie:
        return _finish(error="state_mismatch")

    code     = request.args.get("code", "")
    base_url = config.base_url()

    # Exchange code for tokens
    token_resp = _req.post("https://oauth2.googleapis.com/token", data={
        "code":          code,
        "client_id":     os.getenv("GOOGLE_SSO_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_SSO_CLIENT_SECRET", ""),
        "redirect_uri":  base_url + "/auth/google-sso/callback",
        "grant_type":    "authorization_code",
    }, timeout=10)
    if not token_resp.ok:
        return _finish(error="google_token_failed")

    access_token = token_resp.json().get("access_token")
    if not access_token:
        return _finish(error="no_access_token")

    # Get user info
    info_resp = _req.get("https://www.googleapis.com/oauth2/v2/userinfo",
                          headers={"Authorization": "Bearer " + access_token}, timeout=10)
    if not info_resp.ok:
        return _finish(error="userinfo_failed")

    info     = info_resp.json()
    email    = (info.get("email") or "").lower().strip()
    google_id = info.get("id", "")

    if not email:
        return _finish(error="no_email")

    # Match a login already linked to this Google account first. An email
    # match links the Google account to that login for good, so it only
    # counts when Google says it verified the address (SEC-38): an unverified
    # Google account carrying the owner's email used to be linked to, and
    # signed in as, the owner.
    email_verified = info.get("verified_email") is True or str(info.get("verified_email", "")).lower() == "true"
    conn = _gc()
    row = None
    if google_id:
        row = conn.execute(
            "SELECT * FROM users WHERE google_id=? AND is_active=1 LIMIT 1", (google_id,)
        ).fetchone()
    if not row and email_verified:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(email)=? AND is_active=1 LIMIT 1",
            (email,)
        ).fetchone()
    if not row and not email_verified:
        conn.close()
        return _finish(error="email_unverified")
    if row:
        try:
            if not row["google_id"]:
                conn.execute("UPDATE users SET google_id=? WHERE id=?", (google_id, row["id"]))
                conn.commit()
        except Exception as e:
            # Sign-in still succeeds without the link, but losing this write
            # silently means the account never gets linked and the user is
            # asked to link again on every single Google sign-in.
            import ops
            ops.capture(e, job="google_sso_link", context=f"user_id={row['id']}")
    conn.close()

    if not row:
        return _finish(error="no_account")

    user = dict(row)
    from auth import create_session, update_last_login

    # Password sign-in enforces both of these (see the /login handler). SSO
    # used to skip straight from an email match to a session, which made it a
    # way around both:
    #
    #   - must_reset_password is set by the "This wasn't me" link in a login
    #     alert, which also kills every session. An attacker whose access was
    #     revoked that way could simply come back through Google, because the
    #     lock was only ever checked on the password path.
    #   - 2FA likewise only gated the password path, so enabling it did not
    #     actually require a second factor for any account whose email matched
    #     a Google identity.
    if user.get("must_reset_password"):
        return _finish(error="password_reset_required")

    try:
        from models import get_restaurant as _gr_sso
        rest_sso = _gr_sso(user.get("restaurant_id")) if not user.get("is_admin") else None
        device_ok = False
        if rest_sso and rest_sso.two_fa_enabled:
            from auth import remembered_device_ok as _tdo_sso
            cookie = request.cookies.get("device_token_" + str(user.get("restaurant_id")), "")
            device_ok = bool(cookie) and _tdo_sso(user, cookie)
        sso_needs_2fa = bool(rest_sso and rest_sso.two_fa_enabled and not device_ok)
    except Exception:
        # Fail CLOSED: if we cannot determine whether 2FA applies, do not
        # hand out a session on a path that bypasses it.
        sso_needs_2fa = True

    if sso_needs_2fa:
        # No silent second factor over a redirect callback — send them through
        # the password form, which already implements the full challenge.
        return _finish(error="use_password_for_2fa")

    ip = request.remote_addr or ""
    ua = request.headers.get("User-Agent", "")
    device_id = request.cookies.get("g_sso_device_id") or None
    token = create_session(user["id"], ip_address=ip, user_agent=ua, device_type="ios" if is_mobile else "web", device_id=device_id, restaurant_id=user["restaurant_id"])
    update_last_login(user["id"])

    return _finish(token=token)


@auth_bp.route("/auth/google/disconnect", methods=["POST"])
@csrf_required
@login_required
def gmb_disconnect(current_user):
    """Disconnect Google Business from this restaurant. Owner-only."""
    from permissions import principal_only
    denied = principal_only(current_user, "the Google Business connection")
    if denied:
        return denied
    from models import update_restaurant
    update_restaurant(current_user["restaurant_id"], {
        "gmb_access_token":  "",
        "gmb_refresh_token": "",
        "gmb_account_id":    "",
        "gmb_location_id":   "",
        "gmb_token_expires": "",
    })
    return jsonify(ok=True)




_SIMPLE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cavnar AI</title><style>body{margin:0;background:#0c0c0c;color:#f0ebe0;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif}
.card{max-width:460px;margin:12vh auto;padding:32px 28px;background:#121212;border:1px solid #262626;border-radius:16px}
h1{font-size:22px;margin:0 0 12px}p{font-size:15px;line-height:1.6;color:#cdbfa9;margin:0 0 12px}a{color:#e8956a}
.cbtn{display:inline-block;margin-top:8px;padding:12px 20px;border:0;border-radius:10px;background:#D4583A;color:#fff;font-size:15px;font-weight:700;cursor:pointer}</style></head><body><div class="card">%s</div></body></html>"""


@auth_bp.route("/auth/not-me/<token>", methods=["GET", "POST"])
def login_not_me(token):
    """The login email's 'This wasn't me' button. Signs the account out
    everywhere, forgets every remembered 2FA device, blocks sign-in until
    the password is reset, and emails a reset link. One use, 7 days.

    A GET only asks. It used to act, so a mail scanner prefetching the link
    (Outlook Safe Links, Gmail's link check) locked the account out the
    moment the login email arrived (SEC-7, SEC-34). The button POSTs."""
    from auth import consume_login_report
    from markupsafe import escape as _esc_nm
    page = _SIMPLE_PAGE
    if request.method != "POST":
        return page % ("<h1>Wasn't you?</h1><p>This signs your account out on every device, forgets every "
                       "remembered device, and emails you a link to set a new password. Nobody can sign in "
                       "until it's reset.</p><form method='post'><button type='submit' class='cbtn cbtn-primary'>"
                       "Sign out everywhere</button></form>")
    user = consume_login_report(token)
    if not user:
        return page % "<h1>That link has expired</h1><p>It was already used, or it's more than 7 days old. If you still don't recognize a sign-in, use <a href='/forgot-password'>Forgot password</a> to reset your password now.</p>", 410
    email = user.get("email") or ""
    try:
        from models import create_reset_token, log_event
        from emails import send_password_reset_email
        rt = create_reset_token(email)
        if rt:
            send_password_reset_email(email, f"https://dashboard.cavnar.ai/reset-password/{rt}")
        if user.get("restaurant_id"):
            log_event(user["restaurant_id"], "login_reported_not_me", {"actor": user.get("username")})
    except Exception as _e:
        print(f"[NotMe] reset email error: {_e}")
    return page % ("<h1>You're signed out everywhere</h1><p>Every device has been signed out and every remembered device forgotten. "
                   "Nobody can sign in again until the password is reset — we've emailed a reset link to <strong>%s</strong>.</p>"
                   "<p>If you don't get it in a couple of minutes, <a href='/forgot-password'>request a new one</a>.</p>" % (_esc_nm(email),))
