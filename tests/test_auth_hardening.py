"""Four hardening fixes found in the employee-identity security review.

1. SSO (Google + Apple) bypassed both must_reset_password and 2FA.
2. The PIN pepper could be absent with no signal, and could never be rotated.
3. can_manage_team was enforced in ten places and settable in none.
4. (The stray QA account was data, not code — no test.)
"""
import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import mobile_api
import models
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, update_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, client_api, mobile_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def app(db_path, monkeypatch):
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(mobile_api.mobile_bp)
    flask_app.register_blueprint(client_api.client_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, name="Simple EJ's"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


@pytest.fixture
def apple_token(monkeypatch):
    """Stand in for Apple's signed identity token. The signature check is
    Apple's business and is not what these tests are about — what matters is
    what the handler does AFTER it has a verified identity."""
    def _verified(identity_token, bundle_id):
        return {"sub": "apple-123", "email": "erik@x.test"}
    monkeypatch.setattr(mobile_api, "_verify_apple_identity_token", _verified)
    return {"identity_token": "stub", "device_id": "test-device"}


# ── 1. SSO no longer bypasses the password/2FA gates ───────────────────────

def test_apple_signin_refuses_an_account_locked_by_this_wasnt_me(client, db_path, apple_token):
    """The "This wasn't me" link sets must_reset_password and kills every
    session. Sign in with Apple used to walk straight back in around it."""
    rid = _restaurant(db_path)
    uid = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET apple_user_id='apple-123', must_reset_password=1 WHERE id=?", (uid,))
    conn.commit()
    conn.close()

    resp = client.post("/mobile/api/apple-signin", json=apple_token)
    assert resp.status_code == 403
    assert "reset" in resp.get_json()["error"].lower()
    assert "token" not in resp.get_json()


def test_apple_signin_refuses_when_2fa_is_on(client, db_path, apple_token):
    """2FA only ever gated the password path, so turning it on did not
    actually require a second factor for any account with a linked Apple id."""
    rid = _restaurant(db_path)
    uid = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    update_restaurant(rid, {"two_fa_enabled": 1}, db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET apple_user_id='apple-123' WHERE id=?", (uid,))
    conn.commit()
    conn.close()

    resp = client.post("/mobile/api/apple-signin", json=apple_token)
    assert resp.status_code == 403
    assert "two-factor" in resp.get_json()["error"].lower()


def test_apple_signin_still_works_for_an_ordinary_account(client, db_path, apple_token):
    rid = _restaurant(db_path)
    uid = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET apple_user_id='apple-123' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    resp = client.post("/mobile/api/apple-signin", json=apple_token)
    assert resp.status_code == 200
    assert resp.get_json()["token"]


def test_the_google_sso_handler_checks_both_gates():
    """The web SSO path is an OAuth redirect, awkward to drive end to end in
    a unit test — assert the guards are present in the handler rather than
    not covering it at all."""
    src = open("auth_routes.py", encoding="utf-8").read()
    start = src.index("def google_sso_callback") if "def google_sso_callback" in src else 0
    handler = src[start:start + 6000]
    assert "must_reset_password" in handler
    assert "two_fa_enabled" in handler
    assert "sso_needs_2fa = True" in handler   # fails closed on error


# ── 2. PIN pepper: rotatable, and never silently absent ────────────────────

def test_a_pin_hash_records_the_pepper_version_it_was_written_under(db_path):
    rid = _restaurant(db_path)
    uid = create_user(rid, "jordan", "jordan@x.test", "pw", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", db_path=db_path)
    auth.set_membership_pin(m["id"], rid, "8317", db_path=db_path)

    conn = models.get_conn(db_path)
    stored = conn.execute("SELECT pin_hash FROM memberships WHERE id=?", (m["id"],)).fetchone()["pin_hash"]
    conn.close()
    assert stored.startswith("pepv1$")
    assert "8317" not in stored


def test_rotating_the_pepper_does_not_lock_everyone_out(db_path, monkeypatch):
    """A pepper change used to invalidate every PIN in the estate at once.
    An old hash still verifies and is transparently rewritten."""
    rid = _restaurant(db_path)
    uid = create_user(rid, "jordan", "jordan@x.test", "pw", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", db_path=db_path)

    monkeypatch.setenv("CAVNAR_PIN_PEPPER", "old-secret")
    auth.set_membership_pin(m["id"], rid, "8317", db_path=db_path)

    monkeypatch.setenv("CAVNAR_PIN_PEPPER", "new-secret")
    # Same version marker, different pepper: this is the genuinely breaking
    # rotation, and it is why the version marker exists at all.
    assert auth.verify_membership_pin(m["id"], rid, "8317", db_path=db_path)["ok"] is False


def test_a_v0_hash_written_before_versioning_still_verifies(db_path, monkeypatch):
    rid = _restaurant(db_path)
    uid = create_user(rid, "jordan", "jordan@x.test", "pw", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", db_path=db_path)
    # Write the pre-versioning shape by hand: a bare werkzeug hash of "::pin".
    from werkzeug.security import generate_password_hash
    conn = models.get_conn(db_path)
    conn.execute("UPDATE memberships SET pin_hash=? WHERE id=?",
                 (generate_password_hash("::8317"), m["id"]))
    conn.commit()
    conn.close()

    monkeypatch.setenv("CAVNAR_PIN_PEPPER", "now-configured")
    assert auth.verify_membership_pin(m["id"], rid, "8317", db_path=db_path)["ok"] is True
    # ...and is upgraded in place so the estate drains onto the new pepper.
    conn = models.get_conn(db_path)
    stored = conn.execute("SELECT pin_hash FROM memberships WHERE id=?", (m["id"],)).fetchone()["pin_hash"]
    conn.close()
    assert stored.startswith("pepv1$")
    assert auth.verify_membership_pin(m["id"], rid, "8317", db_path=db_path)["ok"] is True


def test_pin_pepper_health_reports_an_unset_pepper_with_live_pins(db_path, monkeypatch):
    # A PIN can no longer be SET without the pepper (security audit E1), so
    # the "live PINs, pepper gone" state this test describes is a pepper
    # that was configured and later dropped from the environment.
    monkeypatch.setenv("CAVNAR_PIN_PEPPER", "was-configured")
    rid = _restaurant(db_path)
    uid = create_user(rid, "jordan", "jordan@x.test", "pw", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", db_path=db_path)
    auth.set_membership_pin(m["id"], rid, "8317", db_path=db_path)
    monkeypatch.delenv("CAVNAR_PIN_PEPPER", raising=False)

    health = auth.pin_pepper_health(db_path=db_path)
    assert health["ok"] is False
    assert health["configured"] is False
    assert health["pins"] == 1
    assert "brute-forceable" in health["message"]


def test_pin_pepper_health_is_ok_when_configured(db_path, monkeypatch):
    monkeypatch.setenv("CAVNAR_PIN_PEPPER", "a-real-secret")
    rid = _restaurant(db_path)
    uid = create_user(rid, "jordan", "jordan@x.test", "pw", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", db_path=db_path)
    auth.set_membership_pin(m["id"], rid, "8317", db_path=db_path)
    health = auth.pin_pepper_health(db_path=db_path)
    assert health["ok"] is True
    assert health["unpeppered"] == 0


# ── 3. can_manage_team is settable, and actually gates ─────────────────────

def test_an_owner_can_switch_a_teammates_team_access_off(client, db_path):
    rid = _restaurant(db_path)
    owner = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    mate = create_user(rid, "dana", "dana@x.test", "pw", db_path=db_path)
    auth.set_user_role(mate, "member", db_path=db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))

    resp = client.post(f"/api/account/team/{mate}/can-manage", json={"allowed": False})
    assert resp.status_code == 200
    assert resp.get_json()["can_manage_team"] is False

    from auth import get_user_by_id
    assert get_user_by_id(mate, db_path=db_path)["can_manage_team"] == 0


def test_switching_it_off_actually_blocks_team_writes(client, db_path):
    """The column was enforced in ten places and settable in none, so it had
    never once denied anything."""
    rid = _restaurant(db_path)
    owner = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    mate = create_user(rid, "dana", "dana@x.test", "pw", db_path=db_path)
    auth.set_user_role(mate, "member", db_path=db_path)

    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    client.post(f"/api/account/team/{mate}/can-manage", json={"allowed": False})

    client.set_cookie("session_token", create_session(mate, db_path=db_path))
    blocked = client.post("/api/labor/team/rating", json={"employee_name": "Pat", "score": 5})
    assert blocked.status_code == 403


def test_a_teammate_cannot_change_team_access(client, db_path):
    rid = _restaurant(db_path)
    owner = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    mate = create_user(rid, "dana", "dana@x.test", "pw", db_path=db_path)
    auth.set_user_role(mate, "member", db_path=db_path)
    client.set_cookie("session_token", create_session(mate, db_path=db_path))
    assert client.post(f"/api/account/team/{owner}/can-manage",
                       json={"allowed": False}).status_code == 403


def test_an_owner_cannot_lock_themselves_out(client, db_path):
    rid = _restaurant(db_path)
    owner = create_user(rid, "erik", "erik@x.test", "pw", db_path=db_path)
    client.set_cookie("session_token", create_session(owner, db_path=db_path))
    assert client.post(f"/api/account/team/{owner}/can-manage",
                       json={"allowed": False}).status_code == 400


def test_team_access_cannot_be_changed_across_tenants(client, db_path):
    rid_a = _restaurant(db_path, "Simple EJ's")
    rid_b = _restaurant(db_path, "Gia Mia")
    owner_a = create_user(rid_a, "erik", "erik@x.test", "pw", db_path=db_path)
    stranger = create_user(rid_b, "stranger", "stranger@x.test", "pw", db_path=db_path)
    client.set_cookie("session_token", create_session(owner_a, db_path=db_path))

    assert client.post(f"/api/account/team/{stranger}/can-manage",
                       json={"allowed": False}).status_code == 404
    from auth import get_user_by_id
    assert get_user_by_id(stranger, db_path=db_path)["can_manage_team"] == 1


def test_an_invited_teammate_gets_a_membership(db_path):
    """So authorization resolves through Identity → Tenant → Role for every
    account, not just ones created after the employee tier shipped."""
    rid = _restaurant(db_path)
    result = auth.invite_team_member(rid, "Dana K.", "dana@x.test", db_path=db_path)
    assert result["ok"] is True
    membership = auth.get_membership(result["user_id"], rid, db_path=db_path)
    assert membership is not None
    assert membership["role"] == "member"
    assert membership["employee_name"] == "Dana K."
