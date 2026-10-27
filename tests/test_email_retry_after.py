"""Audit #25 A06-15: a Resend 429 says how long to wait, and deliver() waits
that long — inside what is left of the send budget — instead of retrying on
its fixed 0.5 s / 1 s schedule into the same refusal.

requests.post and time.sleep are stubbed: nothing reaches Resend and nothing
really sleeps; the sleeps asked for are recorded.
"""
import json

import pytest

import emails
import models

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


class _Resp:
    def __init__(self, code, body='{"id":"msg_fixture"}', headers=None):
        self.status_code, self.text, self.headers = code, body, headers or {}

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def resend(monkeypatch):
    state = {"answers": [], "posts": 0, "slept": []}

    def post(url, headers=None, json=None, timeout=None, **kw):
        state["posts"] += 1
        return state["answers"].pop(0) if state["answers"] else _Resp(200)
    monkeypatch.setattr("requests.post", post)
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_fixture_key")
    monkeypatch.setattr("time.sleep", lambda s: state["slept"].append(s))
    return state


def _send():
    return emails.deliver({"to": ["owner@fixture.test"], "subject": "Your code", "html": "<p>123456</p>"},
                          email_type="send_2fa_code")


def test_a_429_with_retry_after_waits_that_long_then_sends(db_path, resend):
    resend["answers"] = [_Resp(429, '{"message":"rate limited"}', {"Retry-After": "2"}), _Resp(200)]
    result = _send()
    assert result.ok and result.attempts == 2 and resend["posts"] == 2
    assert resend["slept"] and max(resend["slept"]) >= 2, "retried inside the window Resend asked for"


def test_a_retry_after_longer_than_the_budget_stops_instead_of_retrying_early(db_path, resend):
    resend["answers"] = [_Resp(429, '{"message":"rate limited"}', {"Retry-After": "60"})]
    result = _send()
    assert not result.ok and result.status_code == 429 and resend["posts"] == 1
    assert sum(resend["slept"]) < emails._SEND_BUDGET_SECONDS, "held the caller past the send budget"


def test_a_429_without_retry_after_keeps_the_short_backoff(db_path, resend):
    resend["answers"] = [_Resp(429, '{"message":"rate limited"}'), _Resp(200)]
    assert _send().ok
    assert resend["slept"] == [emails._BACKOFF_BASE]


def test_retry_after_reads_seconds_and_http_dates():
    assert emails._retry_after_seconds(_Resp(429, headers={"Retry-After": "3"})) == 3.0
    assert emails._retry_after_seconds(_Resp(429, headers={"Retry-After": "1.5"})) == 1.5
    assert emails._retry_after_seconds(_Resp(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0.0
    assert emails._retry_after_seconds(_Resp(429, headers={"Retry-After": "Fri, 01 Jan 2100 00:00:00 GMT"})) > 3600
    assert emails._retry_after_seconds(_Resp(429, headers={"Retry-After": "soon"})) is None
    assert emails._retry_after_seconds(_Resp(429)) is None


def test_an_http_date_past_the_budget_stops_too(db_path, resend):
    resend["answers"] = [_Resp(429, "{}", {"Retry-After": "Fri, 01 Jan 2100 00:00:00 GMT"})]
    result = _send()
    assert not result.ok and resend["posts"] == 1
