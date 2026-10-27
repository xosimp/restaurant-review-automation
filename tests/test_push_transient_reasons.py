"""Audit #25 (APNs contract table): TooManyRequests and Shutdown are each
Apple's trouble, not the device's.

push._classify files both under "provider": the alert is retried with
backoff, the device's failure counter does not move, and the token is never
deleted or parked — the rule that stopped one bad night at Apple from
unsubscribing every device (the notifications audit's P0). Each reason is
pinned on its own here, with the status Apple sends it with, and a later
attempt that succeeds delivers the alert.

The HTTP/2 client is a stand-in; nothing reaches Apple.
"""
import pytest

import push
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_conn
from push import _deliver, get_device_tokens, init_push, register_device_token

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

# (HTTP status, APNs reason) as Apple documents them: 429 TooManyRequests —
# too many requests to one device token; 503 Shutdown — the server is
# shutting down; 503 ServiceUnavailable for contrast.
TRANSIENT = [(429, "TooManyRequests"), (503, "Shutdown"), (503, "ServiceUnavailable")]


@pytest.fixture
def token_row(db_path, monkeypatch):
    init_auth(db_path=db_path)
    init_push(db_path=db_path)
    rid = create_restaurant(Restaurant(name="Fixture Push Co", owner_email="push@fixture.test"), db_path=db_path)
    uid = create_user(rid, "owner1", "owner1@fixture.test", "correct-horse-battery", db_path=db_path)
    register_device_token(uid, rid, "f" * 64, "production", db_path=db_path)
    monkeypatch.setattr(push.time, "sleep", lambda s: None)
    monkeypatch.setattr(push, "_provider_jwt", lambda: "fixture-jwt")
    return get_device_tokens(rid, db_path=db_path)[0]


def _client(monkeypatch, answers):
    """Each post takes the next (status, reason); 200 once they run out."""
    calls = []

    class _Resp:
        def __init__(self, status, reason):
            self.status_code, self._reason = status, reason

        def json(self):
            return {"reason": self._reason} if self._reason else {}

    class _Client:
        def post(self, url, content=None, headers=None):
            calls.append(url)
            status, reason = answers.pop(0) if answers else (200, None)
            return _Resp(status, reason)

    client = _Client()
    monkeypatch.setattr(push, "_client", lambda: client)
    return calls


def _device(db_path, token_id):
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT consecutive_failures, disabled_reason FROM device_tokens WHERE id=?",
                         (token_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


@pytest.mark.parametrize("status,reason", TRANSIENT)
def test_the_reason_is_classified_as_apples_trouble(status, reason):
    assert push._classify(status, reason) == "provider"


@pytest.mark.parametrize("status,reason", TRANSIENT)
def test_every_attempt_failing_leaves_the_device_alone(db_path, token_row, monkeypatch, status, reason):
    calls = _client(monkeypatch, [(status, reason)] * 3)
    result = _deliver(token_row, "1star", "1-star review", "A guest left one star", None, db_path=db_path)
    assert len(calls) == 3, "a transient answer must be retried with backoff"
    assert result["ok"] is False and result["error"] == reason
    device = _device(db_path, token_row["id"])
    assert device is not None, "a live token was deleted for Apple's own trouble"
    assert not device["consecutive_failures"] and device["disabled_reason"] is None


@pytest.mark.parametrize("status,reason", TRANSIENT)
def test_a_later_attempt_delivers_the_alert(db_path, token_row, monkeypatch, status, reason):
    calls = _client(monkeypatch, [(status, reason)])
    result = _deliver(token_row, "1star", "1-star review", "A guest left one star", None, db_path=db_path)
    assert result["ok"] is True and result["attempts"] == 2 and len(calls) == 2
    assert not _device(db_path, token_row["id"])["consecutive_failures"]


def test_ten_alerts_in_a_429_storm_never_park_the_device(db_path, token_row, monkeypatch):
    _client(monkeypatch, [(429, "TooManyRequests")] * 30)
    for _ in range(10):
        _deliver(token_row, "1star", "1-star review", "A guest left one star", None, db_path=db_path)
    device = _device(db_path, token_row["id"])
    assert device and not device["consecutive_failures"] and device["disabled_reason"] is None
