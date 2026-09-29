"""Fix round H — the contract side of the billing lifecycle.

#12: a signed contract OWES the payment link and the welcome (the outbox),
marks the restaurant signed with when, and resets no password. #26: DocuSign
reminders are on for every envelope; a resend re-sends the same envelope;
signed-but-unpaid clients carry contract_signed_at for the chase. #78: a
signed contract is never reset to 'sent'. #144: declined, voided and
delivered envelopes reach the restaurant and the ledger; a failed send is
captured, not just printed; the raw payload (signer names and addresses)
is not logged.

DocuSign's HTTP API is stubbed at requests; nothing leaves the process.
"""
import base64
import hashlib
import hmac
import json
import sys

import pytest
from flask import Flask

import admin_routes
import auth
import billing_jobs
import docusign_helper
import emails
import models
import ops
import webhook_routes
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

SECRET = "ds-fixture-secret"


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, webhook_routes, admin_routes, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    monkeypatch.setenv("DOCUSIGN_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(billing_jobs, "_sending_allowed", lambda: True)
    mail = []
    monkeypatch.setattr(emails, "send_payment_email",
                        lambda **k: mail.append(("payment", k)) or emails.SendResult(True))
    monkeypatch.setattr(emails, "send_welcome_with_set_password_link",
                        lambda **k: mail.append(("welcome", k)) or emails.SendResult(True))
    return mail


def _rid(db_path, **kw):
    rid = create_restaurant(Restaurant(name=kw.pop("name", "Contract Co"),
                                       owner_email=kw.pop("owner_email", "o@x.test")), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _rows(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


@pytest.fixture
def ds():
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    client = app.test_client()

    def post(envelope_id, status="completed", extra=None, raw=None):
        payload = raw if raw is not None else dict({"envelopeId": envelope_id, "status": status}, **(extra or {}))
        body = json.dumps(payload).encode()
        sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
        return client.post("/docusign/webhook", data=body, headers={"Content-Type": "application/json",
                                                                    "X-DocuSign-Signature-1": sig})
    return post


def _password_hash(db_path, uid):
    return _rows(db_path, "SELECT password_hash FROM users WHERE id=?", uid)[0]["password_hash"]


# ── #12 signing owes the sends, resets nothing ──────────────────────────────

def test_signing_owes_and_sends_the_payment_link_and_a_set_password_welcome(db_path, ds, _world):
    rid = _rid(db_path, docusign_envelope_id="env_1", contract_status="sent", module_reviews=1)
    uid = create_user(rid, "owner", "owner@x.test", "admin-typed-1", db_path=db_path)
    before = _password_hash(db_path, uid)
    assert ds("env_1").status_code == 200
    r = get_restaurant(rid, db_path)
    assert r.contract_status == "signed" and r.contract_signed_at
    assert [k for k, _ in _world] == ["payment", "welcome"]
    welcome = _world[1][1]
    # THE welcome (emails.send_welcome_with_set_password_link) mints the
    # set-password link itself; the outbox hands it the login, never a password.
    assert "password" not in welcome and welcome["user_id"] == uid and welcome["restaurant_id"] == rid
    assert _password_hash(db_path, uid) == before
    owed = _rows(db_path, "SELECT kind, status FROM owed_sends ORDER BY id")
    assert owed == [{"kind": "payment_link", "status": "sent"}, {"kind": "welcome", "status": "sent"}]
    hist = _rows(db_path, "SELECT source FROM billing_status_history WHERE restaurant_id=? AND field='contract_status'",
                 rid)
    assert hist and hist[-1]["source"] == "docusign"


def test_a_welcome_that_fails_at_signing_stays_owed_and_the_webhook_still_answers_200(db_path, ds, monkeypatch):
    rid = _rid(db_path, docusign_envelope_id="env_1", contract_status="sent", module_reviews=1)
    create_user(rid, "owner", "owner@x.test", "admin-typed-1", db_path=db_path)
    monkeypatch.setattr(emails, "send_welcome_with_set_password_link",
                        lambda **k: emails.SendResult(False, error="Resend 429 quota", status_code=429))
    assert ds("env_1").status_code == 200
    (w,) = _rows(db_path, "SELECT status, last_status_code FROM owed_sends WHERE kind='welcome'")
    assert w == {"status": "pending", "last_status_code": 429}


def test_the_raw_payload_is_not_printed(db_path, ds, capsys):
    rid = _rid(db_path, docusign_envelope_id="env_1", contract_status="sent")
    ds("env_1", extra={"data": {"envelopeSummary": {"recipients": {"signers": [
        {"name": "Erik Secret", "email": "erik.private@x.test"}]}}}})
    out = capsys.readouterr().out
    assert "erik.private@x.test" not in out and "Erik Secret" not in out
    assert rid


# ── #144 declined, voided, delivered ────────────────────────────────────────

def test_a_declined_contract_reaches_the_restaurant_and_the_ledger(db_path, ds):
    rid = _rid(db_path, docusign_envelope_id="env_d", contract_status="sent")
    raw = {"event": "envelope-declined", "data": {"envelopeId": "env_d", "envelopeSummary": {
        "status": "declined", "recipients": {"signers": [{"declinedReason": "Price too high"}]}}}}
    assert ds("env_d", raw=raw).status_code == 200
    assert get_restaurant(rid, db_path).contract_status == "declined"
    (ev,) = _rows(db_path, "SELECT * FROM admin_events WHERE event_type='contract.declined'")
    assert ev["restaurant_id"] == rid and "Price too high" in ev["summary"]
    (env,) = _rows(db_path, "SELECT status, status_reason FROM docusign_envelopes WHERE envelope_id='env_d'")
    assert env == {"status": "declined", "status_reason": "Price too high"}


def test_voiding_a_superseded_envelope_changes_nothing_current(db_path, ds):
    rid = _rid(db_path, contract_status="sent")
    update_restaurant(rid, {"docusign_envelope_id": "env_old"}, db_path=db_path)
    update_restaurant(rid, {"docusign_envelope_id": "env_new"}, db_path=db_path)
    ds("env_old", status="voided")
    assert get_restaurant(rid, db_path).contract_status == "sent"


def test_delivered_marks_an_opened_contract_but_never_a_signed_one(db_path, ds):
    a = _rid(db_path, name="Opened", docusign_envelope_id="env_a", contract_status="sent")
    b = _rid(db_path, name="Signed", docusign_envelope_id="env_b", contract_status="signed")
    ds("env_a", status="delivered")
    ds("env_b", status="delivered")
    assert get_restaurant(a, db_path).contract_status == "delivered"
    assert get_restaurant(b, db_path).contract_status == "signed"


def test_a_bad_docusign_signature_is_counted(db_path):
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    resp = app.test_client().post("/docusign/webhook", data=b"{}", headers={"X-DocuSign-Signature-1": "nope"})
    assert resp.status_code == 401
    (row,) = _rows(db_path, "SELECT * FROM webhook_verifications WHERE provider='docusign'")
    assert row["failures_since_verified"] == 1


# ── #26 reminders; resend the same envelope ─────────────────────────────────

class _Resp:
    def __init__(self, code, payload=None):
        self.status_code, self._p, self.text = code, payload or {}, json.dumps(payload or {})

    def json(self):
        return self._p


def test_every_new_envelope_asks_docusign_to_remind(monkeypatch):
    import requests
    sent = {}
    monkeypatch.setattr(docusign_helper, "INTEGRATION_KEY", "ik")
    monkeypatch.setattr(docusign_helper, "USER_ID", "u")
    monkeypatch.setattr(docusign_helper, "ACCOUNT_ID", "a")
    monkeypatch.setattr(docusign_helper, "TEMPLATE_ID", "t")
    monkeypatch.setattr(docusign_helper, "PRIVATE_KEY", "-----BEGIN x")
    monkeypatch.setattr(docusign_helper, "get_access_token", lambda: "tok")
    monkeypatch.setattr(requests, "post", lambda url, headers=None, json=None, timeout=None:
                        sent.update(json=json, timeout=timeout) or _Resp(201, {"envelopeId": "env_9", "status": "sent"}))
    out = docusign_helper.send_contract("o@x.test", "Owner", "Place", 1, "Review Intelligence", restaurant_id=4)
    assert out["envelope_id"] == "env_9" and sent["timeout"]
    rem = sent["json"]["notification"]["reminders"]
    assert rem["reminderEnabled"] == "true" and int(rem["reminderDelay"]) >= 1


def test_a_failed_send_is_captured_not_just_printed(db_path, monkeypatch):
    monkeypatch.setattr(docusign_helper, "INTEGRATION_KEY", "")
    with pytest.raises(ValueError):
        docusign_helper.send_contract("o@x.test", "Owner", "Place", 1, "x", restaurant_id=7)
    (f,) = _rows(db_path, "SELECT * FROM job_failures WHERE job='docusign_send'")
    assert "restaurant_id=7" in f["context"]


def test_resend_envelope_re_sends_the_same_envelope_with_reminders(monkeypatch):
    import requests
    calls = []
    monkeypatch.setattr(docusign_helper, "get_access_token", lambda: "tok")
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None:
                        calls.append(("GET", url, timeout)) or _Resp(200, {"status": "sent"}))
    monkeypatch.setattr(requests, "put", lambda url, headers=None, json=None, params=None, timeout=None:
                        calls.append(("PUT", url, params, timeout)) or _Resp(200, {}))
    assert docusign_helper.resend_envelope("env_1")["ok"] is True
    assert [c[0] for c in calls] == ["GET", "PUT", "PUT"]
    assert calls[1][1].endswith("/envelopes/env_1/notification")
    assert calls[2][1].endswith("/envelopes/env_1") and calls[2][2] == {"resend_envelope": "true"}
    assert all(c[-1] for c in calls)


def test_a_declined_envelope_cannot_be_resent(monkeypatch):
    import requests
    monkeypatch.setattr(docusign_helper, "get_access_token", lambda: "tok")
    monkeypatch.setattr(requests, "get", lambda url, headers=None, timeout=None: _Resp(200, {"status": "declined"}))
    assert docusign_helper.resend_envelope("env_1") == {"ok": False, "status": "declined", "resendable": False}


# ── #78 the resend-contract route ───────────────────────────────────────────

@pytest.fixture
def admin(db_path, monkeypatch):
    # resend-contract reaches DocuSign, which emails the client: it is
    # refused on a local backend (integration wave), so these run as the
    # production server would.
    monkeypatch.setenv("ALLOW_LOCAL_SCHEDULER", "1")
    monkeypatch.delenv("RESTORE_FROM", raising=False)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_routes.admin_bp)
    home = _rid(db_path, name="Cavnar HQ", owner_email="will@x.test")
    uid = create_user(home, "will", "will@x.test", "admin-pass-1", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True, db_path=db_path))
    return c


def test_a_signed_contract_is_not_reset_by_a_resend(db_path, admin, monkeypatch):
    rid = _rid(db_path, contract_status="signed", docusign_envelope_id="env_s", module_reviews=1)
    monkeypatch.setattr(docusign_helper, "send_contract", lambda **k: pytest.fail("no new envelope"))
    resp = admin.post(f"/admin/resend-contract/{rid}")
    assert resp.status_code == 409 and resp.get_json()["signed"] is True
    assert get_restaurant(rid, db_path).contract_status == "signed"


def test_an_amendment_sends_a_new_envelope_and_keeps_the_signed_contract(db_path, admin, monkeypatch):
    rid = _rid(db_path, contract_status="signed", docusign_envelope_id="env_s", module_reviews=1)
    monkeypatch.setattr(docusign_helper, "send_contract", lambda **k: {"envelope_id": "env_amend"})
    resp = admin.post(f"/admin/resend-contract/{rid}", json={"amendment": True})
    assert resp.status_code == 200 and resp.get_json()["amendment"] is True
    r = get_restaurant(rid, db_path)
    assert r.contract_status == "signed" and r.docusign_envelope_id == "env_amend"


def test_an_unsigned_contract_is_resent_as_the_same_envelope(db_path, admin, monkeypatch):
    rid = _rid(db_path, contract_status="sent", module_reviews=1)
    monkeypatch.setattr(docusign_helper, "send_contract", lambda **k: {"envelope_id": "env_1"})
    resent = []
    monkeypatch.setattr(docusign_helper, "resend_envelope",
                        lambda env, restaurant_id=None: resent.append(env) or {"ok": True, "status": "sent"})
    assert admin.post(f"/admin/resend-contract/{rid}").get_json()["new_envelope"] is True
    for _ in range(2):
        assert admin.post(f"/admin/resend-contract/{rid}").get_json()["resent"] is True
    assert resent == ["env_1", "env_1"]
    (env,) = _rows(db_path, "SELECT resend_count, module_count FROM docusign_envelopes WHERE envelope_id='env_1'")
    assert env["resend_count"] == 2 and env["module_count"] == 4
    # No synthetic 'contract' email row (DocuSign sends that email itself).
    assert _rows(db_path, "SELECT * FROM email_log WHERE email_type='contract'") == []


def test_changed_terms_or_a_declined_envelope_get_a_new_one(db_path, admin, monkeypatch):
    rid = _rid(db_path, contract_status="sent", module_reviews=1)
    made = iter(["env_1", "env_2", "env_3"])
    monkeypatch.setattr(docusign_helper, "send_contract", lambda **k: {"envelope_id": next(made)})
    monkeypatch.setattr(docusign_helper, "resend_envelope", lambda *a, **k: pytest.fail("terms changed"))
    admin.post(f"/admin/resend-contract/{rid}")
    update_restaurant(rid, {"module_labor": 0}, db_path=db_path)          # the terms changed
    assert admin.post(f"/admin/resend-contract/{rid}").get_json()["envelope_id"] == "env_2"
    conn = models.get_conn(db_path)
    conn.execute("UPDATE docusign_envelopes SET status='declined' WHERE envelope_id='env_2'")
    conn.commit(); conn.close()
    assert admin.post(f"/admin/resend-contract/{rid}").get_json()["envelope_id"] == "env_3"
