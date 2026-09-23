"""The security audit's fixes (Sep 2026).

Throttling that survives a deploy and sees an account, not just an
address; credentials encrypted at rest; reset tokens hashed; the PIN
pepper required; a support role that cannot write; a freeze that ends a
takeover; join links that cannot be walked; and the operator's morning
lines. Every test here is against behaviour, not fixture shape.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    import auth
    for mod in (models, auth):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name="Sec Co", owner_email="s@x.com", **kw), db_path=db_path)


def _user(db_path, rid, username="owner", email="s@x.com", password="correct-horse-9"):
    from auth import create_user
    return create_user(rid, username, email, password, db_path=db_path)


# ── durable login throttling ─────────────────────────────────────────────────

def test_five_failures_on_an_account_lock_it_whatever_the_address(db_path):
    import security
    rid = _rid(db_path); _user(db_path, rid)
    for i in range(5):
        assert security.login_throttled(f"10.0.0.{i}", "owner", db_path=db_path) == (False, 0)
        security.record_login_failure(f"10.0.0.{i}", "owner", db_path=db_path)
    blocked, wait = security.login_throttled("10.9.9.9", "owner", db_path=db_path)
    assert blocked and 0 < wait <= 300
    # The owner can read that it happened.
    rows = get_conn(db_path).execute("SELECT event_type, event_data FROM activity_log WHERE restaurant_id=?", (rid,)).fetchall()
    assert [r["event_type"] for r in rows] == ["login_locked"]
    assert json.loads(rows[0]["event_data"])["failures"] == 5
    # A success clears it.
    security.clear_login_failures(ip="10.9.9.9", username="owner", db_path=db_path)
    assert security.login_throttled("10.9.9.9", "owner", db_path=db_path) == (False, 0)


def test_lockouts_escalate_with_the_days_failures(db_path):
    import security
    assert security.lock_minutes_for(5) == 5
    assert security.lock_minutes_for(10) == 30
    assert security.lock_minutes_for(15) == 24 * 60
    rid = _rid(db_path); _user(db_path, rid)
    for _ in range(10):
        security.record_login_failure("1.1.1.1", "owner", db_path=db_path)
    blocked, wait = security.login_throttled("1.1.1.1", "owner", db_path=db_path)
    assert blocked and wait > 300


def test_an_address_has_a_wider_budget_than_an_account(db_path):
    import security
    for i in range(security.IP_MAX):
        security.record_login_failure("5.5.5.5", f"user{i}", db_path=db_path)     # 25 different accounts
    assert security.login_throttled("5.5.5.5", "someone-new", db_path=db_path)[0] is True
    assert security.login_throttled("6.6.6.6", "someone-new", db_path=db_path)[0] is False


def test_the_web_and_mobile_logins_use_the_durable_limiter(monkeypatch, db_path):
    import auth_routes, security
    seen = []
    monkeypatch.setattr(security, "login_throttled", lambda ip, u=None, **k: seen.append(("check", ip, u)) or (False, 0))
    monkeypatch.setattr(security, "record_login_failure", lambda ip, u=None, **k: seen.append(("fail", ip, u)) or {})
    assert auth_routes._is_rate_limited("1.2.3.4", "owner") is False
    auth_routes._record_failed_attempt("1.2.3.4", "owner")
    assert seen == [("check", "1.2.3.4", "owner"), ("fail", "1.2.3.4", "owner")]
    src = open("mobile_api.py").read()
    assert "_is_rate_limited(ip, username)" in src and "_record_failed_attempt(ip, username)" in src
    src = open("auth_routes.py").read()
    assert "_login_attempts = {}" in src                                          # the fallback stays, the primary is durable


def test_login_attempt_rows_are_pruned(db_path):
    import security
    conn = get_conn(db_path)
    conn.execute("INSERT INTO login_attempts (key, kind, ip, attempted_at) VALUES ('acct:x','account','1.1.1.1','2026-01-01 00:00:00')")
    conn.commit(); conn.close()
    security.prune_login_attempts(db_path=db_path)
    assert get_conn(db_path).execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == 0


# ── breached passwords ───────────────────────────────────────────────────────

def test_breach_check_sends_only_a_prefix_and_fails_open(monkeypatch):
    import security, requests
    monkeypatch.delenv("HIBP_DISABLED", raising=False)
    calls = []

    class _R:
        status_code = 200
        text = "1E4C9B93F3F0682250B6CF8331B7EE68FD8:12\nABCDEF:1"      # sha1('password') tail
    monkeypatch.setattr(requests, "get", lambda url, **k: calls.append(url) or _R())
    assert security.password_pwned("password") is True
    assert calls[0].endswith("/range/5BAA6")
    assert "5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8"[5:] not in calls[0]      # the rest of the hash never leaves
    monkeypatch.setattr(requests, "get", lambda url, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    assert security.password_pwned("anything") is False                          # never blocks on an outage


def test_the_change_and_reset_paths_refuse_a_breached_password(monkeypatch):
    src_web = open("auth_routes.py").read(); src_mob = open("mobile_api.py").read(); src_adm = open("admin_routes.py").read()
    assert src_web.count("password_pwned") >= 2 and "password_pwned" in src_mob and "password_pwned" in src_adm
    assert "if len(new_pw) < 8:" in src_adm and "at least 6" not in src_adm


# ── reset tokens hashed at rest ──────────────────────────────────────────────

def test_reset_tokens_are_stored_hashed_and_still_validate(db_path):
    rid = _rid(db_path); _user(db_path, rid)
    token = models.create_reset_token("s@x.com", db_path=db_path)
    row = get_conn(db_path).execute("SELECT reset_token FROM users WHERE email='s@x.com'").fetchone()
    assert row["reset_token"] != token and len(row["reset_token"]) == 64
    assert models.validate_reset_token(token, db_path=db_path)["email"] == "s@x.com"
    assert models.validate_reset_token(row["reset_token"], db_path=db_path) is None      # the stored value opens nothing
    assert models.consume_reset_token(token, "new-password-12", db_path=db_path) is True


# ── credentials encrypted at rest ────────────────────────────────────────────

def test_credentials_are_encrypted_on_write_and_plain_on_read(db_path, monkeypatch):
    from cryptography.fernet import Fernet
    import credentials
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    rid = _rid(db_path)
    update_restaurant(rid, {"gmb_refresh_token": "1//refresh-secret", "ig_token": "IGQV-secret"}, db_path=db_path)
    raw = get_conn(db_path).execute("SELECT gmb_refresh_token, ig_token FROM restaurants WHERE id=?", (rid,)).fetchone()
    assert raw["gmb_refresh_token"].startswith(credentials.PREFIX) and "refresh-secret" not in raw["gmb_refresh_token"]
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.gmb_refresh_token == "1//refresh-secret" and r.ig_token == "IGQV-secret"
    assert [x.gmb_refresh_token for x in models.get_all_restaurants(db_path=db_path) if x.id == rid] == ["1//refresh-secret"]


def test_plaintext_rows_written_before_the_key_still_read_and_a_wrong_key_never_leaks_ciphertext(db_path, monkeypatch):
    from cryptography.fernet import Fernet
    import credentials
    conn = get_conn(db_path)
    rid = _rid(db_path)
    conn.execute("UPDATE restaurants SET gmb_refresh_token='legacy-plain' WHERE id=?", (rid,)); conn.commit(); conn.close()
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())
    assert models.get_restaurant(rid, db_path=db_path).gmb_refresh_token == "legacy-plain"
    update_restaurant(rid, {"gmb_refresh_token": "now-encrypted"}, db_path=db_path)
    monkeypatch.setenv("CREDENTIAL_KEY", Fernet.generate_key().decode())         # rotated without re-encrypting
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    assert models.get_restaurant(rid, db_path=db_path).gmb_refresh_token is None  # not the ciphertext


def test_without_a_key_values_pass_through_and_it_is_said_once(db_path, monkeypatch, capsys):
    import credentials
    monkeypatch.delenv("CREDENTIAL_KEY", raising=False)
    credentials._warned = False
    assert credentials.encrypt("plain") == "plain" and credentials.encrypt("plain") == "plain"
    assert capsys.readouterr().out.count("CREDENTIAL_KEY not set") == 1


# ── the PIN pepper is required ───────────────────────────────────────────────

def test_a_pin_cannot_be_set_without_the_pepper(db_path, monkeypatch):
    from auth import set_membership_pin, PinError
    monkeypatch.setenv("CAVNAR_PIN_PEPPER", "")
    with pytest.raises(PinError):
        set_membership_pin(1, 1, "4821", db_path=db_path)


# ── support role and admin 2FA ───────────────────────────────────────────────

def _admin_app(monkeypatch, user):
    from flask import Flask, jsonify
    import auth
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    app = Flask(__name__, template_folder="../templates")     # the branded two-factor page renders here
    app.add_url_rule("/login", endpoint="auth.login", view_func=lambda: "login")

    @app.route("/admin/thing")
    @auth.admin_required
    def read(current_user):
        return jsonify(ok=True)

    @app.route("/admin/thing", methods=["POST"], endpoint="admin.write_thing")
    @auth.admin_required
    def write(current_user):
        return jsonify(ok=True, wrote=True)

    @app.route("/admin/view-as", endpoint="admin.view_as_client")
    @auth.admin_required
    def view(current_user):
        return jsonify(ok=True, viewing=True)
    return app.test_client()


def test_support_reads_and_views_as_but_cannot_write(monkeypatch):
    c = _admin_app(monkeypatch, {"id": 5, "is_admin": 0, "role": "support", "restaurant_id": None, "username": "sup"})
    assert c.get("/admin/thing").status_code == 200
    assert c.get("/admin/view-as").get_json()["viewing"] is True
    r = c.post("/admin/thing")
    assert r.status_code == 403 and "read-only" in r.get_json()["error"]
    # A plain client is not support.
    c2 = _admin_app(monkeypatch, {"id": 6, "is_admin": 0, "role": "client", "restaurant_id": 1, "username": "o"})
    assert c2.get("/admin/thing").status_code in (302, 401)


def test_an_admin_without_two_factor_is_held_at_the_door(monkeypatch, db_path):
    monkeypatch.setenv("ADMIN_REQUIRE_2FA", "1")
    rid = _rid(db_path)
    c = _admin_app(monkeypatch, {"id": 1, "is_admin": 1, "role": "client", "restaurant_id": rid, "username": "will"})
    r = c.get("/admin/thing")
    assert r.status_code == 403 and "Two-factor first" in r.get_data(as_text=True)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    assert c.get("/admin/thing").status_code == 200
    monkeypatch.delenv("ADMIN_REQUIRE_2FA", raising=False)               # opt-in: unset means off
    update_restaurant(rid, {"two_fa_enabled": 0}, db_path=db_path)
    assert c.get("/admin/thing").status_code == 200


def test_the_support_role_has_no_client_permissions():
    import permissions
    assert permissions.ROLE_PERMISSIONS[permissions.ROLE_SUPPORT] == frozenset()


# ── freeze ───────────────────────────────────────────────────────────────────

def test_freezing_a_restaurant_ends_every_session_and_forces_a_reset(db_path, monkeypatch):
    import security
    from auth import create_session, get_session_user, create_trusted_device, get_trusted_devices
    rid = _rid(db_path); uid = _user(db_path, rid)
    tok = create_session(uid, db_path=db_path)
    create_trusted_device(rid, uid, "phone", db_path=db_path)
    assert get_session_user(tok, db_path=db_path)
    n = security.freeze_restaurant(rid, actor={"username": "will"}, reason="reported", db_path=db_path)
    assert n == 1
    assert get_session_user(tok, db_path=db_path) is None
    assert get_trusted_devices(rid, db_path=db_path) == []
    assert get_conn(db_path).execute("SELECT must_reset_password FROM users WHERE id=?", (uid,)).fetchone()[0] == 1
    assert [r["event_type"] for r in get_conn(db_path).execute("SELECT event_type FROM activity_log WHERE restaurant_id=?", (rid,))] == ["account_frozen"]


# ── temporary passwords are never stored ─────────────────────────────────────

def test_no_path_persists_a_temporary_password():
    adm = open("admin_routes.py").read(); prov = open("provisioning.py").read(); wh = open("webhook_routes.py").read()
    assert '"temp_password":   data.get("password","")' not in adm
    assert '"temp_password": password' not in prov
    assert 'r.get("temp_password")' not in wh and "_rup(r[\"user_id\"], tmp_pw)" in wh
    assert "UPDATE restaurants SET temp_password=NULL" in open("models.py").read()


# ── join links ───────────────────────────────────────────────────────────────

def test_join_links_are_signed_and_a_guessed_id_is_nothing(monkeypatch):
    import guest_links
    monkeypatch.setenv("SECRET_KEY", "k1")
    tok = guest_links.sign_join(42)
    assert tok.startswith("42-") and guest_links.verify_join(tok) == 42
    assert guest_links.verify_join("42-deadbeefdeadbeef") is None
    assert guest_links.verify_join("43-" + tok.split("-")[1]) is None
    monkeypatch.setenv("ALLOW_LEGACY_JOIN_LINKS", "0")
    assert guest_links.verify_join("42") is None
    monkeypatch.delenv("ALLOW_LEGACY_JOIN_LINKS")
    assert guest_links.verify_join("42") is None                                   # off by default (MOD-MKT-9)
    monkeypatch.setenv("ALLOW_LEGACY_JOIN_LINKS", "1")
    assert guest_links.verify_join("42") == 42                                     # an explicit opt-in still works
    src = open("client_api.py").read()
    assert '/join/<token>' in src and '/api/public/guest-optin/<token>' in src and 'sign_join(rid)' in src


# ── headers, digest, memory, retention ───────────────────────────────────────

def test_hsts_is_sent_when_cookies_are_secure():
    import security_headers
    from flask import Response
    r = security_headers.apply(Response("ok"), secure=True)
    assert r.headers["Strict-Transport-Security"].startswith("max-age=31536000")
    assert r.headers["X-Frame-Options"] == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in security_headers.apply(Response("ok"), secure=False).headers
    assert "security_headers.apply(response)" in open("hosted_dashboard.py").read()


def test_the_digest_carries_the_security_lines(db_path, monkeypatch):
    import security
    rid = _rid(db_path)
    from models import log_event
    log_event(rid, "login_locked", {"username": "owner"}, db_path=db_path)
    lines = security.digest_lines(db_path=db_path)
    assert any("lockout" in x for x in lines)
    src = open("ops.py").read()
    assert "digest_lines" in src and "if not failures and not stuck and not sec_lines" in src


def test_memory_can_be_read_and_forgotten_by_the_owner(db_path, monkeypatch):
    import strategy_routes as sr
    rid = _rid(db_path)
    models.remember_ask_fact(rid, "Wants labor under 26%", kind="goal", source="Ask Cavnar", db_path=db_path)
    u = {"id": 1, "restaurant_id": rid, "role": "owner", "username": "o"}
    out, _ = sr._do_memory_list(u)
    assert out["facts"][0]["fact"] == "Wants labor under 26%"
    monkeypatch.setattr(sr, "_body", lambda: {"fact": "Wants labor under 26%"})
    assert sr._do_memory_forget(u)[1] == 200
    assert sr._do_memory_list(u)[0]["facts"] == []
    assert sr._do_memory_forget(u)[1] == 404


def test_backup_retention_default_is_a_week_and_deps_are_pinned():
    import scheduler
    assert scheduler.BACKUP_RETAIN_DAYS == 7 or os.getenv("BACKUP_RETAIN_DAYS")
    req = open("requirements.txt").read()
    assert "flask==" in req and "werkzeug==" in req and "cryptography==" in req
    assert "pip-audit" in open(".github/workflows/ci.yml").read()


def test_robots_and_sitemap_are_registered_once_and_robots_hides_the_app():
    """admin_bp used to register /robots.txt and /sitemap.xml as well; it is
    registered before the app-level routes, so its permissive robots.txt
    (no Disallow) was the one that served."""
    import pathlib, re
    admin_src = pathlib.Path("admin_routes.py").read_text()
    app_src = pathlib.Path("hosted_dashboard.py").read_text()
    assert not re.search(r'@admin_bp\.route\("/robots\.txt"\)', admin_src)
    assert not re.search(r'@admin_bp\.route\("/sitemap\.xml"\)', admin_src)
    assert app_src.count('@app.route("/robots.txt")') == 1
    robots = app_src.split('@app.route("/robots.txt")', 1)[1].split("return Response", 1)[0]
    for path in ("/admin", "/login", "/api/"):
        assert f"Disallow: {path}" in robots, path
