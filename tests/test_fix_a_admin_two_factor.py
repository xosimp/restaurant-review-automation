"""Fix round A, #5 (SECURITY-1): an admin's second factor, through /login.

Lockout safety first. Production's only admin ('will') has 2FA off and
ADMIN_REQUIRE_2FA unset, so:
  * ADMIN_REQUIRE_2FA unset, admin without 2FA -> still signs in with the
    password alone, and the console opens;
  * admin WITH 2FA -> asked for a code on every sign-in path (web password,
    Google SSO, Sign in with Apple, mobile password), and the session records
    that it passed;
  * ADMIN_REQUIRE_2FA=1, admin without 2FA -> sent to /admin/two-factor,
    which he can actually reach and finish, after which the console opens.
Admin 2FA lives on the users row, never on the restaurant he is homed on,
and his backup codes are his own. Every test signs in for real.
"""
import re

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import mobile_api
import models
from auth import create_user, init_auth, upsert_membership, hash_session_token
from models import Restaurant, create_restaurant, update_restaurant

CSRF = "fix-a-2fa-csrf"
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
    auth_routes._login_attempts.clear()
    yield
    auth_routes._login_attempts.clear()


@pytest.fixture
def codes(monkeypatch):
    """Every code a challenge is issued with, and every email/text sent."""
    box = {"codes": [], "email": [], "sms": []}
    real_issue = auth.issue_two_fa_challenge

    def issue(*a, **k):
        pending, code = real_issue(*a, **k)
        box["codes"].append(code)
        return pending, code
    monkeypatch.setattr(auth, "issue_two_fa_challenge", issue)
    import emails
    import notify
    monkeypatch.setattr(emails, "send_2fa_code", lambda to, name, code, owner=None, **k: box["email"].append((to, code)) or True)
    monkeypatch.setattr(notify, "send_2fa_sms", lambda to, name, code: box["sms"].append((to, code)) or True)
    return box


@pytest.fixture
def app():
    a = Flask(__name__, template_folder="../templates")
    a.secret_key = "fix-a"
    for bp in (auth_routes.auth_bp, admin_routes.admin_bp, client_api.client_bp, mobile_api.mobile_bp):
        a.register_blueprint(bp)
    return a


def _admin(db_path, two_fa=False, home_two_fa=False):
    home = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="owner-of-home@x.test"), db_path=db_path)
    if home_two_fa:
        update_restaurant(home, {"two_fa_enabled": 1}, db_path=db_path)
    uid = create_user(home, "will", "will@cavnar.test", ADMIN_PW, is_admin=True, db_path=db_path)
    if two_fa:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE users SET two_fa_enabled=1, two_fa_method='email' WHERE id=?", (uid,))
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


def _verify(c, pending, code, remember=False):
    csrf = c.get_cookie("csrf_token").value
    data = {"pending_token": pending, "code": code, "next_url": "/admin", "csrf_token": csrf}
    if remember:
        data["remember_device"] = "1"
    return c.post("/verify-2fa", data=data)


def _session_row(db_path, token):
    conn = models.get_conn(db_path)
    try:
        return conn.execute("SELECT * FROM sessions WHERE token=?", (hash_session_token(token),)).fetchone()
    finally:
        conn.close()


def _console(c):
    return c.get("/admin/api/system")


# ── the state production is in: 2FA off, not required ───────────────────────

def test_admin_without_2fa_still_signs_in_with_the_password_when_it_is_not_required(app, db_path, codes):
    _admin(db_path)
    c = _client(app)
    r = _login(c)
    assert r.status_code == 302 and r.headers["Location"].endswith("/admin")
    assert c.get_cookie("session_token") is not None
    assert codes["codes"] == []                              # nothing asked
    assert _console(c).status_code == 200                    # and the console opens


def test_the_home_restaurants_flag_no_longer_decides_an_admins_second_factor(app, db_path, codes):
    """The old gate read two_fa_enabled on whatever restaurant the admin is
    homed on — a client's, if the seed attached him to one."""
    _admin(db_path, home_two_fa=True)
    c = _client(app)
    assert _login(c).status_code == 302 and codes["codes"] == []
    assert _console(c).status_code == 200


# ── an admin WITH 2FA: a code on every sign-in path ─────────────────────────

def test_admin_with_2fa_gets_a_code_at_the_web_password_sign_in(app, db_path, codes):
    _home, uid = _admin(db_path, two_fa=True)
    c = _client(app)
    r = _login(c)
    assert r.status_code == 200 and c.get_cookie("session_token") is None
    assert "We emailed a 6-digit code to" in r.get_data(as_text=True)
    assert codes["email"] and codes["email"][-1][0] == "will@cavnar.test"   # his own address
    v = _verify(c, _pending(r), codes["codes"][-1])
    assert v.status_code == 302
    tok = c.get_cookie("session_token").value
    row = _session_row(db_path, tok)
    assert row["two_factor_at"] and row["reauth_at"]
    assert _console(c).status_code == 200


def test_admin_with_2fa_is_refused_by_google_sign_in_without_a_remembered_device(app, db_path, codes, monkeypatch):
    import requests

    class _R:
        ok, status_code = True, 200

        def __init__(self, p):
            self.p = p

        def json(self):
            return self.p
    _admin(db_path, two_fa=True)
    monkeypatch.setattr(requests, "post", lambda *a, **k: _R({"access_token": "at"}))
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R({"email": "will@cavnar.test", "id": "g-1",
                                                               "verified_email": True}))
    c = _client(app)
    c.set_cookie("g_sso_state", "st")
    r = c.get("/auth/google-sso/callback?state=st&code=x")
    assert r.status_code == 302 and "use_password_for_2fa" in r.headers["Location"]
    assert c.get_cookie("session_token") is None


def test_admin_without_2fa_signs_in_with_google_as_before(app, db_path, codes, monkeypatch):
    import requests

    class _R:
        ok, status_code = True, 200

        def __init__(self, p):
            self.p = p

        def json(self):
            return self.p
    _admin(db_path)
    monkeypatch.setattr(requests, "post", lambda *a, **k: _R({"access_token": "at"}))
    monkeypatch.setattr(requests, "get", lambda *a, **k: _R({"email": "will@cavnar.test", "id": "g-1",
                                                               "verified_email": True}))
    c = _client(app)
    c.set_cookie("g_sso_state", "st")
    r = c.get("/auth/google-sso/callback?state=st&code=x")
    assert r.status_code == 302 and "error" not in r.headers["Location"]
    cookie = next(h for h in r.headers.getlist("Set-Cookie") if h.startswith("session_token="))
    assert "Max-Age=%d" % (auth.ADMIN_SESSION_HOURS * 3600) in cookie      # an admin session: 12 hours


def test_admin_with_2fa_is_refused_by_sign_in_with_apple(app, db_path, codes, monkeypatch):
    _admin(db_path, two_fa=True)
    monkeypatch.setattr(mobile_api, "_verify_apple_identity_token",
                        lambda tok, bundle: {"sub": "apple-1", "email": "will@cavnar.test", "email_verified": "true"})
    r = app.test_client().post("/mobile/api/apple-signin", json={"identity_token": "t"})
    assert r.status_code == 403 and "token" not in (r.get_json() or {})


def test_admin_with_2fa_gets_a_code_at_the_mobile_password_sign_in(app, db_path, codes):
    _admin(db_path, two_fa=True)
    c = app.test_client()
    r = c.post("/mobile/api/login", json={"username": "will", "password": ADMIN_PW}).get_json()
    assert r["requires_2fa"] is True and "token" not in r and r["channel"] == "email" and r["code_sent"] is True
    v = c.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": codes["codes"][-1]})
    assert v.status_code == 200
    row = _session_row(db_path, v.get_json()["token"])
    assert row["two_factor_at"] and row["device_type"] == "ios"


def test_a_remembered_device_is_the_admins_second_factor_next_time(app, db_path, codes):
    _admin(db_path, two_fa=True)
    c = _client(app)
    r = _login(c)
    _verify(c, _pending(r), codes["codes"][-1], remember=True)
    c.delete_cookie("session_token")
    n = len(codes["codes"])
    r2 = _login(c)
    assert r2.status_code == 302 and len(codes["codes"]) == n          # no new code
    assert _session_row(db_path, c.get_cookie("session_token").value)["two_factor_at"]


# ── backup codes are the login's own ────────────────────────────────────────

def test_an_admin_passes_with_his_own_backup_code_but_never_the_home_restaurants(app, db_path, codes):
    home, uid = _admin(db_path, two_fa=True)
    restaurant_code = models.generate_backup_codes(home, db_path=db_path)[0]
    own_code = auth.generate_user_backup_codes(uid, db_path=db_path)[0]
    c = _client(app)
    pending = _pending(_login(c))
    bad = _verify(c, pending, restaurant_code)
    assert bad.status_code == 200 and "Incorrect code" in bad.get_data(as_text=True)
    ok = _verify(c, pending, own_code)
    assert ok.status_code == 302 and c.get_cookie("session_token") is not None


# ── ADMIN_REQUIRE_2FA=1: an enrolment page he can reach ─────────────────────

def test_required_and_not_enrolled_goes_to_an_enrolment_page_that_works(app, db_path, codes, monkeypatch):
    monkeypatch.setenv("ADMIN_REQUIRE_2FA", "1")
    _home, uid = _admin(db_path)
    c = _client(app)
    assert _login(c).status_code == 302
    page = c.get("/admin")
    assert page.status_code == 302 and "/admin/two-factor" in page.headers["Location"]
    enrol = c.get("/admin/two-factor")
    assert enrol.status_code == 200 and "Two-factor for your login" in enrol.get_data(as_text=True)
    blocked = _console(c)
    assert blocked.status_code == 403 and blocked.get_json()["two_factor_required"] is True
    sent = c.post("/admin/two-factor/send", json={"method": "email"}, headers={"X-CSRF": CSRF})
    assert sent.status_code == 200 and sent.get_json()["ok"] is True
    done = c.post("/admin/two-factor/verify", json={"code": codes["codes"][-1], "method": "email"},
                  headers={"X-CSRF": CSRF}).get_json()
    assert done["ok"] is True and len(done["backup_codes"]) == 10
    assert _console(c).status_code == 200                                # this session passed it
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT two_fa_enabled FROM users WHERE id=?", (uid,)).fetchone()[0] == 1
    conn.close()
    # ...and from now on every sign-in asks for the code.
    c2 = _client(app)
    assert _login(c2).status_code == 200 and c2.get_cookie("session_token") is None


def test_enrolment_only_switches_on_the_channel_the_code_went_by(app, db_path, codes):
    """A code emailed for enrolment cannot enrol text codes: the challenge is
    bound to the channel it was sent on."""
    _home, uid = _admin(db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET phone='+15125550100' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    c = _client(app)
    _login(c)
    assert c.post("/admin/two-factor/send", json={"method": "email"}, headers={"X-CSRF": CSRF}).status_code == 200
    emailed = codes["codes"][-1]
    r = c.post("/admin/two-factor/verify", json={"code": emailed, "method": "sms"}, headers={"X-CSRF": CSRF})
    assert r.status_code == 400
    ok = c.post("/admin/two-factor/verify", json={"code": emailed, "method": "email"}, headers={"X-CSRF": CSRF})
    assert ok.status_code == 200
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT two_fa_method FROM users WHERE id=?", (uid,)).fetchone()[0] == "email"
    conn.close()


def test_an_admin_can_move_off_the_default_username_with_a_recent_password(app, db_path, codes):
    _home, uid = _admin(db_path)
    c = _client(app)
    _login(c)                                              # the password was just typed: step-up satisfied
    r = c.post("/admin/api/me/username", json={"username": "ops-7f3"}, headers={"X-CSRF": CSRF})
    assert r.status_code == 200 and r.get_json()["username"] == "ops-7f3"
    c2 = _client(app)
    assert _login(c2, "will").status_code == 200           # the old name no longer signs in (form re-rendered)
    assert _login(c2, "ops-7f3").status_code == 302
    # The seed does not re-arm under the old ADMIN_USERNAME.
    assert auth.ensure_admin_login(db_path=db_path, env={"ADMIN_USERNAME": "will",
                                                         "ADMIN_PASSWORD": ADMIN_PW})["action"] == "exists"


def test_required_and_not_enrolled_is_held_on_client_routes_and_the_phone_too(app, db_path, codes, monkeypatch):
    """An admin session reaches client routes that name a restaurant_id;
    the gate there is the same one."""
    monkeypatch.setenv("ADMIN_REQUIRE_2FA", "1")
    _admin(db_path)
    c = _client(app)
    _login(c)
    r = c.get("/api/review-count")
    assert r.status_code == 403 and r.get_json()["two_factor_required"] is True
    tok = app.test_client().post("/mobile/api/login", json={"username": "will", "password": ADMIN_PW}).get_json()["token"]
    m = app.test_client().get("/mobile/api/me", headers={"Authorization": "Bearer " + tok})
    assert m.status_code == 403 and m.get_json()["two_factor_required"] is True


def test_a_session_from_before_enrolment_is_ended_not_waved_through(app, db_path, codes):
    _home, uid = _admin(db_path)
    c = _client(app)
    _login(c)
    tok = c.get_cookie("session_token").value
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET two_fa_enabled=1 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    r = _console(c)
    assert r.status_code == 401 and r.get_json()["two_factor_required"] is True
    assert _session_row(db_path, tok) is None


def test_the_gate_fails_closed_on_an_error_without_serving_the_page(app, db_path, codes, monkeypatch):
    _admin(db_path)
    c = _client(app)
    _login(c)

    def boom(user):
        raise RuntimeError("unreadable")
    monkeypatch.setattr(auth, "user_two_factor_enrolled", boom)
    assert _console(c).status_code == 503


def test_an_admins_sign_in_never_sends_the_home_restaurants_login_alert(app, db_path, codes, monkeypatch):
    home, _ = _admin(db_path)
    update_restaurant(home, {"login_notify": 1}, db_path=db_path)
    import notify
    alerts = []
    monkeypatch.setattr(notify, "send_login_alert", lambda *a, **k: alerts.append(a))
    _login(_client(app))
    assert alerts == []


def test_a_restaurant_login_is_still_asked_by_its_restaurants_switch(app, db_path, codes):
    rid = create_restaurant(Restaurant(name="Client", owner_email="o@x.test"), db_path=db_path)
    uid = create_user(rid, "owner", "o@x.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    c = _client(app)
    r = _login(c, "owner", "Owner-pass-2026")
    assert r.status_code == 200 and c.get_cookie("session_token") is None


def test_an_admin_cannot_flip_his_home_restaurants_switch_as_if_it_were_his_own(app, db_path, codes):
    home, _ = _admin(db_path)
    c = _client(app)
    _login(c)
    r = c.post("/api/toggle-2fa", json={"enabled": True}, headers={"X-CSRF": CSRF})
    assert r.status_code == 403 and "/admin/two-factor" in r.get_json()["error"]
    assert not models.get_restaurant(home, db_path=db_path).two_fa_enabled
