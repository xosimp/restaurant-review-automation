"""Audit #12 remediation — the Reviews module, both platforms.

Every test here was written against a specific finding, and each names the
behaviour that was measured before the fix rather than just asserting the
current shape.
"""
import inspect
import json

import pytest

import client_api
import gmb
import models
import notify
import scheduler
from models import Review, save_reviews


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, notify):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _restaurant(db_path, rid=1, **kw):
    conn = models.get_conn(db_path)
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test"}
    cols.update(kw)
    keys = ",".join(cols)
    marks = ",".join("?" for _ in cols)
    conn.execute(f"INSERT INTO restaurants ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _review(db_path, rid, *, ext, rating=5, days_ago=1, sentiment="positive",
            processed=1, status="pending", cats='["service"]', urgency="normal",
            review_name=None, platform="google"):
    conn = models.get_conn(db_path)
    conn.execute(
        """INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
           review_date, fetched_at, sentiment, categories, summary, urgency, processed,
           response_status, review_name)
           VALUES (?,?,?,?,?,'text here', datetime('now', ?), datetime('now'), ?, ?, 's', ?, ?, ?, ?)""",
        (rid, platform, ext, f"A{ext}", rating, f"-{days_ago} days", sentiment, cats,
         urgency, processed, status, review_name))
    conn.commit()
    conn.close()


# ── P0-1 · the health alert's backstop was unreachable code ───────────────

def test_an_unanalysed_health_review_still_fires_the_alert():
    """`if urgency:` was always true — Review.urgency defaults to "normal"
    and the column is NOT NULL — so the keyword fallback beneath it could
    never run. A review whose analysis failed therefore fired no health
    alert at all, which is exactly when the backstop was needed."""
    assert notify._is_health_alert("I got food poisoning here", urgency="normal",
                                   processed=False) is True


def test_an_analysed_review_still_trusts_the_analyser_over_the_keywords():
    assert notify._is_health_alert(
        "No roach problem here at all, unlike the place down the road.",
        urgency="normal", processed=True) is False


def test_the_alert_path_tells_is_health_alert_whether_the_review_was_analysed():
    src = inspect.getsource(notify.fire_review_alerts)
    assert 'processed=bool(getattr(review, "processed", False))' in src


# ── P0-2 · a failed Google fetch looked like a quiet day ──────────────────

def test_a_failing_gmb_fetch_raises_instead_of_returning_nothing(monkeypatch):
    """It swallowed everything and returned [], so scheduler.py set
    fetched_ok=True, stamped last_fetched_at and the 25-hour staleness
    monitor never fired — ingestion could stop permanently with every
    indicator green."""
    class _Boom:
        def raise_for_status(self): raise RuntimeError("401 Unauthorized")
    monkeypatch.setattr(gmb.requests, "get", lambda *a, **k: _Boom())
    with pytest.raises(RuntimeError):
        gmb.fetch_reviews_via_gmb("tok", "locations/1", 1)


def test_gmb_follows_next_page_token(monkeypatch):
    pages = [
        {"reviews": [{"name": "accounts/1/locations/1/reviews/a", "starRating": "FIVE",
                      "comment": "one", "createTime": "2026-01-01T00:00:00Z",
                      "reviewer": {"displayName": "A"}}],
         "nextPageToken": "p2"},
        {"reviews": [{"name": "accounts/1/locations/1/reviews/b", "starRating": "FOUR",
                      "comment": "two", "createTime": "2026-01-02T00:00:00Z",
                      "reviewer": {"displayName": "B"}}]},
    ]
    seen = []

    class _Resp:
        def __init__(self, body): self._body = body
        def raise_for_status(self): pass
        def json(self): return self._body

    def _get(url, headers=None, params=None, timeout=None):
        seen.append(params.get("pageToken"))
        return _Resp(pages[len(seen) - 1])

    monkeypatch.setattr(gmb.requests, "get", _get)
    out = gmb.fetch_reviews_via_gmb("tok", "locations/1", 1)
    assert [r.external_id.split("/")[-1] for r in out] == ["a", "b"]
    assert seen == [None, "p2"], "the second page must be requested with the token"


# ── P1-1 · the urgent escalation never reached production drafts ──────────

def test_every_draft_call_site_passes_urgency():
    """drafter.py builds a distinct treatment for urgent reviews (80-100
    words, no minimising, invite direct contact) gated on urgency=='high'.
    scheduler.py and both admin paths omitted the argument, so it defaulted
    to "normal" in the one path that drafts essentially every reply."""
    import admin_routes

    def call_args(src):
        """Each draft_response(...) call's argument list, by matching the
        parentheses rather than guessing a window — the scheduler's call
        carries a long comment before its last argument."""
        out = []
        idx = 0
        while True:
            i = src.find("draft_response(", idx)
            if i == -1:
                return out
            if src[max(0, i - 4):i] == "def ":
                idx = i + 1
                continue
            j = i + len("draft_response(")
            depth = 1
            while depth and j < len(src):
                depth += {"(": 1, ")": -1}.get(src[j], 0)
                j += 1
            out.append(src[i:j])
            idx = j

    for src in (inspect.getsource(scheduler.run_daily_fetch),
                inspect.getsource(admin_routes)):
        calls = call_args(src)
        assert calls, "expected at least one draft_response call"
        for call in calls:
            assert "urgency=" in call, "a draft_response call without urgency=: " + call[:140]


# ── P1-2/P1-3 · the insight's axis, and gates on every trend claim ────────

def test_the_insight_times_reviews_by_when_the_guest_wrote_them():
    src = inspect.getsource(client_api._do_review_insight)
    assert "_AXIS = \"COALESCE(NULLIF(review_date,''), fetched_at)\"" in src
    assert "strftime('%Y-W%W', fetched_at)" not in src, "the bucket key must use the same axis"
    assert src.count("deleted_at IS NULL") >= 4


def test_the_insight_will_not_call_a_trend_off_single_review_weeks():
    src = inspect.getsource(client_api._do_review_insight)
    assert "MIN_TREND_REVIEWS_PER_WEEK" in src
    assert 'trend_weeks = [r for r in weekly_rows if (r["cnt"] or 0) >= _MIN_WK]' in src


def test_a_topic_arrow_needs_mentions_on_both_sides_of_the_comparison(db_path):
    """One mention this period against zero last period rendered "trending
    up" — a direction drawn from a single guest."""
    _restaurant(db_path)
    _review(db_path, 1, ext="one", cats='["service"]', days_ago=2)
    rows = {r["category"]: r for r in models.get_topic_heatmap(1, days=30)}
    assert rows["service"]["count"] == 1
    assert rows["service"]["trend"] == "flat"


# ── P1-4 · "the most-mentioned complaint" counted praise too ──────────────

def test_top_issues_counts_complaints_not_compliments(db_path):
    """Home renders position 0 as "Look into {topic} — it's the
    most-mentioned complaint", under "Repeat themes in negative reviews are
    the fixable kind". It counted every sentiment, so a restaurant PRAISED
    for its food quality was told to go and fix its food quality."""
    _restaurant(db_path)
    for i in range(4):
        _review(db_path, 1, ext=f"good{i}", rating=5, sentiment="positive",
                cats='["food_quality"]')
    _review(db_path, 1, ext="bad", rating=1, sentiment="negative", cats='["wait_time"]')

    complaints = models.get_top_issues(1, days=90)
    assert complaints[0]["category"] == "wait_time"
    assert all(i["category"] != "food_quality" for i in complaints)

    # The AI insight wants topics regardless of tone and labels them so.
    topics = models.get_top_issues(1, days=90, sentiment=None)
    assert topics[0]["category"] == "food_quality"


# ── P1-5 · the inbox hid what the badge counted ───────────────────────────

def test_an_unanalysed_review_is_in_the_inbox_not_just_the_badge(db_path):
    """get_review_stats counts every review; the list filtered processed=1.
    The tab badge read 3 over a list with nothing in it."""
    _restaurant(db_path)
    _review(db_path, 1, ext="u1", processed=0, sentiment=None, cats=None, status="pending")
    stats = models.get_review_stats(1)
    rows = models.get_reviews_data(1)
    assert stats["needs_response"] == 1
    assert [r["id"] for r in rows], "the review the badge counts must be in the list"
    assert rows[0]["processed"] is False, "and it must be flagged as not yet analysed"


# ── P1-6 · nothing paginated ──────────────────────────────────────────────

def test_the_inbox_pages_and_reports_how_many_remain(db_path):
    _restaurant(db_path)
    for i in range(7):
        _review(db_path, 1, ext=f"r{i}")
    page, total = models.get_reviews_data(1, limit=3, offset=0, include_total=True)
    assert total == 7 and len(page) == 3
    rest, _ = models.get_reviews_data(1, limit=3, offset=3, include_total=True)
    assert len(rest) == 3
    assert not ({r["id"] for r in page} & {r["id"] for r in rest}), "pages must not overlap"


# ── P1-9 · Retract offered where it could only fail ───────────────────────

def test_can_retract_is_computed_per_review(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="posted-google", status="posted", review_name="accounts/1/locations/1/reviews/x")
    _review(db_path, 1, ext="posted-yelp", status="posted", platform="yelp")
    _review(db_path, 1, ext="imported", status="posted")  # no review_name
    by_ext = {r["external_id"]: r for r in models.get_reviews_data(1)}
    assert by_ext["posted-google"]["can_retract"] is True
    assert by_ext["posted-yelp"]["can_retract"] is False
    assert by_ext["imported"]["can_retract"] is False


# ── P2-2 · one definition of "to approve" ─────────────────────────────────

def test_to_approve_means_the_same_thing_on_the_server_as_on_the_clients(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="drafted", status="drafted")
    _review(db_path, 1, ext="undrafted", status="pending")
    _review(db_path, 1, ext="done", status="posted")
    rows = models.get_reviews_data(1, filter_by="pending")
    assert sorted(r["external_id"] for r in rows) == ["drafted", "undrafted"]


# ── P2-4/P2-5 · the alert types that were missing or unweighted ───────────

def test_a_three_star_review_can_alert():
    src = inspect.getsource(notify.fire_review_alerts)
    assert 'elif rating == 3 and _col("alert_3star", 0):' in src
    assert "alert_3star" in models._ALERT_CONFIG_FIELDS


def test_the_catch_all_any_review_alert_never_beats_a_specific_one():
    src = inspect.getsource(notify.fire_review_alerts)
    assert src.index('rating == 2 and row["alert_2star"]') < src.index('_col("alert_any_review", 0)')


def test_a_spike_needs_a_share_not_just_a_count(db_path):
    """3 negatives out of 200 reviews and 3 out of 5 got the identical
    "trending issue" SMS."""
    _restaurant(db_path)
    for i in range(3):
        _review(db_path, 1, ext=f"neg{i}", rating=1, sentiment="negative")
    for i in range(40):
        _review(db_path, 1, ext=f"pos{i}", rating=5, sentiment="positive")
    count = notify._neg_spike_count(1, db_path=db_path)
    total = notify._neg_spike_window_total(1, db_path=db_path)
    assert count == 3 and total == 43
    assert (count / total) < notify.NEG_SPIKE_MIN_SHARE, "3 of 43 is not a spike"


# ── P2-6 · a guest lowering their own rating was silent ───────────────────

def test_a_downgraded_review_is_collected_for_alerting(db_path):
    _restaurant(db_path)
    save_reviews([Review(restaurant_id=1, platform="google", external_id="x",
                         author="Ann", rating=5, text="Loved it")], db_path=db_path)
    downgrades = []
    new_count, _ = save_reviews(
        [Review(restaurant_id=1, platform="google", external_id="x",
                author="Ann", rating=1, text="Actually terrible")],
        db_path=db_path, downgrades=downgrades)
    assert new_count == 0, "an edit is not a new review"
    assert [r.external_id for r in downgrades] == ["x"]
    assert downgrades[0].previous_rating == 5


def test_a_raised_rating_is_not_alerted_as_a_downgrade(db_path):
    _restaurant(db_path)
    save_reviews([Review(restaurant_id=1, platform="google", external_id="y",
                         author="Bob", rating=1, text="Bad")], db_path=db_path)
    downgrades = []
    save_reviews([Review(restaurant_id=1, platform="google", external_id="y",
                         author="Bob", rating=5, text="They fixed it")],
                 db_path=db_path, downgrades=downgrades)
    assert downgrades == []


# ── P2-7 · retention purged blank-dated reviews immediately ───────────────

def test_a_review_with_a_blank_date_is_not_purged_as_ancient(db_path):
    """COALESCE without NULLIF: '' is not NULL, and '' sorts before any
    date, so a review that arrived today with no date read as older than
    any retention window."""
    _restaurant(db_path, data_retention_months=12)
    conn = models.get_conn(db_path)
    conn.execute(
        """INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
           review_date, fetched_at, processed, response_status)
           VALUES (1,'google','blank','A',5,'t','',datetime('now'),1,'pending')""")
    conn.commit()
    conn.close()
    models.purge_expired_reviews(db_path=db_path)
    row = models.get_conn(db_path).execute(
        "SELECT deleted_at FROM reviews WHERE external_id='blank'").fetchone()
    assert row["deleted_at"] is None


# ── P2-10 · quiet weeks vanished from the trend ───────────────────────────

def test_a_week_with_no_reviews_is_drawn_as_zero_not_skipped(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="a", days_ago=1)
    _review(db_path, 1, ext="b", days_ago=21)
    weeks = models.get_sentiment_trend(1, weeks=8)
    assert len([w for w in weeks if w["total"]]) == 2
    assert len(weeks) >= 4, "the quiet weeks between them must still be emitted"
    assert any(w["total"] == 0 for w in weeks)


def test_a_restaurant_with_nothing_still_returns_an_empty_trend(db_path):
    """Every caller treats [] as "show the empty state" — a row of zero
    bars is worse than no chart."""
    _restaurant(db_path)
    assert models.get_sentiment_trend(1, weeks=8) == []


# ── P2-11 · failed AI work retried forever ────────────────────────────────

def test_a_review_that_keeps_failing_stops_being_retried(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="bad", processed=0, sentiment=None, cats=None)
    assert len(models.get_pending_analysis(1, db_path=db_path)) == 1
    for _ in range(models.MAX_AI_ATTEMPTS):
        rid = models.get_pending_analysis(1, db_path=db_path)[0].id
        models.record_ai_attempt(rid, "analysis", db_path=db_path)
    assert models.get_pending_analysis(1, db_path=db_path) == []
    assert models.count_stalled_reviews(1, db_path=db_path)["unanalysed"] == 1


# ── P3s ───────────────────────────────────────────────────────────────────

def test_fetched_at_is_stamped_in_the_restaurants_own_timezone(db_path):
    _restaurant(db_path, timezone="America/Los_Angeles")
    save_reviews([Review(restaurant_id=1, platform="google", external_id="tz",
                         author="A", rating=5, text="x")], db_path=db_path)
    row = models.get_conn(db_path).execute(
        "SELECT fetched_at FROM reviews WHERE external_id='tz'").fetchone()
    from datetime import datetime
    from zoneinfo import ZoneInfo
    expected_hour = datetime.now(ZoneInfo("America/Los_Angeles")).hour
    assert int(row["fetched_at"][11:13]) == expected_hour


def test_response_performance_ignores_deleted_reviews():
    src = inspect.getsource(models.get_response_performance)
    assert "deleted_at IS NULL" in src


def test_the_review_insight_cache_is_invalidated_with_the_others():
    src = inspect.getsource(client_api.invalidate_insight_cache)
    assert '"review-insight:"' in src


def test_tone_presets_are_gone_rather_than_pointing_at_a_removed_screen():
    import drafter
    assert not hasattr(drafter, "TONE_PRESETS")
    assert "tone" not in inspect.signature(drafter.draft_response).parameters
