"""Fix round G — Google Places and Perplexity.

#123 every Places request goes through ai_utils.places_request: metered as
     it is made, refused before it is sent at the restaurant's Places
     ceiling — including the five paths that were never metered (the
     visibility city lookup, first look, menu notes, the owner's search,
     owner-added competitors).
#151 a breaker and a key check for Perplexity and Places: an empty key sends
     nothing, the first 401/403 opens the breaker, and while it is open
     nothing is sent.
No test here reaches Google or Perplexity: requests.get / requests.post are
stubbed.
"""
import sqlite3

import pytest
import requests

import ai_utils
import models
from models import Restaurant, create_restaurant


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


def _rid(db_path, place_id="ChIJme", name="Places Co", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                        google_place_id=place_id, **kw), db_path=db_path)


def _places_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ai_usage WHERE vendor='google_places' ORDER BY id")]
    finally:
        conn.close()


def _stub_get(monkeypatch, answer):
    sent = []

    def get(url, params=None, timeout=None, **kw):
        sent.append((url, dict(params or {})))
        return answer(url, params or {}) if callable(answer) else answer
    monkeypatch.setattr(requests, "get", get)
    return sent


# ── the helper ──────────────────────────────────────────────────────────────

def test_an_ok_request_is_metered_once_at_its_sku_price(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub_get(monkeypatch, _Resp({"status": "OK", "result": {}}))
    r = ai_utils.places_request("details", {"place_id": "x", "key": "k"}, restaurant_id=rid, action="t")
    assert r.json() == {"status": "OK", "result": {}}
    (row,) = _places_rows(db_path)
    assert (row["restaurant_id"], row["action"], row["model"], row["outcome"]) == (rid, "t", "google-places-details", "ok")
    assert row["cost_usd"] == pytest.approx(ai_utils._PER_CALL_PRICING["google-places-details"])
    assert row["latency_ms"] is not None


def test_no_key_sends_nothing(db_path, monkeypatch):
    rid = _rid(db_path)
    sent = _stub_get(monkeypatch, _Resp({"status": "OK"}))
    with pytest.raises(ai_utils.PlacesUnavailable) as e:
        ai_utils.places_request("details", {"place_id": "x", "key": ""}, restaurant_id=rid, action="t")
    assert e.value.reason == "no_key" and sent == []
    assert _places_rows(db_path)[0]["reason"] == "no_key"


def test_the_places_ceiling_stops_a_loop_before_it_bills(db_path, monkeypatch):
    rid = _rid(db_path)
    sent = _stub_get(monkeypatch, _Resp({"status": "OK", "result": {}}))
    n = int(ai_utils.AI_PLACES_DAILY_BUDGET_USD / ai_utils._PER_CALL_PRICING["google-places-details"]) + 5
    refused = 0
    for _ in range(n):
        ai_utils._budget_cache.clear()
        try:
            ai_utils.places_request("details", {"place_id": "x", "key": "k"}, restaurant_id=rid,
                                    action="weather_geocode")
        except ai_utils.PlacesUnavailable as e:
            assert e.reason == "budget"
            refused += 1
    assert refused >= 4, "the 570-geocode loop would still be billing"
    assert len(sent) < n
    assert ai_utils.places_budget_status(rid)["day"]["over"]
    # ...and it never touched the AI ceiling.
    assert ai_utils.ai_budget_status(rid)["day"]["spend"] == 0.0


def test_a_denied_key_opens_the_places_breaker_on_the_first_answer(db_path, monkeypatch):
    rid = _rid(db_path)
    sent = _stub_get(monkeypatch, _Resp({"status": "REQUEST_DENIED", "error_message": "The provided API key is invalid."}))
    r = ai_utils.places_request("details", {"place_id": "x", "key": "k"}, restaurant_id=rid, action="t")
    assert r.json()["status"] == "REQUEST_DENIED"
    assert ai_utils.breaker_state("google_places")[0] == "open"
    with pytest.raises(ai_utils.PlacesUnavailable) as e:
        ai_utils.places_request("details", {"place_id": "x", "key": "k"}, restaurant_id=rid, action="t")
    assert e.value.reason == "breaker" and len(sent) == 1
    rows = _places_rows(db_path)
    assert [r["outcome"] for r in rows] == ["error", "blocked"] and rows[0]["cost_usd"] == 0.0
    conn = sqlite3.connect(db_path)
    events = [r[0] for r in conn.execute("SELECT event FROM ai_health_events WHERE vendor='google_places'")]
    conn.close()
    assert "breaker_open" in events and "auth_error" in events


def test_server_errors_count_toward_the_breaker_and_a_dead_place_id_does_not(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub_get(monkeypatch, _Resp({"status": "NOT_FOUND"}))
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD + 2):
        ai_utils.places_request("details", {"place_id": "gone", "key": "k"}, restaurant_id=rid, action="t")
    assert ai_utils.breaker_state("google_places")[0] == "closed", "a fact about one Place ID is not an outage"
    _stub_get(monkeypatch, _Resp({}, status_code=503))
    for _ in range(ai_utils.CB_FAILURE_THRESHOLD):
        ai_utils.places_request("details", {"place_id": "x", "key": "k"}, restaurant_id=rid, action="t")
    assert ai_utils.breaker_state("google_places")[0] == "open"


def test_attribution_comes_from_ai_context_for_helpers_that_take_no_restaurant(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub_get(monkeypatch, _Resp({"status": "OK", "results": []}))
    with ai_utils.ai_context(restaurant_id=rid, action="competitor_intel", correlation_id="intel:1"):
        ai_utils.places_request("nearbysearch", {"location": "1,2", "key": "k"})
    (row,) = _places_rows(db_path)
    assert (row["restaurant_id"], row["action"], row["correlation_id"], row["model"]) == \
        (rid, "competitor_intel", "intel:1", "google-places-nearby")


# ── the five paths that were never metered ─────────────────────────────────

def test_menu_notes_are_metered_to_the_restaurant(db_path, monkeypatch):
    import competitor
    rid = _rid(db_path)
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    _stub_get(monkeypatch, _Resp({"status": "OK", "result": {"serves_dinner": True}}))
    assert "dinner" in competitor.fetch_menu_notes_from_places("ChIJme", restaurant_id=rid)
    assert [(r["restaurant_id"], r["action"]) for r in _places_rows(db_path)] == [(rid, "menu_notes")]


def test_the_owners_search_is_metered_as_a_text_search(db_path, monkeypatch):
    import competitor
    rid = _rid(db_path)
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    _stub_get(monkeypatch, _Resp({"status": "OK", "results": [{"place_id": "p", "name": "Rival"}]}))
    with ai_utils.ai_context(restaurant_id=rid):
        assert competitor.search_places_near("Rival")[0]["name"] == "Rival"
    (row,) = _places_rows(db_path)
    assert (row["restaurant_id"], row["model"]) == (rid, "google-places-textsearch")
    assert row["cost_usd"] == pytest.approx(ai_utils._PER_CALL_PRICING["google-places-textsearch"])


def test_the_visibility_city_lookup_is_metered(db_path, monkeypatch):
    import client_api
    rid = _rid(db_path)
    client_api._city_cache.clear()
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "k")
    _stub_get(monkeypatch, _Resp({"status": "OK", "result": {"address_components": [
        {"types": ["locality"], "long_name": "Geneva"}]}}))
    with ai_utils.ai_context(restaurant_id=rid, action="ai_visibility"):
        assert client_api._city_from_place_id("ChIJme") == "Geneva"
    (row,) = _places_rows(db_path)
    assert (row["restaurant_id"], row["action"]) == (rid, "aivis_city")


def test_first_look_is_metered_to_the_account_that_owns_the_place_id(db_path, monkeypatch):
    import config
    import first_look
    rid = _rid(db_path, place_id="ChIJfirst")
    first_look._CACHE.clear()
    monkeypatch.setattr(config, "google_places_key", lambda: "k")
    _stub_get(monkeypatch, _Resp({"status": "OK", "result": {"name": "Places Co", "rating": 4.4,
                                                              "user_ratings_total": 20}}))
    assert first_look.build("ChIJfirst")["rating"] == 4.4
    assert [(r["restaurant_id"], r["action"]) for r in _places_rows(db_path)] == [(rid, "first_look")]


def test_owner_added_competitors_and_every_review_lookup_are_metered(db_path, monkeypatch):
    import competitor
    import webhooks
    rid = _rid(db_path, place_id="ChIJself")
    models.update_restaurant(rid, {"custom_competitors": "ChIJcustom"}, db_path=db_path)
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    monkeypatch.setattr(competitor, "generate_competitor_insight", lambda *a, **k: "A grounded insight.")
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    near = [{"place_id": f"p{i}", "name": f"Rival {i}", "types": ["restaurant"], "user_ratings_total": 90,
             "price_level": 2, "business_status": "OPERATIONAL", "rating": 4.2} for i in range(3)]

    def answer(url, params):
        if "nearbysearch" in url:
            return _Resp({"status": "OK", "results": near})
        if params.get("place_id") == "ChIJself":
            return _Resp({"status": "OK", "result": {"name": "Places Co", "types": ["restaurant"],
                                                      "geometry": {"location": {"lat": 1.0, "lng": 2.0}}}})
        if params.get("place_id") == "ChIJcustom" and "reviews" not in params.get("fields", ""):
            return _Resp({"status": "OK", "result": {"name": "Custom Rival", "business_status": "OPERATIONAL"}})
        return _Resp({"status": "OK", "result": {"reviews": []}})
    _stub_get(monkeypatch, answer)
    assert competitor.run_competitor_analysis(rid)["ok"] is True
    rows = _places_rows(db_path)
    assert {r["restaurant_id"] for r in rows} == {rid}
    corr = {r["correlation_id"] for r in rows}
    assert len(corr) == 1 and next(iter(corr)).startswith("intel:"), "one run, one correlation id"
    details = [r for r in rows if r["model"] == "google-places-details"]
    # own details + the custom competitor's details + a reviews lookup per
    # competitor (three discovered, one added by the owner).
    assert len(details) == 1 + 1 + 4


def test_a_refused_lookup_is_never_reported_as_an_empty_market(db_path, monkeypatch):
    import competitor
    rid = _rid(db_path, place_id="ChIJself")
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    ai_utils.trip_breaker("google_places", "auth")
    out = competitor.run_competitor_analysis(rid)
    assert out["ok"] is False and out["places_status"] == "breaker"
    assert "No nearby competitors" not in out["error"]


# ── Perplexity ──────────────────────────────────────────────────────────────

def _aivis_restaurant(db_path, monkeypatch):
    import client_api
    rid = _rid(db_path, place_id="ChIJvis", neighborhood="Geneva", vibe="lively pizza bar",
               known_for="wood-fired pizza", name="Gia Mia")
    monkeypatch.setattr(client_api, "_city_from_place_id", lambda pid: "Geneva")
    monkeypatch.setattr(client_api, "get_review_stats", lambda r: {"total": 10, "response_rate": 50})
    client_api._aivis_cache.clear()
    return rid


def _perplexity_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM ai_usage WHERE vendor='perplexity' ORDER BY id")]
    finally:
        conn.close()


def test_an_empty_perplexity_key_sends_nothing(db_path, monkeypatch):
    import client_api
    rid = _aivis_restaurant(db_path, monkeypatch)
    posted = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: posted.append(1))
    monkeypatch.setenv("PERPLEXITY_API_KEY", "")
    payload, _ = client_api._do_ai_visibility(rid, force=True)
    assert payload["ok"] is False and posted == []
    (row,) = _perplexity_rows(db_path)
    assert (row["outcome"], row["reason"]) == ("blocked", "no_key")


def test_a_rejected_perplexity_key_stops_the_run_after_the_first_answer(db_path, monkeypatch):
    import client_api
    rid = _aivis_restaurant(db_path, monkeypatch)
    posted = []

    def post(*a, **k):
        posted.append(1)
        return _Resp({}, status_code=401)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "revoked")
    import concurrent.futures
    # One worker, so the order is deterministic: the first 401 trips the
    # breaker and every later query fails fast without a request.
    real_pool = concurrent.futures.ThreadPoolExecutor
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", lambda max_workers=None: real_pool(max_workers=1))
    payload, _ = client_api._do_ai_visibility(rid, force=True)
    assert len(posted) == 1, f"{len(posted)} requests sent with a key Perplexity had already rejected"
    assert ai_utils.breaker_state("perplexity")[0] == "open"
    assert payload.get("ai_score") is None


def test_one_query_is_one_ledger_row_with_its_retry_as_an_attempt(db_path, monkeypatch):
    import client_api
    rid = _aivis_restaurant(db_path, monkeypatch)
    seq = [None, "Try Gia Mia in Geneva."] + ["Try Gia Mia in Geneva."] * 20

    def post(*a, **k):
        a_ = seq.pop(0)
        return _Resp({"choices": [{"message": {"content": a_}}], "citations": [],
                      "usage": {"prompt_tokens": 40, "completion_tokens": 100}} if a_ else {}, 200)
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    import concurrent.futures
    real_pool = concurrent.futures.ThreadPoolExecutor
    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", lambda max_workers=None: real_pool(max_workers=1))
    payload, _ = client_api._do_ai_visibility(rid, force=True)
    rows = _perplexity_rows(db_path)
    assert len(rows) == 8, "one row per query, not per send"
    assert rows[0]["attempts"] == 2 and rows[0]["outcome"] == "ok"
    assert all(r["model"] == client_api.AIVIS_MODEL for r in rows)
    assert rows[1]["cost_usd"] == pytest.approx(0.005 + 140 / 1_000_000)
    corr = {r["correlation_id"] for r in rows}
    assert len(corr) == 1 and next(iter(corr)).startswith("aivis:"), "one run, one correlation id"
    assert {r["restaurant_id"] for r in rows} == {rid}
