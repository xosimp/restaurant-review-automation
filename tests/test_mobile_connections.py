"""mobile_api.py's new Account-tab routes: self-service profile edits
(owner contact + AI-voice notes only, never the admin-set identity fields),
Toast credential connect/disconnect, and the Google Business mobile OAuth
handshake (gmb.sign_mobile_state/verify_mobile_state plus the authorize/
disconnect routes — the callback itself lives in auth_routes.py and needs
a live Google exchange to test past state verification, so it's covered
only up to that boundary here)."""
import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import mobile_api
import models
import notify
import guest_marketing
import value_delivered
from auth import create_user, init_auth
from auth_routes import _login_attempts
from mobile_api import mobile_bp
from models import create_restaurant, Restaurant, get_restaurant


def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, auth_routes, client_api, mobile_api, notify, guest_marketing, value_delivered):
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
    flask_app = Flask(__name__)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    return create_restaurant(
        Restaurant(name=kw.pop("name", "Connections Test Co"), owner_email="c@x.com", **kw), db_path=db_path
    )


def _login(client, db_path, rid, username="alice", password="correct-horse"):
    create_user(rid, username, f"{username}@x.com", password, db_path=db_path)
    resp = client.post("/mobile/api/login", json={"username": username, "password": password})
    return resp.get_json()["token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


# ── Profile self-service edit ──────────────────────────────────────────

def test_update_profile_saves_editable_fields(client, db_path):
    rid = _restaurant(db_path, neighborhood="Old Town", known_for="Deep dish")
    token = _login(client, db_path, rid)

    resp = client.post("/mobile/api/account/update-profile", headers=_auth_headers(token), json={
        "owner_name": "Jamie Rivera", "owner_phone": "312-555-0100",
        "voice_notes": "Warm, a little playful", "never_say": "cheap",
        "menu_notes": "Try the deep dish",
    })
    assert resp.get_json()["ok"] is True

    r = get_restaurant(rid, db_path=db_path)
    assert r.owner_name == "Jamie Rivera"
    assert r.owner_phone == "312-555-0100"
    assert r.voice_notes == "Warm, a little playful"
    assert r.never_say == "cheap"
    assert r.menu_notes == "Try the deep dish"


def test_update_profile_cannot_touch_admin_set_identity_fields(client, db_path):
    """owner_name/etc. are the only fields this route accepts — confirms
    a client can't smuggle restaurant_name/neighborhood/known_for through
    this endpoint even if a future caller starts sending them, since those
    feed exact-string-match parsing elsewhere in client_api.py."""
    rid = _restaurant(db_path, neighborhood="Old Town", known_for="Deep dish")
    token = _login(client, db_path, rid)

    client.post("/mobile/api/account/update-profile", headers=_auth_headers(token), json={
        "owner_name": "Jamie Rivera",
        "neighborhood": "Smuggled Neighborhood",
        "known_for": "Smuggled known-for",
    })

    r = get_restaurant(rid, db_path=db_path)
    assert r.owner_name == "Jamie Rivera"
    assert r.neighborhood == "Old Town"
    assert r.known_for == "Deep dish"


def test_update_profile_strips_html_tags(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    client.post("/mobile/api/account/update-profile", headers=_auth_headers(token), json={
        "voice_notes": "<script>alert(1)</script>Warm and friendly",
    })
    r = get_restaurant(rid, db_path=db_path)
    assert "<script>" not in r.voice_notes
    assert "Warm and friendly" in r.voice_notes


# ── Toast connect/disconnect ────────────────────────────────────────────

def test_connect_toast_saves_credentials(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("toast.get_toast_token", lambda rid: "fake-token")

    resp = client.post("/mobile/api/connections/toast", headers=_auth_headers(token), json={
        "toast_client_id": "cid", "toast_client_secret": "csecret", "toast_restaurant_guid": "guid-1",
    })
    assert resp.get_json()["ok"] is True

    r = get_restaurant(rid, db_path=db_path)
    assert r.toast_client_id == "cid"
    assert r.toast_client_secret == "csecret"
    assert r.toast_restaurant_guid == "guid-1"


def test_connect_toast_requires_all_three_fields(client, db_path):
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.post("/mobile/api/connections/toast", headers=_auth_headers(token), json={
        "toast_client_id": "cid",
    })
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_connect_toast_saves_but_reports_error_when_token_fetch_fails(client, db_path, monkeypatch):
    """Credentials are saved even if the verification call fails, so the
    owner can see what they typed and fix a typo instead of starting over
    on a blank form."""
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    monkeypatch.setattr("toast.get_toast_token", lambda rid: (_ for _ in ()).throw(RuntimeError("bad credentials")))

    resp = client.post("/mobile/api/connections/toast", headers=_auth_headers(token), json={
        "toast_client_id": "cid", "toast_client_secret": "csecret", "toast_restaurant_guid": "guid-1",
    })
    body = resp.get_json()
    assert body["ok"] is False
    assert "bad credentials" in body["error"]

    r = get_restaurant(rid, db_path=db_path)
    assert r.toast_client_id == "cid"


def test_disconnect_toast_clears_credentials(client, db_path):
    rid = _restaurant(db_path, toast_client_id="cid", toast_client_secret="csecret", toast_restaurant_guid="guid-1")
    token = _login(client, db_path, rid)
    resp = client.delete("/mobile/api/connections/toast", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True

    r = get_restaurant(rid, db_path=db_path)
    assert not r.toast_client_id
    assert not r.toast_restaurant_guid


# ── Google Business mobile OAuth ────────────────────────────────────────

def test_google_authorize_returns_error_when_not_configured(client, db_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/connections/google/authorize", headers=_auth_headers(token))
    assert resp.status_code == 500
    assert resp.get_json()["ok"] is False


def test_google_authorize_returns_url_with_signed_state(client, db_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id")
    rid = _restaurant(db_path)
    token = _login(client, db_path, rid)
    resp = client.get("/mobile/api/connections/google/authorize", headers=_auth_headers(token))
    body = resp.get_json()
    assert body["ok"] is True
    assert "accounts.google.com" in body["url"]
    assert "mobile-callback" in body["url"] or "redirect_uri=" in body["url"]

    from gmb import verify_mobile_state
    import urllib.parse
    state = urllib.parse.parse_qs(urllib.parse.urlparse(body["url"]).query)["state"][0]
    assert verify_mobile_state(state) == rid


def test_disconnect_google_clears_tokens(client, db_path):
    rid = _restaurant(db_path, gmb_access_token="tok", gmb_refresh_token="reftok", gmb_account_id="acc1")
    token = _login(client, db_path, rid)
    resp = client.delete("/mobile/api/connections/google", headers=_auth_headers(token))
    assert resp.get_json()["ok"] is True

    r = get_restaurant(rid, db_path=db_path)
    assert not r.gmb_refresh_token
    assert not r.gmb_account_id


# ── gmb.py signed-state helpers (pure unit, no Flask) ───────────────────

def test_sign_and_verify_mobile_state_round_trips():
    from gmb import sign_mobile_state, verify_mobile_state
    state = sign_mobile_state(123)
    assert verify_mobile_state(state) == 123


def test_verify_mobile_state_rejects_tampered_signature():
    from gmb import sign_mobile_state, verify_mobile_state
    state = sign_mobile_state(123)
    rid_part, expires_part, sig = state.split(":")
    # The replacement character is chosen against sig[0] — the character
    # actually being replaced. Choosing it against sig[-1] (as this did
    # originally) left the "tampered" signature byte-identical to the real
    # one whenever sig[0] already happened to be that character, which made
    # this test hand a VALID signature to verify_mobile_state and assert it
    # was rejected. That failed about 7% of runs.
    tampered_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert tampered_sig != sig
    tampered = f"{rid_part}:{expires_part}:{tampered_sig}"
    assert verify_mobile_state(tampered) is None


def test_verify_mobile_state_rejects_different_restaurant_id_swap():
    """Swapping in a different restaurant_id without re-signing must fail —
    otherwise the signature wouldn't actually be binding the two together."""
    from gmb import sign_mobile_state, verify_mobile_state
    state = sign_mobile_state(123)
    _, expires_part, sig = state.split(":")
    forged = f"999:{expires_part}:{sig}"
    assert verify_mobile_state(forged) is None


def test_verify_mobile_state_rejects_expired_token():
    from gmb import verify_mobile_state
    import hmac, hashlib, os, time
    secret = os.getenv("SECRET_KEY", "cavnar-dev-secret").encode()
    expired = int(time.time()) - 10
    payload = f"55:{expired}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()[:32]
    assert verify_mobile_state(f"{payload}:{sig}") is None


def test_verify_mobile_state_rejects_garbage():
    from gmb import verify_mobile_state
    assert verify_mobile_state("not-a-valid-state") is None
    assert verify_mobile_state("") is None
