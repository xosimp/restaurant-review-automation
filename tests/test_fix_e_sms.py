"""Fix round E — SMS: every attempt in sms_log (#14), Twilio's status
callback signed and recorded (#14, #74), one textable() check honouring a
platform STOP in every sender (#107), and issue texts claimed, linked once,
retried with backoff and handed to the owner on a permanent failure (#91)."""
import base64
import hashlib
import hmac
import json

import pytest
from flask import Flask

import auth
import models
import notify
import webhook_routes
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import issues, intraday, people, push
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, notify, issues, intraday, people, push, auth, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(notify, "DB_PATH", db_path)
    # guest_sms_optouts (the platform STOP) and users are created by their
    # own modules' boot-time init, as hosted_dashboard does.
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path)
    auth.init_auth(db_path)


@pytest.fixture
def twilio(monkeypatch):
    """Twilio configured, and requests.post answered from a script."""
    monkeypatch.setattr(notify, "TWILIO_SID", "AC_test")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "tok_test")
    monkeypatch.setattr(notify, "TWILIO_FROM", "+15550000000")
    monkeypatch.setattr(notify, "TWILIO_MESSAGING_SERVICE_SID", "MG_test")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    state = {"answers": [], "posted": []}

    class _R:
        def __init__(self, code, body):
            self.status_code, self._body = code, body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    def post(url, **kw):
        state["posted"].append(kw.get("data"))
        code, body = state["answers"].pop(0) if state["answers"] else (201, {"sid": f"SM{len(state['posted'])}",
                                                                              "status": "queued"})
        return _R(code, body)
    monkeypatch.setattr(notify.requests, "post", post)
    return state


def _rid(db_path, **kw):
    kw.setdefault("name", "Text Co")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _contact(db_path, rid, name="GM", phone="+15555550100", consent=1):
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,?)",
                       (rid, name, phone, consent)).lastrowid
    conn.commit()
    conn.close()
    return cid


def _stop(db_path, phone):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO guest_sms_optouts (restaurant_id, phone) VALUES (0, ?)", (notify._normalize_phone(phone),))
    conn.commit()
    conn.close()


def _rows(db_path, sql, args=()):
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── #14: the SMS ledger ──────────────────────────────────────────────────────

def test_every_attempt_is_logged_with_its_outcome(db_path, twilio):
    rid = _rid(db_path)
    with notify.sms_context(rid):
        assert notify.send_sms("6305550100", "hi") is True
    twilio["answers"].append((400, {"code": 21211, "message": "Invalid 'To' Phone Number"}))
    res = notify.send_sms_result("+1555", "hi", restaurant_id=rid)
    assert not res.ok and res.permanent and res.error_code == "21211"
    rows = _rows(db_path, "SELECT restaurant_id, status, error_code, provider_sid, to_last4, to_hash FROM sms_log ORDER BY id")
    assert rows[0]["status"] == "queued" and rows[0]["provider_sid"] == "SM1" and rows[0]["restaurant_id"] == rid
    assert rows[0]["to_last4"] == "0100" and "6305550100" not in rows[0]["to_hash"]
    assert rows[1]["status"] == "failed" and rows[1]["error_code"] == "21211"
    # The text asks Twilio to report back.
    assert twilio["posted"][0]["StatusCallback"].endswith("/webhooks/twilio/status")


def test_an_unconfigured_twilio_is_logged_not_just_printed(db_path):
    notify.send_sms("6305550100", "hi")
    assert _rows(db_path, "SELECT status FROM sms_log")[0]["status"] == "not_configured"


def test_an_account_level_error_is_raised_once_an_hour(db_path, twilio):
    for _ in range(3):
        twilio["answers"].append((401, {"code": 20003, "message": "Authenticate"}))
        assert notify.send_sms("6305550100", "hi") is False
    caps = [r for r in _rows(db_path, "SELECT job FROM job_failures") if r["job"] == "sms_provider"]
    assert len(caps) == 1


def test_twilios_stop_error_records_the_platform_stop(db_path, twilio):
    twilio["answers"].append((400, {"code": 21610, "message": "Attempt to send to unsubscribed recipient"}))
    assert notify.send_sms("6305550100", "hi") is False
    assert notify.sms_stopped_phones(["6305550100"], db_path=db_path)
    before = len(twilio["posted"])
    assert notify.send_sms("6305550100", "again") is False
    assert len(twilio["posted"]) == before, "a STOPped number reached Twilio again"


# ── #107: one textable() check; send_sms never texts a STOP ─────────────────

def test_a_stopped_number_is_never_sent_an_alert_staff_or_guest_text(db_path, twilio):
    _stop(db_path, "+15555550100")
    for use_case in ("alert", "staff", "guest"):
        assert notify.send_sms("+15555550100", "x", use_case=use_case) is False
    assert twilio["posted"] == []
    assert {r["status"] for r in _rows(db_path, "SELECT status FROM sms_log")} == {"blocked"}


def test_a_verification_code_is_not_blocked_by_an_alert_stop(db_path, twilio):
    _stop(db_path, "+15555550100")
    assert notify.send_sms("+15555550100", "code 123456", use_case="otp") is True


def test_textable_is_consent_and_stop(db_path):
    rid = _rid(db_path)
    _contact(db_path, rid, phone="+15555550100", consent=1)
    _contact(db_path, rid, name="NoConsent", phone="+15555550101", consent=0)
    assert notify.textable("(555) 555-0100", rid, "alert", db_path)
    assert notify.sms_block_reason("+15555550101", rid, "alert", db_path) == "no SMS consent on file for this number"
    _stop(db_path, "+15555550100")
    assert notify.sms_block_reason("+15555550100", rid, "alert", db_path) == "the number replied STOP"


def test_cover_requests_skip_a_stopped_number(db_path, monkeypatch):
    import intraday
    rid = _rid(db_path)
    _contact(db_path, rid, phone="+15555550100", consent=1)
    assert intraday._consented_phone(rid, "+15555550100", db_path) == "+15555550100"
    _stop(db_path, "+15555550100")
    assert intraday._consented_phone(rid, "+15555550100", db_path) is None


def test_staff_reach_drops_a_stopped_number(db_path, monkeypatch):
    import people
    monkeypatch.setattr(people, "staff_sms_ready", lambda: True)
    monkeypatch.setattr(people, "_memberships", lambda rid, db: [
        {"employee_name": "Ana", "user_id": 7, "is_active": 1, "schedule_texts_at": "2026-09-01",
         "claimed_by_phone": "+15555550142"}])
    monkeypatch.setattr(people, "_contacts", lambda rid, db: {})
    rid = _rid(db_path)
    assert people.reach(rid, ["Ana"], db_path=db_path)["Ana"]["sms"] == "+15555550142"
    _stop(db_path, "+15555550142")
    assert people.reach(rid, ["Ana"], db_path=db_path)["Ana"]["sms"] is None


# ── #14 / #74: the status callback ──────────────────────────────────────────

def _twilio_sig(url, params, token="tok_test"):
    payload = url + "".join(k + str(params[k]) for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


@pytest.fixture
def web():
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    return app.test_client()


def test_the_status_callback_moves_a_text_forward_only(db_path, twilio, web):
    notify.send_sms("6305550100", "hi")
    sid = _rows(db_path, "SELECT provider_sid FROM sms_log")[0]["provider_sid"]
    url = "http://localhost/webhooks/twilio/status"
    for status in ("delivered", "sent"):
        params = {"MessageSid": sid, "MessageStatus": status}
        r = web.post("/webhooks/twilio/status", data=params,
                     headers={"X-Twilio-Signature": _twilio_sig(url, params)})
        assert r.status_code == 204
    assert _rows(db_path, "SELECT status FROM sms_log")[0]["status"] == "delivered"
    assert models.inbound_webhook_health(db_path=db_path)["twilio_status"]["last_verified_at"]


def test_an_unsigned_status_callback_is_refused_and_counted(db_path, twilio, web):
    r = web.post("/webhooks/twilio/status", data={"MessageSid": "SMx", "MessageStatus": "delivered"})
    assert r.status_code == 403
    assert models.inbound_webhook_health(db_path=db_path)["twilio_status"]["failed_count"] == 1


def test_an_unsigned_inbound_text_is_counted_too(db_path, twilio, web):
    assert web.post("/webhooks/twilio/sms", data={"From": "+15555550100", "Body": "STOP"}).status_code == 403
    h = models.inbound_webhook_health(db_path=db_path)["twilio"]
    assert h["failed_count"] == 1 and h["failing"]


# ── #91 / #107: issue texts ──────────────────────────────────────────────────

@pytest.fixture
def routed(db_path):
    import issues
    rid = _rid(db_path)
    cid = _contact(db_path, rid)
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    return rid


def test_an_issue_is_texted_once_even_when_the_tick_races_creation(db_path, twilio, routed):
    import issues
    issue, token = issues.create_issue(routed, "manual", "Walk-in is warm", db_path=db_path)
    issues.tick(db_path=db_path)
    assert len(twilio["posted"]) == 1
    assert len(_rows(db_path, "SELECT 1 FROM issue_links WHERE issue_id=?", (issue["id"],))) == 1


def test_a_failed_issue_text_backs_off_and_reuses_its_link(db_path, twilio, routed):
    import issues
    from datetime import datetime, timedelta
    twilio["answers"].append((503, {"message": "down"}))
    twilio["answers"].append((503, {"message": "down"}))
    issue, _ = issues.create_issue(routed, "manual", "Walk-in is warm", db_path=db_path)
    row = _rows(db_path, "SELECT notified_at, notify_attempts, notify_next_at FROM ops_issues")[0]
    assert row["notified_at"] is None and row["notify_attempts"] == 1 and row["notify_next_at"]
    posted = len(twilio["posted"])
    issues.tick(db_path=db_path)                          # inside the backoff: nothing
    assert len(twilio["posted"]) == posted
    issues.tick(db_path=db_path, now=datetime.utcnow() + timedelta(minutes=6))
    assert _rows(db_path, "SELECT notified_at FROM ops_issues")[0]["notified_at"]
    assert len(_rows(db_path, "SELECT 1 FROM issue_links WHERE issue_id=?", (issue["id"],))) == 1


def test_a_permanent_failure_stops_retrying_and_tells_the_owner(db_path, twilio, routed, monkeypatch):
    import issues, emails, push
    told = {"email": [], "push": []}
    monkeypatch.setattr(emails, "deliver", lambda **kw: told["email"].append(kw) or emails.SendResult(True))
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: told["push"].append(a) or 1)
    twilio["answers"].append((400, {"code": 21211, "message": "Invalid 'To' Phone Number"}))
    issues.create_issue(routed, "manual", "Walk-in is warm", db_path=db_path)
    row = _rows(db_path, "SELECT notify_failed_at, notify_error FROM ops_issues")[0]
    assert row["notify_failed_at"] and "Invalid" in row["notify_error"]
    assert len(told["email"]) == 1 and told["email"][0]["email_type"] == "send_issue_fallback_email"
    posted = len(twilio["posted"])
    for _ in range(3):
        issues.tick(db_path=db_path)
    assert len(twilio["posted"]) == posted, "a number that will never work was retried"


def test_an_issue_for_a_stopped_manager_is_not_texted_and_the_owner_is_told(db_path, twilio, routed, monkeypatch):
    import issues, emails
    told = []
    monkeypatch.setattr(emails, "deliver", lambda **kw: told.append(kw) or emails.SendResult(True))
    _stop(db_path, "+15555550100")
    issues.create_issue(routed, "manual", "Walk-in is warm", db_path=db_path)
    issues.tick(db_path=db_path)
    assert twilio["posted"] == [] and len(told) == 1
    assert _rows(db_path, "SELECT notify_error FROM ops_issues")[0]["notify_error"] == "the number replied STOP"


def test_attempts_are_capped(db_path, twilio, routed, monkeypatch):
    import issues, emails
    from datetime import datetime, timedelta
    monkeypatch.setattr(emails, "deliver", lambda **kw: emails.SendResult(True))
    twilio["answers"].extend([(503, {"message": "down"})] * 20)
    issues.create_issue(routed, "manual", "Walk-in is warm", db_path=db_path)
    at = datetime.utcnow()
    for i in range(10):
        at += timedelta(hours=2)
        issues.tick(db_path=db_path, now=at)
    row = _rows(db_path, "SELECT notify_attempts, notify_failed_at FROM ops_issues")[0]
    assert row["notify_attempts"] == issues.MAX_NOTIFY_ATTEMPTS and row["notify_failed_at"]


def test_the_escalation_is_claimed_and_capped(db_path, twilio, routed, monkeypatch):
    import issues, emails
    from datetime import datetime, timedelta
    monkeypatch.setattr(emails, "deliver", lambda **kw: emails.SendResult(True))
    esc = _contact(db_path, routed, name="RM", phone="+15555550177")
    issues.set_routing(routed, "escalation", esc, escalate_after_minutes=15, db_path=db_path)
    issues.create_issue(routed, "manual", "Walk-in is warm", db_path=db_path)
    twilio["answers"].append((400, {"code": 21614, "message": "not a mobile number"}))
    later = datetime.utcnow() + timedelta(minutes=30)
    issues.tick(db_path=db_path, now=later)
    row = _rows(db_path, "SELECT escalated_at, escalation_attempts, escalation_error FROM ops_issues")[0]
    assert row["escalated_at"] is None and row["escalation_attempts"] == issues.MAX_NOTIFY_ATTEMPTS
    posted = len(twilio["posted"])
    issues.tick(db_path=db_path, now=later + timedelta(hours=1))
    assert len(twilio["posted"]) == posted


# ── #74 / #14: one list of what is wrong with messaging, for the pager ─────

def test_messaging_problems_names_a_failing_webhook_and_account_errors(db_path, twilio):
    assert notify.messaging_problems(db_path=db_path) == []
    models.record_inbound_webhook("twilio", False, reason="signature did not verify", db_path=db_path)
    twilio["answers"].append((401, {"code": 20003, "message": "Authenticate"}))
    notify.send_sms("6305550100", "hi")
    problems = notify.messaging_problems(db_path=db_path)
    assert any("Twilio inbound" in p for p in problems)
    assert any("account-level" in p for p in problems)
    models.record_inbound_webhook("twilio", True, event_type="inbound_sms", db_path=db_path)
    assert not any("Twilio inbound" in p for p in notify.messaging_problems(db_path=db_path))


# ── #14: an alert's own row says which channels it went out on ─────────────

def test_the_alert_row_records_its_channels(db_path, monkeypatch):
    import push, webhooks
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: 0)
    rid = _rid(db_path)
    models.update_restaurant(rid, {"urgent_via_sms": 0, "urgent_via_email": 1}, db_path=db_path)
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **k: False)
    notify.deliver_alert(rid, "negative_trend", "sms", "Rating declining", "<p>x</p>", db_path=db_path)
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **k: True)
    notify.deliver_alert(rid, "labor_over", "sms", "Labor", "<p>x</p>", db_path=db_path)
    got = {r["alert_type"]: r["channels"] for r in _rows(db_path, "SELECT alert_type, channels FROM alert_log")}
    assert got == {"negative_trend": "none", "labor_over": "email"}
