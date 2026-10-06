"""Marketing fix round, slice E2 (9/28/26): social measurement honesty,
social security and robustness, and Marketing's error, loading and
accessibility states.

  MB-6   reach is reach alone; the engagement rate is engagements / reach
  MB-7   valid Instagram metrics; per-post metrics_synced_at; unmeasured is
         NULL, never 0; a partial or failed sync is not green
  MB-8   every publish logged with posted_at; the generated row matched by id
  AUX-13 one "best post" with a six-post floor; weekly buckets on posted_at
  MB-9   no Graph bodies or secrets in logs; the OAuth secret in a POST body
  MB-17  the web tab reads stored metrics; one rate-limited owner refresh
  MB-21  the IG callback never says "connected" when nothing was saved;
         Google's double-publish guard; photos on Facebook and Google;
         /g/ minted only to the restaurant's own hosts, tap writes limited
  AUX-10 / 11 / 14 / 17  the web states, keyboard and tokens

Meta and Google are never reached: `requests` is a scripted fake.
"""
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import auth
import client_api
import marketing_links
import marketing_media
import marketing_publish as mp
import marketing_signals as ms
import marketing_tags
import mobile_api
import models
import outcomes
import social_routes
from client_api import client_bp
from mobile_api import mobile_bp
from models import Restaurant, create_restaurant
from social_routes import social_bp

# Imported up front: these tests swap `requests` in sys.modules for a fake,
# and a module first imported while the fake is in place would keep it.
import gmb  # noqa: E402,F401
import marketing  # noqa: E402,F401
import notify  # noqa: E402,F401
import scheduler  # noqa: E402,F401
import weather  # noqa: E402,F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def _template_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("mkt_e2_template") / "template.db")
    models.init_db(db_path=path)
    models.ensure_columns(db_path=path)
    auth.init_auth(db_path=path)
    return path


@pytest.fixture
def db_path(tmp_path, _template_db):
    import shutil
    path = str(tmp_path / "test_reviews.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(_template_db + suffix):
            shutil.copy(_template_db + suffix, path + suffix)
    return path


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, mp, marketing_tags, outcomes, social_routes, client_api, mobile_api,
                ms, marketing_links, marketing_media):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    monkeypatch.setenv("META_APP_ID", "app-id")
    monkeypatch.setenv("META_APP_SECRET", "app-secret-value")


@pytest.fixture
def sleeps(monkeypatch):
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    flask_app.register_blueprint(social_bp)
    flask_app.register_blueprint(client_bp)
    flask_app.register_blueprint(mobile_bp)
    return flask_app


CSRF = "csrf-test-token"


class Session:
    def __init__(self, app, db_path, rid, role="client", username="owner"):
        self.client = app.test_client()
        uid = auth.create_user(rid, username, f"{username}@e2.test", "correct-horse-battery", db_path=db_path)
        auth.set_user_role(uid, role, db_path=db_path)
        self.token = auth.create_session(uid, restaurant_id=rid, db_path=db_path)
        self.client.set_cookie("session_token", self.token)
        self.client.set_cookie("csrf_js", CSRF)

    def post(self, path, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        headers["X-CSRF"] = CSRF
        return self.client.post(path, headers=headers, **kw)

    def get(self, path, **kw):
        return self.client.get(path, **kw)


class FakeResp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("Expecting value")
        return self._body


class FakeGraph:
    """Stands in for `requests`: answers by (method, url substring[, kw
    predicate]), first match wins, and records every call."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def _answer(self, method, url, kw):
        self.calls.append((method, url, kw))
        for route in self.routes:
            m, needle, resp = route[:3]
            when = route[3] if len(route) > 3 else None
            if m == method and needle in url and (when is None or when(kw)):
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"unexpected Graph call {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, kw)

    def post(self, url, **kw):
        return self._answer("POST", url, kw)


def _graph(monkeypatch, routes):
    fake = FakeGraph(routes)
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


def _restaurant(db_path, name="E2 Co", ig=True, fb=True, **fields):
    rid = create_restaurant(Restaurant(name=name, owner_email="owner@e2.test", module_marketing=1,
                                       timezone="America/Chicago"), db_path=db_path)
    f = dict(fields)
    if ig:
        f.update(ig_token="igt", ig_user_id="igu")
    if fb:
        f.update(fb_page_token="fbt", fb_page_id="fbp")
    if f:
        models.update_restaurant(rid, f, db_path=db_path)
    return rid


def _row(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _exec(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _post(db_path, rid, post_id, platform="instagram", days_ago=1, created_days_ago=None, synced=True,
          **metrics):
    """A published row as the sync leaves it: stamped when `synced`."""
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    created = at if created_days_ago is None else \
        (datetime.now(timezone.utc) - timedelta(days=created_days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    cols = {"restaurant_id": rid, "content_type": f"{platform}_post", "topic": f"Topic {post_id}",
            "post_id": post_id, "post_platform": platform, "posted_at": at, "created_at": created,
            "reach": None, "impressions": None, "engaged": None, "likes": None, "comments": None, "shares": None}
    cols.update(metrics)
    if synced:
        cols["metrics_synced_at"] = at
    names = ", ".join(cols)
    return _exec(db_path, f"INSERT INTO marketing_content_log ({names}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))


def _function(src, name):
    i = src.index("function " + name + "(")
    j = src.find("\nfunction ", i + 10)
    return src[i:j if j > 0 else len(src)]


# ── MB-6 reach is reach alone ─────────────────────────────────────────────

def test_the_window_reach_is_reach_alone_and_the_rate_divides_by_it(db_path):
    """Facebook's 1,000 reach and 1,600 impressions are 1,000 reach, and 50
    engagements on it are a 5% rate — not 2,600 "reach" and 1.9%."""
    rid = _restaurant(db_path)
    _post(db_path, rid, "fb_1", platform="facebook", reach=1000, impressions=1600, likes=40, comments=6, shares=4)
    w = ms.performance_window(rid, days=30, db_path=db_path)
    assert w["reach"] == 1000
    assert w["impressions"] == 1600
    assert w["engagement"] == 50
    assert w["engagement_rate"] == 5.0
    assert w["by_platform"][0]["reach"] == 1000


def test_the_header_reach_change_reads_reach_not_impressions(db_path):
    """Instagram's impressions retiring must not read as "Reach down": the
    impressions fall, reach is level."""
    rid = _restaurant(db_path)
    for i, days in enumerate((1, 2, 3)):
        _post(db_path, rid, f"ig_now_{i}", days_ago=days, reach=500, impressions=0, likes=10)
    for i, days in enumerate((31, 32, 33)):
        _post(db_path, rid, f"ig_then_{i}", days_ago=days, reach=500, impressions=4000, likes=10)
    h = ms.header_summary(rid, days=30, db_path=db_path)
    assert h["reach_change"] == 0.0
    assert "level" in h["status"]


def test_the_attribution_engagement_rate_divides_by_reach(db_path):
    rid = _restaurant(db_path)
    row_id = _post(db_path, rid, "fb_2", platform="facebook", reach=200, impressions=800, likes=10)
    row = {"id": row_id, "menu_item_id": None, "occasion": None, "post_kind": None, "topic": "Wings"}

    class _R(dict):
        def keys(self):
            return super().keys()

    out = ms._beyond_sales(rid, _R(row), datetime.now(), ["2026-09-01"], 2, db_path)
    assert out["engagement_rate"] == 0.05


def test_the_analytics_totals_never_add_impressions_to_reach(db_path):
    rid = _restaurant(db_path)
    _post(db_path, rid, "fb_3", platform="facebook", reach=300, impressions=450, likes=3)
    payload, status = client_api._do_mkt_performance(rid)
    assert status == 200
    assert payload["total_reach"] == 300
    assert payload["total_impressions"] == 450


def test_no_reader_in_the_slice_adds_impressions_to_reach():
    """Asserted on the source, so no reader can bring the sum back."""
    for name in ("marketing_signals.py", "client_api.py"):
        src = open(os.path.join(ROOT, name), encoding="utf-8").read()
        assert "COALESCE(reach,0) + COALESCE(impressions,0)" not in src, name
        assert "COALESCE(SUM(reach),0) + COALESCE(SUM(impressions),0)" not in src, name
        assert '(totals["reach"] or 0) + (totals["impressions"] or 0)' not in src, name


# ── MB-7 what the sync measures, and what it says it did ──────────────────

def test_the_instagram_metric_list_is_one_graph_v21_accepts():
    asked = social_routes.IG_INSIGHT_METRICS.split(",")
    assert "impressions" not in asked and "comments_count" not in asked
    assert {"reach", "likes", "comments"} <= set(asked)
    assert all(m in social_routes._METRIC_COLUMNS for m in asked)


def test_a_new_content_row_is_unmeasured_not_zero(db_path):
    rid = _restaurant(db_path)
    row_id = marketing.log_content(rid, "instagram_post", "Brunch", post_id="ig_new", post_platform="instagram")
    r = _row(db_path, "SELECT reach, likes, metrics_synced_at FROM marketing_content_log WHERE id=?", (row_id,))
    assert r == {"reach": None, "likes": None, "metrics_synced_at": None}


def test_boot_blanks_a_legacy_row_nobody_ever_measured(db_path):
    """The metric columns defaulted to 0: a post Meta never answered for read
    "0 reach". init_db turns those into NULL and leaves a real figure alone."""
    rid = _restaurant(db_path)
    zero = _exec(db_path, "INSERT INTO marketing_content_log (restaurant_id, topic, post_id) VALUES (?,?,?)",
                 (rid, "Never measured", "ig_zero"))
    real = _exec(db_path, "INSERT INTO marketing_content_log (restaurant_id, topic, post_id, reach) VALUES (?,?,?,?)",
                 (rid, "Measured", "ig_real", 50))
    assert _row(db_path, "SELECT reach FROM marketing_content_log WHERE id=?", (zero,))["reach"] == 0
    models.init_db(db_path=db_path)
    assert _row(db_path, "SELECT reach, likes FROM marketing_content_log WHERE id=?", (zero,)) == \
        {"reach": None, "likes": None}
    assert _row(db_path, "SELECT reach FROM marketing_content_log WHERE id=?", (real,))["reach"] == 50


def _ig_answer(good_posts):
    def answer(url, kw):
        post = url.split("/")[-2] if url.endswith("/insights") else url.split("/")[-1]
        if post in good_posts and url.endswith("/insights"):
            return FakeResp(200, {"data": [{"name": "reach", "values": [{"value": 300}]},
                                           {"name": "likes", "values": [{"value": 12}]},
                                           {"name": "comments", "values": [{"value": 2}]},
                                           {"name": "shares", "values": [{"value": 1}]}]})
        return FakeResp(400, {"error": {"message": "(#100) unsupported", "code": 100}})
    return answer


def test_a_sync_that_measures_some_posts_says_partial_and_stamps_only_those(db_path, monkeypatch):
    rid = _restaurant(db_path, fb=False)
    a = _post(db_path, rid, "ig_a", synced=False)
    b = _post(db_path, rid, "ig_b", synced=False, days_ago=2)
    _graph(monkeypatch, [("GET", "graph.facebook.com", _ig_answer({"ig_a"}))])
    result = social_routes.refresh_post_metrics(rid)
    assert (result["ok"], result["status"], result["measured"], result["attempted"]) == (False, "partial", 1, 2)
    ra = _row(db_path, "SELECT reach, likes, metrics_synced_at FROM marketing_content_log WHERE id=?", (a,))
    rb = _row(db_path, "SELECT reach, likes, metrics_synced_at FROM marketing_content_log WHERE id=?", (b,))
    assert (ra["reach"], ra["likes"]) == (300, 12) and ra["metrics_synced_at"]
    assert rb == {"reach": None, "likes": None, "metrics_synced_at": None}


def test_a_sync_meta_refused_entirely_is_failed_not_ok(db_path, monkeypatch):
    rid = _restaurant(db_path, fb=False)
    _post(db_path, rid, "ig_c", synced=False)
    _graph(monkeypatch, [("GET", "graph.facebook.com", _ig_answer(set()))])
    result = social_routes.refresh_post_metrics(rid)
    assert (result["ok"], result["status"], result["measured"]) == (False, "failed", 0)


def test_a_sync_that_measures_every_post_is_ok(db_path, monkeypatch):
    rid = _restaurant(db_path, fb=False)
    _post(db_path, rid, "ig_d", synced=False)
    _graph(monkeypatch, [("GET", "graph.facebook.com", _ig_answer({"ig_d"}))])
    result = social_routes.refresh_post_metrics(rid)
    assert (result["ok"], result["status"], result["measured"]) == (True, "ok", 1)


def test_a_first_stamp_does_not_turn_a_default_zero_into_a_measurement(db_path, monkeypatch):
    """A legacy Facebook row: likes measured, reach the column's DEFAULT 0
    because insights never answered. Stamping it must not make that 0 a
    measured reach."""
    rid = _restaurant(db_path, ig=False)
    row = _exec(db_path, "INSERT INTO marketing_content_log (restaurant_id, topic, post_id, post_platform, likes) "
                         "VALUES (?,?,?,?,?)", (rid, "Legacy", "fbp_legacy", "facebook", 5))

    def answer(url, kw):
        # Reach and views refused; the reactions and activity insights answer.
        metric = ((kw or {}).get("params") or kw or {}).get("metric", "")
        if metric == "post_reactions_by_type_total":
            return FakeResp(200, {"data": [{"name": metric, "values": [{"value": {"like": 8}}]}]})
        if metric == "post_activity_by_action_type":
            return FakeResp(200, {"data": [{"name": metric, "values": [{"value": {"comment": 1}}]}]})
        return FakeResp(400, {"error": {"message": "(#100) bad metric", "code": 100}})
    _graph(monkeypatch, [("GET", "graph.facebook.com", answer)])
    social_routes.refresh_post_metrics(rid)
    r = _row(db_path, "SELECT reach, impressions, likes, metrics_synced_at FROM marketing_content_log WHERE id=?",
             (row,))
    assert (r["reach"], r["impressions"], r["likes"]) == (None, None, 8) and r["metrics_synced_at"]
    w = ms.performance_window(rid, days=3650, db_path=db_path)
    assert (w["measured_posts"], w["reach_posts"]) == (1, 0)


def test_google_posts_are_never_sent_to_graph(db_path, monkeypatch):
    rid = _restaurant(db_path, fb=False)
    _post(db_path, rid, "accounts/1/locations/2/localPosts/3", platform="google", synced=False)
    fake = _graph(monkeypatch, [])
    result = social_routes.refresh_post_metrics(rid)
    assert fake.calls == []
    assert result["status"] == "ok" and result["attempted"] == 0


def test_the_nightly_sync_records_a_partial_pass_as_not_ok(db_path, monkeypatch):
    rid = _restaurant(db_path, fb=False)
    seen = []
    monkeypatch.setattr(scheduler, "record_metrics_sync", lambda r, ok, error=None: seen.append((r, ok, error)))
    monkeypatch.setattr(social_routes, "refresh_post_metrics", lambda r, *a, **k: {
        "ok": False, "status": "partial", "error": "1 of 2 posts couldn't be measured", "posts": []})
    scheduler.run_marketing_metrics_sync()
    mine = [s for s in seen if s[0] == rid]
    assert mine == [(rid, False, "1 of 2 posts couldn't be measured")]


def test_an_unmeasured_post_adds_nothing_to_the_window(db_path):
    rid = _restaurant(db_path)
    _post(db_path, rid, "ig_m", reach=400, likes=20)
    _post(db_path, rid, "ig_u", synced=False)     # never measured: NULLs, no stamp
    w = ms.performance_window(rid, days=30, db_path=db_path)
    assert (w["posts"], w["measured_posts"], w["reach_posts"], w["reach"]) == (2, 1, 1, 400)


def test_a_window_with_nothing_measured_says_so(db_path):
    rid = _restaurant(db_path)
    _post(db_path, rid, "ig_u2", synced=False)
    w = ms.performance_window(rid, days=30, db_path=db_path)
    assert w["reach_posts"] == 0 and w["engagement_rate"] is None


def test_recent_topics_show_no_figure_for_an_unmeasured_post(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _exec(db_path, "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform) "
                   "VALUES (?,?,?,?,?)", (rid, "instagram_post", "Unmeasured", "ig_x", "instagram"))
    _post(db_path, rid, "ig_y", platform="facebook", reach=0, impressions=900, likes=4)
    topics = {t["topic"]: t for t in client_api._do_recent_topics(rid)["topics"]}
    assert topics["Unmeasured"]["metrics"] == {}
    # Impressions are never shown as reach.
    assert "reach" not in topics["Topic ig_y"]["metrics"]


# ── MB-8 every publish logged, with its time, against its own row ─────────

IMG = "https://dashboard.cavnar.ai/m/tok.jpg"


def _ig_routes(post_id="ig_post_1"):
    return [
        ("POST", "igu/media_publish", FakeResp(200, {"id": post_id})),
        ("POST", "igu/media", FakeResp(200, {"id": "container_1"})),
        ("GET", "container_1", FakeResp(200, {"status_code": "FINISHED"})),
    ]


def test_an_instagram_post_with_no_topic_is_still_logged_with_its_posted_at(db_path, monkeypatch, sleeps):
    rid = _restaurant(db_path)
    _graph(monkeypatch, _ig_routes())
    payload, _ = social_routes._do_post_to_instagram(rid, "The owner's own caption", IMG, "")
    assert payload["ok"]
    r = _row(db_path, "SELECT topic, post_platform, posted_at FROM marketing_content_log WHERE post_id='ig_post_1'")
    assert r["topic"] == "The owner's own caption" and r["post_platform"] == "instagram" and r["posted_at"]


def test_a_facebook_post_with_no_topic_is_still_logged_with_its_posted_at(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(200, {"id": "fbp_9"}))])
    payload, _ = social_routes._do_post_to_facebook(rid, "Wings tonight", "")
    assert payload["ok"]
    assert _row(db_path, "SELECT posted_at FROM marketing_content_log WHERE post_id='fbp_9'")["posted_at"]


def test_a_publish_completes_the_generated_row_it_names_not_the_latest_by_topic(db_path, monkeypatch):
    """Monday's generation and a later same-topic generation: the post of
    Monday's words completes Monday's row, by id."""
    rid = _restaurant(db_path)
    monday = marketing.log_content(rid, "instagram_post", "Fall menu")
    later = marketing.log_content(rid, "instagram_post", "Fall menu")
    _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(200, {"id": "fbp_10"}))])
    social_routes._do_post_to_facebook(rid, "Fall menu is here", "Fall menu", content_log_id=monday)
    assert _row(db_path, "SELECT post_id, posted_at FROM marketing_content_log WHERE id=?", (monday,))["post_id"] == "fbp_10"
    assert _row(db_path, "SELECT post_id FROM marketing_content_log WHERE id=?", (later,))["post_id"] is None


def test_a_publish_without_an_id_never_guesses_a_row_by_topic(db_path, monkeypatch):
    rid = _restaurant(db_path)
    gen = marketing.log_content(rid, "instagram_post", "Fall menu")
    _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(200, {"id": "fbp_11"}))])
    social_routes._do_post_to_facebook(rid, "Fall menu is here", "Fall menu")
    assert _row(db_path, "SELECT post_id FROM marketing_content_log WHERE id=?", (gen,))["post_id"] is None
    assert _row(db_path, "SELECT posted_at FROM marketing_content_log WHERE post_id='fbp_11'")["posted_at"]


def test_another_restaurants_row_id_is_never_completed(db_path, monkeypatch):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, name="Other Co")
    theirs = marketing.log_content(other, "instagram_post", "Their post")
    _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(200, {"id": "fbp_12"}))])
    social_routes._do_post_to_facebook(rid, "Mine", "Mine", content_log_id=theirs)
    assert _row(db_path, "SELECT post_id FROM marketing_content_log WHERE id=?", (theirs,))["post_id"] is None


def test_the_generate_route_hands_back_the_generated_row(db_path, monkeypatch):
    from response_validation import Validated
    text = Validated("Brunch is back")
    text.content_log_id = 77
    monkeypatch.setattr(marketing, "generate_content", lambda *a, **k: text)
    payload, status = client_api._do_generate_content(1, "instagram_post", "Brunch")
    assert status == 200 and payload["content_log_id"] == 77
    src = open(os.path.join(ROOT, "marketing.py"), encoding="utf-8").read()
    body = src[src.index("def generate_content("):src.index("def calendar_idea_key(")]
    assert "result.content_log_id = row_id" in body


def test_the_web_composer_sends_the_row_and_the_photo_with_every_publish():
    for fn in ("postToInstagram", "postToFacebook", "postToGoogle"):
        body = _function(DASH, fn)
        assert "content_log_id:window._mktContentLogId" in body, fn
        assert "media_id:window._mktPhotoId" in body, fn
    assert "window._mktContentLogId=d.content_log_id||null" in DASH


# ── AUX-13 one best post ──────────────────────────────────────────────────

def test_no_best_post_is_named_under_six_measured_posts(db_path):
    rid = _restaurant(db_path)
    for i in range(5):
        _post(db_path, rid, f"ig_{i}", reach=100 * (i + 1), likes=i)
    r = ms.best_posts(rid, db_path=db_path)
    assert (r["measured"], r["enough"], r["best"], r["weakest"]) == (5, False, [], [])
    assert ms.top_performing(rid, db_path=db_path) == []


def test_the_best_post_is_the_most_engaged_among_measured_posts(db_path):
    rid = _restaurant(db_path)
    for i in range(6):
        _post(db_path, rid, f"ig_{i}", reach=1000, likes=i)
    _post(db_path, rid, "ig_unmeasured", synced=False)   # never counted
    r = ms.best_posts(rid, top=1, db_path=db_path)
    assert r["measured"] == 6 and r["enough"]
    assert r["best"][0]["topic"] == "Topic ig_5"
    assert r["weakest"][0]["topic"] == "Topic ig_0"
    assert ms.top_performing(rid, limit=1, db_path=db_path)[0]["topic"] == "Topic ig_5"


def test_the_analytics_top_post_uses_the_same_ranking_and_floor(db_path):
    rid = _restaurant(db_path)
    _post(db_path, rid, "ig_only", reach=900, likes=90)
    payload, _ = client_api._do_mkt_performance(rid)
    assert payload["top_post"] is None and payload["top_post_floor"] == ms.BEST_POST_MIN_POSTS
    src = open(os.path.join(ROOT, "client_api.py"), encoding="utf-8").read()
    body = src[src.index("def _do_mkt_performance("):src.index("def mkt_performance_api(")]
    assert "best_posts(" in body


def test_the_generator_names_no_winner_under_the_floor(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _post(db_path, rid, "ig_lucky", reach=5000, likes=400)
    monkeypatch.setattr(ms, "review_signal", lambda *a, **k: {})
    monkeypatch.setattr(ms, "weather_signal", lambda *a, **k: {})
    assert "performed best" not in ms.generation_context(rid, db_path=db_path)


def test_weekly_reach_buckets_on_when_the_post_went_out(db_path):
    """Written three weeks ago, posted yesterday: yesterday's week."""
    rid = _restaurant(db_path)
    _post(db_path, rid, "ig_late", days_ago=1, created_days_ago=21, reach=700)
    weeks = ms.weekly_reach(rid, weeks=8, db_path=db_path)
    posted_week = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-W%W")
    assert [w["week"] for w in weeks] == [posted_week]
    assert weeks[0]["total_reach"] == 700 and weeks[0]["reach_posts"] == 1


# ── MB-9 secrets stay out of URLs and logs ────────────────────────────────

def _web_state(rid):
    return "web~" + gmb.sign_mobile_state(rid)


def test_the_code_exchange_sends_the_app_secret_in_a_post_body(app, db_path, monkeypatch):
    rid = _restaurant(db_path, ig=False, fb=False)
    fake = _graph(monkeypatch, [
        ("POST", "oauth/access_token", FakeResp(200, {"access_token": "short"}), lambda kw: "code" in (kw.get("data") or {})),
        ("POST", "oauth/access_token", FakeResp(200, {"access_token": "long"})),
        ("GET", "me/accounts", FakeResp(200, {"data": [{"id": "page1", "name": "Page", "access_token": "pt"}]})),
        ("GET", "page1", FakeResp(200, {"instagram_business_account": {"id": "ig1"}})),
    ])
    resp = app.test_client().get(f"/instagram/callback?code=c&state={_web_state(rid)}")
    assert b"ig:'connected'" in resp.data
    exchanges = [c for c in fake.calls if "oauth/access_token" in c[1]]
    assert exchanges and all(c[0] == "POST" for c in exchanges)
    for _m, url, kw in exchanges:
        assert "client_secret" not in url and "client_secret" not in (kw.get("params") or {})
        assert kw["data"]["client_secret"] == "app-secret-value"


def test_page_tokens_never_reach_the_log(app, db_path, monkeypatch, capsys):
    rid = _restaurant(db_path, ig=False, fb=False)
    _graph(monkeypatch, [
        ("POST", "oauth/access_token", FakeResp(200, {"access_token": "short"})),
        ("GET", "me/accounts", FakeResp(200, {"data": [{"id": "page1", "name": "Page",
                                                         "access_token": "SECRET-PAGE-TOKEN"}]})),
    ])
    resp = app.test_client().get(f"/instagram/callback?code=c&state={_web_state(rid)}")
    assert b"ig:'connected'" in resp.data      # Facebook alone (10/2/26)
    out = capsys.readouterr().out
    assert "SECRET-PAGE-TOKEN" not in out and "page1" in out


def test_a_failed_graph_call_is_redacted_at_the_source():
    answer = social_routes._GraphAnswer(exc=RuntimeError(
        "HTTPSConnectionPool: /v21.0/p1/insights?metric=reach&access_token=EAAB-LEAK (Caused by timeout)"))
    assert "EAAB-LEAK" not in answer.text and "[redacted]" in answer.text


def test_redaction_covers_the_oauth_secret_and_a_json_token():
    from ai_guard import redact_secrets, safe_error
    assert "s3cr3t" not in redact_secrets("GET /oauth/access_token?client_id=1&client_secret=s3cr3t&code=x")
    assert "tok-1" not in redact_secrets("/oauth/access_token?grant_type=x&fb_exchange_token=tok-1")
    assert "EAAB9" not in safe_error('{"data": [{"access_token": "EAAB9", "id": "1"}]}')


def test_insights_errors_are_redacted_before_they_are_printed(monkeypatch, capsys):
    bucket = []
    monkeypatch.setattr(social_routes._insights_pass, "errors", bucket, raising=False)
    fake = type("R", (), {"status_code": 0, "text": "timeout on /p1/insights?access_token=EAAB-X",
                          "json": lambda self: {}})()
    social_routes._capture_insights_error("instagram_insights", "p1", fake)
    assert "EAAB-X" not in capsys.readouterr().out
    assert "EAAB-X" not in bucket[0]["text"]


# ── MB-17 the web tab reads stored metrics ────────────────────────────────

def test_reading_post_insights_never_calls_meta(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _post(db_path, rid, "ig_s", reach=321, likes=9)
    fake = _graph(monkeypatch, [])
    body = Session(app, db_path, rid).get("/api/post-insights").get_json()
    assert fake.calls == []
    assert body["ok"] and body["stored"]
    assert body["posts"][0]["metrics"]["reach"] == 321


def test_the_owner_refresh_is_rate_limited_on_the_phones_key(app, db_path, monkeypatch):
    rid = _restaurant(db_path, fb=False)
    _post(db_path, rid, "ig_r", synced=False)
    fake = _graph(monkeypatch, [("GET", "graph.facebook.com", _ig_answer({"ig_r"}))])
    s = Session(app, db_path, rid)
    answers = [s.post("/api/post-insights").get_json() for _ in range(social_routes.OWNER_REFRESH_MAX_CALLS + 1)]
    assert answers[0]["status"] == "ok" and answers[0]["refreshed"] == 1
    assert answers[-1]["throttled"] is True
    assert len(fake.calls) == social_routes.OWNER_REFRESH_MAX_CALLS
    mob = open(os.path.join(ROOT, "mobile_api.py"), encoding="utf-8").read()
    assert 'f"mktmetrics:{rid}"' in mob and 'f"mktmetrics:{rid}"' in \
        open(os.path.join(ROOT, "social_routes.py"), encoding="utf-8").read()


def test_the_web_tab_no_longer_polls_meta():
    assert "setInterval(refreshMetrics" not in DASH
    assert "fetch('/api/post-insights')" not in DASH
    body = _function(DASH, "mktRefreshFromMeta")
    assert "fetch('/api/post-insights',{method:'POST'" in body
    assert 'id="mkt-refresh-meta-btn"' in DASH


# ── MB-21 the callback, Google's guard, photos, links ─────────────────────

def _expired_state(rid):
    import hmac, hashlib
    payload = f"{rid}:{int(time.time()) - 60}"
    sig = hmac.new(gmb._state_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"web~{payload}:{sig}"


def test_an_expired_web_state_says_error_saves_nothing_and_asks_meta_nothing(app, db_path, monkeypatch):
    rid = _restaurant(db_path, ig=False, fb=False)
    fake = _graph(monkeypatch, [])
    resp = app.test_client().get(f"/instagram/callback?code=c&state={_expired_state(rid)}")
    assert b"ig:'connected'" not in resp.data
    assert b"state_invalid" in resp.data
    assert fake.calls == []
    assert not models.get_restaurant(rid, db_path=db_path).ig_token


def test_a_connection_that_could_not_be_saved_is_not_connected(app, db_path, monkeypatch):
    rid = _restaurant(db_path, ig=False, fb=False)
    _graph(monkeypatch, [
        ("POST", "oauth/access_token", FakeResp(200, {"access_token": "t"})),
        ("GET", "me/accounts", FakeResp(200, {"data": [{"id": "page1", "access_token": "pt"}]})),
        ("GET", "page1", FakeResp(200, {"instagram_business_account": {"id": "ig1"}})),
    ])

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(models, "update_restaurant", boom)
    resp = app.test_client().get(f"/instagram/callback?code=c&state={_web_state(rid)}")
    assert b"ig:'connected'" not in resp.data and b"save_failed" in resp.data


def _gbp(db_path, rid, monkeypatch):
    models.update_restaurant(rid, {"gmb_refresh_token": "r", "gmb_account_id": "accounts/1",
                                   "gmb_location_id": "locations/2"}, db_path=db_path)
    monkeypatch.setattr(gmb, "get_valid_token", lambda r: "tok")
    monkeypatch.setattr(gmb, "is_connected", lambda r: True)


def test_a_second_direct_google_post_inside_the_window_is_refused(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    _gbp(db_path, rid, monkeypatch)
    sent = []
    monkeypatch.setattr(gmb, "create_local_post",
                        lambda r, summary, cta_type=None, cta_url=None: sent.append(summary) or
                        {"ok": True, "name": "accounts/1/locations/2/localPosts/9"})
    s = Session(app, db_path, rid)
    first = s.post("/api/post-to-google", json={"summary": "Oysters tonight"})
    second = s.post("/api/post-to-google", json={"summary": "Oysters tonight"})
    assert first.status_code == 200 and first.get_json()["ok"]
    assert second.status_code == 409 and second.get_json()["duplicate"]
    assert sent == ["Oysters tonight"]
    assert _row(db_path, "SELECT posted_at FROM marketing_content_log WHERE post_id LIKE '%localPosts/9'")["posted_at"]


def test_a_definite_google_refusal_gives_the_claim_back(db_path, monkeypatch):
    rid = _restaurant(db_path)
    answers = iter([{"ok": False, "error": "GBP API 400: bad"}, {"ok": True, "name": "p/1"}])
    monkeypatch.setattr(gmb, "create_local_post", lambda *a, **k: next(answers))
    assert not gmb.post_local(rid, "Same words")["ok"]
    assert gmb.post_local(rid, "Same words")["ok"]


def test_an_unclear_google_answer_may_be_live_and_the_queue_does_not_retry_it(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _gbp(db_path, rid, monkeypatch)
    monkeypatch.setattr(gmb.requests, "post", lambda *a, **k: FakeResp(503, {"error": {}}, text="unavailable"))
    assert gmb.create_local_post(rid, "Hello")["maybe_live"] is True
    monkeypatch.setattr(gmb.requests, "post", lambda *a, **k: FakeResp(403, {}, text="denied"))
    assert gmb.create_local_post(rid, "Hello again")["maybe_live"] is False
    monkeypatch.setattr(gmb.requests, "post", lambda *a, **k: FakeResp(503, {}, text="unavailable"))
    result = mp.publish_now(rid, "google", "Brand new words", db_path=db_path)
    assert result["ok"] is False and result["reached_platform"] is True


def test_a_google_post_carries_the_photo(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _gbp(db_path, rid, monkeypatch)
    bodies = []
    monkeypatch.setattr(gmb.requests, "post", lambda url, **k: bodies.append(k["json"]) or
                        FakeResp(200, {"name": "accounts/1/locations/2/localPosts/5"}))
    result = mp.publish_now(rid, "google", "Pumpkin pie is back", media_token="tok",
                            base_url="https://dashboard.cavnar.ai", db_path=db_path)
    assert result["ok"]
    assert bodies[0]["media"] == [{"mediaFormat": "PHOTO", "sourceUrl": "https://dashboard.cavnar.ai/m/tok.jpg"}]


def test_a_scheduled_facebook_post_carries_its_photo(db_path, monkeypatch):
    rid = _restaurant(db_path)
    fake = _graph(monkeypatch, [("POST", "fbp/photos", FakeResp(200, {"id": "photo_1", "post_id": "fbp_77"}))])
    result = mp.publish_now(rid, "facebook", "Wings tonight", media_token="tok",
                            base_url="https://dashboard.cavnar.ai", db_path=db_path)
    assert result == {"ok": True, "post_id": "fbp_77"}
    _m, _u, kw = fake.calls[0]
    assert kw["data"]["url"] == "https://dashboard.cavnar.ai/m/tok.jpg"
    assert kw["data"]["message"] == "Wings tonight"


def test_a_direct_facebook_post_with_a_library_photo_posts_the_photo(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    media_id = _exec(db_path, "INSERT INTO marketing_media (restaurant_id, token, mime, data) VALUES (?,?,?,?)",
                     (rid, "libtok", "image/jpeg", b"x"))
    fake = _graph(monkeypatch, [("POST", "fbp/photos", FakeResp(200, {"id": "ph", "post_id": "fbp_78"}))])
    resp = Session(app, db_path, rid).post("/api/post-to-facebook",
                                           json={"caption": "Tacos", "media_id": media_id})
    assert resp.get_json()["ok"]
    assert fake.calls[0][2]["data"]["url"].endswith("/m/libtok.jpg")
    assert fake.calls[0][2]["data"]["url"].startswith("http")


def test_another_restaurants_photo_is_refused(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    other = _restaurant(db_path, name="Other Co")
    theirs = _exec(db_path, "INSERT INTO marketing_media (restaurant_id, token, mime, data) VALUES (?,?,?,?)",
                   (other, "theirtok", "image/jpeg", b"x"))
    fake = _graph(monkeypatch, [])
    resp = Session(app, db_path, rid).post("/api/post-to-facebook", json={"caption": "Tacos", "media_id": theirs})
    assert resp.status_code == 400 and fake.calls == []


def test_a_relative_library_url_is_made_absolute(app):
    with app.test_request_context("/api/post-to-instagram", base_url="https://dashboard.cavnar.ai"):
        url, bad = social_routes.photo_url_from(1, {"image_url": "/m/abc.jpg"})
    assert bad is None and url == "https://dashboard.cavnar.ai/m/abc.jpg"


def test_a_short_link_goes_only_to_the_restaurants_own_hosts(db_path):
    rid = _restaurant(db_path, menu_url="https://www.giamia.com/menu", reservation_provider="resy")
    ok = lambda url: marketing_links.create_link(rid, url, db_path=db_path)["ok"]
    assert ok("https://giamia.com/specials")
    assert ok("https://order.giamia.com/fall")
    assert ok("https://resy.com/cities/chi/gia-mia")
    import config
    assert ok(config.base_url() + "/j/abc")
    refused = marketing_links.create_link(rid, "https://evil.example/login", db_path=db_path)
    assert not refused["ok"] and refused["not_own_site"]
    assert not ok("https://giamia.com.evil.example/")


def test_a_restaurant_with_no_site_can_only_link_to_the_platform(db_path):
    rid = _restaurant(db_path)
    assert not marketing_links.create_link(rid, "https://example.com/menu", db_path=db_path)["ok"]


def test_tap_writes_from_one_public_link_are_limited(db_path, monkeypatch):
    rid = _restaurant(db_path, menu_url="https://example.com")
    made = marketing_links.create_link(rid, "https://example.com/menu", db_path=db_path)
    monkeypatch.setattr(marketing_links, "TAP_WRITES_PER_LINK_PER_MINUTE", 3)
    for i in range(10):
        target = marketing_links.resolve(made["token"], db_path=db_path, visitor=f"v{i}")
        assert target.startswith("https://example.com/menu")    # always forwarded
    assert marketing_links.link_stats(rid, db_path=db_path)[0]["clicks"] == 3


def test_meta_review_test_is_kept_and_marked_for_verification():
    src = open(os.path.join(ROOT, "social_routes.py"), encoding="utf-8").read()
    body = src[src.index("def meta_review_test("):]
    assert "Candidate for future cleanup after additional verification" in body


# ── AUX-10 loaders say when they failed ───────────────────────────────────

@pytest.mark.parametrize("fn", ["loadMktQueue", "loadMktDrafts", "loadMktWindow", "loadMktAttribution",
                                "mktLoadHeader", "loadMktPerformance", "loadRecentTopics"])
def test_a_marketing_loader_shows_the_load_failed_state(fn):
    body = _function(DASH, fn)
    assert "loadFailed(" in body, fn
    assert ".catch(function(){})" not in body, fn


def test_the_header_never_stays_on_reading_your_posts():
    body = _function(DASH, "mktLoadHeader")
    assert "_headFailed(" in body and "if(!d||!d.ok){_headFailed(d);return;}" in body
    assert "catch(function(){_headFailed(null);})" in body


def test_an_empty_queue_or_draft_list_is_the_standard_empty_state():
    for fn in ("loadMktQueue", "loadMktDrafts"):
        body = _function(DASH, fn)
        assert 'class="hb-empty"' in body and "no-data" not in body and "font-style:italic" not in body, fn


# ── AUX-11 keyboard and labels ────────────────────────────────────────────

def test_content_type_cards_are_keyboard_radios():
    m = re.search(r'<div class="ct-btn[^>]*>', DASH)
    tag = m.group(0)
    assert 'role="radio"' in tag and 'tabindex="0"' in tag and 'onkeydown="mktCtKey(event,this)"' in tag
    assert 'role="radiogroup"' in DASH
    key = _function(DASH, "mktCtKey")
    assert "'Enter'" in key and "' '" in key
    assert "setAttribute('aria-checked','true')" in _function(DASH, "selectCt")


def test_marketing_sub_tabs_are_a_tablist():
    for tab in ("content", "campaigns", "queue", "analytics"):
        m = re.search(r'<button[^>]*id="mkt-tab-%s-btn"[^>]*>' % tab, DASH)
        assert m and 'role="tab"' in m.group(0) and "aria-selected=" in m.group(0), tab
    assert 'role="tablist"' in DASH
    i = DASH.index("window.switchMktTab = function(tab)")
    assert "setAttribute('aria-selected'" in DASH[i:i + 1500]


def test_the_topic_field_and_the_post_box_are_labelled():
    assert '<label for="mktopic" class="sr-only">' in DASH
    m = re.search(r'<div class="output-box" id="mkoutput"[^>]*>', DASH)
    assert 'aria-label="' in m.group(0) and 'role="textbox"' in m.group(0)


def test_the_popular_badge_is_no_8px_white_text():
    m = re.search(r'<span style="([^"]*)">popular</span>', DASH)
    if m:        # slice E1 may remove the badge; if it stands, it is legible
        style = m.group(1)
        assert "font-size:8px" not in style and "color:white" not in style


# ── AUX-14 tokens and components ──────────────────────────────────────────

def test_calendar_chips_carry_no_brand_hex():
    body = _function(DASH, "renderCal")
    for hexv in ("#e1306c", "#1877f2", "#4285f4", "#ef9f27", "#2d6a4f"):
        assert hexv not in body.lower(), hexv


def test_marketing_status_colours_are_tokens():
    for fn in ("loadMktDrafts", "loadMktQueue", "updateCharCount", "loadRecentTopics", "loadMktPerformance"):
        body = _function(DASH, fn).lower()
        for hexv in ("#2d6a4f", "#ef9f27", "#f0ebe0", "#eaf4ee", "#a7d7b8", "#6fcf97", "#fef0e8"):
            assert hexv not in body, (fn, hexv)


def test_post_metrics_are_words_not_emoji():
    for fn in ("loadRecentTopics", "loadMktPerformance"):
        body = _function(DASH, fn)
        for glyph in ("&#128065;", "&#9825;", "&#128172;", "👁", "♥", "💬"):
            assert glyph not in body, (fn, glyph)


def test_the_top_post_card_is_on_tokens():
    i = DASH.index('<section id="mkt-perf-top-wrap"')
    card = DASH[i:DASH.index("</section>", i)]
    assert "#f0ebe0" not in card.lower() and "rgba(240,235,224" not in card
    assert 'class="hb-card"' in card


# ── AUX-17 the brief's cache comment ──────────────────────────────────────

def test_the_brief_cache_comment_matches_the_code():
    body = _function(DASH, "loadMktInsight")
    assert "4-hour TTL" not in body
    assert "five-minute" in body and "5*60*1000" in body
