"""get_nearby_competitors() — Google Places lookups are real network calls,
so every test here mocks competitor.requests.get, matching test_weather.py's
own pattern (monkeypatch.setattr(competitor.requests, "get", fake_get) /
monkeypatch.setattr(competitor, "PLACES_API_KEY", ...) since the module-level
key is read once at import time and env-var patching after that has no effect
on the already-bound value)."""
import competitor


class FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


_OWN_DETAILS = {
    "status": "OK",
    "result": {
        "name": "Gia Mia",
        "geometry": {"location": {"lat": 41.91, "lng": -88.31}},
        "types": ["restaurant", "food", "point_of_interest", "establishment"],
        "price_level": 2,
    },
}


def _place(name, place_id, types, rating=4.3, reviews=150, price_level=2):
    return {
        "place_id": place_id,
        "name": name,
        "types": types,
        "rating": rating,
        "user_ratings_total": reviews,
        "price_level": price_level,
        "business_status": "OPERATIONAL",
        "vicinity": "123 Main St",
    }


def _make_fake_get(nearby_results, wider_results=None, details=None):
    """Routes on URL + whether the nearbysearch call carries a keyword/
    widened radius, mirroring the real call sequence: Details -> Nearby
    (keyword) -> Nearby (no keyword, only if too few) -> Nearby (wider
    radius, only if still too few)."""
    def fake_get(url, params=None, timeout=None):
        params = params or {}
        if "details" in url:
            return FakeResp(details or _OWN_DETAILS)
        if "nearbysearch" in url:
            if wider_results is not None and params.get("radius", 0) > 2000:
                return FakeResp({"status": "OK", "results": wider_results})
            return FakeResp({"status": "OK", "results": nearby_results})
        raise AssertionError(f"unexpected URL: {url}")
    return fake_get


def test_returns_empty_without_api_key(monkeypatch):
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "")
    assert competitor.get_nearby_competitors("some-place-id") == []


def test_returns_empty_without_place_id(monkeypatch):
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "fake-key")
    assert competitor.get_nearby_competitors("") == []


def test_filters_out_fast_food_chains(monkeypatch):
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "fake-key")
    results = [
        _place("Mio Modo", "p1", ["restaurant", "food"]),
        _place("McDonald's", "p2", ["restaurant", "food"]),
    ]
    monkeypatch.setattr(competitor.requests, "get", _make_fake_get(results))
    names = [c["name"] for c in competitor.get_nearby_competitors("gm")]
    assert "Mio Modo" in names
    assert "McDonald's" not in names


def test_filters_out_pure_beverage_spots_but_keeps_food_serving_bar(monkeypatch):
    """A coffee shop or bar with no food-service type at all isn't a real
    dining competitor and should be dropped — but a brewery/gastropub
    that's ALSO tagged "restaurant" (or meal_takeaway/bakery) genuinely
    competes on food and must survive the filter."""
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "fake-key")
    results = [
        _place("Alter Brewing", "p1", ["bar", "restaurant", "food", "point_of_interest"]),
        _place("Corner Coffee", "p2", ["cafe", "food", "point_of_interest"]),
        _place("Late Night Lounge", "p3", ["bar", "night_club", "point_of_interest"]),
        _place("Downtown Bistro", "p4", ["restaurant", "food", "point_of_interest"]),
    ]
    monkeypatch.setattr(competitor.requests, "get", _make_fake_get(results))
    names = {c["name"] for c in competitor.get_nearby_competitors("gm")}
    assert names == {"Alter Brewing", "Downtown Bistro"}


def test_is_pure_beverage_spot_helper():
    assert competitor._is_pure_beverage_spot(["cafe", "food", "point_of_interest"]) is True
    assert competitor._is_pure_beverage_spot(["bar", "night_club"]) is True
    assert competitor._is_pure_beverage_spot(["bar", "restaurant"]) is False
    assert competitor._is_pure_beverage_spot(["cafe", "bakery"]) is False
    assert competitor._is_pure_beverage_spot(["restaurant", "food"]) is False
    assert competitor._is_pure_beverage_spot([]) is False


def test_widens_radius_when_too_few_competitors_found(monkeypatch):
    """A restaurant in a lower-density area might only have 1-2 comparable
    places within the default radius — rather than showing an owner just
    that, one broader retry should pull in more real candidates."""
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "fake-key")
    narrow = [_place("Mio Modo", "p1", ["restaurant", "food"])]
    wider = [
        _place("Mio Modo", "p1", ["restaurant", "food"]),  # same place, must dedupe
        _place("Alter Brewing", "p2", ["bar", "restaurant", "food"]),
        _place("Downtown Bistro", "p3", ["restaurant", "food"]),
    ]
    monkeypatch.setattr(competitor.requests, "get", _make_fake_get(narrow, wider_results=wider))
    result = competitor.get_nearby_competitors("gm")
    names = [c["name"] for c in result]
    assert names.count("Mio Modo") == 1
    assert "Alter Brewing" in names
    assert "Downtown Bistro" in names


def test_caps_at_max_results(monkeypatch):
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "fake-key")
    results = [_place(f"Restaurant {i}", f"p{i}", ["restaurant", "food"]) for i in range(10)]
    monkeypatch.setattr(competitor.requests, "get", _make_fake_get(results))
    result = competitor.get_nearby_competitors("gm", max_results=5)
    assert len(result) == 5
