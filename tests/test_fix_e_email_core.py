"""Fix round E — the email core: every sender returns a SendResult (#12, #16,
#109), mail is logged against its restaurant (#119), email_log.sent_at is UTC
(#89), suppressions are scoped and audited (#45), operator mail is never
suppressed (#101), delivery status only moves forward (#59), Resend webhook
health is recorded (#59, #74), CAN-SPAM (#159) and kept-secret pay links
(#133)."""
import base64
import hashlib
import hmac
import json
import time

import pytest
from flask import Flask

import auth
import client_api
import emails
import models
import webhook_routes
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, client_api, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


class _Resp:
    def __init__(self, code, body='{"id":"msg_abc"}'):
        self.status_code, self.text = code, body

    def json(self):
        return json.loads(self.text)


def _stub_resend(monkeypatch, responses):
    seq = responses if isinstance(responses, list) else [responses]
    sent = []

    def post(url, headers=None, json=None, timeout=None, **kw):
        sent.append(json)
        return seq[min(len(sent) - 1, len(seq) - 1)]
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr(emails, "_resend_key", lambda: "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return sent


def _rid(db_path, **kw):
    kw.setdefault("name", "Core Co")
    kw.setdefault("owner_email", "owner@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _rows(db_path, sql, args=()):
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── SendResult: every failure says what kind it was ─────────────────────────

def test_send_result_kinds():
    assert emails.SendResult(False, status_code=503, attempts=3).transient
    assert emails.SendResult(False, error="timeout", attempts=2, reason="transient").transient
    assert emails.SendResult(False, status_code=422, attempts=1).refused
    assert emails.SendResult(False, error="recipient suppressed (bounced/complained)", attempts=0).refused
    assert emails.not_sent("nothing_to_send").skipped
    assert emails.not_sent("not_configured").deferred
    assert emails.not_sent("no_postal_address").deferred
    ok = emails.SendResult(True)
    assert not (ok.transient or ok.refused or ok.skipped or ok.deferred)


@pytest.mark.parametrize("call", [
    lambda: emails.send_2fa_code("a@x.test", "R", "123456"),
    lambda: emails.send_login_notification("a@x.test", "R"),
    lambda: emails.send_password_reset_email("a@x.test", "https://x/r/t"),
    lambda: emails.send_password_reset_code_email("a@x.test", "123456"),
    lambda: emails.send_signup_welcome_email("a@x.test", "R"),
    lambda: emails.send_signup_admin_alert("R", "O", "a@x.test"),
    lambda: emails.send_payment_email("a@x.test", "R", module_count=1, restaurant_id=1),
    lambda: emails.send_welcome_email("a@x.test", "R", "u", set_password_url="https://x/s"),
    lambda: emails.send_supplier_order_email("s@x.test", "S", "R", "PO-1", [], 0),
    lambda: emails.send_team_invite_email("a@x.test", "R", "u", "p"),
    lambda: emails.send_onboarding_day2("a@x.test", "R"),
    lambda: emails.send_onboarding_day7("a@x.test", "R"),
    lambda: emails.send_onboarding_day30("a@x.test", "R"),
    lambda: emails.send_reactivation_email("a@x.test", "R"),
    lambda: emails.send_monthly_summary_email("a@x.test", "R"),
    lambda: emails.send_password_changed_email("a@x.test", "R"),
    lambda: emails.send_email_changed_email("a@x.test", "R", "b@x.test"),
    lambda: emails.send_payment_failed_client_email("a@x.test", "R", 10.0),
    lambda: emails.send_recovery_email_code("a@x.test", "123456"),
    lambda: emails.send_account_deletion_request_email("R", "O", "a@x.test", "now"),
    lambda: emails.send_bug_report_email("R", "a@x.test", "m", {}),
    lambda: emails.send_lifecycle_email(60, "a@x.test", "R", restaurant_id=1),
    lambda: emails.send_quarterly_summary_email("a@x.test", "R", restaurant_id=1),
    lambda: emails.send_value_recap_email(1),
    lambda: emails.send_onboarding_nudge({"key": "voice"}, "a@x.test", "R"),
], ids=lambda f: "sender")
def test_every_sender_returns_a_send_result_never_none(db_path, call):
    """With no key configured every sender says so in a SendResult — it
    used to return None, and callers marked the email sent anyway."""
    _rid(db_path)
    result = call()
    assert isinstance(result, emails.SendResult), result
    assert result.ok is False


def test_client_facing_mail_is_logged_against_its_restaurant(db_path, monkeypatch):
    rid = _rid(db_path, module_reviews=1)
    _stub_resend(monkeypatch, _Resp(200))
    assert emails.send_payment_email("owner@x.test", "Core Co", module_count=1, restaurant_id=rid).ok
    assert emails.send_team_invite_email("mate@x.test", "Core Co", "mate", "pw", restaurant_id=rid).ok
    assert emails.send_reactivation_email("owner@x.test", "Core Co", restaurant_id=rid).ok
    types = {r["email_type"] for r in _rows(db_path, "SELECT email_type FROM email_log WHERE restaurant_id=?",
                                            (rid,))}
    assert {"send_payment_email", "send_team_invite_email", "send_reactivation_email"} <= types


# ── #12: the welcome email carries a set-password link, not a password ────

def test_the_welcome_email_sends_a_set_password_link_and_changes_nothing(db_path, monkeypatch):
    rid = _rid(db_path, module_reviews=1)
    auth.init_auth(db_path)
    uid = auth.create_user(rid, "owner1", "owner@x.test", "Original-pass-9", db_path=db_path)
    before = _rows(db_path, "SELECT password_hash FROM users WHERE id=?", (uid,))[0]["password_hash"]
    sent = _stub_resend(monkeypatch, _Resp(200))
    result = emails.send_welcome_with_set_password_link(uid, restaurant_id=rid)
    assert result.ok
    html = sent[0]["html"]
    assert "/reset-password/" in html and "Temporary password" not in html
    assert _rows(db_path, "SELECT password_hash FROM users WHERE id=?", (uid,))[0]["password_hash"] == before
    row = _rows(db_path, "SELECT restaurant_id, email_type, status FROM email_log")[0]
    assert row == {"restaurant_id": rid, "email_type": "send_welcome_set_password_email", "status": "sent"}
    token = html.split("/reset-password/", 1)[1].split('"', 1)[0]
    assert models.validate_reset_token(token, db_path=db_path)["id"] == uid


def test_a_failed_welcome_leaves_the_login_untouched(db_path, monkeypatch):
    rid = _rid(db_path)
    auth.init_auth(db_path)
    uid = auth.create_user(rid, "owner2", "owner@x.test", "Original-pass-9", db_path=db_path)
    before = _rows(db_path, "SELECT password_hash FROM users WHERE id=?", (uid,))[0]["password_hash"]
    _stub_resend(monkeypatch, _Resp(422, '{"message":"invalid"}'))
    result = emails.send_welcome_with_set_password_link(uid, restaurant_id=rid)
    assert not result.ok and result.refused
    assert _rows(db_path, "SELECT password_hash FROM users WHERE id=?", (uid,))[0]["password_hash"] == before
    assert _rows(db_path, "SELECT status FROM email_log")[0]["status"] == "failed"


# ── #89: email_log.sent_at is UTC, migrated once ────────────────────────────

def test_log_email_writes_utc(db_path):
    from datetime import datetime, timezone
    models.log_email(None, "x", "a@x.test", "s", db_path=db_path)
    stamp = _rows(db_path, "SELECT sent_at FROM email_log")[0]["sent_at"]
    got = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - got).total_seconds()) < 120


def test_the_chicago_rows_are_moved_to_utc_exactly_once(db_path):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM data_migrations WHERE name=?", (models.EMAIL_LOG_UTC_MIGRATION,))
    conn.execute("INSERT INTO email_log (email_type, sent_at) VALUES ('a', '2026-09-28 22:14:07')")   # CDT
    conn.execute("INSERT INTO email_log (email_type, sent_at) VALUES ('b', '2026-01-15 09:00:00')")   # CST
    conn.commit()
    conn.close()
    models.init_email_log(db_path)
    got = [r["sent_at"] for r in _rows(db_path, "SELECT sent_at FROM email_log ORDER BY id")]
    assert got == ["2026-09-29 03:14:07", "2026-01-15 15:00:00"]
    models.init_email_log(db_path)          # a second boot must not shift them again
    assert [r["sent_at"] for r in _rows(db_path, "SELECT sent_at FROM email_log ORDER BY id")] == got
    assert _rows(db_path, "SELECT name FROM data_migrations WHERE name=?", (models.EMAIL_LOG_UTC_MIGRATION,))


def test_the_flood_guard_counts_a_utc_window(db_path, monkeypatch):
    for _ in range(emails.FLOOD_LIMITS["send_2fa_code"][0]):
        models.log_email(None, "send_2fa_code", "f@x.test", "s", db_path=db_path)
    assert emails._flood_guard_ok("send_2fa_code", "f@x.test") is False


def test_the_client_history_carries_the_local_time_beside_utc(db_path):
    rid = _rid(db_path, timezone="America/Chicago")
    conn = get_conn(db_path)
    conn.execute("INSERT INTO email_log (restaurant_id, email_type, sent_at, status) "
                 "VALUES (?, 'x', '2026-09-29 03:14:07', 'sent')", (rid,))
    conn.commit()
    conn.close()
    row = models.get_email_log_for_client(rid, db_path=db_path)[0]
    assert row["sent_at"] == "2026-09-29 03:14:07" and row["sent_at_local"] == "2026-09-28 22:14:07"


# ── #45 / #101: scoped, audited suppressions; operator mail never ────────────

def test_a_marketing_suppression_stops_only_cavnar_marketing(db_path):
    models.suppress_email("o@x.test", "complained", scope="marketing", db_path=db_path)
    assert models.is_email_suppressed("o@x.test", db_path=db_path, email_type="send_onboarding_day7")
    assert not models.is_email_suppressed("o@x.test", db_path=db_path, email_type="digest")
    assert not models.is_email_suppressed("o@x.test", db_path=db_path, email_type="alert")


def test_scopes_only_widen(db_path):
    models.suppress_email("w@x.test", "complained", scope="guest", db_path=db_path)
    models.suppress_email("w@x.test", "complained", scope="marketing", db_path=db_path)
    assert models.get_email_suppressions(db_path=db_path)[0]["scope"] == "guest,marketing"
    models.suppress_email("w@x.test", "bounced", scope="all", db_path=db_path)
    models.suppress_email("w@x.test", "complained", scope="marketing", db_path=db_path)
    row = models.get_email_suppressions(db_path=db_path)[0]
    assert row["scope"] == "all" and row["reason"] == "bounced"


def test_the_operator_address_is_never_suppressed(db_path, monkeypatch):
    monkeypatch.setenv("WILL_EMAIL", "ops-owner@x.test")
    assert models.suppress_email("ops-owner@x.test", "bounced", db_path=db_path) is False
    assert _rows(db_path, "SELECT * FROM email_suppressions") == []
    assert any(r["job"] == "operator_email_bounce" for r in _rows(db_path, "SELECT job FROM job_failures"))


def test_operator_mail_goes_out_even_to_a_suppressed_row(db_path, monkeypatch):
    """A row written before the rule (or by hand) must not stop ops mail."""
    monkeypatch.setenv("WILL_EMAIL", "ops-owner@x.test")
    conn = get_conn(db_path)
    conn.execute("INSERT INTO email_suppressions (email, reason, scope) VALUES ('ops-owner@x.test','bounced','all')")
    conn.commit()
    conn.close()
    _stub_resend(monkeypatch, _Resp(200))
    assert emails.deliver({"to": ["ops-owner@x.test"], "subject": "backup", "html": "x"},
                          email_type="ops_backup").ok


def test_an_unattempted_operator_send_raises(db_path, monkeypatch):
    monkeypatch.setattr(emails, "_resend_key", lambda: "")
    with pytest.raises(emails.EmailNotSent):
        emails.deliver_or_raise({"to": ["will@cavnar.ai"], "subject": "digest", "html": "x"},
                                email_type="ops_failure_digest")
    # A client send that was never attempted is still a decision, not raised.
    assert emails.deliver_or_raise({"to": ["o@x.test"], "subject": "s", "html": "x"},
                                   email_type="digest").ok is False


def test_unsuppress_is_audited(db_path):
    models.suppress_email("back@x.test", "bounced", "550 full", db_path=db_path)
    lifted = models.unsuppress_email("back@x.test", actor="will", reason="mailbox emptied", db_path=db_path)
    assert lifted["reason"] == "bounced" and lifted["scope"] == "all"
    assert not models.is_email_suppressed("back@x.test", db_path=db_path)
    ev = _rows(db_path, "SELECT event_type, email, summary FROM admin_events")
    assert ev and ev[-1]["event_type"] == "email.unsuppressed" and "will" in ev[-1]["summary"]
    assert models.unsuppress_email("back@x.test", actor="will", db_path=db_path) is None


def test_suppressions_are_listed_per_client_with_the_role(db_path):
    rid = _rid(db_path, owner_email="own@x.test")
    models.update_restaurant(rid, {"alert_extra_emails": "gm@x.test"}, db_path=db_path)
    models.suppress_email("own@x.test", "bounced", db_path=db_path)
    models.suppress_email("gm@x.test", "complained", scope="marketing", db_path=db_path)
    models.suppress_email("stranger@x.test", "bounced", db_path=db_path)
    got = {r["email"]: r for r in models.suppressions_for_restaurant(rid, db_path=db_path)}
    assert set(got) == {"own@x.test", "gm@x.test"}
    assert got["own@x.test"]["roles"] == ["owner"] and got["gm@x.test"]["roles"] == ["alert copy"]
    assert [r["email"] for r in models.get_email_suppressions(db_path=db_path, q="strang")] == ["stranger@x.test"]


# ── #59 / #74: the Resend webhook ───────────────────────────────────────────

def _svix(body, secret="whsec_" + base64.b64encode(b"topsecret").decode()):
    msg_id, ts = "msg_1", str(int(time.time()))
    key = base64.b64decode(secret.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}",
            "Content-Type": "application/json"}, secret


@pytest.fixture
def web(monkeypatch):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(webhook_routes.webhook_bp)
    app.register_blueprint(client_api.client_bp)
    return app.test_client()


def _post_event(web, monkeypatch, event):
    body = json.dumps(event).encode()
    headers, secret = _svix(body)
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", secret)
    return web.post("/webhooks/resend", data=body, headers=headers)


def test_a_late_delivered_event_does_not_undo_a_bounce(db_path, web, monkeypatch):
    models.log_email(1, "digest", "b@x.test", "s", db_path=db_path, message_id="m1")
    _post_event(web, monkeypatch, {"type": "email.bounced", "data": {"email_id": "m1", "to": ["b@x.test"]}})
    _post_event(web, monkeypatch, {"type": "email.delivered", "data": {"email_id": "m1", "to": ["b@x.test"]}})
    assert _rows(db_path, "SELECT status FROM email_log")[0]["status"] == "bounced"
    models.log_email(1, "digest", "d@x.test", "s", db_path=db_path, message_id="m2")
    assert models.mark_email_delivery_event("m2", "delivered", db_path=db_path)
    assert not models.mark_email_delivery_event("m2", "delayed", db_path=db_path)


def test_a_complaint_about_owner_mail_suppresses_only_marketing(db_path, web, monkeypatch):
    models.log_email(1, "send_onboarding_day7", "own@x.test", "tips", db_path=db_path, message_id="c1")
    _post_event(web, monkeypatch, {"type": "email.complained", "data": {"email_id": "c1", "to": ["own@x.test"]}})
    row = models.get_email_suppressions(db_path=db_path)[0]
    assert row["scope"] == "marketing"
    assert not models.is_email_suppressed("own@x.test", db_path=db_path, email_type="alert")


def test_the_last_verified_event_and_bounces_are_recorded(db_path, web, monkeypatch):
    _post_event(web, monkeypatch, {"type": "email.bounced", "data": {"email_id": "zz", "to": ["x@x.test"]}})
    h = models.inbound_webhook_health(db_path=db_path)["resend"]
    assert h["last_verified_at"] and h["bounces"] == 1 and h["last_event_type"] == "email.bounced"
    assert h["stale"] is False


def test_signature_failures_are_counted_and_captured_once_an_hour(db_path, web, monkeypatch):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", "whsec_" + base64.b64encode(b"k").decode())
    for _ in range(3):
        assert web.post("/webhooks/resend", json={"type": "email.bounced"}).status_code == 403
    h = models.inbound_webhook_health(db_path=db_path)["resend"]
    assert h["failed_count"] == 3 and h["failing"]
    caps = [r for r in _rows(db_path, "SELECT job FROM job_failures") if r["job"] == "webhook_signature_resend"]
    assert len(caps) == 1


# ── #159: CAN-SPAM on Cavnar AI's own marketing; escaped signup fields ──────

def test_marketing_is_not_sent_without_a_postal_address(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.delenv("CAVNAR_POSTAL_ADDRESS", raising=False)
    sent = _stub_resend(monkeypatch, _Resp(200))
    result = emails.deliver({"to": ["o@x.test"], "subject": "tips", "html": "<p>x</p>"},
                            restaurant_id=rid, email_type="send_onboarding_day2")
    assert not result.ok and result.reason == "no_postal_address" and result.deferred
    assert sent == []


def test_marketing_carries_the_postal_address_and_an_opt_out(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setenv("CAVNAR_POSTAL_ADDRESS", "1 Real Rd, Geneva, IL 60134")
    sent = _stub_resend(monkeypatch, _Resp(200))
    emails.deliver({"to": ["o@x.test"], "subject": "tips", "html": "<p>x</p>"},
                   restaurant_id=rid, email_type="send_onboarding_day2")
    assert "1 Real Rd, Geneva, IL 60134" in sent[0]["html"] and "/u/" in sent[0]["html"]


def test_a_referral_opt_out_is_signed_over_the_address(db_path, web, monkeypatch):
    rid = _rid(db_path)
    sent = _stub_resend(monkeypatch, _Resp(200))
    emails.deliver({"to": ["friend@x.test"], "subject": "hi", "html": "<p>x</p>"},
                   restaurant_id=rid, email_type="referral")
    url = sent[0]["headers"]["List-Unsubscribe"].strip("<>")
    path = "/" + url.split("://", 1)[1].split("/", 1)[1]
    assert path.startswith("/u/a.")
    assert web.get(path).status_code == 200 and not models.is_email_suppressed(
        "friend@x.test", db_path=db_path, email_type="referral")          # a GET only asks
    assert web.post(path).status_code == 200
    assert models.is_email_suppressed("friend@x.test", db_path=db_path, email_type="referral")
    assert not models.is_email_suppressed("friend@x.test", db_path=db_path, email_type="digest")
    # The referrer's own restaurant was not unsubscribed.
    assert not models.get_restaurant(rid, db_path=db_path).marketing_emails_opt_out


def test_signup_emails_escape_what_a_stranger_typed(db_path, monkeypatch):
    sent = _stub_resend(monkeypatch, _Resp(200))
    evil = '<a href="https://evil.test">Claim</a>'
    emails.send_signup_welcome_email("n@x.test", evil, owner_name=evil)
    emails.send_signup_admin_alert(evil, evil, "n@x.test", phone=evil)
    for p in sent:
        assert '<a href="https://evil.test">' not in p["html"]


# ── #133: pay links survive a SECRET_KEY rotation ───────────────────────────

def test_pay_links_are_signed_with_a_kept_secret(db_path, monkeypatch):
    rid = _rid(db_path)
    token = emails.pay_link(rid).split("/pay/", 1)[1].split("/", 1)[0]
    assert emails.read_pay_token(token) == rid
    monkeypatch.setenv("SECRET_KEY", "rotated-" + "x" * 20)
    assert emails.read_pay_token(token) == rid, "a SECRET_KEY rotation broke an emailed pay link"
    assert emails.read_pay_token(f"{rid + 1}.{token.split('.', 1)[1]}") is None


def test_an_old_secret_key_pay_link_still_verifies_until_that_key_changes(db_path, monkeypatch):
    rid = _rid(db_path)
    legacy = f"{rid}.{emails._legacy_pay_sig(rid)}"
    assert emails.read_pay_token(legacy) == rid
    monkeypatch.setenv("SECRET_KEY", "a-different-key-entirely")
    assert emails.read_pay_token(legacy) is None


def test_a_rotated_pay_secret_keeps_older_links_working(db_path):
    rid = _rid(db_path)
    old = emails.pay_link(rid).split("/pay/", 1)[1].split("/", 1)[0]
    models.rotate_kept_secret("pay_links", db_path=db_path)
    new = emails.pay_link(rid).split("/pay/", 1)[1].split("/", 1)[0]
    assert old != new and emails.read_pay_token(old) == rid and emails.read_pay_token(new) == rid
