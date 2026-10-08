"""Google Places spend (AI cost audit 10/7/26): what a competitor read and
the review fetch buy from Google, and what they no longer buy twice.

#13 one Details answer per place per day, served to every restaurant that
    tracks the place — a cache hit sends nothing and writes no ledger row;
#14 a rating move alone updates the comparison in place, never a new read;
#15 the daily check carries the newest reviews, and a run reuses them;
#24 the own rating rides on the review fetch, never over a fresher
    Business Profile reading, and the daily check skips it when fresh;
#39 the nearby search runs monthly or on request, not every run, and the
    own listing comes from what is stored;
#40 an owner-added competitor is one lookup, and ten is the cap;
#41 the closure check reuses the status the daily check stored;
#42 a Refresh inside six hours serves the stored read;
#43 a Places request is priced by the data SKUs its fields pull in.
No test here reaches Google: requests.get / competitor._places_request are
stubbed.
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest
import requests

import ai_utils
import competitor
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

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


def _rid(db_path, name="Places Co", place_id="ChIJme", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                       google_place_id=place_id, timezone="America/Chicago"), db_path=db_path)
    if cols:
        _set(db_path, rid, **cols)
    return rid


def _set(db_path, rid, **cols):
    conn = sqlite3.connect(db_path)
    conn.execute(f"UPDATE restaurants SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), rid))
    conn.commit()
    conn.close()


def _rows(db_path, sql="SELECT * FROM ai_usage WHERE vendor='google_places' ORDER BY id", args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _stub_get(monkeypatch, answer):
    sent = []

    def get(url, params=None, timeout=None, **kw):
        sent.append((url, dict(params or {})))
        return answer(url, params or {}) if callable(answer) else answer
    monkeypatch.setattr(requests, "get", get)
    return sent


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="seconds")


def _blob(db_path, rid):
    return json.loads(models.get_restaurant(rid).competitor_intel)


# ── #43 pricing by data SKU ─────────────────────────────────────────────────

def test_places_requests_are_priced_by_the_data_skus_their_fields_pull_in():
    p = ai_utils._PER_CALL_PRICING
    base, atm, contact = p["google-places-details"], p["google-places-atmosphere"], p["google-places-contact"]
    fee = ai_utils.places_fee
    assert fee("details", {"fields": "name,business_status,geometry/location"}) == pytest.approx(base)
    assert fee("details", {"fields": "name,rating,reviews"}) == pytest.approx(base + atm)
    assert fee("details", {"fields": "name,website"}) == pytest.approx(base + contact)
    assert fee("details", {"fields": "rating,opening_hours"}) == pytest.approx(base + atm + contact)
    # No field list: Google returns, and bills, every field.
    assert fee("details", {}) == pytest.approx(base + atm + contact)
    # An unlisted field over-counts rather than slipping under the budget.
    assert fee("details", {"fields": "name,some_new_field"}) == pytest.approx(base + atm + contact)
    for kind in ("nearby", "textsearch"):
        assert fee(kind, {"location": "1,2"}) == pytest.approx(p[f"google-places-{kind}"] + atm + contact)
        assert fee(kind, {}) == pytest.approx(0.040)


def test_the_ledger_books_a_request_at_its_tiered_fee(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub_get(monkeypatch, _Resp({"status": "OK", "result": {"rating": 4.4}}))
    ai_utils.places_request("details", {"place_id": "p", "fields": "rating,user_ratings_total", "key": "k"},
                            restaurant_id=rid, action="t")
    (row,) = _rows(db_path)
    assert row["cost_usd"] == pytest.approx(0.022)


def test_a_refused_request_still_costs_nothing(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub_get(monkeypatch, _Resp({"status": "NOT_FOUND"}))
    ai_utils.places_request("details", {"place_id": "gone", "fields": "rating", "key": "k"},
                            restaurant_id=rid, action="t")
    assert _rows(db_path)[0]["cost_usd"] == 0.0


# ── #13 the Details cache ───────────────────────────────────────────────────

def test_one_details_fetch_per_place_per_day_across_restaurants(db_path, monkeypatch):
    a, b = _rid(db_path, "Alpha Co", "ChIJa"), _rid(db_path, "Beta Co", "ChIJb")
    sent = _stub_get(monkeypatch, _Resp({"status": "OK", "result": {"name": "Rival", "rating": 4.2,
                                                                     "user_ratings_total": 80, "reviews": []}}))
    full = {"place_id": "rival", "fields": "name,rating,user_ratings_total,reviews", "reviews_sort": "newest",
            "key": "k"}
    assert ai_utils.places_request("details", full, restaurant_id=a, action="competitor_daily").json()["result"]["rating"] == 4.2
    # The other restaurant, the same place, a subset of the fields: served.
    sub = {"place_id": "rival", "fields": "rating,name", "key": "k"}
    r = ai_utils.places_request("details", sub, restaurant_id=b, action="competitor_intel")
    assert r.json()["result"]["name"] == "Rival" and r.status_code == 200
    r.raise_for_status()
    assert len(sent) == 1
    rows = _rows(db_path)
    assert len(rows) == 1 and rows[0]["restaurant_id"] == a, "a cache hit writes no ledger row"
    # A field the cached answer does not hold is a new request.
    ai_utils.places_request("details", {"place_id": "rival", "fields": "rating,website", "key": "k"},
                            restaurant_id=b, action="t")
    assert len(sent) == 2


def test_the_cache_matches_the_review_order_and_never_feeds_the_review_fetch(db_path, monkeypatch):
    rid = _rid(db_path)
    sent = _stub_get(monkeypatch, _Resp({"status": "OK", "result": {"reviews": []}}))
    ai_utils.places_request("details", {"place_id": "p", "fields": "reviews", "key": "k"},
                            restaurant_id=rid, action="t")
    # Google's "most relevant" five are not the newest five.
    ai_utils.places_request("details", {"place_id": "p", "fields": "reviews", "reviews_sort": "newest",
                                        "key": "k"}, restaurant_id=rid, action="t")
    assert len(sent) == 2
    # The review fetch runs four times a day to see a review posted since
    # the last one: it always asks Google.
    ai_utils.places_request("details", {"place_id": "p", "fields": "reviews", "reviews_sort": "newest",
                                        "key": "k"}, restaurant_id=rid, action="review_fetch")
    assert len(sent) == 3


def test_only_an_ok_answer_is_cached_and_a_hit_needs_no_budget(db_path, monkeypatch):
    rid = _rid(db_path)
    sent = _stub_get(monkeypatch, _Resp({"status": "NOT_FOUND"}))
    req = {"place_id": "p", "fields": "rating", "key": "k"}
    ai_utils.places_request("details", req, restaurant_id=rid, action="t")
    ai_utils.places_request("details", req, restaurant_id=rid, action="t")
    assert len(sent) == 2
    _stub_get(monkeypatch, _Resp({"status": "OK", "result": {"rating": 4.0}}))
    ai_utils.places_request("details", req, restaurant_id=rid, action="t")
    # With the breaker open nothing can be sent — the cached answer still is.
    ai_utils.trip_breaker("google_places", "auth")
    assert ai_utils.places_request("details", req, restaurant_id=rid, action="t").json()["result"]["rating"] == 4.0


def test_the_cache_is_made_at_boot_and_pruned_inside_googles_30_days():
    import inspect
    import ops
    assert any("CREATE TABLE IF NOT EXISTS places_details_cache" in sql for sql in ai_utils._AI_OPS_DDL)
    for fn in (ai_utils.places_request, ai_utils._places_cache_get, ai_utils._places_cache_put):
        assert "CREATE TABLE" not in inspect.getsource(fn), "no DDL on the request path"
    assert 0 < ops._RETENTION_DAYS["places_details_cache"] <= 30
    assert ops._RETENTION_COLUMN["places_details_cache"] == "created_at"


# ── #14 / #15 the daily check ───────────────────────────────────────────────

def _tracked(db_path, competitors, **cols):
    rid = _rid(db_path, place_id="own", **cols)
    blob = {"competitors": competitors, "insight": "read", "generated_at": "2026-10-01",
            "discovered_at": _ago(days=3), "custom_ids": []}
    _set(db_path, rid, competitor_intel=json.dumps(blob))
    return rid


def _fake_places(answers):
    calls = []

    def fake(endpoint, params, restaurant_id=None, action=None, timeout=10):
        calls.append((endpoint, params.get("place_id"), action, params.get("fields")))
        return _Resp({"status": "OK", "result": answers.get(params.get("place_id"), {})})
    return fake, calls


def _review(ts, text="Great"):
    return {"author_name": "Ann", "rating": 5, "text": text, "time": ts, "relative_time_description": "a day ago"}


def test_a_rating_move_alone_updates_the_comparison_and_reads_nothing_again(db_path, monkeypatch):
    rid = _tracked(db_path, [{"name": "Rec Haus", "place_id": "p1", "rating": 4.5, "review_count": 300,
                              "reviews": [{"text": "old", "ts": 1_700_000_000}]}])
    fake, calls = _fake_places({"p1": {"rating": 4.3, "user_ratings_total": 302, "business_status": "OPERATIONAL",
                                       "reviews": [_review(1_600_000_000)]}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.check_ratings(rid)
    assert any("4.5★ → 4.3★" in m for m in out["moved"])
    assert out["reanalyse"] is False and out["triggers"] == []
    c = _blob(db_path, rid)["competitors"][0]
    assert (c["rating"], c["review_count"], c["business_status"]) == (4.3, 302, "OPERATIONAL")


def test_newer_reviews_are_carried_and_only_a_new_complaint_asks_for_a_new_read(db_path, monkeypatch):
    # Since re-audit P2 (10/7/26) one new 5★ review is reported, carried and
    # NOT a reason to buy a new read — any newer review used to be, which
    # re-read most restaurants every morning. A new review at 2★ or below is.
    rid = _tracked(db_path, [{"name": "Rec Haus", "place_id": "p1", "rating": 4.5, "review_count": 300,
                              "reviews": [{"text": "old", "ts": 1_700_000_000}]}])
    fake, calls = _fake_places({"p1": {"rating": 4.5, "user_ratings_total": 301, "business_status": "OPERATIONAL",
                                       "reviews": [_review(1_700_000_500, "New"), _review(1_699_000_000)]}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.check_ratings(rid)
    assert out["reanalyse"] is False and out["triggers"] == []
    assert any("1 review since the last read" in m for m in out["moved"])
    # The newest reviews ride on the same call (one Atmosphere SKU), newest first.
    (call,) = [c for c in calls if c[1] == "p1"]
    assert "reviews" in call[3]
    c = _blob(db_path, rid)["competitors"][0]
    assert [r["text"] for r in c["latest_reviews"]] == ["New", "Great"] and c["latest_reviews_at"]


def test_a_stored_read_without_review_times_compares_by_date(db_path, monkeypatch):
    rid = _tracked(db_path, [{"name": "Rec Haus", "place_id": "p1", "rating": 4.5, "review_count": 300,
                              "reviews": [{"text": "old", "date": "2023-11-14"}]}])
    same_day = int(datetime(2023, 11, 14, 20, tzinfo=timezone.utc).timestamp())
    fake, _calls = _fake_places({"p1": {"rating": 4.5, "user_ratings_total": 300,
                                        "reviews": [_review(same_day)]}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    assert competitor.check_ratings(rid)["reanalyse"] is False


def test_the_daily_check_buys_no_own_rating_when_one_is_fresh(db_path, monkeypatch):
    rid = _tracked(db_path, [{"name": "Rec Haus", "place_id": "p1", "rating": 4.5, "review_count": 300}],
                   gbp_rating=4.6, gbp_rating_updated_at=_ago(hours=3))
    fake, calls = _fake_places({"p1": {"rating": 4.5, "user_ratings_total": 300}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    competitor.check_ratings(rid)
    assert [c[2] for c in calls] == ["competitor_daily"]
    _set(db_path, rid, gbp_rating_updated_at=_ago(hours=30))
    calls.clear()
    competitor.check_ratings(rid)
    assert [c[2] for c in calls] == ["competitor_daily", "own_rating"]


def test_the_scheduler_reads_the_reanalyse_key():
    import inspect
    import scheduler
    assert 'res.get("reanalyse")' in inspect.getsource(scheduler.run_daily_competitor_ratings)


# ── #24 the own rating ──────────────────────────────────────────────────────

def test_the_review_fetch_carries_the_own_rating(db_path, monkeypatch):
    import fetcher
    monkeypatch.setattr(fetcher, "GOOGLE_API_KEY", "k")
    rid = _rid(db_path, place_id="ChIJown")
    sent = _stub_get(monkeypatch, _Resp({"status": "OK", "result": {
        "reviews": [], "user_ratings_total": 210, "rating": 4.4, "types": ["restaurant", "bar"], "price_level": 2}}))
    fetcher.fetch_google("ChIJown", rid)
    fields = set(sent[0][1]["fields"].split(","))
    assert {"reviews", "rating", "types", "price_level", "user_ratings_total"} <= fields
    # rating / price_level are Atmosphere — the SKU reviews already bill — and
    # types is Basic: the request costs what it did.
    assert ai_utils.places_fee("details", sent[0][1]) == ai_utils.places_fee("details", {"fields": "reviews"})
    r = models.get_restaurant(rid)
    assert (r.gbp_rating, r.gbp_review_count, r.google_price_level) == (4.4, 210, 2)
    assert json.loads(r.google_types) == ["restaurant", "bar"]


def test_a_places_rating_never_overwrites_a_fresher_business_profile_reading(db_path, monkeypatch):
    import event_memory
    rid = _rid(db_path, place_id="ChIJown", gbp_rating=4.7, gbp_review_count=500,
               gbp_rating_updated_at=_ago(hours=2))
    event_memory.record_own_rating(rid, 4.7, 500, source="business_profile")
    competitor._remember_own_listing("ChIJown", ["restaurant"], 2, 4.1, 480)
    r = models.get_restaurant(rid)
    assert (r.gbp_rating, r.gbp_review_count) == (4.7, 500)
    assert r.google_price_level == 2, "types and price level are still kept"
    # A day later the Places reading is the freshest there is.
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE own_rating_history SET recorded_at=datetime('now','-30 hours')")
    conn.commit()
    conn.close()
    competitor._remember_own_listing("ChIJown", ["restaurant"], 2, 4.1, 480)
    assert models.get_restaurant(rid).gbp_rating == 4.1


def test_an_answer_without_types_leaves_the_stored_ones(db_path):
    rid = _rid(db_path, place_id="ChIJown", google_types=json.dumps(["restaurant"]), google_price_level=3)
    competitor._remember_own_listing("ChIJown", None, None, 4.2, 10)
    r = models.get_restaurant(rid)
    assert (json.loads(r.google_types), r.google_price_level, r.gbp_rating) == (["restaurant"], 3, 4.2)


# ── #39 / #40 / #41 the full run ────────────────────────────────────────────

def _quiet_run(monkeypatch):
    import webhooks
    monkeypatch.setattr(competitor, "generate_competitor_insight", lambda *a, **k: "A grounded insight.")
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)


def _stored(pid, name, **kw):
    c = {"place_id": pid, "name": name, "rating": 4.2, "review_count": 90, "match_basis": "nearby",
         "reviews": [], "reviews_at": _ago(days=5)}
    c.update(kw)
    return c


def test_a_reanalysis_reuses_the_stored_set_and_the_mornings_reviews(db_path, monkeypatch):
    _quiet_run(monkeypatch)
    rid = _tracked(db_path, [
        _stored("p1", "Rec Haus", latest_reviews=[{"text": "New", "ts": 1}], latest_reviews_at=_ago(hours=2)),
        _stored("p2", "Whiskey Bend", latest_reviews=[], latest_reviews_at=_ago(hours=3))])
    fake, calls = _fake_places({})
    monkeypatch.setattr(competitor, "_places_request", fake)
    monkeypatch.setattr(competitor, "get_nearby_competitors",
                        lambda *a, **k: pytest.fail("no nearby search while discovery is not due"))
    out = competitor.run_competitor_analysis(rid)
    assert out["ok"] is True
    assert calls == [], "the morning's figures and reviews are reused — not one Places call"
    assert [c["place_id"] for c in out["competitors"]] == ["p1", "p2"]
    assert out["competitors"][0]["reviews"] == [{"text": "New", "ts": 1}]
    assert "latest_reviews" not in out["competitors"][0]


def test_a_stored_competitor_with_stale_reviews_is_one_lookup_that_refreshes_it(db_path, monkeypatch):
    _quiet_run(monkeypatch)
    rid = _tracked(db_path, [_stored("p1", "Rec Haus"), _stored("p2", "Gone Diner")])
    fake, calls = _fake_places({
        "p1": {"name": "Rec Haus", "rating": 4.6, "user_ratings_total": 99, "business_status": "OPERATIONAL",
               "reviews": [_review(1_700_000_000)]},
        "p2": {"name": "Gone Diner", "business_status": "CLOSED_PERMANENTLY"}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.run_competitor_analysis(rid)
    assert sorted(c[1] for c in calls) == ["p1", "p2"]
    assert all(c[3] == competitor.COMPETITOR_FIELDS for c in calls)
    (c,) = out["competitors"]
    assert (c["place_id"], c["rating"], c["review_count"], len(c["reviews"])) == ("p1", 4.6, 99, 1)


def test_discovery_runs_monthly_on_request_or_when_the_owners_list_changes():
    fresh = {"competitors": [{"place_id": "p1"}], "discovered_at": _ago(days=3), "custom_ids": ["c1"]}
    assert competitor._discovery_due(fresh, ["c1"]) is False
    assert competitor._discovery_due(dict(fresh, discovered_at=_ago(days=competitor.REDISCOVER_DAYS)), ["c1"])
    assert competitor._discovery_due({k: v for k, v in fresh.items() if k != "discovered_at"}, ["c1"])
    assert competitor._discovery_due(fresh, ["c1", "c2"])
    assert competitor._discovery_due({"competitors": []}, [])
    assert competitor.REDISCOVER_DAYS < 30, "names, types and prices stay inside Google's 30 days"


def test_an_owners_refresh_searches_again(db_path, monkeypatch):
    _quiet_run(monkeypatch)
    rid = _tracked(db_path, [_stored("p1", "Rec Haus", reviews_at=_ago(hours=1))])
    seen = []
    monkeypatch.setattr(competitor, "get_nearby_competitors",
                        lambda pid, usage=None, **k: seen.append(k) or [dict(_stored("p9", "New Spot"))])
    monkeypatch.setattr(competitor, "get_competitor_reviews", lambda pid, max_reviews=5: [])
    with competitor.rediscovery_requested():
        out = competitor.run_competitor_analysis(rid)
    assert seen and [c["place_id"] for c in out["competitors"]] == ["p9"]
    assert _blob(db_path, rid)["discovered_at"]


def test_discovery_takes_the_own_listing_from_what_is_stored(db_path, monkeypatch):
    rid = _rid(db_path, place_id="ChIJown", latitude=41.9, longitude=-88.3,
               google_types=json.dumps(["restaurant", "bar"]), google_price_level=2)
    sent = _stub_get(monkeypatch, lambda url, params: _Resp({"status": "OK", "results": []}))
    competitor._remember_own_listing("ChIJown", ["restaurant", "bar"], 2)
    own = competitor._stored_own_listing(models.get_restaurant(rid))
    assert own and own["lat"] == 41.9 and own["types"] == ["restaurant", "bar"]
    competitor.get_nearby_competitors("ChIJown", own=own)
    assert sent and all("nearbysearch" in url for url, _p in sent), "no own Details call"
    assert "keyword" in sent[0][1] and sent[0][1]["location"] == "41.9,-88.3"


def test_stored_own_types_past_googles_limit_are_read_again(db_path):
    rid = _rid(db_path, place_id="ChIJown", latitude=41.9, longitude=-88.3,
               google_types=json.dumps(["restaurant"]))
    assert competitor._stored_own_listing(models.get_restaurant(rid)) is None, "never stamped: read again"
    competitor._remember_own_listing("ChIJown", ["restaurant"], None)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE job_cursors SET updated_at=datetime('now', ?) WHERE key=?",
                 (f"-{competitor.REDISCOVER_DAYS} days", competitor._own_listing_key("ChIJown")))
    conn.commit()
    conn.close()
    assert competitor._stored_own_listing(models.get_restaurant(rid)) is None


def test_an_owner_added_competitor_is_one_lookup_with_its_reviews(db_path, monkeypatch):
    _quiet_run(monkeypatch)
    rid = _tracked(db_path, [_stored("p1", "Rec Haus", reviews_at=_ago(hours=1))])
    _set(db_path, rid, custom_competitors="c1")
    blob = _blob(db_path, rid)
    blob["custom_ids"] = ["c1"]
    _set(db_path, rid, competitor_intel=json.dumps(blob))
    fake, calls = _fake_places({"c1": {"name": "Custom Rival", "business_status": "OPERATIONAL", "rating": 4.0,
                                       "user_ratings_total": 40, "reviews": [_review(1_700_000_000)]}})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor.run_competitor_analysis(rid)
    assert [(c[1], c[3]) for c in calls] == [("c1", competitor.CUSTOM_FIELDS)]
    custom = next(c for c in out["competitors"] if c["place_id"] == "c1")
    assert custom["custom"] and len(custom["reviews"]) == 1


def test_the_closure_check_reuses_the_status_the_daily_check_stored(db_path, monkeypatch):
    rid = _rid(db_path)
    models.record_competitor_snapshot(rid, [{"place_id": "p1", "name": "Rec Haus"},
                                            {"place_id": "p2", "name": "Gone Diner"},
                                            {"place_id": "p3", "name": "Quiet One"}], db_path=db_path)
    from time_utils import restaurant_now_by_id
    prev = {"ratings_checked_at": restaurant_now_by_id(rid).strftime("%Y-%m-%d"),
            "competitors": [{"place_id": "p2", "business_status": "CLOSED_PERMANENTLY"},
                            {"place_id": "p3", "business_status": "OPERATIONAL"}]}
    fake, calls = _fake_places({})
    monkeypatch.setattr(competitor, "_places_request", fake)
    out = competitor._closures_among_dropped(rid, [{"place_id": "p1"}], prev_blob=prev)
    assert [c["place_id"] for c in out] == ["p2"] and calls == []
    # A check from three days ago is not trusted: Google is asked.
    prev["ratings_checked_at"] = "2020-01-01"
    competitor._closures_among_dropped(rid, [{"place_id": "p1"}], prev_blob=prev)
    assert sorted(c[1] for c in calls) == ["p2", "p3"]


# ── #40 the cap and #42 the fresh Refresh ───────────────────────────────────

def test_the_add_route_caps_owner_added_competitors_at_ten():
    import inspect
    import mobile_api
    src = inspect.getsource(mobile_api.mobile_add_competitor)
    assert "CUSTOM_COMPETITORS_MAX" in src and "Remove one to add another" in src
    assert competitor.CUSTOM_COMPETITORS_MAX == 10
    import client_api
    assert 'mobile_add_competitor' in inspect.getsource(client_api.intel_add_competitor), \
        "the web route runs the same body"


def _fresh_read(db_path, hours_ago, custom="", custom_ids=()):
    import uuid
    rid = _rid(db_path, place_id=f"ChIJ{uuid.uuid4().hex[:8]}", custom_competitors=custom,
               competitor_updated_at=_ago(hours=hours_ago))
    blob = {"competitors": [{"place_id": "p1", "name": "Rec Haus"}], "insight": "The read.",
            "generated_at": "2026-10-07", "custom_ids": list(custom_ids)}
    _set(db_path, rid, competitor_intel=json.dumps(blob))
    return rid


def test_a_refresh_inside_six_hours_serves_the_stored_read(db_path, monkeypatch):
    import admin_routes
    import ops
    rid = _fresh_read(db_path, hours_ago=2)
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda r: pytest.fail("no new run"))
    job_id = admin_routes.start_competitor_job(rid)
    job = ops.read_async_job(job_id, restaurant_id=rid)
    assert job["status"] == "done"
    res = job["result"]
    assert res["ok"] and res["fresh"] and res["insight"] == "The read." and res["competitors"]
    assert res["note"] == "Already up to date — refreshed 2h ago" and res["updated_at"]


def test_a_stale_read_a_changed_list_or_a_forced_refresh_runs_again(db_path, monkeypatch):
    import admin_routes
    assert competitor.fresh_stored_read(_fresh_read(db_path, hours_ago=7)) is None
    assert competitor.fresh_stored_read(_fresh_read(db_path, hours_ago=1, custom="c1")) is None
    assert competitor.fresh_stored_read(_fresh_read(db_path, hours_ago=1, custom="c1", custom_ids=["c1"]))
    rid = _fresh_read(db_path, hours_ago=1)
    started = []
    monkeypatch.setattr(admin_routes, "_run_competitor_job", lambda job_id, r: started.append(r))
    import threading
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    admin_routes.start_competitor_job(rid, force=True)
    assert started == [rid]


class _ImmediateThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        self._t(*self._a, **self._k)


def test_only_an_admin_may_force_and_the_web_shows_the_note():
    import inspect
    import admin_routes
    src = inspect.getsource(admin_routes.refresh_competitor_intel)
    assert 'current_user.get("is_admin")' in src and "record_admin_action" in src
    html = open("templates/dashboard.html", encoding="utf-8").read()
    assert html.count("toast(s.note||") == 2
