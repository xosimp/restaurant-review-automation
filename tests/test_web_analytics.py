"""Website analytics (web_analytics, 10/2/26): GA4 and Search Console read
through a read-only service account, stored per day, judged against the same
weekday, lined up with the rest of the restaurant's data — and surfaced on
Marketing, in "Cavnar AI found" and in Ask. No model call anywhere."""
import base64
import inspect
import json
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import models
import web_analytics as wa
from models import Restaurant, create_restaurant


def _key_pair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return key, pem


@pytest.fixture
def sa(monkeypatch):
    key, pem = _key_pair()
    blob = {"type": "service_account", "client_email": "cavnar-analytics@proj.iam.gserviceaccount.com",
            "private_key": pem, "private_key_id": "kid1"}
    monkeypatch.setenv(wa.SERVICE_ACCOUNT_ENV, json.dumps(blob))
    wa._token_cache.clear()
    return key


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return create_restaurant(Restaurant(name="EJ Co", owner_email="e@x.test", module_marketing=1,
                                        timezone="America/Chicago"), db_path=db_path)


class Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body
        self.content = b"x"

    def json(self):
        return self._b


def _fake_google(monkeypatch, days, ga_status=200, gsc_status=200, calls=None):
    """GA4 and Search Console answering for `days` (list of date)."""
    import requests

    def post(url, json=None, data=None, headers=None, timeout=None):
        assert timeout, "every outbound call names a timeout"
        if calls is not None:
            calls.append((url, json))
        if url == wa.TOKEN_URL:
            return Resp(200, {"access_token": "tok", "expires_in": 3600})
        if "analyticsdata" in url:
            if ga_status != 200:
                return Resp(ga_status, {"error": {"status": "PERMISSION_DENIED"}})
            dims = [d["name"] for d in json["dimensions"]]
            rows = []
            for d in days:
                g = d.strftime("%Y%m%d")
                if dims == ["date"] and len(json["metrics"]) == 4:
                    rows.append({"dimensionValues": [{"value": g}],
                                 "metricValues": [{"value": "100"}, {"value": "80"}, {"value": "300"}, {"value": "60"}]})
                elif dims == ["date", "sessionDefaultChannelGroup"]:
                    rows.append({"dimensionValues": [{"value": g}, {"value": "Organic Search"}],
                                 "metricValues": [{"value": "70"}]})
                elif dims == ["date", "linkDomain"]:
                    rows.append({"dimensionValues": [{"value": g}, {"value": "www.exploretock.com"}],
                                 "metricValues": [{"value": "12"}]})
                    rows.append({"dimensionValues": [{"value": g}, {"value": "buy.stripe.com"}],
                                 "metricValues": [{"value": "4"}]})
                else:
                    rows.append({"dimensionValues": [{"value": g}], "metricValues": [{"value": "40"}]})
            return Resp(200, {"rows": rows})
        if "searchAnalytics" in url:
            if gsc_status != 200:
                return Resp(gsc_status, {"error": {"status": "PERMISSION_DENIED"}})
            if json["dimensions"] == ["query"]:
                return Resp(200, {"rows": [{"keys": ["simple ejs"], "clicks": 50, "impressions": 400, "position": 1.2}]})
            return Resp(200, {"rows": [{"keys": [d.isoformat()], "clicks": 20, "impressions": 600, "position": 4.5}
                                       for d in days]})
        raise AssertionError(url)
    monkeypatch.setattr(requests, "post", post)


# ── inputs ──────────────────────────────────────────────────────────────────

def test_a_property_id_and_a_site_url_are_read_strictly():
    assert wa.clean_property_id("412345678") == "412345678"
    assert wa.clean_property_id(" properties/412345678 ") == "412345678"
    assert wa.clean_property_id("G-ABC123") == "" and wa.clean_property_id("12") == ""
    assert wa.clean_site_url("sc-domain:SimpleEJs.com") == "sc-domain:simpleejs.com"
    assert wa.clean_site_url("https://simpleejs.com") == "https://simpleejs.com/"
    assert wa.clean_site_url("simpleejs.com") == "" and wa.clean_site_url("sc-domain:") == ""


def test_outbound_domains_are_grouped_by_what_a_click_means():
    assert wa.domain_family("www.exploretock.com") == "booking_clicks"
    assert wa.domain_family("order.toasttab.com") == "ordering_clicks"
    assert wa.domain_family("buy.stripe.com") == "checkout_clicks"
    assert wa.domain_family("instagram.com") == "other_clicks"


def test_the_service_account_signs_a_jwt_google_can_verify(sa):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    tok = wa._assertion(wa.service_account(), now=1_800_000_000)
    head, claims, sig = tok.split(".")
    pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
    c = json.loads(base64.urlsafe_b64decode(pad(claims)))
    assert c["iss"] == "cavnar-analytics@proj.iam.gserviceaccount.com" and c["aud"] == wa.TOKEN_URL
    assert "analytics.readonly" in c["scope"] and "webmasters.readonly" in c["scope"] and c["exp"] - c["iat"] == 3600
    sa.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{head}.{claims}".encode(),
                           padding.PKCS1v15(), hashes.SHA256())
    assert wa.service_email() == "cavnar-analytics@proj.iam.gserviceaccount.com"


def test_without_a_key_everything_is_dormant(rid, monkeypatch):
    monkeypatch.delenv(wa.SERVICE_ACCOUNT_ENV, raising=False)
    models.update_restaurant(rid, {"ga4_property_id": "412345678"})
    assert not wa.configured() and wa.service_email() is None
    assert wa.sync(rid)["code"] == "not_configured"
    import scheduler
    assert scheduler.run_daily_web_analytics()["attempted"] == 0


# ── the read ────────────────────────────────────────────────────────────────

def test_the_first_read_backfills_and_later_reads_restate_recent_days(rid, db_path, sa, monkeypatch):
    models.update_restaurant(rid, {"ga4_property_id": "412345678", "gsc_site_url": "sc-domain:ejs.com"})
    calls = []
    end = wa._today(models.get_restaurant(rid)) - timedelta(days=1)
    _fake_google(monkeypatch, [end - timedelta(days=i) for i in range(3)], calls=calls)
    out = wa.sync(rid)
    assert out["ok"] and out["ga4"]["ok"] and out["gsc"]["queries"] == 1
    first = [j for u, j in calls if "analyticsdata" in u][0]
    assert first["dateRanges"][0]["startDate"] == (end - timedelta(days=wa.BACKFILL_DAYS - 1)).isoformat()
    s = wa._series(rid, ["sessions", "booking_clicks", "checkout_clicks", "menu_views", "search_clicks"],
                   end - timedelta(days=5), end, db_path)
    assert s["sessions"][end.isoformat()] == 100 and s["booking_clicks"][end.isoformat()] == 12
    assert s["checkout_clicks"][end.isoformat()] == 4 and s["menu_views"][end.isoformat()] == 40
    assert s["search_clicks"][end.isoformat()] == 20
    r = models.get_restaurant(rid)
    assert r.web_analytics_synced_at and r.web_analytics_error is None
    calls.clear()
    wa.sync(rid)
    again = [j for u, j in calls if "analyticsdata" in u][0]
    assert again["dateRanges"][0]["startDate"] == (end - timedelta(days=wa.RECENT_DAYS - 1)).isoformat()


def test_a_property_that_has_not_let_us_in_says_how_to_fix_it(rid, sa, monkeypatch):
    models.update_restaurant(rid, {"ga4_property_id": "412345678", "gsc_site_url": "sc-domain:ejs.com"})
    _fake_google(monkeypatch, [date.today()], ga_status=403)
    out = wa.sync(rid)
    assert not out["ok"] and out["ga4"]["code"] == "no_access" and out["gsc"]["ok"], "one refusing never empties the other"
    assert "cavnar-analytics@proj.iam.gserviceaccount.com" in out["ga4"]["error"]
    assert "Property access management" in out["ga4"]["error"]
    assert models.get_restaurant(rid).web_analytics_error == out["error"]


# ── what the numbers say ────────────────────────────────────────────────────

def _seed(rid, metric, values_by_day, source="ga4"):
    conn = models.get_conn()
    conn.executemany("INSERT OR REPLACE INTO web_analytics_daily (restaurant_id, source, day, metric, value) "
                     "VALUES (?,?,?,?,?)", [(rid, source, d.isoformat(), metric, v) for d, v in values_by_day.items()])
    conn.commit()
    conn.close()


def test_a_day_is_judged_against_its_own_weekday_and_needs_enough_of_them(rid):
    last = date(2026, 10, 1)            # a Thursday
    days = {last - timedelta(days=i): 100.0 for i in range(70)}
    days[last] = 220.0                   # spike
    days[last - timedelta(days=1)] = 40.0  # dip on a Wednesday
    _seed(rid, "sessions", days)
    items = {(i["kind"], i["day"]) for i in wa.signals(rid)["items"]}
    assert ("spike", last.isoformat()) in items and ("dip", (last - timedelta(days=1)).isoformat()) in items
    # Too little history: never judged against a stand-in.
    value, typical, n = wa.judge_day({last.isoformat(): 5.0, (last - timedelta(weeks=1)).isoformat(): 1.0}, last)
    assert typical is None and n == 1


def test_a_tiny_gap_is_not_a_spike(rid):
    last = date(2026, 10, 1)
    days = {last - timedelta(days=i): 4.0 for i in range(70)}
    days[last] = 9.0                     # +125%, but 5 visits
    _seed(rid, "sessions", days)
    assert not wa.signals(rid)["items"]


def test_a_spike_names_what_else_happened_that_day(rid):
    import demand_signals
    last = date(2026, 10, 1)
    days = {last - timedelta(days=i): 100.0 for i in range(70)}
    days[last] = 260.0
    _seed(rid, "sessions", days)
    conn = models.get_conn()
    for i in range(1, 9):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales) VALUES (?,?,?)",
                     (rid, (last - timedelta(weeks=i)).isoformat(), 5000))
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales) VALUES (?,?,?)", (rid, last.isoformat(), 6000))
    conn.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform, posted_at) "
                 "VALUES (?,?,?,?,?,?)", (rid, "instagram_post", "game day", "p1", "instagram", f"{last.isoformat()} 15:00:00"))
    conn.commit()
    conn.close()
    demand_signals.save(rid, [{"date": last.isoformat(), "kind": "event", "label": "Bears home game"}])
    spike = [i for i in wa.signals(rid)["items"] if i["kind"] == "spike"][0]
    assert "sales +20% against a typical Thursday" in spike["context"]
    assert "Bears home game" in spike["context"] and "you posted on Instagram" in spike["context"]
    assert "not proof" in wa.signals(rid)["basis"]


def test_three_falling_weeks_are_a_run(rid):
    last = date(2026, 10, 1)
    days = {}
    for i in range(70):
        d = last - timedelta(days=i)
        week = i // 7
        days[d] = {0: 50.0, 1: 70.0, 2: 85.0}.get(week, 100.0)
    _seed(rid, "booking_clicks", days)
    items = wa.signals(rid)["items"]
    run = [i for i in items if i["kind"] == "trend_down"]
    assert run and run[0]["metric"] == "booking_clicks" and run[0]["weeks"] == [700.0, 595.0, 490.0, 350.0]
    assert "in the week 3 weeks earlier" in run[0]["text"]
    # The run's own dips are the same news: not listed again.
    assert not [i for i in items if i["kind"] == "dip" and i["metric"] == "booking_clicks"]


# ── where it shows ──────────────────────────────────────────────────────────

def test_the_feed_offers_a_card_for_a_falling_run_only(rid):
    import marketing_opportunities as mo
    models.update_restaurant(rid, {"ga4_property_id": "412345678"})
    assert len(mo.website(rid)) == 0
    last = date(2026, 10, 1)
    _seed(rid, "booking_clicks", {last - timedelta(days=i): {0: 50.0, 1: 70.0, 2: 85.0}.get(i // 7, 100.0)
                                  for i in range(70)})
    cards = mo.website(rid)
    assert len(cards) == 1 and cards[0]["kind"] == "web_dip" and cards[0]["subject"] == "booking_clicks"
    assert "web_dip" in mo.FEED_KINDS and ("website", "your website") in mo.SOURCES
    models.update_restaurant(rid, {"ga4_property_id": None})
    assert mo.website(rid).state == "no_data"


def test_routes_owner_only_to_change_and_checked_with_google(rid, sa, monkeypatch):
    import client_api
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    started = []
    monkeypatch.setattr(client_api, "_start_web_analytics_sync", lambda r: started.append(r) or True)
    _fake_google(monkeypatch, [date.today() - timedelta(days=1)])
    owner = {"restaurant_id": rid, "role": "owner", "id": 1}
    manager = {"restaurant_id": rid, "role": "manager", "id": 2}
    app = Flask(__name__)
    with app.test_request_context("/x", method="POST", json={"ga4_property_id": "412345678"}):
        assert client_api._do_web_analytics_set(manager)[1] == 403
    with app.test_request_context("/x", method="POST", json={"ga4_property_id": "G-ABC"}):
        assert client_api._do_web_analytics_set(owner)[1] == 400
    with app.test_request_context("/x", method="POST", json={"ga4_property_id": "412345678",
                                                            "gsc_site_url": "https://ejs.com"}):
        out, code = client_api._do_web_analytics_set(owner)
    assert code == 200 and out["checks"]["ga4"]["ok"] and out["syncing"] and started == [rid]
    assert out["gsc_site_url"] == "https://ejs.com/"
    with app.test_request_context("/x"):
        st, _ = client_api._do_web_analytics_get(manager)
    assert st["service_email"] == "cavnar-analytics@proj.iam.gserviceaccount.com" and st["can_edit"] is False
    with app.test_request_context("/x", method="POST"):
        assert client_api._do_web_analytics_disconnect(manager)[1] == 403
        assert client_api._do_web_analytics_disconnect(owner)[1] == 200
    assert not models.get_restaurant(rid).ga4_property_id
    import mobile_api
    src = inspect.getsource(mobile_api)
    for twin in ("_capi._do_web_analytics_set", "_capi._do_web_analytics_disconnect", "_capi._do_web_analytics_sync",
                 "_capi._do_marketing_website(current_user)"):
        assert twin in src
    import auth
    assert any(p == "/api/marketing/" for p, _m in auth._MODULE_PREFIXES), "the figures are Marketing's"
    assert "/api/web-analytics" in auth._UNGATED_PREFIXES


def test_ask_reads_it_as_untrusted_marketing_data(rid):
    import ask_cavnar_tools as t
    tool = [x for x in t.TOOLS if x["spec"]["name"] == "read_website_analytics"][0]
    assert tool["module"] == "module_marketing" and "read_website_analytics" in t._UNTRUSTED_CONTENT_TOOLS
    assert t._read_website_analytics(rid)["has_data"] is False


def test_the_daily_job_is_registered_and_wired():
    import jobs_registry
    import scheduler
    job = jobs_registry.JOBS["web_analytics"]
    assert job["target"] == ("scheduler", "run_daily_web_analytics") and job["sends"] is False
    src = inspect.getsource(scheduler.scheduler_loop)
    assert '_ops.run_in_lane("intel", "web_analytics", run_daily_web_analytics)' in src
    assert "resumable_sweep(_WEB_ANALYTICS_CURSOR_KEY" in inspect.getsource(scheduler.run_daily_web_analytics)
