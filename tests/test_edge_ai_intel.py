"""Edge cases in the Intel AI: the weekly competitor and AI-visibility sweeps,
the competitor menu fetch, and Google Places failures on the nearby search.

What these protect:
  - The two Monday sweeps stop at a time bound and pick up where they
    stopped on the next pass, the way run_daily_fetch does — rather than one
    serial loop over every full-tier restaurant on the scheduler thread (AI-9).
  - The competitor menu fetch never follows a third party's website to an
    internal address (the cloud metadata endpoint), and its model spend is
    attributed to the restaurant it was run for (AI-30).
  - A Places key, billing or quota problem on the nearby search is reported
    as that, not as "No nearby competitors found", and is not metered as a
    successful call (AI-6).

Nothing here reaches Google, Anthropic or Perplexity: requests.get, the model
call and the visibility run are stubbed on the module that uses them. The
sweeps run against a fake clock, so "a day per restaurant" costs nothing.
"""
import json
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import requests
from flask import Flask

import admin_routes
import auth
import client_api
import competitor
import models
import scheduler
from models import Restaurant, create_restaurant, get_conn


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """admin_routes and auth bind get_conn at import. competitor, scheduler
    and ai_utils import it inside function bodies, which models.get_conn
    covers."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(competitor, "PLACES_API_KEY", "test-places-key")


def _message(text, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=50, output_tokens=50,
                              cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )


def _full_tier(db_path, n=1, **kw):
    ids = []
    for i in range(n):
        ids.append(create_restaurant(Restaurant(
            name="Intel Co %d" % i, owner_email="i%d@x.test" % i, timezone="America/Chicago",
            google_place_id="ChIJplace%d" % i,
            module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1, **kw,
        ), db_path=db_path))
    return ids


def _cursor_rows(db_path):
    conn = get_conn(db_path)
    rows = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM job_cursors")}
    conn.close()
    return rows


class _FakeClock:
    """time.time / monotonic / perf_counter that only move when a stub says
    so — a sweep can be made to take simulated days without sleeping."""

    def __init__(self):
        self.now = 1_800_000_000.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = _FakeClock()
    for name in ("time", "monotonic", "perf_counter"):
        monkeypatch.setattr(time, name, fake)
    return fake


DAY = 24 * 3600


# ── AI-9: the weekly sweeps are bounded and resumable ───────────────────────

def test_one_restaurants_failure_does_not_stop_the_competitor_sweep(db_path, monkeypatch):
    ids = _full_tier(db_path, 3)
    seen = []

    def analyse(rid):
        seen.append(rid)
        if rid == ids[0]:
            raise RuntimeError("database is locked")
        return {"ok": True}

    monkeypatch.setattr(competitor, "run_competitor_analysis", analyse)
    result = scheduler.run_weekly_competitor_analysis()
    assert sorted(seen) == sorted(ids)
    assert result == {"analysed": 2, "failed": 1}


def test_the_weekly_sweeps_skip_restaurants_that_are_not_full_tier(db_path, monkeypatch):
    full = _full_tier(db_path, 1)
    create_restaurant(Restaurant(name="Reviews Only", owner_email="r@x.test",
                                 google_place_id="ChIJreviews", module_reviews=1, module_labor=0,
                                 module_inventory=0, module_marketing=0), db_path=db_path)
    seen_c, seen_v = [], []
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda rid: seen_c.append(rid) or {"ok": True})
    monkeypatch.setattr(client_api, "_do_ai_visibility_inner",
                        lambda rid, force=False: (seen_v.append(rid) or ({"ok": True}, 200)))
    scheduler.run_weekly_competitor_analysis()
    scheduler.run_weekly_ai_visibility()
    assert seen_c == full and seen_v == full


def test_the_competitor_sweep_stops_at_a_time_bound_and_resumes_from_a_cursor(db_path, monkeypatch, clock):
    ids = _full_tier(db_path, 12)
    seen = []

    def slow(rid):
        seen.append(rid)
        clock.advance(DAY)          # each restaurant takes a simulated day
        return {"ok": True}

    monkeypatch.setattr(competitor, "run_competitor_analysis", slow)
    cursors_before = _cursor_rows(db_path)

    scheduler.run_weekly_competitor_analysis()
    first = list(seen)
    assert 0 < len(first) < len(ids), \
        "the sweep ran %d simulated days without stopping" % len(first)
    assert _cursor_rows(db_path) != cursors_before, "no job_cursors cursor was written"

    del seen[:]
    scheduler.run_weekly_competitor_analysis()
    assert set(seen) - set(first), "the next pass started from the top again"


def test_the_visibility_sweep_stops_at_a_time_bound_and_resumes_from_a_cursor(db_path, monkeypatch, clock):
    ids = _full_tier(db_path, 12)
    seen = []

    def slow(rid, force=False):
        seen.append(rid)
        clock.advance(DAY)          # 8 paced Perplexity queries, at scale
        return {"ok": True}, 200

    monkeypatch.setattr(client_api, "_do_ai_visibility_inner", slow)
    cursors_before = _cursor_rows(db_path)

    scheduler.run_weekly_ai_visibility()
    first = list(seen)
    assert 0 < len(first) < len(ids), \
        "the sweep ran %d simulated days without stopping" % len(first)
    assert _cursor_rows(db_path) != cursors_before, "no job_cursors cursor was written"

    del seen[:]
    scheduler.run_weekly_ai_visibility()
    assert set(seen) - set(first), "the next pass started from the top again"


# ── AI-30: the competitor menu fetch ────────────────────────────────────────

PUBLIC_URL = "https://93.184.216.34/menu"
METADATA_URL = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"

MENU_PAGE = "<html><body><h1>Menu</h1><p>" + " ".join(
    ["Margherita pizza with basil", "Cacio e pepe with pecorino", "Braised short rib",
     "Burrata with heirloom tomato", "Tiramisu made daily", "Negroni and Aperol spritz"] * 12
) + "</p></body></html>"

METADATA_PAGE = "<html><body><pre>" + " ".join(
    ["AccessKeyId ASIAEXAMPLE SecretAccessKey wJalrXUtnFEMI Token IQoJb3JpZ2luX2Vj Expiration"] * 20
) + "</pre></body></html>"


class _Resp:
    def __init__(self, url, status=200, text="", headers=None, history=None, payload=None):
        self.url = url
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.headers = headers or {}
        self.history = history or []
        self.is_redirect = status in (301, 302, 303, 307, 308)
        self.ok = status < 400
        self._payload = payload

    def json(self):
        return self._payload if self._payload is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _web(monkeypatch, contacted, redirect_to=METADATA_URL, places=None):
    """A faithful little internet. PUBLIC_URL answers 302 → `redirect_to`.
    With allow_redirects=True (requests' default) the hop is followed and
    the internal host is contacted, exactly as requests would; with it off,
    the 302 comes back for the caller to vet. Every host reached is logged
    in `contacted`."""
    def fetch(url, allow_redirects=True, **kw):
        contacted.append(url)
        host = urlparse(url).hostname
        if places is not None and host == "maps.googleapis.com":
            return _Resp(url, payload=places(url, kw.get("params") or {}))
        if url == PUBLIC_URL and redirect_to:
            hop = _Resp(url, status=302, headers={"Location": redirect_to})
            if not allow_redirects:
                return hop
            final = fetch(redirect_to, allow_redirects=True, **kw)
            final.history = [hop]
            return final
        if url == PUBLIC_URL:
            return _Resp(url, text=MENU_PAGE)
        if host == "169.254.169.254" or host.startswith("10."):
            return _Resp(url, text=METADATA_PAGE)
        return _Resp(url, status=404)

    def get(url, params=None, **kw):
        return fetch(url, params=params, **kw)

    def request(method, url, **kw):
        return fetch(url, **kw)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "request", request)
    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: fetch(url, **kw))
    monkeypatch.setattr(requests.Session, "request", lambda self, method, url, **kw: fetch(url, **kw))


def _model(monkeypatch, calls, text="Signature dishes: Margherita, cacio e pepe, braised short rib, tiramisu."):
    monkeypatch.setattr(competitor, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(competitor, "create_with_retry",
                        lambda client, **k: calls.append(k) or _message(text))


def test_a_public_menu_page_is_summarised_and_billed_to_the_restaurant_asked_for(db_path, monkeypatch):
    contacted, calls = [], []
    _web(monkeypatch, contacted, redirect_to=None)
    _model(monkeypatch, calls)

    out = competitor.fetch_menu_from_url(PUBLIC_URL, restaurant_id=42)

    assert "Margherita" in out
    assert calls and calls[0]["restaurant_id"] == 42
    assert "UNTRUSTED_GUEST_TEXT" in calls[0]["messages"][0]["content"]


def test_a_menu_url_that_redirects_to_the_metadata_endpoint_is_refused(db_path, monkeypatch):
    contacted, calls = [], []
    _web(monkeypatch, contacted)
    _model(monkeypatch, calls)

    out = competitor.fetch_menu_from_url(PUBLIC_URL, restaurant_id=42)

    reached = [u for u in contacted if urlparse(u).hostname == "169.254.169.254"]
    assert reached == [], "the fetch followed the redirect to %s" % reached
    assert not any("AccessKeyId" in c["messages"][0]["content"] for c in calls)
    assert out == ""


def test_a_menu_url_on_a_private_address_is_never_fetched(db_path, monkeypatch):
    contacted, calls = [], []
    _web(monkeypatch, contacted)
    _model(monkeypatch, calls)

    out = competitor.fetch_menu_from_url("http://10.0.0.5/menu", restaurant_id=42)

    assert contacted == [], "a private address was fetched: %s" % contacted
    assert out == ""


@pytest.fixture
def admin_app(monkeypatch):
    import auth_routes
    admin = {"id": 1, "restaurant_id": None, "is_admin": 1, "username": "will", "email": "w@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: admin)
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(admin_routes.admin_bp)
    flask_app.register_blueprint(auth_routes.auth_bp)
    return flask_app


def test_refreshing_menu_notes_bills_the_extraction_to_that_restaurant(db_path, monkeypatch, admin_app):
    (rid,) = _full_tier(db_path, 1)
    contacted, calls = [], []

    def places(url, params):
        return {"status": "OK", "result": {
            "website": PUBLIC_URL, "types": ["italian_restaurant", "restaurant"],
            "editorial_summary": {"overview": "Neighbourhood trattoria with house-made pasta."}}}

    _web(monkeypatch, contacted, redirect_to=None, places=places)
    _model(monkeypatch, calls)

    resp = admin_app.test_client().post("/admin/refresh-menu-notes/%d" % rid, json={})

    assert resp.get_json()["ok"] is True
    assert calls, "the menu page was never summarised"
    assert calls[0]["restaurant_id"] == rid, \
        "menu extraction billed to %r, not restaurant %d" % (calls[0]["restaurant_id"], rid)


# ── AI-6: a Places error on the nearby search is not "no competitors" ───────

def _places(nearby_status="OK", details_status="OK"):
    def answer(url, params):
        if "details" in url:
            if details_status != "OK":
                return {"status": details_status, "error_message": "The provided API key is invalid."}
            return {"status": "OK", "result": {
                "name": "Intel Co 0", "types": ["italian_restaurant", "restaurant"],
                "geometry": {"location": {"lat": 41.9, "lng": -87.6}}, "price_level": 2}}
        if nearby_status in ("OK", "ZERO_RESULTS"):
            return {"status": nearby_status, "results": []}
        return {"status": nearby_status, "error_message": "You have exceeded your daily request quota for this API."
                if nearby_status == "OVER_QUERY_LIMIT" else "The provided API key is invalid.", "results": []}
    return answer


def _ledger(db_path):
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT model, status, cost_usd FROM ai_usage")]
    except Exception:
        return []
    finally:
        conn.close()


def test_a_genuinely_empty_neighbourhood_reports_no_competitors(db_path, monkeypatch):
    """ZERO_RESULTS is Google saying "nothing here" — the one case where
    "No nearby competitors found" is the truth."""
    (rid,) = _full_tier(db_path, 1)
    _web(monkeypatch, [], places=_places(nearby_status="ZERO_RESULTS"))

    result = competitor.run_competitor_analysis(rid)

    assert result == {"ok": False, "error": "No nearby competitors found"}


@pytest.mark.parametrize("stage,status", [
    ("nearby", "REQUEST_DENIED"),
    ("nearby", "OVER_QUERY_LIMIT"),
    ("details", "REQUEST_DENIED"),
])
def test_a_places_error_is_not_reported_as_no_nearby_competitors(db_path, monkeypatch, stage, status):
    (rid,) = _full_tier(db_path, 1)
    kw = {"nearby_status": status} if stage == "nearby" else {"details_status": status}
    _web(monkeypatch, [], places=_places(**kw))

    result = competitor.run_competitor_analysis(rid)

    assert result["ok"] is False
    assert "No nearby competitors" not in result.get("error", ""), \
        "a %s from Places was reported to the owner as an empty neighbourhood" % status


def test_a_refused_nearby_search_is_not_metered_as_a_successful_call(db_path, monkeypatch):
    (rid,) = _full_tier(db_path, 1)
    _web(monkeypatch, [], places=_places(nearby_status="OVER_QUERY_LIMIT"))

    competitor.run_competitor_analysis(rid)

    ok_nearby = [r for r in _ledger(db_path)
                 if r["model"] == "google-places-nearby" and r["status"] == "ok"]
    assert ok_nearby == [], "a refused search was metered as ok: %s" % ok_nearby
