"""Edge cases in the outbound delivery channels: Resend (email), Twilio
(SMS), APNs (push) and the National Weather Service.

What these tests protect is the shape of a delivery under a transient
failure. A retry must not become a second copy of a supplier order (Resend
needs one Idempotency-Key across every attempt of one send). A single Twilio
503 must not lose an owner's health alert text. An NWS outage must not make
every Home and Labor request pay two 10-second timeouts again and again.
A push dropped because the pool is saturated must leave a trace against the
alert it belonged to, not only a line in the failure digest.

Audit: edge_audit/AI.md, findings AI-21, AI-27, AI-28, AI-29 and the
"External Integrations" appendix items 12-14. Confirmed defects are asserted
as the correct behaviour and marked xfail(strict=True). No test reaches the
network: requests.post / requests.get are stubbed and time.sleep is instant.
"""
import json

import pytest
import requests

import auth
import emails
import models
import notify
import push
import weather
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, push):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr("time.sleep", lambda *_: None)   # retry backoff is instant


def _restaurant(db_path, **post):
    rid = create_restaurant(Restaurant(name="Channel Co", owner_email="owner@channel.test"),
                            db_path=db_path)
    if post:
        update_restaurant(rid, post, db_path=db_path)
    return rid


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


def _sequence(monkeypatch, target, outcomes):
    """Replace `target` (a dotted path to requests.post/get) with a stub that
    plays `outcomes` in order — a _Resp is returned, an exception raised —
    repeating the last one. Returns the list of recorded calls."""
    calls = []

    def fake(url, *a, **k):
        calls.append({"url": url, "headers": dict(k.get("headers") or {}), "kw": k})
        out = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
        if isinstance(out, BaseException):
            raise out
        return out

    monkeypatch.setattr(target, fake)
    return calls


# ═══ AI-21 · Resend: every attempt of one send carries one Idempotency-Key ══

def _idempotency_key(call):
    return {k.lower(): v for k, v in call["headers"].items()}.get("idempotency-key")


@pytest.mark.xfail(strict=True, reason="AI-21: deliver retries timeouts/5xx with no Idempotency-Key, so a slow success is sent twice")
def test_every_retry_of_one_email_send_carries_the_same_idempotency_key(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    # Resend accepted the first request but the response never arrived.
    calls = _sequence(monkeypatch, "requests.post", [
        requests.Timeout("read timed out after 15s"),
        _Resp(503, text="service unavailable"),
        _Resp(200, {"id": "msg_1"}),
    ])
    result = emails.deliver({"to": ["supplier@vendor.test"], "subject": "Order #88",
                             "html": "<p>12 cases</p>"},
                            restaurant_id=rid, email_type="send_supplier_order")
    assert result.ok and len(calls) == 3
    keys = [_idempotency_key(c) for c in calls]
    assert keys[0], "no Idempotency-Key — Resend cannot tell a retry from a new order"
    assert len(set(keys)) == 1, f"the retries of one send carried different keys: {keys}"


@pytest.mark.xfail(strict=True, reason="AI-21: no Idempotency-Key is sent at all")
def test_two_different_email_sends_carry_different_idempotency_keys(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    calls = _sequence(monkeypatch, "requests.post", [_Resp(200, {"id": "msg_x"})])
    emails.deliver({"to": ["a@vendor.test"], "subject": "Order #1", "html": "<p>1</p>"},
                   restaurant_id=rid, email_type="send_supplier_order")
    emails.deliver({"to": ["a@vendor.test"], "subject": "Order #2", "html": "<p>2</p>"},
                   restaurant_id=rid, email_type="send_supplier_order")
    keys = [_idempotency_key(c) for c in calls]
    assert all(keys), "no Idempotency-Key sent"
    assert keys[0] != keys[1], "two distinct orders shared a key — Resend would drop the second"


def test_a_timed_out_email_send_is_retried_and_the_outcome_is_recorded_once(db_path, monkeypatch):
    """Pins the retry itself (the fix is a header, not removing the retry),
    and that email_log holds one row for one logical send."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    calls = _sequence(monkeypatch, "requests.post", [requests.Timeout("slow"), _Resp(200, {"id": "m2"})])
    result = emails.deliver({"to": ["o@x.test"], "subject": "Alert", "html": "<p>x</p>"},
                            restaurant_id=rid, email_type="send_alert_email")
    assert result.ok and result.attempts == 2 and len(calls) == 2
    conn = models.get_conn(db_path)
    rows = conn.execute("SELECT status FROM email_log WHERE restaurant_id=?", (rid,)).fetchall()
    conn.close()
    assert [r["status"] for r in rows] == ["sent"]


# ═══ AI-27 · Twilio: one transient failure does not lose the text ═══════════

@pytest.fixture
def twilio(monkeypatch):
    monkeypatch.setattr(notify, "TWILIO_SID", "AC_test")
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "tok_test")
    monkeypatch.setattr(notify, "TWILIO_FROM", "+15550000000")
    monkeypatch.setattr(notify, "TWILIO_MESSAGING_SERVICE_SID", "MG_test")


@pytest.mark.xfail(strict=True, reason="AI-27: send_sms makes one attempt; a 429/5xx/connection error loses the SMS")
@pytest.mark.parametrize("first", [
    pytest.param(_Resp(503, text="Service Unavailable"), id="http-503"),
    pytest.param(_Resp(500, text="Internal Server Error"), id="http-500"),
    pytest.param(_Resp(429, text="Too Many Requests"), id="http-429"),
    pytest.param(requests.ConnectionError("connection reset by peer"), id="connection-reset"),
])
def test_a_transient_twilio_failure_then_success_delivers_the_sms_once(twilio, monkeypatch, first):
    calls = _sequence(monkeypatch, "requests.post", [first, _Resp(201, {"sid": "SM1"})])
    assert notify.send_sms("(630) 555-0100", "1-star review at Channel Co") is True, \
        "a single Twilio blip lost the owner's alert text"
    assert len(calls) == 2, f"expected one retry, made {len(calls)} request(s)"


def test_a_permanent_twilio_rejection_is_not_retried(twilio, monkeypatch):
    """A 400 (bad number, opted-out recipient) will fail the same way every
    time. The retry the fix adds must stay off for it."""
    calls = _sequence(monkeypatch, "requests.post", [_Resp(400, text="invalid 'To' number")])
    assert notify.send_sms("+1555", "hello") is False
    assert len(calls) == 1


def test_a_successful_twilio_send_is_one_request_through_the_messaging_service(twilio, monkeypatch):
    calls = _sequence(monkeypatch, "requests.post", [_Resp(201, {"sid": "SM2"})])
    assert notify.send_sms("6305550100", "hi") is True
    assert len(calls) == 1
    data = calls[0]["kw"]["data"]
    assert data["To"] == "+16305550100" and data["MessagingServiceSid"] == "MG_test"
    assert calls[0]["kw"].get("timeout"), "every outbound call names a timeout"


# ═══ AI-28 · NWS: a failure is cached too ═══════════════════════════════════

_WEEK = ["2026-09-21", "2026-09-22", "2026-09-23"]


def _nws(monkeypatch, points_status):
    calls = []

    def get(url, *a, **k):
        calls.append(url)
        if "api.weather.gov/points" in url:
            if points_status == "timeout":
                raise requests.Timeout("NWS did not answer in 10s")
            return _Resp(points_status, {"title": "Unexpected Problem"})
        raise AssertionError(f"unexpected URL after a failed /points: {url}")

    monkeypatch.setattr(weather.requests, "get", get)
    return calls


@pytest.mark.xfail(strict=True, reason="AI-28: an NWS failure writes nothing, so every call repeats the blocking requests")
@pytest.mark.parametrize("points_status", [
    pytest.param(500, id="nws-500-outage"),
    pytest.param(404, id="nws-404-non-us-location"),
    pytest.param("timeout", id="nws-timeout"),
])
def test_two_forecast_calls_during_an_nws_failure_make_one_network_attempt(db_path, monkeypatch, points_status):
    rid = _restaurant(db_path, latitude=41.91, longitude=-88.31)
    calls = _nws(monkeypatch, points_status)
    # Each caller reads the restaurant fresh, the way a request does.
    assert weather.get_forecast_for_week(get_restaurant(rid, db_path=db_path), _WEEK, db_path=db_path) == []
    assert weather.get_forecast_for_week(get_restaurant(rid, db_path=db_path), _WEEK, db_path=db_path) == []
    assert len(calls) == 1, f"the second call went back to NWS: {len(calls)} requests"


def test_a_successful_nws_forecast_is_served_from_cache_on_the_next_call(db_path, monkeypatch):
    rid = _restaurant(db_path, latitude=41.91, longitude=-88.31)
    calls = []
    forecast_url = "https://api.weather.gov/gridpoints/LOT/75,73/forecast"
    periods = [{"isDaytime": True, "startTime": "2026-09-22T06:00:00-05:00", "name": "Tuesday",
                "temperature": 71, "shortForecast": "Sunny",
                "probabilityOfPrecipitation": {"value": 10}}]

    def get(url, *a, **k):
        calls.append(url)
        if "api.weather.gov/points/" in url:
            return _Resp(200, {"properties": {"forecast": forecast_url}})
        return _Resp(200, {"properties": {"periods": periods}})

    monkeypatch.setattr(weather.requests, "get", get)
    first = weather.get_forecast_for_week(get_restaurant(rid, db_path=db_path), _WEEK, db_path=db_path)
    second = weather.get_forecast_for_week(get_restaurant(rid, db_path=db_path), _WEEK, db_path=db_path)
    assert first == second and first[0]["high_f"] == 71
    assert len(calls) == 2, "the cached forecast was re-fetched"


# ═══ AI-29 · push: a dropped delivery is recorded against its alert ════════

@pytest.fixture
def owner_with_devices(db_path):
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    rid = _restaurant(db_path)
    uid = auth.create_user(rid, "owner1", "owner1@channel.test", "correct-horse", db_path=db_path)
    for ch in "xyz":
        push.register_device_token(uid, rid, ch * 64, "production", db_path=db_path)
    return rid


def _fire_with_a_full_queue(monkeypatch, rid, db_path):
    delivered = []
    # Recorded BEFORE this test builds its own pool, so teardown restores the
    # process's original executor. Recording it after the shutdown below
    # would hand every later test a shut-down pool (it broke
    # test_push_delivery::test_every_device_is_still_delivered_to).
    monkeypatch.setattr(push, "_executor", None)
    monkeypatch.setattr(push, "_deliver", lambda *a, **k: delivered.append(a))
    monkeypatch.setattr(push, "_MAX_PUSH_QUEUED", 0)
    push.fire_push(rid, "1star", "1-star review", "Cold soup.", db_path=db_path)
    push._push_executor().shutdown(wait=True)
    push._executor = None
    return delivered


def _push_rows(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT device_token_id, alert_type, ok, error FROM push_deliveries WHERE restaurant_id=?",
            (rid,))]
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="AI-29: a push dropped at the queue ceiling leaves no push_deliveries row for its alert")
def test_every_push_dropped_at_the_queue_ceiling_is_recorded_against_its_alert(db_path, monkeypatch, owner_with_devices):
    rid = owner_with_devices
    delivered = _fire_with_a_full_queue(monkeypatch, rid, db_path)
    assert delivered == []
    rows = _push_rows(db_path, rid)
    assert len(rows) == 3, \
        f"3 devices' pushes were dropped and the delivery ledger recorded {len(rows)}"
    assert all(r["alert_type"] == "1star" and r["ok"] == 0 and r["error"] for r in rows)


def test_a_push_dropped_at_the_queue_ceiling_still_reaches_the_failure_digest(db_path, monkeypatch, owner_with_devices):
    """What does work today, and must survive the fix: the drop is captured."""
    rid = owner_with_devices
    _fire_with_a_full_queue(monkeypatch, rid, db_path)
    conn = models.get_conn(db_path)
    jobs = [r["job"] for r in conn.execute("SELECT job FROM job_failures")]
    conn.close()
    assert "fire_push" in jobs
