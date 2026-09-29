"""Fix round A, #147 (SECURITY-14) and the step-up contract.

  * sessions.expires_at is written in SQLite's own UTC form and compared as
    an instant: ISO text with a 'T' compared as a string outlived its expiry
    to the end of that UTC day. Rows written the old way are rewritten at
    boot, and the lookup tolerates any left.
  * an admin (or support) session lasts ADMIN_SESSION_HOURS from sign-in, on
    every device, rows from before included;
  * a support view-as is read-only on the session row itself — pruning
    view_as_sessions can no longer make it writable;
  * auth.recent_auth_required + POST /admin/api/reauth: a sensitive admin
    action needs the password typed within RECENT_AUTH_MINUTES.
"""
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import models
from auth import create_session, create_user, get_session_user, hash_session_token, init_auth
from models import Restaurant, create_restaurant

CSRF = "fix-a-sessions-csrf"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.delenv("ADMIN_REQUIRE_2FA", raising=False)
    init_auth(db_path=db_path)


def _rid(db_path, name="Co"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _q(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _exec(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


# ── one expiry format ───────────────────────────────────────────────────────

def test_expiry_is_written_in_sqlites_own_utc_form(db_path):
    uid = create_user(_rid(db_path), "o", "o@x.test", "Owner-pass-2026", db_path=db_path)
    tok = create_session(uid, db_path=db_path)
    exp = _q(db_path, "SELECT expires_at FROM sessions WHERE token=?", (hash_session_token(tok),))[0]
    assert "T" not in exp and len(exp) == 19
    assert _q(db_path, "SELECT datetime(?) = ?", (exp, exp))[0] == 1


def test_a_session_ends_at_its_expiry_not_at_the_end_of_that_utc_day(db_path):
    """The bug: '2026-09-29T08:00:00+00:00' > '2026-09-29 21:00:00' as text."""
    uid = create_user(_rid(db_path), "o", "o@x.test", "Owner-pass-2026", db_path=db_path)
    tok = create_session(uid, db_path=db_path)
    past_iso = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()   # same UTC day, 'T' form
    _exec(db_path, "UPDATE sessions SET expires_at=? WHERE token=?", (past_iso, hash_session_token(tok)))
    assert get_session_user(tok, db_path=db_path) is None


def test_boot_rewrites_old_iso_rows_and_ends_legacy_view_as_sessions(db_path):
    rid = _rid(db_path)
    uid = create_user(rid, "o", "o@x.test", "Owner-pass-2026", db_path=db_path)
    keep = create_session(uid, db_path=db_path)
    old_view = create_session(uid, db_path=db_path)
    future_iso = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    _exec(db_path, "UPDATE sessions SET expires_at=?, last_active=? WHERE token=?",
          (future_iso, datetime.now(timezone.utc).isoformat(), hash_session_token(keep)))
    _exec(db_path, "UPDATE sessions SET device_type='admin-view-as' WHERE token=?", (hash_session_token(old_view),))
    out = auth.normalize_session_rows(db_path=db_path)
    assert out["expiry_fixed"] >= 1 and out["view_as_ended"] == 1
    exp = _q(db_path, "SELECT expires_at FROM sessions WHERE token=?", (hash_session_token(keep),))[0]
    assert "T" not in exp
    assert get_session_user(keep, db_path=db_path) is not None
    assert _q(db_path, "SELECT COUNT(*) FROM sessions WHERE device_type='admin-view-as'")[0] == 0
    assert auth.normalize_session_rows(db_path=db_path)["expiry_fixed"] == 0      # idempotent


# ── admin sessions: 12 hours absolute ───────────────────────────────────────

def test_an_admin_session_is_created_for_twelve_hours_whatever_was_asked(db_path):
    home = _rid(db_path, "Cavnar AI Admin")
    admin = create_user(home, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    tok = create_session(admin, days=30, device_type="ios", db_path=db_path)
    exp = datetime.fromisoformat(_q(db_path, "SELECT expires_at FROM sessions WHERE token=?",
                                    (hash_session_token(tok),))[0])
    left = exp - datetime.utcnow()
    assert timedelta(hours=auth.ADMIN_SESSION_HOURS) - timedelta(minutes=5) < left <= timedelta(hours=auth.ADMIN_SESSION_HOURS)


def test_an_older_admin_session_is_refused_even_with_a_long_expiry(db_path):
    """Rows from before the cap (30-day admin sessions) are held to it."""
    home = _rid(db_path, "Cavnar AI Admin")
    admin = create_user(home, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    tok = create_session(admin, db_path=db_path)
    _exec(db_path, "UPDATE sessions SET created_at=datetime('now','-13 hours'), expires_at=datetime('now','+20 days') "
                   "WHERE token=?", (hash_session_token(tok),))
    assert get_session_user(tok, db_path=db_path) is None
    assert _q(db_path, "SELECT COUNT(*) FROM sessions WHERE token=?", (hash_session_token(tok),))[0] == 0


def test_a_clients_session_keeps_its_thirty_days(db_path):
    uid = create_user(_rid(db_path), "o", "o@x.test", "Owner-pass-2026", db_path=db_path)
    tok = create_session(uid, device_type="ios", db_path=db_path)
    _exec(db_path, "UPDATE sessions SET created_at=datetime('now','-13 hours') WHERE token=?", (hash_session_token(tok),))
    assert get_session_user(tok, db_path=db_path) is not None


def test_boot_caps_existing_admin_sessions(db_path):
    home = _rid(db_path, "Cavnar AI Admin")
    admin = create_user(home, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    tok = create_session(admin, db_path=db_path)
    _exec(db_path, "UPDATE sessions SET expires_at=datetime('now','+29 days') WHERE token=?", (hash_session_token(tok),))
    assert auth.normalize_session_rows(db_path=db_path)["internal_capped"] == 1
    exp = datetime.fromisoformat(_q(db_path, "SELECT expires_at FROM sessions WHERE token=?",
                                    (hash_session_token(tok),))[0])
    assert exp - datetime.utcnow() <= timedelta(hours=auth.ADMIN_SESSION_HOURS)


# ── a support view-as is read-only on the session row ───────────────────────

def test_a_support_view_as_stays_read_only_when_view_as_sessions_is_pruned(db_path):
    rid = _rid(db_path)
    owner = create_user(rid, "o", "o@x.test", "Owner-pass-2026", db_path=db_path)
    hq = _rid(db_path, "Cavnar AI Admin")
    sup = create_user(hq, "sup", "s@x.test", "Support-pass-2026", role="support", db_path=db_path)
    tok = auth.create_view_as_session(owner, {"id": sup, "is_admin": 0}, read_only=True, db_path=db_path)
    _exec(db_path, "DELETE FROM view_as_sessions")                     # what the 2-day prune did
    user = get_session_user(tok, db_path=db_path)
    assert user["view_as_read_only"] is True and user["acting_admin_id"] == sup


def test_a_view_as_ends_when_the_admin_behind_it_loses_access(db_path):
    rid = _rid(db_path)
    owner = create_user(rid, "o", "o@x.test", "Owner-pass-2026", db_path=db_path)
    hq = _rid(db_path, "Cavnar AI Admin")
    admin = create_user(hq, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    tok = auth.create_view_as_session(owner, {"id": admin, "is_admin": 1}, read_only=False, db_path=db_path)
    assert get_session_user(tok, db_path=db_path)["acting_admin"] == "will"
    _exec(db_path, "UPDATE users SET is_admin=0 WHERE id=?", (admin,))
    assert get_session_user(tok, db_path=db_path) is None


# ── step-up ─────────────────────────────────────────────────────────────────

def _stepup_app(monkeypatch, user):
    from flask import jsonify
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    app = Flask(__name__)
    app.add_url_rule("/login", endpoint="auth.login", view_func=lambda: "login")

    @app.route("/admin/danger", methods=["POST"], endpoint="admin.danger")
    @auth.admin_required
    @auth.recent_auth_required()
    def danger(current_user):
        return jsonify(ok=True)
    return app.test_client()


def test_a_sensitive_action_needs_a_recent_password(monkeypatch):
    base = {"id": 1, "is_admin": 1, "username": "will"}
    r = _stepup_app(monkeypatch, dict(base)).post("/admin/danger")
    assert r.status_code == 403 and r.get_json()["reauth_required"] is True
    stale = auth.sql_utc(datetime.now(timezone.utc) - timedelta(minutes=auth.RECENT_AUTH_MINUTES + 1))
    assert _stepup_app(monkeypatch, dict(base, reauth_at=stale)).post("/admin/danger").status_code == 403
    fresh = auth.sql_utc()
    assert _stepup_app(monkeypatch, dict(base, reauth_at=fresh)).post("/admin/danger").status_code == 200


def _admin_app():
    a = Flask(__name__, template_folder="../templates")
    a.secret_key = "fix-a"
    a.register_blueprint(auth_routes.auth_bp)
    a.register_blueprint(admin_routes.admin_bp)
    return a


def test_reauth_stamps_the_session_and_a_wrong_password_does_not(db_path):
    home = _rid(db_path, "Cavnar AI Admin")
    admin = create_user(home, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    tok = create_session(admin, db_path=db_path)                        # no password typed on this session
    c = _admin_app().test_client()
    c.set_cookie("session_token", tok)
    c.set_cookie("csrf_js", CSRF)
    bad = c.post("/admin/api/reauth", json={"password": "nope"}, headers={"X-CSRF": CSRF})
    assert bad.status_code == 403
    assert _q(db_path, "SELECT reauth_at FROM sessions WHERE token=?", (hash_session_token(tok),))[0] is None
    ok = c.post("/admin/api/reauth", json={"password": "Admin-pass-2026"}, headers={"X-CSRF": CSRF})
    assert ok.status_code == 200 and ok.get_json()["ok"] is True
    user = get_session_user(tok, db_path=db_path)
    assert auth.reauth_is_recent(user)


def test_five_wrong_step_up_passwords_end_the_session(db_path):
    home = _rid(db_path, "Cavnar AI Admin")
    admin = create_user(home, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    tok = create_session(admin, db_path=db_path)
    c = _admin_app().test_client()
    c.set_cookie("session_token", tok)
    c.set_cookie("csrf_js", CSRF)
    for _ in range(admin_routes.REAUTH_MAX_MISSES - 1):
        assert c.post("/admin/api/reauth", json={"password": "x"}, headers={"X-CSRF": CSRF}).status_code == 403
    last = c.post("/admin/api/reauth", json={"password": "x"}, headers={"X-CSRF": CSRF})
    assert last.status_code == 401 and last.get_json()["session_expired"] is True
    assert get_session_user(tok, db_path=db_path) is None


def test_a_password_sign_in_counts_as_a_recent_password(db_path):
    home = _rid(db_path, "Cavnar AI Admin")
    admin = create_user(home, "will", "w@x.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    tok = create_session(admin, password_verified_at=True, db_path=db_path)
    assert auth.reauth_is_recent(get_session_user(tok, db_path=db_path))
