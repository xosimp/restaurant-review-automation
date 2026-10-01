"""Passkeys (owner, 9/30/26: "add an option for users to use a passkey as an
option of signing back in"), driven end to end with a software authenticator:
an EC P-256 key making the same attestation ("none") and assertion bytes a
phone's Face ID would, verified by passkeys.py through py_webauthn."""
import base64
import hashlib
import json
import os
import struct

import cbor2
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask

import auth
import models
from test_friction_o1 import db, _restaurant  # noqa: F401  (fixtures)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = "localhost:5050"
ORIGIN = "http://localhost:5050"
RP_ID = "localhost"


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Authenticator:
    """One passkey on one device."""

    def __init__(self, rp_id=RP_ID, origin=ORIGIN):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0
        self.rp_id, self.origin = rp_id, origin

    def _cose(self):
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _client(self, kind, challenge):
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin}).encode()

    def create(self, options, uv=True):
        cdj = self._client("webauthn.create", options["challenge"])
        flags = 0x01 | (0x04 if uv else 0) | 0x40
        auth_data = (hashlib.sha256(self.rp_id.encode()).digest() + bytes([flags]) + struct.pack(">I", 0)
                     + b"\0" * 16 + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(cdj), "attestationObject": b64u(att), "transports": ["internal"]}}

    def get(self, options, uv=True, count=None):
        self.count = self.count + 1 if count is None else count
        cdj = self._client("webauthn.get", options["challenge"])
        auth_data = hashlib.sha256(self.rp_id.encode()).digest() + bytes([0x01 | (0x04 if uv else 0)]) \
            + struct.pack(">I", self.count)
        sig = self.key.sign(auth_data + hashlib.sha256(cdj).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key",
                "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(cdj), "authenticatorData": b64u(auth_data),
                             "signature": b64u(sig), "userHandle": None}}


@pytest.fixture
def app(db, monkeypatch):
    import auth_routes
    a = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    a.register_blueprint(auth_routes.auth_bp)
    monkeypatch.setattr(auth_routes, "_send_restaurant_login_alert", lambda *a, **k: None)
    monkeypatch.setattr(auth_routes, "get_conn", models.get_conn)
    monkeypatch.setenv("BASE_URL", "https://dashboard.cavnar.ai")
    return a


def _login(db, rid, username="owner.ej", is_admin=False):
    uid = auth.create_user(rid, username, f"{username}@x.test", "correct horse battery", db_path=db)
    if is_admin:
        c = models.get_conn(db)
        c.execute("UPDATE users SET is_admin=1 WHERE id=?", (uid,))
        c.commit()
        c.close()
    return uid


def _register(app, db, monkeypatch, uid, device):
    """The Account -> Security ceremony, as the signed-in owner."""
    user = auth.get_user_by_id(uid, db_path=db)
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(user))
    c = app.test_client()
    c.set_cookie("csrf_js", "tok")
    h = {"X-CSRF": "tok", "Host": HOST}
    r = c.post("/api/passkeys/options", json={"password": "wrong"}, headers=h)
    assert r.status_code == 403 and "password" in r.get_json()["error"]
    r = c.post("/api/passkeys/options", json={"password": "correct horse battery"}, headers=h)
    assert r.status_code == 200, r.get_json()
    opts = r.get_json()["options"]
    assert opts["rp"]["id"] == RP_ID and opts["authenticatorSelection"]["residentKey"] == "required"
    assert opts["authenticatorSelection"]["userVerification"] == "required"
    r = c.post("/api/passkeys", json={"credential": device.create(opts)}, headers=h)
    assert r.status_code == 200, r.get_json()
    return c, h


def _sign_in(app, device, **kw):
    c = app.test_client()
    c.set_cookie("csrf_token", "form-tok")
    h = {"Host": HOST}
    r = c.post("/auth/passkey/options", json={"csrf_token": "form-tok"}, headers=h)
    assert r.status_code == 200, r.get_json()
    opts = r.get_json()["options"]
    assert opts["userVerification"] == "required" and not opts.get("allowCredentials")
    return c, c.post("/auth/passkey/verify", json={"csrf_token": "form-tok", "credential": device.get(opts, **kw)},
                     headers=h)


def test_a_passkey_added_in_account_signs_the_owner_in_with_no_password_or_code(app, db, monkeypatch):
    rid = _restaurant(db, two_fa_enabled=1)
    uid = _login(db, rid)
    phone = Authenticator()
    _register(app, db, monkeypatch, uid, phone)
    rows = models.get_conn(db).execute("SELECT user_id, sign_count FROM user_passkeys").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(uid, 0)]

    monkeypatch.setattr(auth, "get_current_user", lambda: None)
    c, r = _sign_in(app, phone)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["redirect"] == "/" and "session_token=" in r.headers.get("Set-Cookie", "")
    # the session passed a second factor: 2FA is on and no code was asked
    sess = models.get_conn(db).execute("SELECT user_id, two_factor_at IS NOT NULL FROM sessions").fetchall()
    assert [(s[0], s[1]) for s in sess] == [(uid, 1)]
    # the counter moved
    assert models.get_conn(db).execute("SELECT sign_count FROM user_passkeys").fetchone()[0] == 1


def test_what_a_passkey_sign_in_refuses(app, db, monkeypatch):
    rid = _restaurant(db)
    uid = _login(db, rid)
    phone = Authenticator()
    _register(app, db, monkeypatch, uid, phone)
    monkeypatch.setattr(auth, "get_current_user", lambda: None)
    c = app.test_client()
    c.set_cookie("csrf_token", "f")
    # no CSRF token, no options; a host that isn't ours, no ceremony
    assert c.post("/auth/passkey/options", json={}, headers={"Host": HOST}).status_code == 403
    c.set_cookie("csrf_token", "f", domain="phish.example")
    r = c.post("/auth/passkey/options", json={"csrf_token": "f"}, headers={"Host": "phish.example"})
    assert r.status_code == 400 and "dashboard.cavnar.ai" in r.get_json()["error"]
    # a challenge is single use
    opts = c.post("/auth/passkey/options", json={"csrf_token": "f"}, headers={"Host": HOST}).get_json()["options"]
    cred = phone.get(opts, count=10)
    assert c.post("/auth/passkey/verify", json={"csrf_token": "f", "credential": cred},
                  headers={"Host": HOST}).status_code == 200
    assert c.post("/auth/passkey/verify", json={"csrf_token": "f", "credential": cred},
                  headers={"Host": HOST}).status_code == 401
    # a login locked by "This wasn't me" stays locked
    conn = models.get_conn(db)
    conn.execute("UPDATE users SET must_reset_password=1 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    assert _sign_in(app, phone, count=20)[1].status_code == 403
    conn = models.get_conn(db)
    conn.execute("UPDATE users SET must_reset_password=0 WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    # what a sign-in turns away (five failures from one address is the
    # anonymous throttle - security.IP_MAX_ANON - so they come last)
    # no Face ID / PIN step
    assert _sign_in(app, phone, uv=False)[1].status_code == 401
    # a counter that went backwards is a cloned key
    assert _sign_in(app, phone, count=1)[1].status_code == 401
    # a passkey no login saved
    assert _sign_in(app, Authenticator())[1].status_code == 401
    # a passkey made for another site
    evil = Authenticator(rp_id="evil.test", origin="https://evil.test")
    assert _sign_in(app, evil)[1].status_code == 401


def test_an_internal_login_keeps_its_authenticator_app(app, db, monkeypatch):
    rid = _restaurant(db)
    uid = _login(db, rid, "will.admin", is_admin=True)
    phone = Authenticator()
    _register(app, db, monkeypatch, uid, phone)
    monkeypatch.setattr(auth, "get_current_user", lambda: None)
    r = _sign_in(app, phone)[1]
    assert r.status_code == 403 and "authenticator" in r.get_json()["error"]


def test_view_as_cannot_plant_a_passkey_and_removal_is_per_login(app, db, monkeypatch):
    rid = _restaurant(db)
    uid = _login(db, rid)
    _register(app, db, monkeypatch, uid, Authenticator())
    pid = models.get_conn(db).execute("SELECT id FROM user_passkeys").fetchone()[0]
    user = dict(auth.get_user_by_id(uid, db_path=db), acting_admin="will")
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    c = app.test_client()
    c.set_cookie("csrf_js", "tok")
    h = {"X-CSRF": "tok", "Host": HOST}
    assert c.post("/api/passkeys/options", json={"password": "correct horse battery"}, headers=h).status_code == 403
    other = _login(db, rid, "server.ana")
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(auth.get_user_by_id(other, db_path=db)))
    assert c.post(f"/api/passkeys/{pid}/remove", headers=h).status_code == 404
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(auth.get_user_by_id(uid, db_path=db)))
    listed = c.get("/api/passkeys", headers=h).get_json()["passkeys"]
    assert [p["id"] for p in listed] == [pid] and listed[0]["created"].count("/") == 2
    assert c.post(f"/api/passkeys/{pid}/remove", headers=h).status_code == 200
    assert c.get("/api/passkeys", headers=h).get_json()["passkeys"] == []


def test_the_login_page_offers_passkeys_and_apple_only_when_set_up(app, monkeypatch):
    c = app.test_client()
    monkeypatch.delenv("APPLE_WEB_SERVICES_ID", raising=False)
    page = c.get("/login").get_data(as_text=True)
    assert 'id="sso-passkey"' in page and 'autocomplete="username webauthn"' in page
    assert 'id="sso-apple"' not in page and "appleid.auth.js" not in page
    monkeypatch.setenv("APPLE_WEB_SERVICES_ID", "ai.cavnar.dashboard")
    page = c.get("/login").get_data(as_text=True)
    google, apple = page.find('id="sso-google"'), page.find('id="sso-apple"')
    assert apple > 0 and "appleid.auth.js" in page
    if google > 0:
        assert google < apple, "Apple sits below Google"


def test_apple_on_the_web_checks_the_services_id_and_the_nonce(app, db, monkeypatch):
    import hashlib as _h
    import mobile_api
    rid = _restaurant(db)
    uid = _login(db, rid)
    conn = models.get_conn(db)
    conn.execute("UPDATE users SET apple_user_id='apple-sub-1' WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    monkeypatch.setenv("APPLE_WEB_SERVICES_ID", "ai.cavnar.dashboard")
    seen = {}

    def verify(token, audience):
        seen["aud"] = audience
        if token != "good":
            raise ValueError("bad signature")
        return {"sub": "apple-sub-1", "nonce": seen["nonce"]}
    monkeypatch.setattr(mobile_api, "_verify_apple_identity_token", verify)
    c = app.test_client()
    c.set_cookie("csrf_token", "f")
    n = c.post("/auth/apple/nonce", json={}).get_json()
    assert n["client_id"] == "ai.cavnar.dashboard" and n["redirect_uri"].endswith("/auth/apple/callback")
    raw = c.get_cookie("a_sso_nonce").value
    assert n["nonce"] == _h.sha256(raw.encode()).hexdigest()
    seen["nonce"] = "someone-elses"
    assert c.post("/auth/apple/web", json={"csrf_token": "f", "id_token": "good"}).status_code == 401
    assert c.post("/auth/apple/web", json={"csrf_token": "f", "id_token": "forged"}).status_code == 401
    seen["nonce"] = n["nonce"]
    r = c.post("/auth/apple/web", json={"csrf_token": "f", "id_token": "good"})
    assert r.status_code == 200, r.get_json()
    assert seen["aud"] == "ai.cavnar.dashboard"


def test_the_site_lets_the_app_share_its_passkeys():
    # Read from the source: importing hosted_dashboard registers every
    # blueprint again, which Flask refuses once another test has done it.
    import ast
    src = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "apple_app_site_association")
    body = ast.get_source_segment(src, fn)
    assert '"webcredentials": {"apps": [APPLE_APP_ID]}' in body


def test_a_fresh_password_sign_in_is_offered_a_passkey_without_a_second_password(app, db, monkeypatch):
    from datetime import datetime
    rid = _restaurant(db)
    uid = _login(db, rid)
    fresh = dict(auth.get_user_by_id(uid, db_path=db), reauth_at=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
    monkeypatch.setattr(auth, "get_current_user", lambda: fresh)
    c = app.test_client()
    c.set_cookie("csrf_js", "tok")
    h = {"X-CSRF": "tok", "Host": HOST}
    assert c.get("/api/passkeys", headers=h).get_json()["offer"] is True
    opts = c.post("/api/passkeys/options", json={}, headers=h)
    assert opts.status_code == 200, opts.get_json()
    assert c.post("/api/passkeys", json={"credential": Authenticator().create(opts.get_json()["options"])},
                  headers=h).status_code == 200
    # one saved: no more offers; a stale sign-in still needs the password
    assert c.get("/api/passkeys", headers=h).get_json()["offer"] is False
    stale = dict(fresh, reauth_at="2026-01-01 00:00:00")
    monkeypatch.setattr(auth, "get_current_user", lambda: stale)
    assert c.post("/api/passkeys/options", json={}, headers=h).status_code == 403
    # never in view-as
    monkeypatch.setattr(auth, "get_current_user", lambda: dict(fresh, acting_admin="will"))
    conn = models.get_conn(db)
    conn.execute("DELETE FROM user_passkeys")
    conn.commit()
    conn.close()
    assert c.get("/api/passkeys", headers=h).get_json()["offer"] is False
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "function pkOffer()" in src and "conditionalCreate" in src and "cav-pk-offer-skip" in src
