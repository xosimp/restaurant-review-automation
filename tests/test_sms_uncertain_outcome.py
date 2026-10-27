"""Audit #25 A06-11: a text Twilio took and never answered is "unknown",
never an ordinary failure.

Twilio's Messages API has no idempotency key. A read timeout, or a
connection reset after the request was written, may follow a text that went
out: notify.send_sms_result says so (SmsResult.uncertain, sms_log status
"unknown", one attempt, never retried inline), and every caller that retries
failed texts counts it as sent — the issue-text loop, its escalation, a
staff text (a cover ask), a guest reply — so nobody is texted twice. A
connection that never opened is still a plain failure, retried once.

Synthetic numbers only; requests.post is scripted, nothing reaches Twilio.
"""
import http.client

import pytest
import requests
import urllib3

import auth
import models
import notify
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import guest_hub, guest_marketing, intraday, issues, people, push
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, notify, issues, intraday, people, push, auth, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(notify, "DB_PATH", db_path)
    monkeypatch.setattr(guest_marketing, "DB_PATH", db_path)
    guest_marketing.init_guest_marketing(db_path)
    auth.init_auth(db_path)
    guest_hub._SENDER_CACHE["at"] = 0.0


class _R:
    def __init__(self, code=201, body=None):
        self.status_code, self._body = code, body if body is not None else {"sid": "SM1", "status": "queued"}
        self.text = str(self._body)

    def json(self):
        return self._body


@pytest.fixture
def twilio(monkeypatch):
    """Twilio configured; each POST takes the next scripted answer — a
    status code and body, or an exception to raise — and 201 after that."""
    monkeypatch.setattr(notify, "TWILIO_SID", "AC_test")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "tok_test")
    monkeypatch.setattr(notify, "TWILIO_FROM", "+15550000000")
    monkeypatch.setattr(notify, "TWILIO_MESSAGING_SERVICE_SID", "MG_test")
    monkeypatch.setattr(notify, "TWILIO_GUEST_MESSAGING_SERVICE_SID", "MG_guest_test")
    monkeypatch.setattr(notify, "TWILIO_STAFF_MESSAGING_SERVICE_SID", "MG_staff_test")
    monkeypatch.setattr(notify, "STAFF_SMS_HOLD", False)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    state = {"answers": [], "posted": []}

    def post(url, **kw):
        state["posted"].append(kw.get("data"))
        answer = state["answers"].pop(0) if state["answers"] else (201, None)
        if isinstance(answer, BaseException):
            raise answer
        return _R(*answer)
    monkeypatch.setattr(notify.requests, "post", post)
    return state


def _reset_after_send():
    # What requests raises when the server drops the connection while the
    # answer is being read: the request was already written.
    return requests.exceptions.ConnectionError(
        urllib3.exceptions.ProtocolError("Connection aborted.", http.client.RemoteDisconnected("closed")))


def _rid(db_path):
    return create_restaurant(Restaurant(name="Fixture Grill", owner_email="o@fixture.test"), db_path=db_path)


def _rows(db_path, sql, args=()):
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── notify: the outcome itself ───────────────────────────────────────────────

def test_a_read_timeout_is_one_attempt_of_unknown_outcome(db_path, twilio):
    rid = _rid(db_path)
    twilio["answers"].append(requests.exceptions.ReadTimeout("read timed out (read timeout=10)"))
    res = notify.send_sms_result("6305550100", "hi", restaurant_id=rid)
    assert len(twilio["posted"]) == 1, "a text that may have gone out was sent again"
    assert not res.ok and res.uncertain and res.maybe_sent and not res.permanent
    assert res.status == notify.SMS_UNKNOWN_STATUS
    (row,) = _rows(db_path, "SELECT status, error FROM sms_log")
    assert row["status"] == "unknown" and "delivery unknown" in row["error"]
    assert notify.last_sms_uncertain() is True


def test_a_reset_after_the_request_was_written_is_unknown_too(db_path, twilio):
    twilio["answers"].append(_reset_after_send())
    res = notify.send_sms_result("6305550100", "hi", restaurant_id=_rid(db_path))
    assert len(twilio["posted"]) == 1 and res.uncertain and res.status == "unknown"


def test_a_connection_that_never_opened_is_retried_once_and_is_a_plain_failure(db_path, twilio):
    twilio["answers"].extend([requests.exceptions.ConnectTimeout("connect timed out")] * 2)
    res = notify.send_sms_result("6305550100", "hi", restaurant_id=_rid(db_path))
    assert len(twilio["posted"]) == 2
    assert not res.ok and not res.uncertain and not res.maybe_sent and res.status == "failed"
    assert _rows(db_path, "SELECT status FROM sms_log")[0]["status"] == "failed"


def test_an_accepted_text_is_never_uncertain(db_path, twilio):
    res = notify.send_sms_result("6305550100", "hi", restaurant_id=_rid(db_path))
    assert res.ok and not res.uncertain and res.maybe_sent


def test_the_console_health_check_says_unknown(db_path, twilio):
    twilio["answers"].append(requests.exceptions.ReadTimeout("read timed out"))
    notify.send_sms_result("6305550100", "hi", restaurant_id=_rid(db_path))
    assert notify.sms_stats(hours=24, db_path=db_path)["by_status"].get("unknown") == 1


# ── issues: the loop that retries failed texts ──────────────────────────────

@pytest.fixture
def routed(db_path):
    import issues
    rid = _rid(db_path)
    conn = get_conn(db_path)
    cid = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,1)",
                       (rid, "GM", "+15555550100")).lastrowid
    conn.commit()
    conn.close()
    issues.set_routing(rid, "manager", cid, db_path=db_path)
    return rid


def _notified_at(db_path):
    from datetime import datetime
    stamp = _rows(db_path, "SELECT notified_at FROM ops_issues")[0]["notified_at"]
    return datetime.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")


def test_the_issue_text_loop_never_resends_a_text_of_unknown_outcome(db_path, twilio, routed):
    import issues
    from datetime import timedelta
    twilio["answers"].append(requests.exceptions.ReadTimeout("read timed out"))
    issues.create_issue(routed, "manual", "Walk-in is warm", db_path=db_path)
    row = _rows(db_path, "SELECT notified_at, notify_attempts, notify_next_at, notify_error, notify_failed_at "
                         "FROM ops_issues")[0]
    assert row["notified_at"], "the claim was released, so the tick would text again"
    assert not row["notify_next_at"] and not row["notify_failed_at"] and not row["notify_attempts"]
    assert "delivery unknown" in row["notify_error"]
    at = _notified_at(db_path)
    for minutes in (6, 20, 50, 180):
        issues.tick(db_path=db_path, now=at + timedelta(minutes=minutes))
    assert len(twilio["posted"]) == 1, "the manager was texted twice about one issue"


def test_an_escalation_of_unknown_outcome_is_not_resent(db_path, twilio, routed, monkeypatch):
    import emails, issues
    from datetime import timedelta
    monkeypatch.setattr(emails, "deliver", lambda **kw: emails.SendResult(True))
    conn = get_conn(db_path)
    esc = conn.execute("INSERT INTO alert_contacts (restaurant_id, name, phone, sms_consent) VALUES (?,?,?,1)",
                       (routed, "RM", "+15555550177")).lastrowid
    conn.commit()
    conn.close()
    issues.set_routing(routed, "escalation", esc, escalate_after_minutes=15, db_path=db_path)
    issues.create_issue(routed, "manual", "Walk-in is warm", db_path=db_path)        # accepted
    assert len(twilio["posted"]) == 1
    at = _notified_at(db_path)
    twilio["answers"].append(requests.exceptions.ReadTimeout("read timed out"))
    issues.tick(db_path=db_path, now=at + timedelta(minutes=30))
    assert len(twilio["posted"]) == 2
    row = _rows(db_path, "SELECT escalated_at, escalation_attempts, escalation_error FROM ops_issues")[0]
    assert row["escalated_at"] and not row["escalation_attempts"] and "delivery unknown" in row["escalation_error"]
    for hours in (1, 3, 6):
        issues.tick(db_path=db_path, now=at + timedelta(hours=hours))
    assert len(twilio["posted"]) == 2, "the escalation contact was texted twice"


# ── staff texts (a cover ask) and guest replies ─────────────────────────────

def test_a_staff_text_of_unknown_outcome_counts_as_sent(db_path, twilio):
    import people
    rid = _rid(db_path)
    twilio["answers"].append(requests.exceptions.ReadTimeout("read timed out"))
    assert people._send_staff_text(rid, "+15555550142", "Can you cover?") is True
    assert len(twilio["posted"]) == 1
    twilio["answers"].append((400, {"code": 21211, "message": "Invalid 'To' Phone Number"}))
    assert people._send_staff_text(rid, "+15555550142", "Can you cover?") is False


def test_a_guest_reply_of_unknown_outcome_is_not_offered_again(db_path, twilio):
    import guest_hub
    rid = _rid(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO guest_inbox (restaurant_id, phone, direction, body, kind, status) "
                 "VALUES (?,?,'in','Are you open late?','reply','received')", (rid, "+16305550123"))
    conn.commit()
    conn.close()
    twilio["answers"].append(requests.exceptions.ReadTimeout("read timed out"))
    out = guest_hub.reply(rid, "+16305550123", "Until midnight!", user_id=1, db_path=db_path)
    assert out["ok"] is False and out["uncertain"] is True
    assert "Try again" not in out["error"] and "may have" in out["error"]
    assert _rows(db_path, "SELECT status FROM guest_inbox WHERE direction='out'")[0]["status"] == "unknown"
    assert len(twilio["posted"]) == 1
