"""Audit #25 A06-12: the inbound signature checks against the vendors' own
published examples, not against themselves.

The other signature tests compute the expected value with the same
algorithm as the code, so a refactor that changed the ordering or encoding
would change both and stay green — while every real STOP was refused with a
403. These vectors come from the vendors' documentation:

- Twilio, "Validating Requests" (webhook security): auth token 12345, URL
  https://mycompany.com/myapp.php?foo=1&bar=2 and five POST fields signs to
  0/KCTR6DLpKmkAf8muzZqo1nDgQ= (HMAC-SHA1 of the URL plus the fields sorted
  by name, base64).
- Svix, "Verifying webhooks manually" (the svix libraries' own test): secret
  whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw, id msg_p5jXN8AQM9LWM0D4loKWxJek,
  timestamp 1614265330, body {"test": 2432232314} signs to
  v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE= (HMAC-SHA256 of
  "{id}.{timestamp}.{body}" keyed by the base64 secret).
"""
import time

import pytest

import notify
import webhook_routes

TWILIO_TOKEN = "12345"
TWILIO_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
TWILIO_PARAMS = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
                 "From": "+12349013030", "To": "+18005551212"}
TWILIO_SIGNATURE = "0/KCTR6DLpKmkAf8muzZqo1nDgQ="

SVIX_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SVIX_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
SVIX_TIMESTAMP = 1614265330
SVIX_BODY = b'{"test": 2432232314}'
SVIX_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_twilio_signature_matches_vendor_vector(monkeypatch):
    monkeypatch.setattr(notify, "TWILIO_TOKEN", TWILIO_TOKEN)
    assert notify.validate_twilio_signature(TWILIO_URL, dict(TWILIO_PARAMS), TWILIO_SIGNATURE) is True


@pytest.mark.parametrize("change", ["field", "url", "token"])
def test_twilio_vendor_vector_refuses_any_change(monkeypatch, change):
    monkeypatch.setattr(notify, "TWILIO_TOKEN", "54321" if change == "token" else TWILIO_TOKEN)
    params = dict(TWILIO_PARAMS, Digits="1235") if change == "field" else dict(TWILIO_PARAMS)
    url = TWILIO_URL.replace("bar=2", "bar=3") if change == "url" else TWILIO_URL
    assert notify.validate_twilio_signature(url, params, TWILIO_SIGNATURE) is False


def _svix_headers(signature=SVIX_SIGNATURE, ts=SVIX_TIMESTAMP):
    return {"svix-id": SVIX_ID, "svix-timestamp": str(ts), "svix-signature": signature}


@pytest.fixture
def at_vector_time(monkeypatch):
    """The vector is from 2021: the clock is put next to its timestamp so the
    five-minute replay window (SVIX_TOLERANCE_SECONDS) is not what decides."""
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", SVIX_SECRET)
    monkeypatch.setattr(time, "time", lambda: float(SVIX_TIMESTAMP + 30))


def test_svix_signature_matches_vendor_vector(at_vector_time):
    assert webhook_routes._verify_svix(SVIX_BODY, _svix_headers()) is True
    # Several signatures, space-separated (a rotated secret): any valid one passes.
    assert webhook_routes._verify_svix(SVIX_BODY, _svix_headers("v1,bm90LXRoaXMtb25l " + SVIX_SIGNATURE)) is True


def test_svix_vendor_vector_refuses_a_changed_body_or_secret(at_vector_time, monkeypatch):
    assert webhook_routes._verify_svix(b'{"test": 2432232315}', _svix_headers()) is False
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", "whsec_" + "QUJD" * 8)
    assert webhook_routes._verify_svix(SVIX_BODY, _svix_headers()) is False


def test_svix_vendor_vector_outside_the_replay_window_is_refused(monkeypatch):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", SVIX_SECRET)
    monkeypatch.setattr(time, "time", lambda: float(SVIX_TIMESTAMP + webhook_routes.SVIX_TOLERANCE_SECONDS + 1))
    assert webhook_routes._verify_svix(SVIX_BODY, _svix_headers()) is False
