"""fetch_gmb_logo_url — backfilling the Account hero banner's logo from the
restaurant's own Business Profile LOGO-category photo.

The endpoint itself can't be exercised here (no Google Business Profile can
be connected in this environment), so these pin down everything that IS
ours: never overwriting an existing brand_logo_url, the request shape, and
honest failure when Google has no logo on file.
"""
import pytest

import models
import gmb
from models import create_restaurant, Restaurant, update_restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real_get_conn(db_path))


def _connected(db_path, brand_logo_url=None):
    rid = create_restaurant(Restaurant(name="G", owner_email="g@x.com"), db_path=db_path)
    update_restaurant(rid, {"gmb_refresh_token": "r", "gmb_account_id": "accounts/123",
                            "gmb_location_id": "locations/456",
                            "brand_logo_url": brand_logo_url}, db_path=db_path)
    return rid


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_finds_the_logo_category_item_and_caches_it(db_path, monkeypatch):
    rid = _connected(db_path)
    captured = {}

    def _get(url, headers=None, timeout=None):
        captured.update(url=url, headers=headers)
        return _Resp(200, {"mediaItems": [
            {"locationAssociation": {"category": "COVER"}, "googleUrl": "https://x/cover.jpg"},
            {"locationAssociation": {"category": "LOGO"}, "googleUrl": "https://x/logo.jpg"},
        ]})

    monkeypatch.setattr(gmb.requests, "get", _get)
    result = gmb.fetch_gmb_logo_url(rid, "tok", "accounts/123", "locations/456")
    assert result == {"ok": True, "url": "https://x/logo.jpg"}
    assert captured["url"] == "https://mybusiness.googleapis.com/v4/accounts/123/locations/456/media"
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert get_restaurant(rid, db_path=db_path).brand_logo_url == "https://x/logo.jpg"


def test_never_overwrites_an_existing_logo(db_path, monkeypatch):
    rid = _connected(db_path, brand_logo_url="https://existing/logo.png")

    def _explode(*a, **k):
        raise AssertionError("should not have called Google")
    monkeypatch.setattr(gmb.requests, "get", _explode)

    result = gmb.fetch_gmb_logo_url(rid, "tok", "accounts/123", "locations/456")
    assert result["ok"] is False
    assert get_restaurant(rid, db_path=db_path).brand_logo_url == "https://existing/logo.png"


def test_no_logo_category_item_is_an_honest_miss(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(gmb.requests, "get",
                        lambda url, headers=None, timeout=None:
                        _Resp(200, {"mediaItems": [
                            {"locationAssociation": {"category": "COVER"}, "googleUrl": "https://x/cover.jpg"},
                        ]}))
    result = gmb.fetch_gmb_logo_url(rid, "tok", "accounts/123", "locations/456")
    assert result["ok"] is False
    assert get_restaurant(rid, db_path=db_path).brand_logo_url is None


def test_api_error_is_reported_not_raised(db_path, monkeypatch):
    rid = _connected(db_path)
    monkeypatch.setattr(gmb.requests, "get",
                        lambda url, headers=None, timeout=None: _Resp(404, text="not found"))
    result = gmb.fetch_gmb_logo_url(rid, "tok", "accounts/123", "locations/456")
    assert result["ok"] is False
    assert "404" in result["error"]


def test_missing_account_or_location_id_short_circuits(db_path, monkeypatch):
    rid = _connected(db_path)

    def _explode(*a, **k):
        raise AssertionError("should not have called Google")
    monkeypatch.setattr(gmb.requests, "get", _explode)

    assert gmb.fetch_gmb_logo_url(rid, "tok", None, "locations/456")["ok"] is False
    assert gmb.fetch_gmb_logo_url(rid, "tok", "accounts/123", None)["ok"] is False
