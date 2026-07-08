"""weather.py — NWS forecast lookup for weather-aware labor scheduling.
Geocoding (Google Places) and the forecast (api.weather.gov) are both real
network calls, so every test here mocks weather.requests.get rather than
hitting either service. The whole module's contract is "never raises, never
blocks schedule generation" — most tests are about confirming a failure at
any stage degrades to [] / (None, None) instead of propagating."""
import json

import pytest

import weather
from models import create_restaurant, get_restaurant, update_restaurant, Restaurant

# create_restaurant's INSERT statement is a fixed column list that doesn't
# include latitude/longitude/weather_cache_json/weather_cached_at (the same
# gap several other Restaurant fields already have) — those can only be set
# via update_restaurant after creation, so tests needing them pre-populated
# go through that path rather than the Restaurant(...) constructor.
_POST_CREATE_KEYS = {"latitude", "longitude", "weather_cache_json", "weather_cached_at"}


def _restaurant(db_path, **kw):
    post = {k: kw.pop(k) for k in list(kw.keys()) if k in _POST_CREATE_KEYS}
    rid = create_restaurant(Restaurant(name=kw.pop("name", "Weather Test Co"), owner_email="w@x.com", **kw), db_path=db_path)
    if post:
        update_restaurant(rid, post, db_path=db_path)
    return get_restaurant(rid, db_path=db_path)


def _fake_get(points_json=None, forecast_json=None, places_json=None, raise_on=None):
    """Builds a fake requests.get that branches on URL, matching the real
    call sequence: Google Places Details (geocode) -> NWS /points -> NWS
    forecast URL."""
    class FakeResp:
        def __init__(self, payload):
            self._payload = payload
        def raise_for_status(self):
            pass
        def json(self):
            return self._payload

    def fake_get(url, *a, **kw):
        if raise_on and raise_on in url:
            raise RuntimeError("simulated network failure")
        if "maps.googleapis.com" in url:
            return FakeResp(places_json or {})
        if "api.weather.gov/points" in url:
            return FakeResp(points_json or {})
        return FakeResp(forecast_json or {})

    return fake_get


_PLACES_OK = {"result": {"geometry": {"location": {"lat": 41.91, "lng": -88.31}}}}
_POINTS_OK = {"properties": {"forecast": "https://api.weather.gov/gridpoints/LOT/75,73/forecast"}}


def _periods(dates_and_temps):
    """dates_and_temps: list of (date_str, day_name, temp, short, precip)."""
    periods = []
    for date_str, day_name, temp, short, precip in dates_and_temps:
        periods.append({
            "name": day_name, "startTime": f"{date_str}T06:00:00-05:00",
            "isDaytime": True, "temperature": temp, "shortForecast": short,
            "probabilityOfPrecipitation": {"value": precip},
        })
        periods.append({
            "name": day_name + " Night", "startTime": f"{date_str}T18:00:00-05:00",
            "isDaytime": False, "temperature": temp - 15, "shortForecast": "Clear",
            "probabilityOfPrecipitation": {"value": precip},
        })
    return periods


# ── geocoding ─────────────────────────────────────────────────────────────────

def test_geocode_returns_cached_coordinates_without_api_call(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest", latitude=41.91, longitude=-88.31)

    def fail_if_called(*a, **kw):
        raise AssertionError("should not call requests.get when coordinates are already cached")
    monkeypatch.setattr(weather.requests, "get", fail_if_called)

    lat, lon = weather._geocode(r, db_path=db_path)
    assert (lat, lon) == (41.91, -88.31)


def test_geocode_returns_none_without_place_id(db_path, monkeypatch):
    r = _restaurant(db_path)  # no google_place_id
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "fake-key")
    lat, lon = weather._geocode(r, db_path=db_path)
    assert (lat, lon) == (None, None)


def test_geocode_returns_none_without_api_key(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest")
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "")
    lat, lon = weather._geocode(r, db_path=db_path)
    assert (lat, lon) == (None, None)


def test_geocode_fetches_and_caches_coordinates(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest")
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "fake-key")
    monkeypatch.setattr(weather.requests, "get", _fake_get(places_json=_PLACES_OK))

    lat, lon = weather._geocode(r, db_path=db_path)
    assert (lat, lon) == (41.91, -88.31)

    refetched = get_restaurant(r.id, db_path=db_path)
    assert refetched.latitude == 41.91
    assert refetched.longitude == -88.31


def test_geocode_handles_api_failure_gracefully(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest")
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "fake-key")
    monkeypatch.setattr(weather.requests, "get", _fake_get(raise_on="maps.googleapis.com"))

    lat, lon = weather._geocode(r, db_path=db_path)
    assert (lat, lon) == (None, None)


def test_geocode_handles_missing_geometry_in_response(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest")
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "fake-key")
    monkeypatch.setattr(weather.requests, "get", _fake_get(places_json={"result": {}}))

    assert weather._geocode(r, db_path=db_path) == (None, None)


# ── get_forecast_for_week ─────────────────────────────────────────────────────

def test_forecast_returns_empty_for_no_restaurant(db_path):
    assert weather.get_forecast_for_week(None, ["2026-07-13"], db_path=db_path) == []


def test_forecast_returns_empty_when_geocoding_fails(db_path):
    r = _restaurant(db_path)  # no google_place_id -> geocoding fails
    assert weather.get_forecast_for_week(r, ["2026-07-13"], db_path=db_path) == []


def test_forecast_returns_empty_on_nws_failure(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest")
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "fake-key")
    monkeypatch.setattr(weather.requests, "get", _fake_get(
        places_json=_PLACES_OK, raise_on="api.weather.gov"
    ))
    assert weather.get_forecast_for_week(r, ["2026-07-13"], db_path=db_path) == []


def test_forecast_fetches_and_matches_week_dates(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest")
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "fake-key")
    week_dates = ["2026-07-13", "2026-07-14", "2026-07-15"]
    periods = _periods([
        ("2026-07-13", "Monday", 89, "Sunny", 5),
        ("2026-07-14", "Tuesday", 72, "Rain Showers", 80),
    ])
    monkeypatch.setattr(weather.requests, "get", _fake_get(
        places_json=_PLACES_OK, points_json=_POINTS_OK,
        forecast_json={"properties": {"periods": periods}},
    ))

    rows = weather.get_forecast_for_week(r, week_dates, db_path=db_path)

    assert len(rows) == 2  # 2026-07-15 has no matching period -> omitted, not guessed
    assert rows[0] == {"date": "2026-07-13", "day_name": "Monday", "high_f": 89,
                        "short_forecast": "Sunny", "precip_pct": 5}
    assert rows[1]["short_forecast"] == "Rain Showers"
    assert rows[1]["precip_pct"] == 80


def test_forecast_skips_night_periods(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest")
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "fake-key")
    periods = _periods([("2026-07-13", "Monday", 89, "Sunny", 5)])
    monkeypatch.setattr(weather.requests, "get", _fake_get(
        places_json=_PLACES_OK, points_json=_POINTS_OK,
        forecast_json={"properties": {"periods": periods}},
    ))

    rows = weather.get_forecast_for_week(r, ["2026-07-13"], db_path=db_path)

    assert len(rows) == 1
    assert rows[0]["high_f"] == 89  # the day period's temp, not the night period's (89-15=74)


def test_forecast_caches_result_on_restaurant(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtest")
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "fake-key")
    periods = _periods([("2026-07-13", "Monday", 89, "Sunny", 5)])
    monkeypatch.setattr(weather.requests, "get", _fake_get(
        places_json=_PLACES_OK, points_json=_POINTS_OK,
        forecast_json={"properties": {"periods": periods}},
    ))

    weather.get_forecast_for_week(r, ["2026-07-13"], db_path=db_path)

    refetched = get_restaurant(r.id, db_path=db_path)
    assert refetched.weather_cached_at is not None
    assert json.loads(refetched.weather_cache_json) == periods


def test_forecast_uses_cache_within_ttl_without_new_api_calls(db_path, monkeypatch):
    from datetime import datetime
    periods = _periods([("2026-07-13", "Monday", 89, "Sunny", 5)])
    r = _restaurant(db_path, google_place_id="ChIJtest", latitude=41.91, longitude=-88.31,
                     weather_cache_json=json.dumps(periods), weather_cached_at=datetime.now().isoformat())

    def fail_if_called(*a, **kw):
        raise AssertionError("should not call requests.get when cache is fresh")
    monkeypatch.setattr(weather.requests, "get", fail_if_called)

    rows = weather.get_forecast_for_week(r, ["2026-07-13"], db_path=db_path)
    assert rows[0]["short_forecast"] == "Sunny"


def test_forecast_refetches_when_cache_is_stale(db_path, monkeypatch):
    from datetime import datetime, timedelta
    old_periods = _periods([("2026-07-13", "Monday", 60, "Cloudy", 50)])
    r = _restaurant(
        db_path, google_place_id="ChIJtest", latitude=41.91, longitude=-88.31,
        weather_cache_json=json.dumps(old_periods),
        weather_cached_at=(datetime.now() - timedelta(hours=weather._CACHE_HOURS + 1)).isoformat(),
    )
    fresh_periods = _periods([("2026-07-13", "Monday", 89, "Sunny", 5)])
    monkeypatch.setattr(weather.requests, "get", _fake_get(
        points_json=_POINTS_OK, forecast_json={"properties": {"periods": fresh_periods}},
    ))

    rows = weather.get_forecast_for_week(r, ["2026-07-13"], db_path=db_path)
    assert rows[0]["short_forecast"] == "Sunny"  # refetched, not the stale cached value
