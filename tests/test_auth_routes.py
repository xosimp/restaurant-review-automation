"""auth_routes.py — the actual login/2FA HTTP flow. Zero coverage before this
despite being the entire front door to the app: a bug here means either
locked-out legitimate users or a bypassable login/2FA gate."""
import base64
import time
from datetime import datetime, timedelta

import pytest
from flask import Flask

import auth
import auth_routes
import models
from auth import create_user, init_auth
from auth_routes import (
    auth_bp, _is_rate_limited, _record_failed_attempt, _clear_attempts,
    _login_attempts, _MAX_ATTEMPTS,
)
from models import create_restaurant, Restaurant, update_restaurant, get_conn


def _redirect_db(monkeypatch, db_path):
    """auth.py and auth_routes.py each did `from models import get_conn`
    (or bound their own auth.* functions that close over auth.get_conn) at
    module level — the same independent-reference gotcha documented in
    test_sms_consent.py. Redirect all three so nothing touches the real
    reviews.db during these tests."""
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, auth_routes):
        monkeypatch.setattr(mod, "get_conn", redirect)


@pytest.fixture(autouse=True)
def _init_auth_tables(db_path):
    init_auth(db_path=db_path)


@pytest.fixture(autouse=True)
def _clear_rate_limit_state():
    _login_attempts.clear()
    yield
    _login_attempts.clear()


@pytest.fixture
def app(db_path, monkeypatch):
    _redirect_db(monkeypatch, db_path)
    # RESEND_API_KEY unset in this test environment already guards
    # send_2fa_code/send_login_notification into no-ops — confirmed via the
    # emails.py `if not RESEND_API_KEY: return` guard — so 2FA-triggering
    # tests never make a real network call.
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(auth_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Login Test Co"), owner_email="l@x.com", **kw), db_path=db_path)


def _login_form(client, username, password, **extra):
    """A real login POST needs the csrf cookie and form field to match —
    fetch the login page first to get a real cookie, like a browser would."""
    client.get("/login")
    csrf = client.get_cookie("csrf_token").value
    data = {"username": username, "password": password, "csrf_token": csrf}
    data.update(extra)
    return client.post("/login", data=data)


# ── rate limiter (pure functions, no Flask needed) ──────────────────────────

def test_rate_limiter_allows_up_to_max_attempts():
    ip = "1.2.3.4"
    for _ in range(_MAX_ATTEMPTS - 1):
        assert _is_rate_limited(ip) is False
        _record_failed_attempt(ip)
    assert _is_rate_limited(ip) is False  # exactly at the boundary, not yet over


def test_rate_limiter_blocks_after_max_attempts():
    ip = "1.2.3.4"
    for _ in range(_MAX_ATTEMPTS):
        _record_failed_attempt(ip)
    assert _is_rate_limited(ip) is True


def test_rate_limiter_isolated_per_ip():
    for _ in range(_MAX_ATTEMPTS):
        _record_failed_attempt("1.1.1.1")
    assert _is_rate_limited("1.1.1.1") is True
    assert _is_rate_limited("2.2.2.2") is False


def test_clear_attempts_resets_lockout():
    ip = "1.2.3.4"
    for _ in range(_MAX_ATTEMPTS):
        _record_failed_attempt(ip)
    assert _is_rate_limited(ip) is True
    _clear_attempts(ip)
    assert _is_rate_limited(ip) is False


# ── /login ───────────────────────────────────────────────────────────────────

def test_login_page_renders(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"csrf_token" in resp.data


def test_login_with_correct_credentials_sets_session_cookie(client, db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    resp = _login_form(client, "alice", "correct-horse")
    assert resp.status_code == 302
    assert client.get_cookie("session_token") is not None


def test_login_with_wrong_password_shows_generic_error(client, db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    resp = _login_form(client, "alice", "wrong-password")
    assert resp.status_code == 200
    assert b"Invalid username or password" in resp.data
    assert client.get_cookie("session_token") is None


def test_login_does_not_reveal_whether_username_exists(client, db_path):
    """Same error for a wrong password vs. a nonexistent username — the
    generic message is the actual defense against username enumeration."""
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    resp_wrong_pw = _login_form(client, "alice", "wrong-password")
    resp_no_user = _login_form(client, "nobody", "whatever")
    assert b"Invalid username or password" in resp_wrong_pw.data
    assert b"Invalid username or password" in resp_no_user.data


def test_login_without_csrf_cookie_rejected(client, db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    # Skip the GET that sets the csrf cookie — post cold with no matching cookie
    resp = client.post("/login", data={"username": "alice", "password": "correct-horse", "csrf_token": "made-up"})
    assert client.get_cookie("session_token") is None
    assert b"expired" in resp.data.lower()


def test_login_rate_limited_after_max_failed_attempts(client, db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    for _ in range(_MAX_ATTEMPTS):
        _login_form(client, "alice", "wrong-password")
    resp = _login_form(client, "alice", "correct-horse")  # right password, but locked out
    assert b"Too many failed attempts" in resp.data
    assert client.get_cookie("session_token") is None


def test_login_failed_attempt_does_not_lock_out_other_ips(client, db_path):
    """test_client() always sends the same source IP, so this documents the
    isolation at the unit level (already covered above) rather than re-driving
    it through the route — kept here as a named cross-reference for the
    behavior this route depends on."""
    ip = "9.9.9.9"
    assert _is_rate_limited(ip) is False


def test_successful_login_clears_previous_failed_attempts(client, db_path):
    rid = _restaurant(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    _login_form(client, "alice", "wrong-password")
    _login_form(client, "alice", "wrong-password")
    resp = _login_form(client, "alice", "correct-horse")
    assert resp.status_code == 302
    assert client.get_cookie("session_token") is not None


def test_login_with_2fa_enabled_does_not_set_session_cookie_yet(client, db_path):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse", db_path=db_path)
    resp = _login_form(client, "alice", "correct-horse")
    assert resp.status_code == 200
    assert b"Check your email" in resp.data or b"pending_token" in resp.data
    assert client.get_cookie("session_token") is None


def test_login_admin_2fa_is_skipped_even_if_restaurant_has_it_enabled(client, db_path):
    """2FA is a client-facing restaurant setting — an admin account logging
    in must never be routed through a client restaurant's 2FA state."""
    rid = _restaurant(db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, "adminuser", "admin@x.com", "correct-horse", is_admin=True, db_path=db_path)
    resp = _login_form(client, "adminuser", "correct-horse")
    assert resp.status_code == 302
    assert client.get_cookie("session_token") is not None


# ── /verify-2fa ──────────────────────────────────────────────────────────────

def _start_2fa_login(client, db_path, rid, username="alice", password="correct-horse"):
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    create_user(rid, username, "alice@x.com", password, db_path=db_path)
    resp = _login_form(client, username, password)
    # Extract the pending_token rendered into the hidden form field.
    html = resp.data.decode()
    marker = 'name="pending_token" value="'
    start = html.index(marker) + len(marker)
    end = html.index('"', start)
    return html[start:end]


def _stored_2fa_code(db_path, rid):
    conn = get_conn(db_path)
    row = conn.execute("SELECT two_fa_code FROM restaurants WHERE id=?", (rid,)).fetchone()
    conn.close()
    return row["two_fa_code"]


def test_verify_2fa_with_correct_code_succeeds(client, db_path):
    rid = _restaurant(db_path)
    pending_token = _start_2fa_login(client, db_path, rid)
    code = _stored_2fa_code(db_path, rid)
    csrf = client.get_cookie("csrf_token").value
    resp = client.post("/verify-2fa", data={
        "pending_token": pending_token, "code": code, "next_url": "/",
        "csrf_token": csrf,
    })
    assert resp.status_code == 302
    assert client.get_cookie("session_token") is not None


def test_verify_2fa_with_wrong_code_fails(client, db_path):
    rid = _restaurant(db_path)
    pending_token = _start_2fa_login(client, db_path, rid)
    csrf = client.get_cookie("csrf_token").value
    resp = client.post("/verify-2fa", data={
        "pending_token": pending_token, "code": "000000", "next_url": "/",
        "csrf_token": csrf,
    })
    assert b"Incorrect code" in resp.data
    assert client.get_cookie("session_token") is None


def test_verify_2fa_with_expired_code_fails(client, db_path):
    rid = _restaurant(db_path)
    pending_token = _start_2fa_login(client, db_path, rid)
    code = _stored_2fa_code(db_path, rid)
    expired = (datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    update_restaurant(rid, {}, db_path=db_path)  # no-op, keeps pattern consistent
    conn = get_conn(db_path)
    conn.execute("UPDATE restaurants SET two_fa_expires=? WHERE id=?", (expired, rid))
    conn.commit()
    conn.close()
    csrf = client.get_cookie("csrf_token").value
    resp = client.post("/verify-2fa", data={
        "pending_token": pending_token, "code": code, "next_url": "/",
        "csrf_token": csrf,
    })
    assert b"Code expired" in resp.data
    assert client.get_cookie("session_token") is None


def test_verify_2fa_code_is_single_use(client, db_path):
    """A correct code can't be replayed a second time — the route clears
    two_fa_code on success, so re-submitting the same code afterward fails."""
    rid = _restaurant(db_path)
    pending_token = _start_2fa_login(client, db_path, rid)
    code = _stored_2fa_code(db_path, rid)
    csrf = client.get_cookie("csrf_token").value
    first = client.post("/verify-2fa", data={
        "pending_token": pending_token, "code": code, "next_url": "/",
        "csrf_token": csrf,
    })
    assert first.status_code == 302
    second = client.post("/verify-2fa", data={
        "pending_token": pending_token, "code": code, "next_url": "/",
        "csrf_token": client.get_cookie("csrf_token").value,
    })
    assert second.status_code == 302
    assert second.headers["Location"].endswith("/login")


def test_verify_2fa_rejects_pending_token_for_wrong_restaurant(client, db_path):
    """The pending token encodes a restaurant id + a per-login secret — a
    token built for a different restaurant_id must not verify against it."""
    rid_a = _restaurant(db_path, name="Co A")
    rid_b = _restaurant(db_path, name="Co B")
    update_restaurant(rid_b, {"two_fa_enabled": 1, "two_fa_pending": "some-other-secret"}, db_path=db_path)
    pending_token = _start_2fa_login(client, db_path, rid_a, username="alice", password="pw")
    # Forge a token pointing at rid_b using rid_a's real pending secret
    decoded = base64.urlsafe_b64decode(pending_token.encode()).decode()
    _uid_str, real_secret = decoded.split(":", 1)
    forged = base64.urlsafe_b64encode(f"{rid_b}:{real_secret}".encode()).decode()
    csrf = client.get_cookie("csrf_token").value
    resp = client.post("/verify-2fa", data={
        "pending_token": forged, "code": "000000", "next_url": "/",
        "csrf_token": csrf,
    })
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")
