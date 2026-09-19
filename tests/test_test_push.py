"""The self-serve test push.

"Is push actually working for me?" had no answer short of reaching into the
database, and the same question about a client needed a shell. Two rules
this has to hold to: it goes to the CALLER's devices and nobody else's, and
it reports what APNs actually said rather than "we tried".
"""
import pytest

import push
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture
def rid(db_path):
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    return create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@x.test"),
                             db_path=db_path)


@pytest.fixture
def erik(db_path, rid):
    return create_user(rid, "erik", "erik@x.test", "correct-horse", db_path=db_path)


@pytest.fixture
def jim(db_path, rid):
    return create_user(rid, "jim", "jim@x.test", "correct-horse", db_path=db_path)


def _arm(monkeypatch, status=200, reason=None):
    calls = []

    class _Resp:
        status_code = status

        def json(self):
            return {"reason": reason} if reason else {}

    class _Client:
        def post(self, url, content=None, headers=None):
            calls.append((url, content, headers))
            return _Resp()

    monkeypatch.setattr(push, "_provider_jwt", lambda: "fake-jwt")
    monkeypatch.setattr(push, "_client", lambda: _Client())
    monkeypatch.setattr(push.time, "sleep", lambda s: None)
    return calls


def test_it_goes_to_the_callers_own_devices_only(db_path, rid, erik, jim, monkeypatch):
    """Erik owns Simple EJ's with Jim. A test that buzzes Jim's phone is not
    a test."""
    push.register_device_token(erik, rid, "e" * 64, "sandbox", db_path=db_path)
    push.register_device_token(jim, rid, "j" * 64, "sandbox", db_path=db_path)
    calls = _arm(monkeypatch)

    result = push.send_test_push(rid, erik, db_path=db_path)

    assert result == {"ok": True, "devices": 1, "sent": 1, "failures": [], "error": None}
    assert len(calls) == 1
    assert calls[0][0].endswith("e" * 64)


def test_no_device_says_what_to_do_about_it(db_path, rid, erik, monkeypatch):
    _arm(monkeypatch)
    result = push.send_test_push(rid, erik, db_path=db_path)
    assert result["ok"] is False and result["devices"] == 0
    assert "allow notifications" in result["error"].lower()


def test_it_reports_what_apple_actually_said(db_path, rid, erik, monkeypatch):
    """The whole point: "we tried" is what the background pool already gives
    you, and it is useless for answering the question."""
    push.register_device_token(erik, rid, "e" * 64, "sandbox", db_path=db_path)
    _arm(monkeypatch, status=403, reason="ExpiredProviderToken")

    result = push.send_test_push(rid, erik, db_path=db_path)

    assert result["ok"] is False
    assert result["error"] == "ExpiredProviderToken"


def test_a_parked_device_is_not_tested(db_path, rid, erik, monkeypatch):
    push.register_device_token(erik, rid, "e" * 64, "sandbox", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE device_tokens SET disabled_reason='parked'")
    conn.commit()
    conn.close()
    _arm(monkeypatch)

    assert push.send_test_push(rid, erik, db_path=db_path)["devices"] == 0


def test_two_tests_in_one_day_both_arrive(db_path, rid, erik, monkeypatch):
    """The collapse id is keyed on the date for anything without a review
    behind it, so without a per-test key the second test would silently
    replace the first on the lock screen — which reads as "it didn't work"."""
    push.register_device_token(erik, rid, "e" * 64, "sandbox", db_path=db_path)
    calls = _arm(monkeypatch)

    push.send_test_push(rid, erik, db_path=db_path)
    push.send_test_push(rid, erik, db_path=db_path)

    keys = [c[2]["apns-collapse-id"] for c in calls]
    assert len(keys) == 2 and keys[0] != keys[1]


def test_every_attempt_is_logged_like_a_real_delivery(db_path, rid, erik, monkeypatch):
    push.register_device_token(erik, rid, "e" * 64, "sandbox", db_path=db_path)
    _arm(monkeypatch)

    push.send_test_push(rid, erik, db_path=db_path)

    conn = get_conn(db_path)
    row = conn.execute("SELECT alert_type, ok FROM push_deliveries").fetchone()
    conn.close()
    assert row["alert_type"] == "test_push" and row["ok"] == 1
