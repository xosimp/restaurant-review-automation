"""Fix round A: lockouts (#135), the boot seed (#136), the password policy
and the demo password (#86), and where a sign-in code actually went (#132).
"""
import io
import json
import re
from contextlib import redirect_stdout

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import client_api
import mobile_api
import models
import security
from auth import create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, update_restaurant

CSRF = "fix-a-lsp-csrf"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, mobile_api, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.delenv("ADMIN_REQUIRE_2FA", raising=False)
    init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    models.init_email_log(db_path=db_path)
    auth_routes._login_attempts.clear()
    yield
    auth_routes._login_attempts.clear()


@pytest.fixture
def app():
    a = Flask(__name__, template_folder="../templates")
    a.secret_key = "fix-a"
    for bp in (auth_routes.auth_bp, admin_routes.admin_bp, client_api.client_bp, mobile_api.mobile_bp):
        a.register_blueprint(bp)
    return a


def _login_form(c, username, password):
    page = c.get("/login").get_data(as_text=True)
    tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    return c.post("/login", data={"username": username, "password": password, "csrf_token": tok})


def _admin(db_path):
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="w@x.test"), db_path=db_path)
    return hq, create_user(hq, "will", "will@x.test", "Admin-pass-2026!", is_admin=True, db_path=db_path)


# ── #135: nobody can lock the admin out from their own address ──────────────

def test_failures_from_one_address_do_not_lock_the_admin_out_at_another(app, db_path):
    _admin(db_path)
    attacker = app.test_client()
    for _ in range(20):
        attacker.post("/mobile/api/login", json={"username": "will", "password": "guess"},
                      environ_base={"REMOTE_ADDR": "198.51.100.7"})
    # The attacker's address is locked out of the account...
    r = attacker.post("/mobile/api/login", json={"username": "will", "password": "Admin-pass-2026!"},
                      environ_base={"REMOTE_ADDR": "198.51.100.7"})
    assert r.status_code == 429
    # ...and Will, somewhere else, signs straight in.
    ok = app.test_client().post("/mobile/api/login", json={"username": "will", "password": "Admin-pass-2026!"},
                                environ_base={"REMOTE_ADDR": "203.0.113.5"})
    assert ok.status_code == 200 and ok.get_json()["token"]


def test_a_restaurant_login_is_still_locked_account_wide(app, db_path):
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)
    create_user(rid, "owner", "o@x.test", "Owner-pass-2026", db_path=db_path)
    for i in range(security.ACCOUNT_MAX):
        security.record_login_failure(f"10.0.0.{i}", "owner")
    r = app.test_client().post("/mobile/api/login", json={"username": "owner", "password": "Owner-pass-2026"},
                               environ_base={"REMOTE_ADDR": "203.0.113.5"})
    assert r.status_code == 429


def test_a_very_wide_guess_still_hits_a_cap_on_the_internal_account(db_path):
    _admin(db_path)
    for i in range(security.INTERNAL_DAY_MAX):
        security.record_login_failure(f"10.1.{i // 250}.{i % 250}", "will", internal=True)
    assert security.login_throttled("203.0.113.5", "will", internal=True)[0] is True


def test_a_device_the_login_remembered_skips_the_account_lock(app, db_path):
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)
    owner = create_user(rid, "owner", "o@x.test", "Owner-pass-2026", db_path=db_path)
    device = auth.create_trusted_device(rid, owner, "laptop", db_path=db_path)
    for i in range(security.ACCOUNT_MAX):
        security.record_login_failure(f"10.0.0.{i}", "owner")
    c = app.test_client()
    blocked = c.post("/mobile/api/login", json={"username": "owner", "password": "Owner-pass-2026"})
    assert blocked.status_code == 429
    ok = c.post("/mobile/api/login", json={"username": "owner", "password": "Owner-pass-2026", "device_token": device})
    assert ok.status_code == 200


def test_break_glass_unlock_by_env_and_by_script(db_path, monkeypatch):
    _admin(db_path)
    for i in range(security.ACCOUNT_MAX):
        security.record_login_failure("198.51.100.7", "will", internal=True)
    assert security.login_throttled("198.51.100.7", "will", internal=True)[0] is True
    assert security.apply_boot_unlocks(env={"LOGIN_UNLOCK_USERNAMES": "Will"}) == ["will"]
    assert security.login_throttled("198.51.100.7", "will", internal=True)[0] is False
    for i in range(security.ACCOUNT_MAX):
        security.record_login_failure("198.51.100.7", "will", internal=True)
    import importlib.util
    import os
    spec = importlib.util.spec_from_file_location(
        "unlock_login", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "unlock_login.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with redirect_stdout(io.StringIO()):
        assert mod.main(["will"]) == 0
    assert security.login_throttled("198.51.100.7", "will", internal=True)[0] is False


def test_break_glass_turns_off_an_admins_own_second_factor_and_nothing_else(db_path):
    """For the only admin, codes not arriving and backup codes lost: set
    ADMIN_2FA_RESET_USERNAMES, redeploy, sign in. Only internal logins."""
    _hq, admin = _admin(db_path)
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)
    create_user(rid, "owner", "o@x.test", "Owner-pass-2026", db_path=db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET two_fa_enabled=1, two_fa_method='email' WHERE id=?", (admin,))
    conn.commit()
    conn.close()
    auth.generate_user_backup_codes(admin, db_path=db_path)
    with redirect_stdout(io.StringIO()):
        done = auth.apply_boot_two_factor_resets(env={"ADMIN_2FA_RESET_USERNAMES": "Will, owner"}, db_path=db_path)
    assert done == ["will"]                                              # the restaurant login is skipped
    conn = models.get_conn(db_path)
    try:
        assert conn.execute("SELECT two_fa_enabled FROM users WHERE id=?", (admin,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM admin_events WHERE event_type='two_factor_reset_break_glass'"
                            ).fetchone()[0] == 1
    finally:
        conn.close()
    assert auth.count_unused_user_backup_codes(admin, db_path=db_path) == 0
    assert models.get_restaurant(rid, db_path=db_path).two_fa_enabled == 1


# ── #136: the boot seed ─────────────────────────────────────────────────────

def test_the_seed_does_nothing_when_any_admin_exists_even_under_another_name(db_path):
    _admin(db_path)
    out = auth.ensure_admin_login(db_path=db_path, env={"ADMIN_USERNAME": "renamed", "ADMIN_PASSWORD": "Admin-pass-2026!"})
    assert out["action"] == "exists"
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0] == 1
    conn.close()


def test_the_seed_never_uses_a_default_password(db_path, monkeypatch):
    alerts = []
    import ops
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: alerts.append(subject) or True)
    import config
    monkeypatch.setattr(config, "on_railway", lambda: True)
    with redirect_stdout(io.StringIO()) as out:
        res = auth.ensure_admin_login(db_path=db_path, env={})
    assert res["action"] == "refused" and "ADMIN_PASSWORD" in res["reason"]
    assert "changeme" not in out.getvalue()
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    conn.close()
    assert alerts, "a refused seed on Railway must reach the operator"
    short = auth.ensure_admin_login(db_path=db_path, env={"ADMIN_PASSWORD": "short"})
    assert short["action"] == "refused" and "policy" in short["reason"]


def test_the_seed_makes_its_own_home_and_never_relabels_a_client(db_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "on_railway", lambda: False)
    client = create_restaurant(Restaurant(name="Paying Client", owner_email="c@x.test"), db_path=db_path)
    update_restaurant(client, {"billing_status": "active"}, db_path=db_path)
    with redirect_stdout(io.StringIO()):
        res = auth.ensure_admin_login(db_path=db_path, env={"ADMIN_PASSWORD": "Admin-pass-2026!"})
    assert res["action"] == "created"
    conn = models.get_conn(db_path)
    try:
        home = conn.execute("SELECT r.id, r.name, r.billing_status FROM users u JOIN restaurants r "
                            "ON r.id=u.restaurant_id WHERE u.id=?", (res["user_id"],)).fetchone()
        assert home["id"] != client and home["name"] == auth.ADMIN_HOME_NAME and home["billing_status"] == "internal"
        assert conn.execute("SELECT billing_status FROM restaurants WHERE id=?", (client,)).fetchone()[0] == "active"
        assert conn.execute("SELECT COUNT(*) FROM admin_events WHERE event_type='admin_seed_created'").fetchone()[0] == 1
    finally:
        conn.close()


# ── #86: one password policy; the demo password neither stored nor printed ──

def test_create_user_refuses_a_short_or_breached_password(db_path, monkeypatch):
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", True)
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)
    with pytest.raises(auth.PasswordPolicyError) as e:
        create_user(rid, "a", "a@x.test", "short", db_path=db_path)
    assert "at least 8" in str(e.value)
    monkeypatch.setattr(security, "password_pwned", lambda pw, **k: pw == "password123")
    with pytest.raises(auth.PasswordPolicyError):
        create_user(rid, "b", "b@x.test", "password123", db_path=db_path)
    assert create_user(rid, "c", "c@x.test", "Owner-pass-2026", db_path=db_path)
    assert create_user(rid, "d", "d@x.test", "x", db_path=db_path, generated=True)      # a minted secret


def test_admin_create_client_refuses_a_weak_password_and_leaves_nothing_behind(app, db_path, monkeypatch):
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", True)
    _hq, admin = _admin(db_path)
    c = app.test_client()
    c.set_cookie("session_token", auth.create_session(admin, password_verified_at=True, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    r = c.post("/admin/create-client", json={"restaurant_name": "Weak Co", "owner_email": "weak@x.test",
                                             "username": "weakco", "password": "123"}, headers={"X-CSRF": CSRF})
    assert r.get_json()["ok"] is False and "at least 8" in r.get_json()["error"]
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM restaurants WHERE name='Weak Co'").fetchone()[0] == 0
    conn.close()


def test_the_demo_login_password_is_never_stored_or_printed(db_path, monkeypatch):
    import demo_seed
    monkeypatch.setattr(auth, "ENFORCE_PASSWORD_POLICY", True)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    rid = create_restaurant(Restaurant(name=demo_seed.SIMPLE_EJS_NAME, owner_email="e@x.test", is_demo=1),
                            db_path=db_path)
    with redirect_stdout(io.StringIO()) as out:
        demo_seed._ensure_ejs_login(rid, db_path)
    log = out.getvalue()
    assert "created" in log and "generated:" not in log
    assert not models.get_restaurant(rid, db_path=db_path).temp_password
    monkeypatch.setenv("DEMO_PASSWORD", "ErikDemo2026!")
    with redirect_stdout(io.StringIO()) as out:
        demo_seed._ensure_ejs_login(rid, db_path)
    assert "ErikDemo2026!" not in out.getvalue()
    assert auth.verify_password(demo_seed.SIMPLE_EJS_USERNAME, "ErikDemo2026!", db_path=db_path)
    assert not models.get_restaurant(rid, db_path=db_path).temp_password


# ── #132: where the code went, said truthfully ──────────────────────────────

def _owner_with_sms(db_path, email="owner@x.test"):
    rid = create_restaurant(Restaurant(name="R", owner_email="owner@x.test", owner_phone="+15125550100"),
                            db_path=db_path)
    uid = create_user(rid, "owner", email, "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    update_restaurant(rid, {"two_fa_enabled": 1, "two_fa_method": "sms"}, db_path=db_path)
    return rid, uid


def test_codes_go_on_the_verification_service_not_the_alert_one(monkeypatch):
    import notify
    calls = []
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert", **k: calls.append(use_case) or True)
    assert notify.send_2fa_sms("+15125550100", "R", "123456") is True
    assert calls == ["otp"]


def test_a_failed_text_falls_back_to_email_and_the_page_says_emailed(app, db_path, monkeypatch):
    import emails
    import notify
    mails = []
    monkeypatch.setattr(notify, "send_2fa_sms", lambda *a, **k: False)
    monkeypatch.setattr(emails, "send_2fa_code", lambda to, name, code, owner=None, **k: mails.append(to) or True)
    _owner_with_sms(db_path)
    html = _login_form(app.test_client(), "owner", "Owner-pass-2026").get_data(as_text=True)
    assert mails == ["owner@x.test"]
    assert "We emailed a 6-digit code to" in html and "We texted" not in html


def test_a_number_that_texted_stop_gets_its_code_by_email(app, db_path, monkeypatch):
    import emails
    import notify
    texts, mails = [], []
    monkeypatch.setattr(notify, "send_2fa_sms", lambda to, name, code: texts.append(to) or True)
    monkeypatch.setattr(notify, "sms_stopped_phones", lambda phones, **k: set(phones))
    monkeypatch.setattr(emails, "send_2fa_code", lambda to, name, code, owner=None, **k: mails.append(to) or True)
    _owner_with_sms(db_path)
    r = app.test_client().post("/mobile/api/login", json={"username": "owner", "password": "Owner-pass-2026"}).get_json()
    assert texts == [] and mails == ["owner@x.test"]
    assert r["channel"] == "email" and r["fell_back"] is True and r["code_sent"] is True


def test_when_no_code_went_anywhere_the_sign_in_says_so(app, db_path, monkeypatch):
    import emails
    import notify
    monkeypatch.setattr(notify, "send_2fa_sms", lambda *a, **k: False)
    monkeypatch.setattr(emails, "send_2fa_code", lambda *a, **k: False)
    _owner_with_sms(db_path)
    html = _login_form(app.test_client(), "owner", "Owner-pass-2026").get_data(as_text=True)
    assert "We texted" not in html and "We emailed" not in html
    assert "couldn" in html and "backup code" in html
    r = app.test_client().post("/mobile/api/login", json={"username": "owner", "password": "Owner-pass-2026"}).get_json()
    assert r["code_sent"] is False and r["channel"] is None and r["delivery_error"]


def test_the_2fa_setup_routes_now_carry_csrf(app, db_path):
    rid = create_restaurant(Restaurant(name="R", owner_email="o@x.test"), db_path=db_path)
    uid = create_user(rid, "owner", "o@x.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", auth.create_session(uid, db_path=db_path))
    for path in ("/api/send-2fa-test", "/api/verify-2fa-setup"):
        assert c.post(path, json={"method": "email", "code": "000000"}).status_code == 403, path
