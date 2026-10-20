"""Blind re-audit 10/20/26, R7 (multi-tenant security) — the fix round's tests.

Each section names the finding it holds (/Users/simp/scale-reaudit-1020/R7_security.md).
"""
import os

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import mobile_api
import models
import security
from auth import create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, mobile_api, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    auth_routes._login_attempts.clear()
    yield
    auth_routes._login_attempts.clear()


@pytest.fixture
def sent(monkeypatch):
    import emails
    import notify
    box = {"email": [], "sms": [], "notice": []}
    monkeypatch.setattr(emails, "send_2fa_code",
                        lambda to, name, code, owner=None, **k: box["email"].append((to, code)) or True)
    monkeypatch.setattr(notify, "send_2fa_sms", lambda to, name, code: box["sms"].append((to, code)) or True)
    monkeypatch.setattr(emails, "send_security_notice_email",
                        lambda to, name, kind, detail=None, **k: box["notice"].append((to, kind, detail)) or True)
    return box


@pytest.fixture
def client():
    app = Flask(__name__, template_folder=TEMPLATES)
    app.secret_key = "test-secret"
    for bp in (auth_routes.auth_bp, mobile_api.mobile_bp, client_api.client_bp):
        app.register_blueprint(bp)
    return app.test_client()


def _setup(db_path, two_fa=True):
    rid = create_restaurant(Restaurant(name="R", owner_email="owner@x.test"), db_path=db_path)
    owner = create_user(rid, "owner", "owner@x.test", "ownerpass1", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    if two_fa:
        update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    return rid, owner


def _phone_login(client, username="owner", password="ownerpass1"):
    return client.post("/mobile/api/login", json={"username": username, "password": password}).get_json()


# ── R7-01: a challenge dies after N wrong codes; the login locks ────────────

def test_a_challenge_dies_on_its_fifth_wrong_code(db_path):
    rid, owner = _setup(db_path)
    pending, code = auth.issue_two_fa_challenge(rid, owner, db_path=db_path)
    wrong = "100000" if code != "100000" else "100001"
    for _ in range(auth.TWO_FA_CHALLENGE_MAX_WRONG - 1):
        assert auth.check_two_fa_code(rid, owner, wrong, pending=pending, consume=False, db_path=db_path) == "wrong"
    assert auth.two_fa_challenge_exists(rid, owner, pending, db_path=db_path)
    assert auth.check_two_fa_code(rid, owner, wrong, pending=pending, consume=False, db_path=db_path) == "wrong"
    # Gone: the right code no longer passes; the sign-in starts again.
    assert not auth.two_fa_challenge_exists(rid, owner, pending, db_path=db_path)
    assert auth.check_two_fa_code(rid, owner, code, pending=pending, db_path=db_path) == "missing"


def test_a_resend_keeps_the_wrong_count(db_path):
    rid, owner = _setup(db_path)
    pending, code = auth.issue_two_fa_challenge(rid, owner, db_path=db_path)
    for _ in range(auth.TWO_FA_CHALLENGE_MAX_WRONG - 1):
        auth.check_two_fa_code(rid, owner, "000000", pending=pending, consume=False, db_path=db_path)
    new_code = auth.reissue_two_fa_code(rid, owner, pending, db_path=db_path)
    assert new_code
    bad = "000000" if new_code != "000000" else "000001"
    assert auth.check_two_fa_code(rid, owner, bad, pending=pending, consume=False, db_path=db_path) == "wrong"
    assert not auth.two_fa_challenge_exists(rid, owner, pending, db_path=db_path)


def test_the_login_locks_after_a_days_wrong_codes_from_any_address(db_path, client, sent):
    rid, owner = _setup(db_path)
    for i in range(security.SECOND_FACTOR_DAY_MAX):
        r = _phone_login(client)
        auth_routes._login_attempts.clear()
        security.clear_login_failures(ip="2fa:127.0.0.1", db_path=db_path)   # a fresh address each time
        v = client.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": "000000"})
        assert v.status_code == 401, i
    # Locked: even the right code is refused, from a clean address.
    security.clear_login_failures(ip="2fa:127.0.0.1", db_path=db_path)
    r = _phone_login(client)
    right = sent["email"][-1][1]
    v = client.post("/mobile/api/verify-2fa", json={"pending_token": r["pending_token"], "code": right})
    assert v.status_code == 429 and v.get_json()["second_factor_locked"] is True
    # Told once, at the lock.
    assert [k for _to, k, _d in sent["notice"]] == ["two_factor_locked"]
    ev = [e for e in models.get_conn(db_path).execute(
        "SELECT event_type FROM activity_log WHERE restaurant_id=?", (rid,)).fetchall()]
    assert any(e["event_type"] == "two_factor_locked" for e in ev)
    # The console's Clear lockout (and the break-glass unlock) lifts it.
    assert security.lockout_state("owner", db_path=db_path)["second_factor_seconds_left"] > 0
    assert any(s["username"] == "owner" for s in security.active_lockouts(db_path=db_path))
    security.clear_account_lock("owner", db_path=db_path)
    assert auth.second_factor_lock_left(owner, db_path=db_path) == 0


def test_the_web_second_step_honours_the_login_lock(db_path, client, sent):
    rid, owner = _setup(db_path)
    for _ in range(security.SECOND_FACTOR_DAY_MAX):
        security.record_second_factor_failure(owner, "1.2.3.4", db_path=db_path)
    client.get("/login")
    csrf = client.get_cookie("csrf_token").value
    html = client.post("/login", data={"username": "owner", "password": "ownerpass1",
                                       "csrf_token": csrf}).get_data(as_text=True)
    marker = 'name="pending_token" value="'
    start = html.index(marker) + len(marker)
    pending = html[start:html.index('"', start)]
    csrf = client.get_cookie("csrf_token").value
    resp = client.post("/verify-2fa", data={"pending_token": pending, "code": sent["email"][-1][1],
                                            "next_url": "/", "csrf_token": csrf})
    assert resp.status_code == 429 and "Too many wrong codes" in resp.get_data(as_text=True)
    assert not client.get_cookie("session_token")
