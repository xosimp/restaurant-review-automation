"""R10 (9/29/26): an authenticator app (TOTP) as an internal login's second
factor, beside email and text.

Will's admin login is enrolled on email or sms in production with
ADMIN_REQUIRE_2FA=1, so the first rule is that nothing about those
enrolments changes. Then: the TOTP maths (RFC 6238 vectors, the ±1 step
window, one use per step), the enrolment (a pending secret, active only once
a code from the app confirms it), sign-in with the app (nothing sent, no
Resend, no email fallback, backup codes still work), switching an enrolled
login (step-up), and the secret as a credential (encrypted at rest, never in
an audit row, a log or an off-site backup). Every flow signs in for real.
"""
import base64
import os
import re
import shutil
import sqlite3

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import credentials
import mobile_api
import models
from auth import create_user, init_auth, hash_session_token
from models import Restaurant, create_restaurant

CSRF = "r10-totp-csrf"
ADMIN_PW = "Admin-pass-2026!"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, mobile_api, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    models.init_email_log(db_path=db_path)
    monkeypatch.delenv("ADMIN_REQUIRE_2FA", raising=False)
    from cryptography.fernet import Fernet
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    auth_routes._login_attempts.clear()
    yield
    auth_routes._login_attempts.clear()


@pytest.fixture
def sent(monkeypatch):
    """Every challenge code issued, and every email/text that went out."""
    box = {"codes": [], "email": [], "sms": []}
    real_issue = auth.issue_two_fa_challenge

    def issue(*a, **k):
        pending, code = real_issue(*a, **k)
        box["codes"].append(code)
        return pending, code
    monkeypatch.setattr(auth, "issue_two_fa_challenge", issue)
    import emails
    import notify
    monkeypatch.setattr(emails, "send_2fa_code",
                        lambda to, name, code, owner=None, **k: box["email"].append((to, code)) or True)
    monkeypatch.setattr(notify, "send_2fa_sms", lambda to, name, code: box["sms"].append((to, code)) or True)
    return box


@pytest.fixture
def app():
    a = Flask(__name__, template_folder="../templates")
    a.secret_key = "r10"
    for bp in (auth_routes.auth_bp, admin_routes.admin_bp, client_api.client_bp, mobile_api.mobile_bp):
        a.register_blueprint(bp)
    return a


def _q(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _admin(db_path, method=None, phone=None):
    home = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="home@x.test"), db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", ADMIN_PW, is_admin=True, db_path=db_path)
    conn = models.get_conn(db_path)
    if phone:
        conn.execute("UPDATE users SET phone=? WHERE id=?", (phone, uid))
    if method:
        conn.execute("UPDATE users SET two_fa_enabled=1, two_fa_method=? WHERE id=?", (method, uid))
    conn.commit()
    conn.close()
    return home, uid


def _client(app):
    c = app.test_client()
    c.set_cookie("csrf_js", CSRF)
    return c


def _login(c, username="will", password=ADMIN_PW):
    page = c.get("/login").get_data(as_text=True)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    return c.post("/login", data={"username": username, "password": password, "csrf_token": tok})


def _pending(resp):
    return re.search(r'name="pending_token" value="([^"]+)"', resp.get_data(as_text=True)).group(1)


def _verify(c, pending, code):
    csrf = c.get_cookie("csrf_token").value
    return c.post("/verify-2fa", data={"pending_token": pending, "code": code, "next_url": "/admin",
                                       "csrf_token": csrf})


def _post(c, path, body):
    return c.post(path, json=body, headers={"X-CSRF": CSRF})


def _secret_of(resp_json):
    return resp_json["secret"].replace(" ", "")


def _now_code(secret, offset=0):
    return auth.totp_code(secret, auth.totp_step() + offset)


def _enrol_app(c, db_path):
    """Through the page's own two posts; returns the plaintext secret."""
    r = _post(c, "/admin/two-factor/send", {"method": "app"})
    assert r.status_code == 200, r.get_json()
    secret = _secret_of(r.get_json())
    done = _post(c, "/admin/two-factor/verify", {"method": "app", "code": _now_code(secret)})
    assert done.status_code == 200, done.get_json()
    return secret, done.get_json()


def _signed_in_admin_on_app(app, db_path, sent):
    _home, uid = _admin(db_path)
    c = _client(app)
    assert _login(c).status_code == 302
    secret, _ = _enrol_app(c, db_path)
    return uid, secret


# ── the maths ───────────────────────────────────────────────────────────────

RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


@pytest.mark.parametrize("t,expected", [(59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"),
                                        (1234567890, "89005924"), (2000000000, "69279037"),
                                        (20000000000, "65353130")])
def test_rfc_6238_sha1_vectors(t, expected):
    step = auth.totp_step(t)
    assert auth.totp_code(RFC_SECRET, step, digits=8) == expected
    # The six-digit code the apps show is the same number's last six digits.
    assert auth.totp_code(RFC_SECRET, step) == expected[-6:]


def test_one_step_either_side_is_accepted_and_no_further():
    t = 1111111109
    code = auth.totp_code(RFC_SECRET, auth.totp_step(t))
    assert auth.totp_match(RFC_SECRET, code, now=t) == auth.totp_step(t)
    assert auth.totp_match(RFC_SECRET, code, now=t + 30) == auth.totp_step(t)       # the app is a step behind
    assert auth.totp_match(RFC_SECRET, code, now=t - 30) == auth.totp_step(t)       # the app is a step ahead
    assert auth.totp_match(RFC_SECRET, code, now=t + 60) is None
    assert auth.totp_match(RFC_SECRET, code, now=t - 60) is None
    assert auth.totp_match(RFC_SECRET, "12345", now=t) is None and auth.totp_match(RFC_SECRET, "", now=t) is None
    assert auth.totp_match(RFC_SECRET, code, now=t, last_step=auth.totp_step(t)) is None   # already used


def test_a_new_secret_is_160_random_bits_and_the_uri_names_cavnar_ai():
    s1, s2 = auth.totp_new_secret(), auth.totp_new_secret()
    assert s1 != s2 and len(base64.b32decode(s1 + "=" * (-len(s1) % 8))) * 8 >= 160
    uri = auth.totp_uri("will", s1)
    assert uri.startswith("otpauth://totp/Cavnar%20AI:will?secret=" + s1)
    assert "issuer=Cavnar%20AI" in uri
    qr = auth.totp_qr_data_uri(uri)
    assert qr.startswith("data:image/svg+xml;base64,")
    assert b"<svg" in base64.b64decode(qr.split(",", 1)[1])


def test_the_same_step_signs_in_once_even_twice_in_a_row(db_path):
    _home, uid = _admin(db_path)
    secret = auth.start_totp_enrolment(uid, db_path=db_path)
    assert auth.confirm_totp_enrolment(uid, _now_code(secret), db_path=db_path) == "ok"
    # The step that confirmed it is spent; the next one works once.
    assert auth.verify_user_totp(uid, _now_code(secret), db_path=db_path) is False
    nxt = _now_code(secret, 1)
    assert auth.verify_user_totp(uid, nxt, db_path=db_path) is True
    assert auth.verify_user_totp(uid, nxt, db_path=db_path) is False
    assert _q(db_path, "SELECT last_step FROM user_totp WHERE user_id=?", (uid,))[0] == auth.totp_step() + 1


# ── enrolment ───────────────────────────────────────────────────────────────

def test_the_qr_makes_a_pending_secret_that_is_not_active_until_a_code_confirms_it(app, db_path, sent):
    _home, uid = _admin(db_path)
    c = _client(app)
    _login(c)
    page = c.get("/admin/two-factor").get_data(as_text=True)
    assert "Authenticator app" in page and 'value="app"' in page
    r = _post(c, "/admin/two-factor/send", {"method": "app"})
    body = r.get_json()
    assert r.status_code == 200 and body["method"] == "app" and body["qr"].startswith("data:image/svg+xml;base64,")
    assert r.headers.get("Cache-Control") == "no-store"
    assert "http" not in body["qr"]                          # drawn here, never by a QR service
    assert sent["email"] == [] and sent["sms"] == []         # nothing is sent for the app
    assert _q(db_path, "SELECT two_fa_enabled FROM users WHERE id=?", (uid,))[0] == 0
    row = _q(db_path, "SELECT secret, pending_secret FROM user_totp WHERE user_id=?", (uid,))
    assert row["secret"] is None and row["pending_secret"].startswith(credentials.PREFIX)
    secret = _secret_of(body)

    wrong = _post(c, "/admin/two-factor/verify", {"method": "app", "code": "000000" if _now_code(secret) != "000000"
                                                   else "111111"})
    assert wrong.status_code == 400
    assert _q(db_path, "SELECT two_fa_enabled FROM users WHERE id=?", (uid,))[0] == 0

    ok = _post(c, "/admin/two-factor/verify", {"method": "app", "code": _now_code(secret)}).get_json()
    assert ok["ok"] is True and ok["method"] == "app" and len(ok["backup_codes"]) == 10
    u = _q(db_path, "SELECT two_fa_enabled, two_fa_method FROM users WHERE id=?", (uid,))
    assert (u["two_fa_enabled"], u["two_fa_method"]) == (1, "app")
    row = _q(db_path, "SELECT secret, pending_secret, last_step FROM user_totp WHERE user_id=?", (uid,))
    assert row["pending_secret"] is None and row["secret"].startswith(credentials.PREFIX)
    assert credentials.decrypt(row["secret"]) == secret and row["last_step"] is not None
    # This session just passed the second factor.
    tok = c.get_cookie("session_token").value
    assert _q(db_path, "SELECT two_factor_at FROM sessions WHERE token=?", (hash_session_token(tok),))[0]


def test_an_expired_or_replaced_qr_code_is_refused(app, db_path, sent):
    _home, uid = _admin(db_path)
    c = _client(app)
    _login(c)
    first = _secret_of(_post(c, "/admin/two-factor/send", {"method": "app"}).get_json())
    second = _secret_of(_post(c, "/admin/two-factor/send", {"method": "app"}).get_json())
    assert first != second
    assert _post(c, "/admin/two-factor/verify", {"method": "app", "code": _now_code(first)}).status_code == 400
    conn = models.get_conn(db_path)
    conn.execute("UPDATE user_totp SET pending_expires_at=datetime('now','-1 minute') WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()
    r = _post(c, "/admin/two-factor/verify", {"method": "app", "code": _now_code(second)})
    assert r.status_code == 400 and "expired" in r.get_json()["error"]
    assert _q(db_path, "SELECT two_fa_enabled FROM users WHERE id=?", (uid,))[0] == 0


def test_wrong_enrolment_codes_are_throttled_per_login(app, db_path, sent):
    _admin(db_path)
    c = _client(app)
    _login(c)
    secret = _secret_of(_post(c, "/admin/two-factor/send", {"method": "app"}).get_json())
    good = _now_code(secret)
    bad = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert _post(c, "/admin/two-factor/verify", {"method": "app", "code": bad}).status_code == 400
    assert _post(c, "/admin/two-factor/verify", {"method": "app", "code": good}).status_code == 429


def test_no_credential_key_no_authenticator(app, db_path, sent, monkeypatch):
    _home, uid = _admin(db_path)
    c = _client(app)
    _login(c)
    monkeypatch.delenv("CREDENTIAL_KEY", raising=False)
    assert "needs CREDENTIAL_KEY" in c.get("/admin/two-factor").get_data(as_text=True)
    r = _post(c, "/admin/two-factor/send", {"method": "app"})
    assert r.status_code == 409 and "CREDENTIAL_KEY" in r.get_json()["error"]
    assert _q(db_path, "SELECT COUNT(*) FROM user_totp")[0] == 0     # never stored in clear
    with pytest.raises(RuntimeError):
        auth.start_totp_enrolment(uid, db_path=db_path)


# ── signing in with the app ─────────────────────────────────────────────────

def test_sign_in_asks_for_the_app_code_and_sends_nothing(app, db_path, sent):
    uid, secret = _signed_in_admin_on_app(app, db_path, sent)
    c = _client(app)
    r = _login(c)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and c.get_cookie("session_token") is None
    assert "Open your authenticator app" in html and "Use a backup code" in html
    assert 'id="resend-btn"' not in html                              # nothing to resend
    assert sent["email"] == [] and sent["sms"] == []
    pending = _pending(r)
    # The challenge's own random code was never sent anywhere and never passes.
    bad = _verify(c, pending, sent["codes"][-1])
    assert bad.status_code == 200 and "Incorrect code" in bad.get_data(as_text=True)
    ok = _verify(c, pending, _now_code(secret, 1))
    assert ok.status_code == 302 and ok.headers["Location"].endswith("/admin")
    row = _q(db_path, "SELECT two_factor_at FROM sessions WHERE token=?",
             (hash_session_token(c.get_cookie("session_token").value),))
    assert row["two_factor_at"]
    assert c.get("/admin/api/system").status_code == 200


def test_a_code_that_signed_in_once_does_not_sign_in_again(app, db_path, sent):
    uid, secret = _signed_in_admin_on_app(app, db_path, sent)
    code = _now_code(secret, 1)
    c1 = _client(app)
    assert _verify(c1, _pending(_login(c1)), code).status_code == 302
    c2 = _client(app)
    again = _verify(c2, _pending(_login(c2)), code)
    assert again.status_code == 200 and c2.get_cookie("session_token") is None


def test_resend_is_refused_for_an_app_login_and_nothing_is_emailed(app, db_path, sent):
    _signed_in_admin_on_app(app, db_path, sent)
    c = _client(app)
    pending = _pending(_login(c))
    r = c.post("/resend-2fa", json={"pending_token": pending})
    assert r.status_code == 400 and r.get_json()["channel"] == "app" and "backup code" in r.get_json()["error"]
    m = app.test_client().post("/mobile/api/resend-2fa", json={"pending_token": pending})
    assert m.status_code == 400
    assert sent["email"] == [] and sent["sms"] == []


def test_a_backup_code_still_signs_an_app_login_in(app, db_path, sent):
    uid, _secret = _signed_in_admin_on_app(app, db_path, sent)
    code = auth.generate_user_backup_codes(uid, db_path=db_path)[0]
    c = _client(app)
    ok = _verify(c, _pending(_login(c)), code)
    assert ok.status_code == 302 and c.get_cookie("session_token") is not None


def test_wrong_app_codes_are_throttled_per_login_from_any_address(app, db_path, sent):
    uid, secret = _signed_in_admin_on_app(app, db_path, sent)
    assert auth.second_factor_throttle_key(auth.get_user_by_id(uid, db_path=db_path)) == f"2fa-app:{uid}"
    good = _now_code(secret, 1)
    bad = "000000" if good != "000000" else "111111"
    for i in range(5):                                  # five addresses, one guess each
        c = _client(app)
        c.environ_base["REMOTE_ADDR"] = f"10.0.0.{i + 1}"
        pending = _pending(_login(c))
        assert _verify(c, pending, bad).status_code == 200
    c = _client(app)
    c.environ_base["REMOTE_ADDR"] = "10.0.0.99"
    pending = _pending(_login(c))
    r = _verify(c, pending, good)
    assert r.status_code == 200 and "Too many attempts" in r.get_data(as_text=True)
    assert c.get_cookie("session_token") is None


def test_the_phone_signs_an_app_login_in_with_the_app_code(app, db_path, sent):
    _uid, secret = _signed_in_admin_on_app(app, db_path, sent)
    c = app.test_client()
    r = c.post("/mobile/api/login", json={"username": "will", "password": ADMIN_PW}).get_json()
    assert r["requires_2fa"] is True and r["channel"] == "app" and r["code_sent"] is True
    assert sent["email"] == [] and sent["sms"] == []
    bad = c.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": sent["codes"][-1]})
    assert bad.status_code == 401
    ok = c.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": _now_code(secret, 1)})
    assert ok.status_code == 200 and ok.get_json()["token"]


# ── switching an enrolled login ─────────────────────────────────────────────

def _email_signed_in(app, db_path, sent):
    """An admin on email codes (production's state), signed in with one."""
    _home, uid = _admin(db_path, method="email")
    c = _client(app)
    r = _login(c)
    assert _verify(c, _pending(r), sent["codes"][-1]).status_code == 302
    return uid, c


def _age_reauth(db_path, c):
    conn = models.get_conn(db_path)
    conn.execute("UPDATE sessions SET reauth_at=datetime('now','-1 hour') WHERE token=?",
                 (hash_session_token(c.get_cookie("session_token").value),))
    conn.commit()
    conn.close()


def test_switching_email_to_the_app_needs_the_step_up_and_keeps_email_until_confirmed(app, db_path, sent):
    uid, c = _email_signed_in(app, db_path, sent)
    old_codes = auth.generate_user_backup_codes(uid, db_path=db_path)
    _age_reauth(db_path, c)
    refused = _post(c, "/admin/two-factor/send", {"method": "app"})
    assert refused.status_code == 403 and refused.get_json()["reauth_required"] is True
    assert _q(db_path, "SELECT COUNT(*) FROM user_totp")[0] == 0
    assert _post(c, "/admin/api/reauth", {"password": ADMIN_PW}).status_code == 200
    body = _post(c, "/admin/two-factor/send", {"method": "app"}).get_json()
    secret = _secret_of(body)
    # Until a code from the app confirms it, email still signs him in.
    assert _q(db_path, "SELECT two_fa_method FROM users WHERE id=?", (uid,))[0] == "email"
    other = _client(app)
    r = _login(other)
    assert "We emailed a 6-digit code" in r.get_data(as_text=True)
    assert _verify(other, _pending(r), sent["codes"][-1]).status_code == 302

    # The confirm needs the step-up too.
    _age_reauth(db_path, c)
    assert _post(c, "/admin/two-factor/verify", {"method": "app", "code": _now_code(secret)}).status_code == 403
    _post(c, "/admin/api/reauth", {"password": ADMIN_PW})
    done = _post(c, "/admin/two-factor/verify", {"method": "app", "code": _now_code(secret)}).get_json()
    assert done["ok"] is True and done["switched"] is True and "backup_codes" not in done
    assert _q(db_path, "SELECT two_fa_method FROM users WHERE id=?", (uid,))[0] == "app"
    # His saved backup codes still work; email no longer does.
    assert auth.count_unused_user_backup_codes(uid, db_path=db_path) == len(old_codes)
    n_email = len(sent["email"])
    c3 = _client(app)
    r3 = _login(c3)
    assert "Open your authenticator app" in r3.get_data(as_text=True) and len(sent["email"]) == n_email
    assert _verify(c3, _pending(r3), old_codes[0]).status_code == 302
    ev = _q(db_path, "SELECT event_type, before_json, after_json FROM admin_events "
                     "WHERE event_type='admin_two_factor_method_changed'")
    assert ev is not None and '"email"' in ev["before_json"] and '"app"' in ev["after_json"]


def test_leaving_the_app_for_email_forgets_its_secret(app, db_path, sent):
    uid, secret = _signed_in_admin_on_app(app, db_path, sent)
    c = _client(app)
    assert _verify(c, _pending(_login(c)), _now_code(secret, 1)).status_code == 302
    assert _post(c, "/admin/two-factor/send", {"method": "email"}).status_code == 200
    done = _post(c, "/admin/two-factor/verify", {"method": "email", "code": sent["codes"][-1]}).get_json()
    assert done["ok"] is True and done["switched"] is True
    assert _q(db_path, "SELECT two_fa_method FROM users WHERE id=?", (uid,))[0] == "email"
    assert _q(db_path, "SELECT COUNT(*) FROM user_totp WHERE user_id=?", (uid,))[0] == 0
    assert auth.verify_user_totp(uid, _now_code(secret, 1), db_path=db_path) is False


def test_turning_two_factor_off_or_an_admin_reset_forgets_the_secret(app, db_path, sent):
    uid, _secret = _signed_in_admin_on_app(app, db_path, sent)
    assert _q(db_path, "SELECT COUNT(*) FROM user_totp WHERE user_id=?", (uid,))[0] == 1
    auth.clear_user_two_factor(uid, db_path=db_path)
    assert _q(db_path, "SELECT COUNT(*) FROM user_totp WHERE user_id=?", (uid,))[0] == 0
    u = _q(db_path, "SELECT two_fa_enabled, two_fa_method FROM users WHERE id=?", (uid,))
    assert (u[0], u[1]) == (0, None)


# ── email and sms enrolments are unchanged ──────────────────────────────────

@pytest.mark.parametrize("method", ["email", "sms"])
def test_an_email_or_sms_enrolment_signs_in_exactly_as_before(app, db_path, sent, method):
    _home, uid = _admin(db_path, method=method, phone="+15125550100")
    assert auth.second_factor_throttle_key(auth.get_user_by_id(uid, db_path=db_path)) is None
    c = _client(app)
    r = _login(c)
    html = r.get_data(as_text=True)
    assert ("We texted a 6-digit code" if method == "sms" else "We emailed a 6-digit code") in html
    assert 'id="resend-btn"' in html
    assert len(sent[method]) == 1
    rr = c.post("/resend-2fa", json={"pending_token": _pending(r)})
    assert rr.status_code == 200 and rr.get_json()["channel"] == method
    assert len(sent[method]) == 2                                    # the resend went, the same way
    assert _verify(c, _pending(r), sent[method][-1][1]).status_code == 302
    assert _q(db_path, "SELECT COUNT(*) FROM user_totp")[0] == 0


def test_first_enrolment_by_email_still_works_and_the_page_offers_the_app(app, db_path, sent):
    _home, uid = _admin(db_path)
    c = _client(app)
    _login(c)
    assert _post(c, "/admin/two-factor/send", {"method": "email"}).status_code == 200
    done = _post(c, "/admin/two-factor/verify", {"method": "email", "code": sent["codes"][-1]}).get_json()
    assert done["ok"] is True and len(done["backup_codes"]) == 10
    page = c.get("/admin/two-factor").get_data(as_text=True)
    assert "Use an authenticator app instead" in page


# ── the secret is a credential ──────────────────────────────────────────────

def _file_bytes(path):
    out = b""
    for p in (path, path + "-wal"):
        if os.path.exists(p):
            with open(p, "rb") as fh:
                out += fh.read()
    return out


def test_the_secret_is_ciphertext_at_rest_and_in_no_audit_row_or_log(app, db_path, sent, capsys):
    uid, secret = _signed_in_admin_on_app(app, db_path, sent)
    grouped = auth.totp_secret_groups(secret)
    raw = _file_bytes(db_path)
    assert secret.encode() not in raw and grouped.encode() not in raw
    conn = models.get_conn(db_path)
    try:
        audit = conn.execute("SELECT * FROM admin_events WHERE event_type LIKE 'admin_two_factor%'").fetchall()
        acts = {r["event_type"] for r in audit}
        assert {"admin_two_factor_app_qr_shown", "admin_two_factor_enabled"} <= acts
        for r in audit:
            assert secret not in " ".join(str(v) for v in dict(r).values())
        tables = [t[0] for t in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            for row in conn.execute(f'SELECT * FROM "{t}"').fetchall():
                assert secret not in " ".join(str(v) for v in tuple(row)), t
    finally:
        conn.close()
    out = capsys.readouterr()
    assert secret not in out.out and secret not in out.err
    # Never on the user every request loads.
    assert not any("totp" in k or "secret" in k for k in auth.get_user_by_id(uid, db_path=db_path))


def test_the_off_site_copy_empties_the_secrets(app, db_path, sent, tmp_path):
    import offsite_backup
    uid, _secret = _signed_in_admin_on_app(app, db_path, sent)
    assert "user_totp" in offsite_backup.SCRUB_TABLES
    copy = str(tmp_path / "copy.db")
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(copy)
    src.backup(dst)
    src.close()
    dst.close()
    done = offsite_backup.redact(copy)
    assert "user_totp" in done["tables"] and not done["unclassified"]
    conn = sqlite3.connect(copy)
    try:
        assert conn.execute("SELECT COUNT(*) FROM user_totp").fetchone()[0] == 0
        # The login's method survives a restore: it signs in with a backup
        # code, then scans a new QR code.
        assert conn.execute("SELECT two_fa_method FROM users WHERE id=?", (uid,)).fetchone()[0] == "app"
    finally:
        conn.close()


def test_the_security_view_names_the_method_and_never_the_secret(app, db_path, sent):
    uid, secret = _signed_in_admin_on_app(app, db_path, sent)
    c = _client(app)
    _verify(c, _pending(_login(c)), _now_code(secret, 1))
    r = c.get(f"/admin/api/users/{uid}/security")
    assert r.status_code == 200
    text = r.get_data(as_text=True)
    assert '"method":"app"' in text.replace(" ", "") and secret not in text
