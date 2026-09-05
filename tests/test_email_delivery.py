"""Email delivery: status tracking, retry, suppression, unsubscribe, history.

Before this, a send that failed was indistinguishable from one that
arrived — senders returned a bool nobody read, and email_log recorded
'sent' unconditionally because log_email() had no status parameter.
"""
import base64
import hashlib
import hmac
import json

import pytest
from flask import Flask

import auth
import client_api
import emails
import models
import webhook_routes
from client_api import client_bp
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(webhook_routes.webhook_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Delivery Co")
    kw.setdefault("owner_email", "owner@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _login_as(monkeypatch, rid, email="owner@x.test"):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0,
                                 "username": "webuser", "email": email})


class _Resp:
    def __init__(self, code, body='{"id":"msg_abc"}'):
        self.status_code, self.text = code, body

    def json(self):
        return json.loads(self.text)


def _stub_post(monkeypatch, responses):
    """responses: list of _Resp (consumed in order) or a single _Resp."""
    seq = responses if isinstance(responses, list) else [responses]
    calls = {"n": 0}

    def post(url, headers=None, json=None, timeout=None, **kw):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, len(seq) - 1)]

    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr(emails, "_resend_key", lambda: "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)   # keep retries instant
    return calls


# ── Delivery status (#1) ─────────────────────────────────────────────────

def test_a_delivered_email_is_logged_as_sent_with_its_message_id(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _stub_post(monkeypatch, _Resp(200))
    result = emails.deliver({"to": ["a@b.test"], "subject": "Hi", "html": "<p>x</p>"},
                            restaurant_id=rid, email_type="send_welcome_email")
    assert result.ok and result.message_id == "msg_abc"
    row = get_conn(db_path).execute("SELECT status, message_id FROM email_log").fetchone()
    assert row["status"] == "sent" and row["message_id"] == "msg_abc"


def test_a_failed_email_is_logged_as_failed_with_the_real_error(db_path, monkeypatch):
    """The whole point: this row used to say 'sent'."""
    rid = _restaurant(db_path)
    _stub_post(monkeypatch, _Resp(422, "invalid recipient"))
    result = emails.deliver({"to": ["bad"], "subject": "Hi", "html": "<p>x</p>"},
                            restaurant_id=rid, email_type="send_welcome_email")
    assert result.ok is False and result.status_code == 422
    row = get_conn(db_path).execute("SELECT status, error FROM email_log").fetchone()
    assert row["status"] == "failed" and "invalid recipient" in row["error"]


def test_the_result_stays_truthy_for_old_bool_call_sites(db_path, monkeypatch):
    _stub_post(monkeypatch, _Resp(200))
    assert bool(emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "x"})) is True
    _stub_post(monkeypatch, _Resp(500, "nope"))
    assert bool(emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "x"})) is False


# ── Retry (#6) ───────────────────────────────────────────────────────────

def test_a_transient_failure_is_retried_and_can_succeed(db_path, monkeypatch):
    calls = _stub_post(monkeypatch, [_Resp(503, "down"), _Resp(200)])
    result = emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "x"})
    assert result.ok and result.attempts == 2 and calls["n"] == 2


def test_a_permanent_failure_is_not_retried(db_path, monkeypatch):
    """Retrying a bad address just makes the same mistake three times."""
    calls = _stub_post(monkeypatch, _Resp(422, "bad address"))
    result = emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "x"})
    assert result.ok is False and calls["n"] == 1


def test_retries_give_up_after_three_attempts(db_path, monkeypatch):
    calls = _stub_post(monkeypatch, _Resp(503, "still down"))
    result = emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "x"})
    assert result.ok is False and result.attempts == 3 and calls["n"] == 3


def test_a_network_exception_is_caught_and_retried(db_path, monkeypatch):
    def boom(*a, **kw):
        raise ConnectionError("dns fail")
    monkeypatch.setattr("requests.post", boom)
    monkeypatch.setattr(emails, "_resend_key", lambda: "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    result = emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "x"})
    assert result.ok is False and result.attempts == 3 and "dns fail" in result.error


# ── Suppression + webhook (#2) ───────────────────────────────────────────

def test_a_bounced_address_is_suppressed_and_then_not_mailed(db_path, monkeypatch):
    from models import suppress_email
    rid = _restaurant(db_path)
    suppress_email("gone@x.test", "bounced", "550 no such user", db_path=db_path)
    calls = _stub_post(monkeypatch, _Resp(200))
    result = emails.deliver({"to": ["gone@x.test"], "subject": "s", "html": "x"},
                            restaurant_id=rid, email_type="send_onboarding_day2")
    assert result.ok is False
    assert calls["n"] == 0                      # never hit the network at all
    assert "suppressed" in result.error


def test_security_email_still_reaches_a_suppressed_address(db_path, monkeypatch):
    """Locking someone out of their own account because a newsletter
    bounced would be worse than the problem suppression solves."""
    from models import suppress_email
    suppress_email("gone@x.test", "bounced", db_path=db_path)
    calls = _stub_post(monkeypatch, _Resp(200))
    result = emails.deliver({"to": ["gone@x.test"], "subject": "code", "html": "x"},
                            email_type="send_2fa_code")
    assert result.ok is True and calls["n"] == 1


def _svix_headers(body: bytes, secret="whsec_" + base64.b64encode(b"topsecret").decode()):
    msg_id, ts = "msg_1", "1700000000"
    key = base64.b64decode(secret.split("_", 1)[1])
    sig = base64.b64encode(
        hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}, secret


def test_the_resend_webhook_rejects_an_unsigned_request(client, db_path, monkeypatch):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET",
                        "whsec_" + base64.b64encode(b"topsecret").decode())
    resp = client.post("/webhooks/resend", json={"type": "email.bounced"})
    assert resp.status_code == 403


def test_a_signed_bounce_suppresses_the_address(client, db_path, monkeypatch):
    from models import is_email_suppressed
    body = json.dumps({"type": "email.bounced",
                       "data": {"email_id": "msg_abc", "to": ["gone@x.test"],
                                "bounce": {"message": "550 no such user"}}}).encode()
    headers, secret = _svix_headers(body)
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", secret)
    resp = client.post("/webhooks/resend", data=body,
                       headers={**headers, "Content-Type": "application/json"})
    assert resp.status_code == 200
    assert is_email_suppressed("gone@x.test", db_path=db_path) is True


def test_a_signed_complaint_suppresses_the_address(client, db_path, monkeypatch):
    from models import is_email_suppressed
    body = json.dumps({"type": "email.complained",
                       "data": {"email_id": "m2", "to": ["angry@x.test"]}}).encode()
    headers, secret = _svix_headers(body)
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", secret)
    client.post("/webhooks/resend", data=body,
                headers={**headers, "Content-Type": "application/json"})
    assert is_email_suppressed("angry@x.test", db_path=db_path) is True


def test_a_delivered_event_does_not_suppress_anyone(client, db_path, monkeypatch):
    from models import is_email_suppressed
    body = json.dumps({"type": "email.delivered",
                       "data": {"email_id": "m3", "to": ["fine@x.test"]}}).encode()
    headers, secret = _svix_headers(body)
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", secret)
    client.post("/webhooks/resend", data=body,
                headers={**headers, "Content-Type": "application/json"})
    assert is_email_suppressed("fine@x.test", db_path=db_path) is False


def test_a_webhook_event_reconciles_the_logged_row(db_path, monkeypatch):
    from models import mark_email_delivery_event
    rid = _restaurant(db_path)
    _stub_post(monkeypatch, _Resp(200))
    emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "x"},
                   restaurant_id=rid, email_type="send_welcome_email")
    assert mark_email_delivery_event("msg_abc", "bounced", "550") is True
    row = get_conn(db_path).execute("SELECT status FROM email_log").fetchone()
    assert row["status"] == "bounced"


# ── Unsubscribe (#3) ─────────────────────────────────────────────────────

def test_marketing_email_carries_an_unsubscribe_link_and_headers(db_path, monkeypatch):
    rid = _restaurant(db_path)
    sent = {}
    monkeypatch.setattr(emails, "_resend_key", lambda: "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("requests.post",
                        lambda url, headers=None, json=None, timeout=None, **kw: (sent.update(json or {}), _Resp(200))[1])
    emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "<html><body><p>hi</p></body></html>"},
                   restaurant_id=rid, email_type="send_onboarding_day2")
    assert "/u/" in sent["html"]
    assert "Unsubscribe" in sent["html"]
    assert sent["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert "/u/" in sent["headers"]["List-Unsubscribe"]


def test_transactional_email_gets_no_unsubscribe(db_path, monkeypatch):
    """A supplier order or 2FA code must never offer to turn itself off."""
    rid = _restaurant(db_path)
    sent = {}
    monkeypatch.setattr(emails, "_resend_key", lambda: "fake-key")
    monkeypatch.setattr("requests.post",
                        lambda url, headers=None, json=None, timeout=None, **kw: (sent.update(json or {}), _Resp(200))[1])
    emails.deliver({"to": ["a@b.test"], "subject": "s", "html": "<html><body><p>hi</p></body></html>"},
                   restaurant_id=rid, email_type="send_supplier_order_email")
    assert "/u/" not in sent["html"]
    assert "headers" not in sent


def test_the_unsubscribe_link_actually_opts_the_restaurant_out(client, db_path):
    from models import unsubscribe_token, get_restaurant
    rid = _restaurant(db_path)
    assert not get_restaurant(rid, db_path=db_path).marketing_emails_opt_out
    resp = client.get(f"/u/{unsubscribe_token(rid)}")
    assert resp.status_code == 200
    assert get_restaurant(rid, db_path=db_path).marketing_emails_opt_out == 1


def test_one_click_post_also_works(client, db_path):
    """RFC 8058: the mail client POSTs, it doesn't render the page."""
    from models import unsubscribe_token, get_restaurant
    rid = _restaurant(db_path)
    assert client.post(f"/u/{unsubscribe_token(rid)}").status_code == 200
    assert get_restaurant(rid, db_path=db_path).marketing_emails_opt_out == 1


def test_a_forged_unsubscribe_token_changes_nothing(client, db_path):
    from models import get_restaurant
    rid = _restaurant(db_path)
    assert client.get(f"/u/{rid}.forgedsignature").status_code == 404
    assert not get_restaurant(rid, db_path=db_path).marketing_emails_opt_out


# ── Client email history + preview (#5, #7) ──────────────────────────────

def test_email_history_shows_this_restaurants_mail_with_real_status(client, db_path, monkeypatch):
    from models import log_email
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    log_email(rid, "send_staff_schedule_email", "chef@x.test", "Your schedule", db_path=db_path)
    log_email(rid, "send_supplier_order_email", "bad@x.test", "Order", db_path=db_path,
              status="failed", error="550 rejected")
    rows = client.get("/api/email-history").get_json()["emails"]
    assert [r["label"] for r in rows] == ["Supplier order", "Staff schedule"]
    assert rows[0]["failed"] is True and rows[1]["failed"] is False


def test_email_history_never_leaks_another_restaurants_mail(client, db_path, monkeypatch):
    from models import log_email
    mine, theirs = _restaurant(db_path), _restaurant(db_path, name="Other Co")
    log_email(theirs, "send_welcome_email", "them@x.test", "Theirs", db_path=db_path)
    _login_as(monkeypatch, mine)
    assert client.get("/api/email-history").get_json()["emails"] == []


def test_email_history_hides_raw_provider_errors_from_owners(client, db_path, monkeypatch):
    from models import log_email
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid)
    log_email(rid, "send_welcome_email", "a@x.test", "S", db_path=db_path,
              status="failed", error="SMTP 550 internal relay detail")
    row = client.get("/api/email-history").get_json()["emails"][0]
    assert "error" not in row and row["failed"] is True


def test_web_preview_digest_sends_and_is_logged(client, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid, email="me@x.test")
    monkeypatch.setattr("reporter.build_report_from_db", lambda *a, **kw: {"stub": True})
    monkeypatch.setattr("reporter.render_html", lambda *a, **kw: "<p>digest</p>")
    _stub_post(monkeypatch, _Resp(200))
    data = client.post("/api/send-test-digest").get_json()
    assert data["ok"] is True and data["email"] == "me@x.test"
    row = get_conn(db_path).execute(
        "SELECT email_type, status FROM email_log ORDER BY id DESC").fetchone()
    assert row["email_type"] == "digest_preview" and row["status"] == "sent"


def test_web_preview_digest_reports_a_send_failure(client, db_path, monkeypatch):
    """It must not claim success when the send failed — the exact class of
    lie this whole change set exists to remove."""
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid, email="me@x.test")
    monkeypatch.setattr("reporter.build_report_from_db", lambda *a, **kw: {"stub": True})
    monkeypatch.setattr("reporter.render_html", lambda *a, **kw: "<p>digest</p>")
    _stub_post(monkeypatch, _Resp(500, "resend down"))
    resp = client.post("/api/send-test-digest")
    assert resp.status_code == 502
    assert resp.get_json()["ok"] is False


def test_ask_cavnar_rate_limit_is_five_per_minute():
    """Lowered from 10; each question can now cost up to _MAX_TOOL_ROUNDS
    model calls, so 10/min under-priced the actual cost."""
    import inspect
    import client_api
    src = inspect.getsource(client_api._do_ask_cavnar)
    assert 'max_calls=5' in src
    src2 = inspect.getsource(client_api._ask_cavnar_stream_response)
    assert 'max_calls=5' in src2
