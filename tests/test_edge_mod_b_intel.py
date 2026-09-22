"""Edge cases for Intel data: competitor analysis, AI visibility, weather.

Every test here comes from the MOD edge-case audit (appendix A2, findings
MOD-INT-1..8 and the Intel half of MOD-REV-2). The theme is the Intel audit's
own invariant: a missing measurement is never a zero, and our own lookup
failing is never reported to an owner as a fact about their market.

Tests marked xfail(strict=True) assert the CORRECT behaviour for a defect the
audit confirmed; they flip to a failure the day the defect is fixed, so the
marker comes off with the fix.
"""
import pytest

import client_api
import competitor
import models
import notify
import scheduler
import weather
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


# ── shared plumbing ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def db(db_path, monkeypatch):
    """Point every module that bound get_conn at the throwaway database."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, client_api, notify, scheduler):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: _all(db_path))
    return db_path


_REAL_GET_ALL = models.get_all_restaurants


def _all(db_path):
    return _REAL_GET_ALL(db_path=db_path)


def _full_tier(db_path, name="Full Co", **kw):
    return create_restaurant(Restaurant(
        name=name, owner_email=f"{name.split()[0].lower()}@x.test", google_place_id=kw.pop("place_id", "ChIJ" + name.replace(" ", "")),
        module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1,
        **kw), db_path=db_path)


# ── I1. Competitor analysis ────────────────────────────────────────────────

_OWN_DETAILS = {"status": "OK", "result": {
    "name": "Gia Mia", "geometry": {"location": {"lat": 41.91, "lng": -88.31}},
    "types": ["restaurant", "food"], "price_level": 2}}


def _place(name, pid, rating=4.3, reviews=150, status="OPERATIONAL", **extra):
    p = {"place_id": pid, "name": name, "types": ["restaurant", "food"],
         "user_ratings_total": reviews, "price_level": 2,
         "business_status": status, "vicinity": "1 Main St"}
    if rating is not None:
        p["rating"] = rating
    p.update(extra)
    return p


def _places_get(nearby, custom=None, custom_raises=False, status=None):
    """A fake requests.get that answers Places the way the real call
    sequence expects: own Details, then nearby searches, then a Details per
    custom competitor."""
    calls = []

    def fake_get(url, params=None, timeout=None, **kw):
        params = params or {}
        calls.append((url, dict(params)))
        if status:
            return _Resp({"status": status, "results": [], "error_message": "quota"})
        if "details" in url:
            pid = params.get("place_id")
            if custom and pid in custom:
                if custom_raises:
                    raise ConnectionError("Places timed out")
                return _Resp({"status": "OK", "result": custom[pid]})
            return _Resp(_OWN_DETAILS)
        if "nearbysearch" in url:
            return _Resp({"status": "OK", "results": nearby})
        raise AssertionError(url)
    fake_get.calls = calls
    return fake_get


def _analysis_ready(monkeypatch, db_path, fake_get, custom_ids=""):
    rid = _full_tier(db_path, place_id="ChIJself-owner")
    if custom_ids:
        update_restaurant(rid, {"custom_competitors": custom_ids}, db_path=db_path)
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    monkeypatch.setattr(competitor.requests, "get", fake_get)
    monkeypatch.setattr(competitor, "get_competitor_reviews", lambda pid, max_reviews=5: [])
    monkeypatch.setattr(competitor, "generate_competitor_insight", lambda *a, **k: "A grounded insight.")
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    return rid


@pytest.mark.parametrize("status", ["OVER_QUERY_LIMIT", "REQUEST_DENIED"])
def test_a_places_refusal_is_distinguished_from_an_empty_market(db, monkeypatch, status):
    """A2 I1 #8 / MOD-INT-2 — the error must name Google not answering, not
    the market being empty."""
    rid = _analysis_ready(monkeypatch, db, _places_get([], status=status))
    out = competitor.run_competitor_analysis(rid)
    assert "no nearby competitors" not in (out.get("error") or "").lower()


def test_a_real_zero_results_answer_is_still_no_competitors(db, monkeypatch):
    """A2 I1 #8 — the honest 'nobody nearby' case must keep working."""
    rid = _analysis_ready(monkeypatch, db, _places_get([]))
    out = competitor.run_competitor_analysis(rid)
    assert out == {"ok": False, "error": "No nearby competitors found"}


def test_the_weekly_competitor_job_counts_a_failed_analysis_as_failed(db, monkeypatch):
    """A2 I1 #9 / MOD-INT-2."""
    _full_tier(db)
    monkeypatch.setattr(competitor, "run_competitor_analysis",
                        lambda rid: {"ok": False, "error": "Competitor analysis could not be generated"})
    out = scheduler.run_weekly_competitor_analysis()
    assert out == {"analysed": 0, "failed": 1}


def test_a_closed_nearby_place_is_left_out_of_the_competitor_set(monkeypatch):
    """A2 I1 #10 — a CLOSED_PERMANENTLY listing is not a rival."""
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    nearby = [_place("Open One", "p1"), _place("Gone Two", "p2", status="CLOSED_PERMANENTLY"),
              _place("Paused Three", "p3", status="CLOSED_TEMPORARILY"), _place("Open Four", "p4")]
    monkeypatch.setattr(competitor.requests, "get", _places_get(nearby))
    names = [c["name"] for c in competitor.get_nearby_competitors("ChIJself")]
    assert "Open One" in names and "Open Four" in names
    assert "Gone Two" not in names and "Paused Three" not in names


def test_a_closed_competitor_the_owner_added_is_reported_not_silently_kept(db, monkeypatch):
    """A2 I1 #10 — an owner-added rival that has closed is named in
    closed_custom and not analysed as though it were trading."""
    nearby = [_place("Open One", "p1"), _place("Open Two", "p2"), _place("Open Three", "p3")]
    custom = {"cust1": {"name": "Rosie's", "business_status": "CLOSED_PERMANENTLY", "rating": 4.1,
                        "user_ratings_total": 90}}
    rid = _analysis_ready(monkeypatch, db, _places_get(nearby, custom=custom), custom_ids="cust1")
    out = competitor.run_competitor_analysis(rid)
    assert out["ok"] is True
    assert [c["name"] for c in out["closed_custom"]] == ["Rosie's"]
    assert "Rosie's" not in [c["name"] for c in out["competitors"]]


def test_a_custom_competitor_whose_lookup_fails_is_not_reported_gone(db, monkeypatch):
    """A2 I1 #11 / MOD-INT-8 — our own lookup failing is not a rival closing."""
    nearby = [_place("Open One", "p1"), _place("Open Two", "p2"), _place("Open Three", "p3")]
    custom = {"cust1": {"name": "Rosie's", "business_status": "OPERATIONAL", "rating": 4.1,
                        "user_ratings_total": 90}}
    rid = _analysis_ready(monkeypatch, db, _places_get(nearby, custom=custom), custom_ids="cust1")
    assert competitor.run_competitor_analysis(rid)["ok"] is True
    conn = models.get_conn(db)
    conn.execute("UPDATE competitor_snapshots SET captured_at=datetime('now','-7 days')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(competitor.requests, "get", _places_get(nearby, custom=custom, custom_raises=True))
    assert competitor.run_competitor_analysis(rid)["ok"] is True
    changes = models.competitor_roster_changes(rid, db_path=db)
    assert "cust1" not in [g["place_id"] for g in changes["gone"]]


def test_a_place_with_no_rating_is_kept_as_unrated_not_zero(monkeypatch):
    """A2 I1 #12 / MOD-INT-3 — Places omits `rating` for a place with no
    reviews; that is a missing measurement, not a zero-star restaurant."""
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "k")
    nearby = [_place("Brand New Taqueria", "p1", rating=None, reviews=0),
              _place("Open Two", "p2"), _place("Open Three", "p3")]
    monkeypatch.setattr(competitor.requests, "get", _places_get(nearby))
    new = next(c for c in competitor.get_nearby_competitors("ChIJself") if c["place_id"] == "p1")
    assert new["rating"] is None


def test_a_first_snapshot_with_no_rating_does_not_produce_a_movement(db):
    """A2 I1 #12 / MOD-INT-3 — 'rating 0 then 4.6' is a place getting its
    first reviews, not a +4.6 swing."""
    models.record_competitor_snapshot(1, [
        {"place_id": "p1", "name": "Brand New Taqueria", "rating": 0, "review_count": 0}], db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE competitor_snapshots SET captured_at=datetime('now','-20 days')")
    conn.commit()
    conn.close()
    models.record_competitor_snapshot(1, [
        {"place_id": "p1", "name": "Brand New Taqueria", "rating": 4.6, "review_count": 40}], db_path=db)
    assert models.competitor_movement(1, days=60, db_path=db) == []


def test_a_run_that_widens_the_radius_meters_every_nearby_search(db, monkeypatch):
    """MOD-INT-6 — keyword search, broad search and widened search are three
    billed Places requests; the ledger must see three."""
    metered = []
    monkeypatch.setattr(competitor, "_meter_places", lambda rid, action, kind="details", **k: metered.append(kind))
    rid = _analysis_ready(monkeypatch, db, _places_get([_place("Only One", "p1")]))
    competitor.run_competitor_analysis(rid)
    assert metered.count("nearby") == 3


def _record_calls(monkeypatch, target, attr, result):
    seen = []

    def fake(rid, *a, **k):
        seen.append(rid)
        return result() if callable(result) else result
    monkeypatch.setattr(target, attr, fake)
    return seen


class _FakeClock:
    """time.time() that only moves when the stubbed work says so."""
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


def test_the_weekly_competitor_job_is_bounded_and_resumes_where_it_stopped(db, monkeypatch):
    """A2 I1 #17 / MOD-INT-1 — each restaurant takes ten hours of fake wall
    clock; a bounded job stops long before ten of them, records a cursor, and
    the next run starts with the ones it did not reach."""
    ids = [_full_tier(db, name=f"R{i} Co") for i in range(10)]
    clock = _FakeClock()
    monkeypatch.setattr(scheduler.time, "time", clock)
    first = []

    def slow(rid):
        first.append(rid)
        clock.now += 10 * 3600
        return {"ok": True}
    monkeypatch.setattr(competitor, "run_competitor_analysis", slow)
    scheduler.run_weekly_competitor_analysis()
    assert 0 < len(first) < len(ids)
    conn = models.get_conn(db)
    keys = [r["key"] for r in conn.execute("SELECT key FROM job_cursors")]
    conn.close()
    assert any("competitor" in k for k in keys)
    second = []
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda rid: (second.append(rid), {"ok": True})[1])
    scheduler.run_weekly_competitor_analysis()
    assert second[0] not in first


def test_a_churned_restaurant_is_skipped_by_the_weekly_competitor_job(db, monkeypatch):
    """A2 I1 #18 / MOD-REV-2 — no Places or Claude spend on a cancelled
    customer."""
    live = _full_tier(db, name="Live Co", billing_status="active")
    gone = _full_tier(db, name="Gone Co", billing_status="churned")
    seen = _record_calls(monkeypatch, competitor, "run_competitor_analysis", {"ok": True})
    scheduler.run_weekly_competitor_analysis()
    assert live in seen
    assert gone not in seen


# ── I2. AI visibility ──────────────────────────────────────────────────────

def _aivis(monkeypatch, db_path, *, answers, city="Geneva", neighborhood="Geneva",
           place_id="ChIJx", name="Gia Mia", rid=1, create=True):
    """The real visibility path with Perplexity and Places stubbed (same
    shape as tests/test_intel_integrity.py::_payload)."""
    if create:
        conn = models.get_conn(db_path)
        conn.execute("INSERT INTO restaurants (id,name,owner_email,google_place_id,neighborhood,"
                     "vibe,known_for) VALUES (?,?,?,?,?,'lively pizza bar','wood-fired pizza')",
                     (rid, name, "o@x.test", place_id, neighborhood))
        conn.commit()
        conn.close()
    monkeypatch.setattr(client_api, "get_restaurant", lambda r: models.get_restaurant(r, db_path))
    monkeypatch.setattr(client_api, "_city_from_place_id", lambda pid: city if pid else "")
    monkeypatch.setattr(client_api, "get_review_stats", lambda r: {"total": 10, "response_rate": 50})
    client_api._aivis_cache.clear()
    seq = list(answers)
    posted = []

    class _P:
        status_code = 200

        def json(self):
            a = seq.pop(0) if seq else None
            if a is None:
                return {}
            return {"choices": [{"message": {"content": a}}], "citations": ["https://x.test"]}

    import requests as _rq

    def fake_post(*a, **kw):
        posted.append(1)
        return _P()
    monkeypatch.setattr(_rq, "post", fake_post)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    monkeypatch.setattr(client_api, "ai_budget_exceeded", lambda r: None, raising=False)
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    payload, status = client_api._do_ai_visibility_inner(rid, force=True)
    payload["_posted"] = len(posted)
    return payload


def _runs(db_path):
    conn = models.get_conn(db_path)
    rows = [dict(r) for r in conn.execute("SELECT ai_score, answered, appeared FROM ai_visibility_runs")]
    conn.close()
    return rows


def test_no_city_is_flagged_as_location_unknown(db, monkeypatch):
    """A2 I2 #2 (reporting half, already true) — kept as the baseline for the
    recording half below."""
    p = _aivis(monkeypatch, db, answers=["Try Gia Mia in St. Charles."] * 12,
               city="", neighborhood="", place_id="")
    assert p["location_known"] is False


def test_no_city_records_no_score_rather_than_a_zero(db, monkeypatch):
    """A2 I2 #2 / MOD-INT-4 — with no city nothing can match, so the 0 is
    ours, not the restaurant's. No score, no history row."""
    p = _aivis(monkeypatch, db, answers=["Try Gia Mia in St. Charles."] * 12,
               city="", neighborhood="", place_id="")
    assert p["ai_score"] is None
    assert _runs(db) == []


def test_a_city_lookup_that_raised_is_retried_on_the_next_call(monkeypatch):
    """A2 I2 #4 / MOD-INT-4 — one Places blip must not blank the city for a
    day."""
    import requests as _rq
    monkeypatch.setattr(client_api.config, "google_places_key", lambda: "k")
    client_api._city_cache.clear()

    def boom(*a, **k):
        raise ConnectionError("Places blip")
    monkeypatch.setattr(_rq, "get", boom)
    assert client_api._city_from_place_id("ChIJblip") == ""
    monkeypatch.setattr(_rq, "get", lambda *a, **k: _Resp({"status": "OK", "result": {
        "address_components": [{"long_name": "Geneva", "types": ["locality"]}]}}))
    try:
        assert client_api._city_from_place_id("ChIJblip") == "Geneva"
    finally:
        client_api._city_cache.clear()


def test_a_successful_city_lookup_is_cached(monkeypatch):
    """A2 I2 #4 — the flip side: a real answer is still reused."""
    import requests as _rq
    monkeypatch.setattr(client_api.config, "google_places_key", lambda: "k")
    client_api._city_cache.clear()
    calls = []

    def ok(*a, **k):
        calls.append(1)
        return _Resp({"status": "OK", "result": {"address_components": [
            {"long_name": "Geneva", "types": ["locality"]}]}})
    monkeypatch.setattr(_rq, "get", ok)
    try:
        assert client_api._city_from_place_id("ChIJok") == "Geneva"
        assert client_api._city_from_place_id("ChIJok") == "Geneva"
        assert len(calls) == 1
    finally:
        client_api._city_cache.clear()


def test_runs_measured_against_different_cities_are_not_compared(db, monkeypatch):
    """A2 I2 #11 / MOD-INT-4 — a profile-city run and a Google-city run ask
    different questions and match against different strings; a drop between
    them is a change of ruler, not of visibility."""
    hit = "Gia Mia in Downtown St. Charles is lovely."
    _aivis(monkeypatch, db, answers=[hit] * 12, city="", neighborhood="Downtown St. Charles",
           place_id="ChIJx")
    assert _runs(db) and _runs(db)[0]["appeared"] >= 5
    _aivis(monkeypatch, db, answers=["Try Somewhere Else in Naperville."] * 12, city="St. Charles",
           neighborhood="Downtown St. Charles", place_id="ChIJx", create=False)
    assert notify._ai_visibility_drop(models.last_two_ai_visibility_runs(1, db_path=db)) is None


def test_the_weekly_visibility_job_is_bounded_and_resumes_where_it_stopped(db, monkeypatch):
    """A2 I2 #13 / MOD-INT-1."""
    ids = [_full_tier(db, name=f"V{i} Co") for i in range(10)]
    clock = _FakeClock()
    monkeypatch.setattr(scheduler.time, "time", clock)
    first = []

    def slow(rid, force=False):
        first.append(rid)
        clock.now += 10 * 3600
        return {"ok": True}, 200
    monkeypatch.setattr(client_api, "_do_ai_visibility_inner", slow)
    scheduler.run_weekly_ai_visibility()
    assert 0 < len(first) < len(ids)
    conn = models.get_conn(db)
    keys = [r["key"] for r in conn.execute("SELECT key FROM job_cursors")]
    conn.close()
    assert any("visib" in k for k in keys)


def test_a_churned_restaurant_is_skipped_by_the_weekly_visibility_job(db, monkeypatch):
    """A2 I2 #13 / MOD-REV-2 — no Perplexity spend on a cancelled customer."""
    live = _full_tier(db, name="Live Co", billing_status="active")
    gone = _full_tier(db, name="Gone Co", billing_status="churned")
    seen = []
    monkeypatch.setattr(client_api, "_do_ai_visibility_inner",
                        lambda rid, force=False: (seen.append(rid), ({"ok": True}, 200))[1])
    scheduler.run_weekly_ai_visibility()
    assert live in seen and gone not in seen


def test_the_visibility_screen_is_served_from_the_stored_run(db, monkeypatch):
    """A2 I2 #14 / MOD-INT-5 — the process cache is gone after every deploy;
    the recorded run is the answer, not eight live queries on a request
    thread."""
    hit = "Gia Mia in Geneva is excellent."
    first = _aivis(monkeypatch, db, answers=[hit] * 12)
    assert first["ok"] and _runs(db)
    client_api._aivis_cache.clear()          # a redeploy
    import requests as _rq
    live = []
    monkeypatch.setattr(_rq, "post", lambda *a, **k: live.append(1))
    payload, status = client_api._do_ai_visibility_inner(1)
    assert live == [], "a live Perplexity query ran on the request path"
    assert status == 200 and payload.get("ok") is True
    assert payload.get("ai_score") == first["ai_score"]


def test_an_exhausted_budget_pauses_the_run_without_asking_perplexity(db, monkeypatch):
    """A2 I2 #15 — the budget ceiling is real, not stubbed to None: no query
    goes out, no run is recorded, and the owner is told why."""
    conn = models.get_conn(db)
    conn.execute("INSERT INTO restaurants (id,name,owner_email,google_place_id,neighborhood) "
                 "VALUES (1,'Gia Mia','o@x.test','ChIJx','Geneva')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(client_api, "get_restaurant", lambda r: models.get_restaurant(r, db))
    client_api._aivis_cache.clear()
    import ai_utils
    import requests as _rq
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: "the monthly AI budget")
    posted = []
    monkeypatch.setattr(_rq, "post", lambda *a, **k: posted.append(1))
    payload, status = client_api._do_ai_visibility_inner(1, force=True)
    assert payload["ok"] is False and "paused" in payload["error"]
    assert posted == []
    assert _runs(db) == []


# ── I3. Weather ────────────────────────────────────────────────────────────

def _wx_restaurant(db_path, **kw):
    rid = create_restaurant(Restaurant(name="Weather Co", owner_email="w@x.test",
                                       google_place_id=kw.pop("place_id", "ChIJwx")), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return get_restaurant(rid, db_path=db_path)


def test_a_network_blip_during_geocode_does_not_black_out_weather_for_a_week(db, monkeypatch):
    """A2 I3 #8 / MOD-INT-7."""
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "k")
    r = _wx_restaurant(db)

    def blip(url, *a, **k):
        raise ConnectionError("read timed out")
    monkeypatch.setattr(weather.requests, "get", blip)
    assert weather._geocode(r, db_path=db) == (None, None)
    assert get_restaurant(r.id, db_path=db).geocode_failed_at in (None, "")


def test_a_place_with_no_geometry_is_still_remembered_as_failed(db, monkeypatch):
    """A2 I3 #8 — the permanent case the 7-day window was built for keeps
    working."""
    monkeypatch.setattr(weather, "_GOOGLE_KEY", "k")
    r = _wx_restaurant(db)
    monkeypatch.setattr(weather.requests, "get", lambda url, *a, **k: _Resp({"result": {}}))
    assert weather._geocode(r, db_path=db) == (None, None)
    assert get_restaurant(r.id, db_path=db).geocode_failed_at


def test_an_nws_outage_is_not_re_hit_on_every_call(db, monkeypatch):
    """A2 I3 #9 / MOD-INT-7 — within a short window after a failure, no new
    HTTP call."""
    r = _wx_restaurant(db, latitude=41.9, longitude=-88.3)
    calls = []

    def down(url, *a, **k):
        calls.append(url)
        raise ConnectionError("NWS down")
    monkeypatch.setattr(weather.requests, "get", down)
    assert weather.get_forecast_for_week(r, ["2026-09-22"], db_path=db) == []
    n = len(calls)
    r = get_restaurant(r.id, db_path=db)
    assert weather.get_forecast_for_week(r, ["2026-09-22"], db_path=db) == []
    assert len(calls) == n


def test_a_stale_forecast_is_used_when_the_refresh_fails(db, monkeypatch):
    """A2 I3 #9 / MOD-INT-7 — yesterday's forecast for tomorrow beats none."""
    import json
    from datetime import datetime, timedelta
    periods = [{"name": "Tuesday", "startTime": "2026-09-22T06:00:00-05:00", "isDaytime": True,
                "temperature": 71, "shortForecast": "Sunny", "probabilityOfPrecipitation": {"value": 5}}]
    r = _wx_restaurant(db, latitude=41.9, longitude=-88.3, weather_cache_json=json.dumps(periods),
                       weather_cached_at=(datetime.now() - timedelta(hours=12)).isoformat())
    monkeypatch.setattr(weather.requests, "get", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    rows = weather.get_forecast_for_week(r, ["2026-09-22"], db_path=db)
    assert [x["high_f"] for x in rows] == [71]


def test_a_non_us_restaurant_gets_no_forecast_and_no_error(db, monkeypatch):
    """A2 I3 #10 — NWS answers 404 outside the US; weather degrades to []."""
    r = _wx_restaurant(db, latitude=51.5, longitude=-0.12)
    monkeypatch.setattr(weather.requests, "get", lambda url, *a, **k: _Resp({"title": "Not Found"}, 404))
    assert weather.get_forecast_for_week(r, ["2026-09-22"], db_path=db) == []


def test_a_non_us_restaurant_does_not_re_ask_nws_every_time(db, monkeypatch):
    """A2 I3 #10 / MOD-INT-7 — a permanent 404 is remembered."""
    r = _wx_restaurant(db, latitude=51.5, longitude=-0.12)
    calls = []

    def nf(url, *a, **k):
        calls.append(url)
        return _Resp({"title": "Not Found"}, 404)
    monkeypatch.setattr(weather.requests, "get", nf)
    weather.get_forecast_for_week(r, ["2026-09-22"], db_path=db)
    n = len(calls)
    weather.get_forecast_for_week(get_restaurant(r.id, db_path=db), ["2026-09-22"], db_path=db)
    assert len(calls) == n
