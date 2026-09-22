"""Webhooks (Stripe, DocuSign) and the POS connect routes: the edges an
outside caller can reach.

These routes are public by necessity — Stripe and DocuSign are the callers —
so a signature is the only thing standing between the internet and a
restaurant's billing state, its contract status and its owner's password.
What is protected here:

- a Stripe event with a bad signature is refused (400) and changes nothing,
  and a verified checkout.session.completed activates every location billed
  to the same Stripe customer, not only the one it resolved to;
- the DocuSign webhook refuses an unsigned or badly signed request, and
  refuses everything when no signing secret is configured (SEC-11);
- a DocuSign completion never replaces the password of an owner who has
  already logged in (SEC-11);
- the public DocuSign OAuth callback does not reflect its ``error`` query
  parameter as markup (SEC-10);
- a Stripe or DocuSign event whose work fails after the claim returns 5xx
  and leaves the event unclaimed, so the provider's retry can still succeed
  (SEC-27);
- connecting Toast from the phone behaves like connecting it from the web:
  a failed connect leaves working credentials in place, and new credentials
  drop the cached access token minted for the old ones (SEC-25).

The ``stripe`` package is not installed in every interpreter this suite runs
under (SEC-40), so a small stand-in implementing Stripe's documented
signature scheme (``t=<ts>,v1=<hex HMAC-SHA256 of "<ts>.<payload>">``) is put
in ``sys.modules`` for the Stripe tests. Nothing here touches the network:
Resend, the welcome/payment emails and Toast's token endpoint are stubbed.
"""
import base64
import hashlib
import hmac
import json
import sqlite3
import sys
import time
import types

import pytest
from flask import Flask

import auth
import client_api
import guest_marketing
import mobile_api
import models
import toast
import toast_routes
import webhook_routes
from auth import create_session, create_user, init_auth, update_last_login
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    """Every module that bound get_conn at import time gets the test
    database; models.get_conn is the one the lazily-imported helpers read."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, mobile_api, guest_marketing, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)


@pytest.fixture
def sent(monkeypatch):
    """Stub every email webhook_routes can send; record what would have gone."""
    out = []
    monkeypatch.setattr(webhook_routes, "_resend_key", lambda: "re_test_key")
    monkeypatch.setattr(webhook_routes, "send_payment_email", lambda **k: out.append(("payment", k)))
    monkeypatch.setattr(webhook_routes, "send_welcome_email", lambda **k: out.append(("welcome", k)))
    monkeypatch.setattr(webhook_routes, "log_email", lambda *a, **k: None)
    return out


@pytest.fixture
def wh_client():
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    return app.test_client()


def _raise(exc):
    def _f(*a, **k):
        raise exc
    return _f


def _password_hash(db_path, uid):
    conn = models.get_conn(db_path)
    try:
        return conn.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()[0]
    finally:
        conn.close()


def _count(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


# ── Stripe: a stand-in SDK with the real signature scheme ───────────────────

STRIPE_SECRET = "whsec_test_edge"


class _SignatureVerificationError(Exception):
    pass


def _fake_stripe_module():
    """Just enough of the stripe SDK for stripe_webhook: construct_event
    verifies ``t=..,v1=..`` against HMAC-SHA256("<t>.<payload>") and returns
    the parsed event, raising SignatureVerificationError otherwise."""
    mod = types.ModuleType("stripe")

    class Webhook:
        @staticmethod
        def construct_event(payload, sig_header, secret):
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            parts = dict(p.split("=", 1) for p in (sig_header or "").split(",") if "=" in p)
            t, v1 = parts.get("t"), parts.get("v1")
            if not secret or not t or not v1:
                raise _SignatureVerificationError("No signatures found matching the expected signature")
            expected = hmac.new(secret.encode(), f"{t}.{payload}".encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, v1):
                raise _SignatureVerificationError("No signatures found matching the expected signature")
            return json.loads(payload)

    mod.Webhook = Webhook
    mod.SignatureVerificationError = _SignatureVerificationError
    mod.error = types.SimpleNamespace(SignatureVerificationError=_SignatureVerificationError)
    return mod


@pytest.fixture
def stripe_ok(monkeypatch, sent):
    monkeypatch.setitem(sys.modules, "stripe", _fake_stripe_module())
    monkeypatch.setattr(webhook_routes, "STRIPE_WEBHOOK_SECRET", STRIPE_SECRET)
    # The ops alert goes through send_alert, which prints when there is no key.
    # sent already set a key for the DocuSign path; the Stripe alert would then
    # use the Resend SDK, which conftest blocks — so blank it here.
    monkeypatch.setattr(webhook_routes, "_resend_key", lambda: "")


def _stripe_sign(body: str, secret: str = STRIPE_SECRET) -> str:
    t = str(int(time.time()))
    v1 = hmac.new(secret.encode(), f"{t}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


def _checkout_event(event_id, rid, customer="cus_EDGE1"):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {"object": {
            "customer": customer,
            "subscription": "sub_EDGE1",
            "customer_details": {"email": "owner@x.test"},
            "metadata": {"restaurant_id": str(rid), "restaurant": "Edge Group"},
        }},
    }


def _post_stripe(client, event, sig=None):
    body = json.dumps(event)
    return client.post("/stripe-webhook", data=body, content_type="application/json",
                       headers={"Stripe-Signature": sig if sig is not None else _stripe_sign(body)})


def _two_locations_one_customer(db_path):
    """Two locations billed to one Stripe customer, both pending."""
    a = create_restaurant(Restaurant(name="Edge Downtown", owner_email="owner@x.test"), db_path=db_path)
    b = create_restaurant(Restaurant(name="Edge Uptown", owner_email="owner@x.test"), db_path=db_path)
    for rid in (a, b):
        update_restaurant(rid, {"billing_status": "pending", "stripe_customer_id": "cus_EDGE1"}, db_path=db_path)
    return a, b


def test_a_stripe_event_with_a_bad_signature_is_refused_and_changes_nothing(wh_client, db_path, stripe_ok):
    a, b = _two_locations_one_customer(db_path)
    resp = _post_stripe(wh_client, _checkout_event("evt_bad_sig", a), sig="t=1,v1=deadbeef")
    assert resp.status_code == 400
    assert get_restaurant(a, db_path=db_path).billing_status == "pending"
    assert get_restaurant(b, db_path=db_path).billing_status == "pending"
    assert _count(db_path, "SELECT COUNT(*) FROM stripe_events_seen WHERE event_id=?", ("evt_bad_sig",)) == 0


def test_a_stripe_event_signed_with_the_wrong_secret_is_refused(wh_client, db_path, stripe_ok):
    a, _ = _two_locations_one_customer(db_path)
    event = _checkout_event("evt_wrong_secret", a)
    body = json.dumps(event)
    resp = _post_stripe(wh_client, event, sig=_stripe_sign(body, secret="whsec_someone_else"))
    assert resp.status_code == 400
    assert get_restaurant(a, db_path=db_path).billing_status == "pending"


def test_a_stripe_event_with_no_signature_header_is_refused(wh_client, db_path, stripe_ok):
    a, _ = _two_locations_one_customer(db_path)
    resp = _post_stripe(wh_client, _checkout_event("evt_no_sig", a), sig="")
    assert resp.status_code == 400
    assert get_restaurant(a, db_path=db_path).billing_status == "pending"


def test_a_signed_checkout_completion_activates_every_location_billed_to_the_customer(
        wh_client, db_path, stripe_ok):
    a, b = _two_locations_one_customer(db_path)
    resp = _post_stripe(wh_client, _checkout_event("evt_checkout_ok", a))
    assert resp.status_code == 200
    assert get_restaurant(a, db_path=db_path).billing_status == "active"
    assert get_restaurant(b, db_path=db_path).billing_status == "active"
    assert _count(db_path, "SELECT COUNT(*) FROM stripe_events_seen WHERE event_id=?", ("evt_checkout_ok",)) == 1


def test_a_replayed_signed_checkout_completion_is_acknowledged_as_a_duplicate(wh_client, db_path, stripe_ok):
    a, _ = _two_locations_one_customer(db_path)
    event = _checkout_event("evt_replay", a)
    assert _post_stripe(wh_client, event).status_code == 200
    again = _post_stripe(wh_client, event)
    assert again.status_code == 200
    assert again.get_json().get("duplicate") is True


def test_a_failing_stripe_activation_returns_5xx_and_leaves_the_event_unclaimed(
        wh_client, db_path, stripe_ok, monkeypatch):
    a, b = _two_locations_one_customer(db_path)
    event = _checkout_event("evt_activation_fails", a)

    monkeypatch.setattr(webhook_routes, "update_restaurant",
                        _raise(sqlite3.OperationalError("database is locked")))
    first = _post_stripe(wh_client, event)
    assert first.status_code >= 500
    assert _count(db_path, "SELECT COUNT(*) FROM stripe_events_seen WHERE event_id=?",
                  ("evt_activation_fails",)) == 0

    # Stripe retries the non-2xx; with the database back, the retry activates.
    monkeypatch.setattr(webhook_routes, "update_restaurant", models.update_restaurant)
    retry = _post_stripe(wh_client, event)
    assert retry.status_code == 200
    assert get_restaurant(a, db_path=db_path).billing_status == "active"
    assert get_restaurant(b, db_path=db_path).billing_status == "active"


# ── DocuSign ────────────────────────────────────────────────────────────────

DS_SECRET = "ds-connect-hmac-key"
ENVELOPE = "ENV-EDGE-123"


def _contracted_owner(db_path, logged_in=False):
    rid = create_restaurant(Restaurant(name="Edge Signed", owner_email="o@x.test"), db_path=db_path)
    uid = create_user(rid, "owner", "o@x.test", "originalpw1", db_path=db_path)
    update_restaurant(rid, {"docusign_envelope_id": ENVELOPE, "contract_status": "sent"}, db_path=db_path)
    if logged_in:
        update_last_login(uid, db_path=db_path)
    return rid, uid


def _completed_body():
    return json.dumps({"envelopeId": ENVELOPE, "status": "completed"})


def _ds_sign(body: str, secret: str = DS_SECRET) -> str:
    """DocuSign Connect HMAC: base64(HMAC-SHA256(key, raw body))."""
    return base64.b64encode(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest()).decode()


def _post_docusign(client, body, signature=None):
    headers = {"X-DocuSign-Signature-1": signature} if signature is not None else {}
    return client.post("/docusign/webhook", data=body, content_type="application/json", headers=headers)


def test_the_docusign_callback_escapes_its_error_parameter(wh_client):
    resp = wh_client.get("/docusign/callback?error=<script>alert(document.domain)</script>")
    body = resp.get_data(as_text=True)
    assert "<script>alert(document.domain)</script>" not in body
    assert "<script" not in body.lower()


def test_the_second_docusign_callback_path_escapes_its_error_parameter(wh_client):
    resp = wh_client.get('/docusign/callback2?error="><img src=x onerror=alert(1)>')
    body = resp.get_data(as_text=True)
    assert "<img src=x onerror=alert(1)>" not in body


def test_the_docusign_callback_confirms_consent_and_otherwise_redirects(wh_client):
    ok = wh_client.get("/docusign/callback?code=abc")
    assert ok.status_code == 200
    assert "DocuSign Connected" in ok.get_data(as_text=True)
    bare = wh_client.get("/docusign/callback")
    assert bare.status_code in (301, 302, 303, 307, 308)
    assert bare.headers["Location"].endswith("/admin")


@pytest.mark.xfail(strict=True, reason="SEC-11: with DOCUSIGN_WEBHOOK_SECRET unset the HMAC check is skipped; an unsigned completion marks the contract signed and resets the owner's password")
def test_the_docusign_webhook_refuses_every_request_when_no_secret_is_configured(
        wh_client, db_path, sent, monkeypatch):
    monkeypatch.delenv("DOCUSIGN_WEBHOOK_SECRET", raising=False)
    rid, uid = _contracted_owner(db_path)
    before = _password_hash(db_path, uid)

    resp = _post_docusign(wh_client, _completed_body())

    assert resp.status_code in (401, 403)
    assert get_restaurant(rid, db_path=db_path).contract_status == "sent"
    assert _password_hash(db_path, uid) == before
    assert sent == []


def test_a_docusign_request_with_a_bad_signature_is_refused_and_changes_nothing(
        wh_client, db_path, sent, monkeypatch):
    monkeypatch.setenv("DOCUSIGN_WEBHOOK_SECRET", DS_SECRET)
    rid, uid = _contracted_owner(db_path)
    before = _password_hash(db_path, uid)

    resp = _post_docusign(wh_client, _completed_body(), signature=_ds_sign(_completed_body(), "wrong-key"))

    assert resp.status_code == 401
    assert get_restaurant(rid, db_path=db_path).contract_status == "sent"
    assert _password_hash(db_path, uid) == before
    assert sent == []
    assert _count(db_path, "SELECT COUNT(*) FROM docusign_events_seen") == 0


def test_a_docusign_request_with_no_signature_header_is_refused_when_a_secret_is_set(
        wh_client, db_path, sent, monkeypatch):
    monkeypatch.setenv("DOCUSIGN_WEBHOOK_SECRET", DS_SECRET)
    rid, _ = _contracted_owner(db_path)
    resp = _post_docusign(wh_client, _completed_body())
    assert resp.status_code == 401
    assert get_restaurant(rid, db_path=db_path).contract_status == "sent"
    assert sent == []


def test_a_docusign_signature_over_a_different_body_is_refused(wh_client, db_path, sent, monkeypatch):
    """A captured signature cannot be replayed onto an edited payload."""
    monkeypatch.setenv("DOCUSIGN_WEBHOOK_SECRET", DS_SECRET)
    rid, _ = _contracted_owner(db_path)
    other = json.dumps({"envelopeId": ENVELOPE, "status": "sent"})
    resp = _post_docusign(wh_client, _completed_body(), signature=_ds_sign(other))
    assert resp.status_code == 401
    assert get_restaurant(rid, db_path=db_path).contract_status == "sent"


def test_a_correctly_signed_docusign_completion_marks_the_contract_signed(
        wh_client, db_path, sent, monkeypatch):
    """Positive control for the signature tests above: the same request with
    the right HMAC is accepted, so the 401s are the signature and nothing else."""
    monkeypatch.setenv("DOCUSIGN_WEBHOOK_SECRET", DS_SECRET)
    rid, _ = _contracted_owner(db_path)
    body = _completed_body()
    resp = _post_docusign(wh_client, body, signature=_ds_sign(body))
    assert resp.status_code == 200
    assert get_restaurant(rid, db_path=db_path).contract_status == "signed"
    assert [kind for kind, _ in sent] == ["payment", "welcome"]


def test_a_docusign_completion_never_replaces_the_password_of_an_owner_who_has_logged_in(
        wh_client, db_path, sent, monkeypatch):
    monkeypatch.setenv("DOCUSIGN_WEBHOOK_SECRET", DS_SECRET)
    rid, uid = _contracted_owner(db_path, logged_in=True)
    before = _password_hash(db_path, uid)
    body = _completed_body()

    resp = _post_docusign(wh_client, body, signature=_ds_sign(body))

    assert resp.status_code == 200
    assert _password_hash(db_path, uid) == before
    assert not any(kind == "welcome" and k.get("password") for kind, k in sent)


def test_a_failing_docusign_completion_returns_5xx_and_leaves_the_event_unclaimed(
        wh_client, db_path, sent, monkeypatch):
    monkeypatch.setenv("DOCUSIGN_WEBHOOK_SECRET", DS_SECRET)
    rid, _ = _contracted_owner(db_path)
    body = _completed_body()

    # The handler has no update_restaurant call to break: it marks the contract
    # through its own get_conn(). Let the claim's connection through and fail
    # the next one, the way a locked database would.
    real = webhook_routes.get_conn
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(*a, **k)

    monkeypatch.setattr(webhook_routes, "get_conn", flaky)
    first = _post_docusign(wh_client, body, signature=_ds_sign(body))
    monkeypatch.setattr(webhook_routes, "get_conn", real)

    assert first.status_code >= 500
    assert _count(db_path, "SELECT COUNT(*) FROM docusign_events_seen WHERE envelope_id=?", (ENVELOPE,)) == 0

    retry = _post_docusign(wh_client, body, signature=_ds_sign(body))
    assert retry.status_code == 200
    assert get_restaurant(rid, db_path=db_path).contract_status == "signed"
    assert [kind for kind, _ in sent] == ["payment", "welcome"]


# ── Toast connect: web and mobile must agree ────────────────────────────────

OLD = {"toast_client_id": "old-cid", "toast_client_secret": "old-secret", "toast_restaurant_guid": "old-guid"}
NEW = {"toast_client_id": "new-cid", "toast_client_secret": "new-secret", "toast_restaurant_guid": "new-guid"}


def _toast_restaurant(db_path, cached_token=True):
    """A restaurant with working Toast credentials and, optionally, a cached
    token for them that is still valid for hours."""
    rid = create_restaurant(Restaurant(name="Edge Toast", owner_email="t@x.test"), db_path=db_path)
    updates = dict(OLD, pos_system="Toast")
    if cached_token:
        from datetime import datetime, timedelta, timezone
        updates["toast_access_token"] = "OLD-TOKEN"
        updates["toast_token_expires"] = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    update_restaurant(rid, updates, db_path=db_path)
    uid = create_user(rid, "toastowner", "t@x.test", "correct-horse", db_path=db_path)
    return rid, uid


def _toast_rejects_everything(monkeypatch):
    """Toast's login endpoint and the web route's check both say no."""
    monkeypatch.setattr(toast, "_get_raw_token", _raise(RuntimeError("401 Unauthorized from Toast")))
    monkeypatch.setattr(toast, "test_credentials",
                        lambda cid, sec, guid: {"ok": False, "error": "Auth failed (401) — check client ID and secret"})


def _toast_accepts_everything(monkeypatch):
    monkeypatch.setattr(toast, "_get_raw_token",
                        lambda cid, sec, base=None: {"token": {"accessToken": f"TOKEN-FOR-{cid}", "expiresIn": 86400}})
    monkeypatch.setattr(toast, "test_credentials", lambda cid, sec, guid: {"ok": True})


@pytest.fixture
def web_client():
    app = Flask(__name__)
    app.register_blueprint(toast_routes.toast_bp)
    return app.test_client()


@pytest.fixture
def mobile_client():
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    return app.test_client()


def _web_save(client, db_path, uid, creds):
    token = create_session(uid, db_path=db_path)
    client.set_cookie("session_token", token)
    # toast_bp is CSRF-protected once hosted_dashboard has been imported
    # (the hook is attached to the blueprint object), so send the pair.
    client.set_cookie("csrf_js", "edge-csrf")
    payload = {"client_id": creds["toast_client_id"], "client_secret": creds["toast_client_secret"],
               "restaurant_guid": creds["toast_restaurant_guid"]}
    return client.post("/api/toast/save", json=payload, headers={"X-CSRF": "edge-csrf"})


def _mobile_save(client, db_path, uid, creds):
    token = create_session(uid, device_type="ios", db_path=db_path)
    return client.post("/mobile/api/connections/toast", json=creds,
                       headers={"Authorization": f"Bearer {token}"})


def _creds(db_path, rid):
    r = get_restaurant(rid, db_path=db_path)
    return {"toast_client_id": r.toast_client_id, "toast_client_secret": r.toast_client_secret,
            "toast_restaurant_guid": r.toast_restaurant_guid}


def test_a_failed_web_toast_connect_leaves_the_previous_credentials_in_place(web_client, db_path, monkeypatch):
    rid, uid = _toast_restaurant(db_path)
    _toast_rejects_everything(monkeypatch)
    resp = _web_save(web_client, db_path, uid, NEW)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is False
    assert _creds(db_path, rid) == OLD
    assert get_restaurant(rid, db_path=db_path).toast_access_token == "OLD-TOKEN"


def test_a_successful_web_toast_connect_drops_the_token_cached_for_the_old_credentials(
        web_client, db_path, monkeypatch):
    rid, uid = _toast_restaurant(db_path)
    _toast_accepts_everything(monkeypatch)
    resp = _web_save(web_client, db_path, uid, NEW)
    assert resp.get_json()["ok"] is True
    r = get_restaurant(rid, db_path=db_path)
    assert _creds(db_path, rid) == NEW
    assert r.toast_access_token != "OLD-TOKEN"


@pytest.mark.xfail(strict=True, reason="SEC-25: mobile Toast connect writes the new credentials before testing them, so a typo overwrites working ones")
def test_a_failed_mobile_toast_connect_leaves_the_previous_credentials_in_place(
        mobile_client, db_path, monkeypatch):
    # No cached token, so the mobile route's check really goes to Toast.
    rid, uid = _toast_restaurant(db_path, cached_token=False)
    _toast_rejects_everything(monkeypatch)
    resp = _mobile_save(mobile_client, db_path, uid, NEW)
    assert resp.get_json()["ok"] is False
    assert _creds(db_path, rid) == OLD


@pytest.mark.xfail(strict=True, reason="SEC-25: mobile Toast connect keeps toast_access_token/toast_token_expires, so the old credentials' token 'verifies' the new ones")
def test_a_mobile_toast_connect_drops_the_token_cached_for_the_old_credentials(
        mobile_client, db_path, monkeypatch):
    rid, uid = _toast_restaurant(db_path, cached_token=True)
    _toast_accepts_everything(monkeypatch)
    resp = _mobile_save(mobile_client, db_path, uid, NEW)
    assert resp.get_json()["ok"] is True
    assert _creds(db_path, rid) == NEW
    assert get_restaurant(rid, db_path=db_path).toast_access_token != "OLD-TOKEN"


@pytest.mark.xfail(strict=True, reason="SEC-25: with a valid cached token the mobile route never checks the new credentials against Toast at all")
def test_a_mobile_toast_connect_checks_the_new_credentials_even_with_a_cached_token(
        mobile_client, db_path, monkeypatch):
    rid, uid = _toast_restaurant(db_path, cached_token=True)
    _toast_rejects_everything(monkeypatch)
    resp = _mobile_save(mobile_client, db_path, uid, NEW)
    assert resp.get_json()["ok"] is False
    assert _creds(db_path, rid) == OLD
