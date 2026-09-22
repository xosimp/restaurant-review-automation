"""Edge cases for what marketing measures: post attribution and tags
(marketing_signals.py, marketing_tags.py) and the nightly Meta metrics sync
(scheduler.run_marketing_metrics_sync → social_routes.refresh_post_metrics).

What these protect:

  * a sales "lift" is only ever computed against the restaurant's own local
    business dates, never inflated by a zero baseline, never credited twice
    to posts that share a window, and never computed from labor's SAMPLE
    data for a restaurant that has none (MOD-MKT-16);
  * the Ask Cavnar path does not reload the whole sales history once per
    post (MOD-MKT-16);
  * occasion tags do not misfire on ordinary menu words (MOD-MKT-18), and an
    unmeasured engagement rate is None, not 0.0 (MOD-MKT-18);
  * a failed Graph call never overwrites stored reach/impressions with 0;
    the nightly pass is bounded and resumable and skips churned accounts; an
    expired token is one operator signal, not fifty (MOD-MKT-15).
"""
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

import auth
import marketing_signals as ms
import marketing_tags
import models
import social_routes
from models import Restaurant, create_restaurant

# Imported up front, not lazily inside a test: these swap `requests` in
# sys.modules for a fake, and a module first imported while the fake is in
# place (notify does `import requests` at top level) would keep it for the
# rest of the process.
import gmb  # noqa: E402,F401
import marketing  # noqa: E402,F401
import notify  # noqa: E402,F401
import scheduler  # noqa: E402,F401
import weather  # noqa: E402,F401


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def _template_db(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("mkt_signals_template") / "template.db")
    models.init_db(db_path=path)
    models.ensure_columns(db_path=path)
    auth.init_auth(db_path=path)
    return path


@pytest.fixture
def db_path(tmp_path, _template_db):
    """A copy of a module-built template (init_db + ensure_columns +
    init_auth) instead of conftest's per-test migration run."""
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
    for mod in (models, auth, ms, marketing_tags, social_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)


@pytest.fixture
def captures(monkeypatch):
    import ops
    seen = []
    monkeypatch.setattr(ops, "capture", lambda exc, job="unknown", context="", db_path=None: seen.append((job, context)))
    return seen


def _restaurant(db_path, name="Signals Co", tz="America/Chicago", **fields):
    rid = create_restaurant(Restaurant(name=name, owner_email="owner@signals.test", module_marketing=1,
                                       timezone=tz), db_path=db_path)
    if fields:
        models.update_restaurant(rid, fields, db_path=db_path)
    return rid


def _post(db_path, rid, posted_at, topic="Wings", platform="instagram", post_id=None, **metrics):
    cols = ["restaurant_id", "content_type", "topic", "post_id", "post_platform", "posted_at", "created_at"]
    vals = [rid, f"{platform}_post", topic, post_id or f"{platform}_{posted_at}", platform, posted_at, posted_at]
    for k, v in metrics.items():
        cols.append(k)
        vals.append(v)
    conn = sqlite3.connect(db_path)
    cur = conn.execute(f"INSERT INTO marketing_content_log ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def _flat_sales(start, end, value=1000.0):
    out, d = {}, datetime.strptime(start, "%Y-%m-%d")
    while d.strftime("%Y-%m-%d") <= end:
        out[d.strftime("%Y-%m-%d")] = value
        d += timedelta(days=1)
    return out


# ── Attribution #6 zero baseline ──────────────────────────────────────────

def test_a_zero_baseline_says_nothing_rather_than_an_infinite_lift(db_path, monkeypatch):
    """A6 Attribution #6: the same weekdays before were all closed (0)."""
    rid = _restaurant(db_path)
    sales = {d: 0.0 for d in _flat_sales("2026-08-01", "2026-09-10")}
    sales.update({"2026-09-11": 1500.0, "2026-09-12": 1500.0})
    monkeypatch.setattr(ms, "daily_sales", lambda r: sales)
    pid = _post(db_path, rid, "2026-09-11 17:00:00")
    assert ms.attribution_for_post(rid, pid, db_path=db_path) == {"ok": False, "reason": "not_enough_history"}


def test_a_closed_day_is_left_out_of_daily_sales_rather_than_counted_as_zero(db_path, monkeypatch):
    """A6 Attribution #6: a $0 day is a closure, not a bad day."""
    import labor
    rid = _restaurant(db_path)
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda r: [
        {"date": "2026-09-07", "sales_that_day": "0"},
        {"date": "2026-09-08", "sales_that_day": "2400.50"},
        {"date": "2026-09-08", "sales_that_day": "2400.50"},   # a second shift the same day
    ])
    assert ms.daily_sales(rid) == {"2026-09-08": 2400.50}


# ── Attribution #7 two posts, one window (MOD-MKT-16) ─────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-16: two posts on the same day get the identical window and baseline and are each reported with the full lift, unflagged")
def test_two_posts_sharing_a_window_are_flagged_as_overlapping(db_path, monkeypatch):
    """A6 Attribution #7 / MOD-MKT-16: one busy Friday cannot be credited
    to both the lunch post and the dinner post."""
    rid = _restaurant(db_path)
    sales = _flat_sales("2026-08-01", "2026-09-10")
    sales.update({"2026-09-11": 1300.0, "2026-09-12": 1300.0})
    monkeypatch.setattr(ms, "daily_sales", lambda r: sales)
    lunch = _post(db_path, rid, "2026-09-11 16:00:00", topic="Lunch special", post_id="p_lunch")
    dinner = _post(db_path, rid, "2026-09-11 22:00:00", topic="Dinner special", post_id="p_dinner")
    a = ms.attribution_for_post(rid, lunch, db_path=db_path)
    b = ms.attribution_for_post(rid, dinner, db_path=db_path)
    assert a["ok"] and b["ok"]
    assert a.get("overlapping") and b.get("overlapping")


# ── Attribution #8 UTC timestamp vs local business date (MOD-MKT-16) ──────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-16: posted_at is SQLite datetime('now') (UTC) and is bucketed as if it were the local business date")
def test_an_evening_post_is_measured_from_its_own_local_night(db_path, monkeypatch):
    """A6 Attribution #8 / MOD-MKT-16: posted 7:30pm Chicago on 9/18/26,
    stored as 00:30 UTC on 9/19. Its window is 9/18-9/19 local; measured
    from 9/19 it misses the night it was posted for."""
    rid = _restaurant(db_path, tz="America/Chicago")
    sales = _flat_sales("2026-08-01", "2026-09-30")
    sales["2026-09-18"] = 2000.0            # the night of the post
    monkeypatch.setattr(ms, "daily_sales", lambda r: sales)
    pid = _post(db_path, rid, "2026-09-19 00:30:00")   # how _log_published stores it
    result = ms.attribution_for_post(rid, pid, db_path=db_path)
    assert result["ok"]
    assert result["lift_pct"] == 50.0, result


def test_a_midday_post_is_unaffected_by_the_utc_offset(db_path, monkeypatch):
    """A6 Attribution #8 control: noon Chicago is the same date in UTC."""
    rid = _restaurant(db_path, tz="America/Chicago")
    sales = _flat_sales("2026-08-01", "2026-09-30")
    sales["2026-09-18"] = 2000.0
    monkeypatch.setattr(ms, "daily_sales", lambda r: sales)
    pid = _post(db_path, rid, "2026-09-18 17:00:00")
    assert ms.attribution_for_post(rid, pid, db_path=db_path)["lift_pct"] == 50.0


# ── Attribution #9 the SAMPLE fallback (MOD-MKT-16) ───────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-16: daily_sales falls back to labor's SAMPLE shifts CSV for a restaurant with no data, so 'no_pos_data' is unreachable")
def test_a_restaurant_with_no_sales_data_has_no_daily_sales(db_path):
    """A6 Attribution #9 / MOD-MKT-16 (probe p03_attr_sample): without the
    monkeypatch the existing test relies on, the real path reads the demo
    week and would attribute this restaurant's posts against someone
    else's numbers."""
    rid = _restaurant(db_path, name="No POS Co")
    assert ms.daily_sales(rid) == {}
    import labor
    sample_dates = sorted({s.get("date") for s in labor.load_shifts() if s.get("date")})
    pid = _post(db_path, rid, sample_dates[-2] + " 12:00:00")
    assert ms.attribution_for_post(rid, pid, db_path=db_path) == {"ok": False, "reason": "no_pos_data"}


# ── Attribution #10 tag false positives (MOD-MKT-18) ──────────────────────

@pytest.mark.parametrize("text, not_occasion", [
    ("Gluten free pasta is back on the menu", "offer"),
    ("Cold brew on tap all day", "weather"),
    ("Meet the game-changer: our smash burger", "game_day"),
])
@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: occasion regexes match ordinary menu words ('free' in gluten free, 'cold' in cold brew, 'the game')")
def test_a_menu_phrase_is_not_mistaken_for_an_occasion(text, not_occasion):
    """A6 Attribution #10 / MOD-MKT-18: these tags drive the by_occasion and
    by_kind lift groups in the attribution summary."""
    assert marketing_tags.occasion_of(text) != not_occasion


@pytest.mark.parametrize("text, occasion", [
    ("Happy hour 3-6 every weekday", "offer"),
    ("Free dessert with any entree tonight", "offer"),
    ("Patio is open and it's sunny", "weather"),
    ("Bears game day wing specials", "game_day"),
])
def test_a_real_occasion_is_still_tagged(text, occasion):
    """A6 Attribution #10 control: the fix must not blind the tagger."""
    assert marketing_tags.occasion_of(text) == occasion


# ── Attribution #11 summary cost on the request path (MOD-MKT-16) ─────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-16: attribution_summary reloads and re-parses the full shift history once per post (up to 25x) on the Ask Cavnar request path")
def test_the_attribution_summary_loads_sales_once_not_once_per_post(db_path, monkeypatch):
    """A6 Attribution #11 / MOD-MKT-16."""
    import labor
    rid = _restaurant(db_path)
    rows = [{"date": d, "sales_that_day": "1000"} for d in _flat_sales("2026-08-01", "2026-09-20")]
    loads = []
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda r: loads.append(r) or rows)
    for day in ("2026-09-08", "2026-09-10", "2026-09-12", "2026-09-14", "2026-09-16"):
        _post(db_path, rid, f"{day} 17:00:00", post_id=f"p{day}")
    ms.attribution_summary(rid, db_path=db_path)
    assert len(loads) == 1, f"loaded the sales history {len(loads)} times for one summary"


def test_the_attribution_summary_looks_at_no_more_than_25_posts(db_path, monkeypatch):
    """A6 Attribution #11: the per-request work is capped by post count."""
    rid = _restaurant(db_path)
    for i in range(30):
        _post(db_path, rid, f"2026-08-{(i % 28) + 1:02d} 17:00:00", post_id=f"p{i}")
    looked = []
    monkeypatch.setattr(ms, "attribution_for_post",
                        lambda r, cid, db_path=None: looked.append(cid) or {"ok": False, "reason": "x"})
    ms.attribution_summary(rid, db_path=db_path)
    assert len(looked) == 25


# ── MOD-MKT-18 engagement rate when nothing was measured ──────────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: performance_window reports engagement_rate 0.0 (not None) when no reach or impressions were measured")
def test_an_unmeasured_engagement_rate_is_none_not_zero(db_path):
    """MOD-MKT-18: 0.0% reads as "nobody engaged"; the truth is "not
    measured" (CLAUDE.md: value delivered is only what was measured)."""
    rid = _restaurant(db_path)
    _post(db_path, rid, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), reach=0, impressions=0, likes=0)
    window = ms.performance_window(rid, days=30, db_path=db_path)
    assert window["posts"] == 1
    assert window["engagement_rate"] is None
    assert window["previous"]["engagement_rate"] is None


# ── Metrics sync #5 a failed call overwrites stored numbers (MOD-MKT-15) ──

class FakeResp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeGraph:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, params))
        return self.answer(url, params or {})


def _stored(db_path, row_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT reach, impressions, engaged, likes, comments, shares "
                                 "FROM marketing_content_log WHERE id=?", (row_id,)).fetchone())
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="MOD-MKT-15: refresh_post_metrics writes metrics.get('reach', 0) etc., so a failed insights call zeroes stored reach/impressions")
def test_a_failed_facebook_insights_call_leaves_stored_reach_and_impressions_alone(db_path, monkeypatch, captures):
    """A6 Metrics #5 / MOD-MKT-15: engagement came back, insights did not."""
    rid = _restaurant(db_path, fb_page_token="fbt", fb_page_id="fbp")
    row = _post(db_path, rid, "2026-09-10 17:00:00", platform="facebook", post_id="fb_1",
                reach=500, impressions=900, likes=3)

    def answer(url, params):
        if url.endswith("/insights"):
            return FakeResp(400, {"error": {"message": "(#100) The value must be a valid insights metric"}})
        return FakeResp(200, {"reactions": {"summary": {"total_count": 5}},
                              "comments": {"summary": {"total_count": 1}}, "shares": {"count": 0}})
    monkeypatch.setitem(sys.modules, "requests", FakeGraph(answer))
    social_routes.refresh_post_metrics(rid)
    after = _stored(db_path, row)
    assert after["likes"] == 5
    assert (after["reach"], after["impressions"]) == (500, 900), after


@pytest.mark.xfail(strict=True, reason="MOD-MKT-15: the IG fallback metric set omits impressions and the write replaces the stored value with 0")
def test_the_instagram_fallback_keeps_the_impressions_it_did_not_fetch(db_path, monkeypatch, captures):
    """A6 Metrics #5 / MOD-MKT-15: Meta rejected `impressions`; the retry
    without it returned reach — the old impressions figure must survive."""
    rid = _restaurant(db_path, ig_token="igt", ig_user_id="igu")
    row = _post(db_path, rid, "2026-09-10 17:00:00", platform="instagram", post_id="ig_1",
                reach=400, impressions=900, likes=10)

    def answer(url, params):
        if "impressions" in params.get("metric", ""):
            return FakeResp(400, {"error": {"message": "impressions is no longer supported"}})
        return FakeResp(200, {"data": [{"name": "reach", "values": [{"value": 450}]},
                                       {"name": "likes", "values": [{"value": 12}]}]})
    monkeypatch.setitem(sys.modules, "requests", FakeGraph(answer))
    social_routes.refresh_post_metrics(rid)
    after = _stored(db_path, row)
    assert (after["reach"], after["likes"]) == (450, 12)
    assert after["impressions"] == 900, after


@pytest.mark.xfail(strict=True, reason="MOD-MKT-15: neither metric function produces 'engaged', so every sync writes engaged=0")
def test_a_sync_never_zeroes_a_metric_it_did_not_measure(db_path, monkeypatch, captures):
    """A6 Metrics #5 / MOD-MKT-15: `engaged` is not a Graph field at all."""
    rid = _restaurant(db_path, ig_token="igt", ig_user_id="igu")
    row = _post(db_path, rid, "2026-09-10 17:00:00", platform="instagram", post_id="ig_2", engaged=7)
    monkeypatch.setitem(sys.modules, "requests", FakeGraph(lambda url, params: FakeResp(
        200, {"data": [{"name": "reach", "values": [{"value": 300}]}]})))
    social_routes.refresh_post_metrics(rid)
    after = _stored(db_path, row)
    assert after["reach"] == 300
    assert after["engaged"] == 7


def test_a_sync_where_every_call_failed_writes_nothing(db_path, monkeypatch, captures):
    """A6 Metrics #5: an empty metrics dict is skipped outright today."""
    rid = _restaurant(db_path, ig_token="igt", ig_user_id="igu")
    row = _post(db_path, rid, "2026-09-10 17:00:00", platform="instagram", post_id="ig_3",
                reach=400, impressions=900, likes=10)
    monkeypatch.setitem(sys.modules, "requests", FakeGraph(lambda url, params: FakeResp(500, {"error": {}})))
    social_routes.refresh_post_metrics(rid)
    assert _stored(db_path, row) == {"reach": 400, "impressions": 900, "engaged": 0, "likes": 10,
                                     "comments": 0, "shares": 0}


# ── Metrics sync #6 bounded, resumable, live accounts only (MOD-MKT-15) ───

def _connected_restaurants(db_path, n, **extra):
    return [_restaurant(db_path, name=f"Sync {i}", ig_token="igt", ig_user_id="igu", **extra) for i in range(n)]


def _run_sync_with_slow_restaurants(monkeypatch, hours_each=2.0):
    """Runs the nightly sync twice with a fake clock that moves `hours_each`
    per restaurant; returns the ids each pass reached."""
    import scheduler
    clock = {"t": 2_000_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    passes = [[]]

    def slow_refresh(rid, limit=25):
        passes[-1].append(rid)
        clock["t"] += hours_each * 3600
        return {"ok": True, "posts": []}
    monkeypatch.setattr(social_routes, "refresh_post_metrics", slow_refresh)
    scheduler.run_marketing_metrics_sync()
    passes.append([])
    scheduler.run_marketing_metrics_sync()
    return passes


@pytest.mark.xfail(strict=True, reason="MOD-MKT-15: run_marketing_metrics_sync loops every connected restaurant serially with no wall-clock bound and no job_cursors cursor")
def test_the_nightly_metrics_sync_is_bounded_and_resumes_where_it_stopped(db_path, monkeypatch, captures):
    """A6 Metrics #6 / MOD-MKT-15: CLAUDE.md — work that iterates every
    restaurant must be bounded and resumable (the run_daily_fetch pattern).
    At 2h per restaurant, 20 restaurants cannot fit in one night; the pass
    must stop and the next one must reach the ones it did not."""
    ids = _connected_restaurants(db_path, 20)
    first, second = _run_sync_with_slow_restaurants(monkeypatch)
    assert len(first) < len(ids), "one pass ran 40 fake hours without stopping"
    assert set(second) - set(first), "the second pass started over at the top instead of resuming"


@pytest.mark.xfail(strict=True, reason="MOD-MKT-15: the metrics sync has no billing_status filter and spends Graph calls on churned accounts")
def test_the_nightly_metrics_sync_skips_a_churned_restaurant(db_path, monkeypatch, captures):
    """A6 Metrics #6 / MOD-MKT-15."""
    live = _restaurant(db_path, name="Live", ig_token="igt", ig_user_id="igu", billing_status="active")
    gone = _restaurant(db_path, name="Gone", ig_token="igt", ig_user_id="igu", billing_status="churned")
    first, _second = _run_sync_with_slow_restaurants(monkeypatch, hours_each=0.0)
    assert live in first
    assert gone not in first


def test_the_nightly_metrics_sync_skips_restaurants_without_marketing_or_a_connection(db_path, monkeypatch, captures):
    """A6 Metrics #6: the candidate filter that exists today."""
    on = _restaurant(db_path, name="On", ig_token="igt", ig_user_id="igu")
    off = _restaurant(db_path, name="Off", ig_token="igt", ig_user_id="igu", module_marketing=0)
    unconnected = _restaurant(db_path, name="Unconnected")
    first, _second = _run_sync_with_slow_restaurants(monkeypatch, hours_each=0.0)
    assert on in first
    assert off not in first and unconnected not in first


# ── Metrics sync #7 expired token → capture flood (MOD-MKT-15) ────────────

@pytest.mark.xfail(strict=True, reason="MOD-MKT-15: every failed Graph call runs ops.capture, so one expired token is ~50 operator-digest entries a night")
def test_an_expired_token_is_one_operator_signal_per_restaurant_not_one_per_call(db_path, monkeypatch, captures):
    """A6 Metrics #7 / MOD-MKT-15: 25 recent IG posts, token expired."""
    import scheduler
    rid = _restaurant(db_path, ig_token="expired", ig_user_id="igu")
    for i in range(25):
        _post(db_path, rid, f"2026-09-{(i % 20) + 1:02d} 17:00:00", post_id=f"ig_{i}", reach=10)
    monkeypatch.setitem(sys.modules, "requests", FakeGraph(lambda url, params: FakeResp(
        400, {"error": {"message": "Error validating access token: Session has expired", "code": 190}})))
    scheduler.run_marketing_metrics_sync()
    assert len(captures) <= 1, f"{len(captures)} captures for one expired token"
