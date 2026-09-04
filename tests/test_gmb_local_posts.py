"""create_local_post's request construction.

The endpoint itself can't be exercised here (no Google Business Profile
can be connected in this environment yet), so these pin down everything
that IS ours: the parent path, the body shape, and the call-to-action
rules Google enforces.
"""
import pytest

import models
import gmb
from models import create_restaurant, Restaurant, update_restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """create_local_post resolves the restaurant through models.get_restaurant,
    which opens the default database unless get_conn is redirected — same
    gotcha (and same fix) as test_mobile_api.py's own _redirect_db."""
    real_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real_get_conn(db_path))


def _connected(db_path):
    rid = create_restaurant(Restaurant(name="G", owner_email="g@x.com"), db_path=db_path)
    update_restaurant(rid, {"gmb_refresh_token": "r", "gmb_account_id": "accounts/123",
                            "gmb_location_id": "locations/456"}, db_path=db_path)
    return rid


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_posts_to_the_combined_account_and_location_parent(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(gmb, "get_valid_token", lambda r: "tok")
    captured = {}

    def _post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)
        return _Resp(200, {"name": "accounts/123/locations/456/localPosts/1"})

    monkeypatch.setattr(gmb.requests, "post", _post)
    result = gmb.create_local_post(rid, "Fresh catch in today")
    assert result["ok"] is True
    assert captured["url"] == (
        "https://mybusiness.googleapis.com/v4/accounts/123/locations/456/localPosts")
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert captured["body"] == {"languageCode": "en-US", "summary": "Fresh catch in today",
                                "topicType": "STANDARD"}


def test_a_link_button_carries_its_url(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(gmb, "get_valid_token", lambda r: "tok")
    captured = {}
    monkeypatch.setattr(gmb.requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        captured.update(body=json) or _Resp(200, {"name": "n"}))
    gmb.create_local_post(rid, "Book now", cta_type="book", cta_url="https://gia.test/book")
    assert captured["body"]["callToAction"] == {"actionType": "BOOK", "url": "https://gia.test/book"}


def test_call_to_action_rules_are_enforced_before_any_request(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(gmb, "get_valid_token", lambda r: "tok")
    def _explode(*a, **k):
        raise AssertionError("should not have called Google")
    monkeypatch.setattr(gmb.requests, "post", _explode)

    # CALL uses the listing's own number, so a url is a contradiction.
    assert gmb.create_local_post(rid, "Ring us", cta_type="CALL",
                                 cta_url="https://x.test")["ok"] is False
    # Every other action type needs one.
    assert gmb.create_local_post(rid, "Shop", cta_type="SHOP")["ok"] is False
    # Unknown action types never reach Google.
    assert gmb.create_local_post(rid, "Hi", cta_type="TELEPORT")["ok"] is False


def test_call_button_sends_no_url(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(gmb, "get_valid_token", lambda r: "tok")
    captured = {}
    monkeypatch.setattr(gmb.requests, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        captured.update(body=json) or _Resp(200, {"name": "n"}))
    gmb.create_local_post(rid, "Ring us", cta_type="CALL")
    assert captured["body"]["callToAction"] == {"actionType": "CALL"}


def test_empty_and_oversized_text_are_refused_locally(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(gmb, "get_valid_token", lambda r: "tok")
    def _explode(*a, **k):
        raise AssertionError("should not have called Google")
    monkeypatch.setattr(gmb.requests, "post", _explode)
    assert gmb.create_local_post(rid, "   ")["ok"] is False
    assert gmb.create_local_post(rid, "x" * 1501)["ok"] is False


def test_an_unconnected_restaurant_never_calls_google(db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="NoGMB", owner_email="n@x.com"), db_path=db_path)
    def _explode(*a, **k):
        raise AssertionError("should not have called Google")
    monkeypatch.setattr(gmb.requests, "post", _explode)
    result = gmb.create_local_post(rid, "Hello")
    assert result["ok"] is False and "not connected" in result["error"].lower()


def test_a_google_error_is_surfaced_not_swallowed(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(gmb, "get_valid_token", lambda r: "tok")
    monkeypatch.setattr(gmb.requests, "post",
                        lambda *a, **k: _Resp(403, text="permission denied"))
    result = gmb.create_local_post(rid, "Hello")
    assert result["ok"] is False and "403" in result["error"]
