"""Audit #10 remediation: the numbers and claims the Reviews module shows.

Every test here was written against a measured wrong answer. The module was
bucketing three years of reviews into one week because it timed them by when
Cavnar AI fetched them, texting owners on day one that four negative reviews
had arrived that week, hiding reviews whose AI analysis failed from the
owner's totals and from their reply queue, and binding every restaurant in a
group to whichever Google location happened to be listed first.
"""
import json

import pytest

import models
import notify
from models import Review, save_reviews


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    """get_review_stats, get_sentiment_trend and get_top_issues all call a
    bare get_conn() with no db_path parameter, so without this they read
    whatever reviews.db is sitting in the working directory."""
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


def _review(db_path, rid, *, ext, rating, written_days_ago, fetched_days_ago=0,
            sentiment="positive", processed=1, cats='["service"]', status="pending"):
    conn = models.get_conn(db_path)
    conn.execute(
        """INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
           review_date, fetched_at, sentiment, categories, summary, urgency, processed,
           response_status)
           VALUES (?,'google',?,?,?,'text here',
                   datetime('now', ?), datetime('now', ?), ?, ?, 's','normal',?,?)""",
        (rid, ext, f"A{ext}", rating, f"-{written_days_ago} days",
         f"-{fetched_days_ago} days", sentiment, cats, processed, status))
    conn.commit()
    conn.close()


# ── The time axis is when the guest wrote, not when we fetched ─────────────

def test_old_reviews_fetched_today_do_not_collapse_into_this_week(db_path):
    """Measured before the fix: an 8-week sentiment trend rendered a single
    bar holding three years of reviews, because it bucketed on fetched_at
    and a first connect stamps every review with the same one."""
    _restaurant(db_path)
    for i in range(6):
        _review(db_path, 1, ext=f"g{i}", rating=5, written_days_ago=30 * (i + 1) * 2)
    weeks = models.get_sentiment_trend(1, weeks=8)
    assert len(weeks) <= 2, f"3 years of reviews landed in {len(weeks)} bucket(s)"
    assert sum(w["total"] for w in weeks) < 6, "old reviews are inside an 8-week window"


def test_a_review_written_this_week_does_appear_in_the_trend(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="g1", rating=5, written_days_ago=2)
    weeks = models.get_sentiment_trend(1, weeks=8)
    assert sum(w["total"] for w in weeks) == 1


def test_top_issues_over_90_days_excludes_a_three_year_old_review(db_path):
    """Measured before the fix: every review ever imported counted as inside
    the 90-day window."""
    _restaurant(db_path)
    _review(db_path, 1, ext="old", rating=1, written_days_ago=1000, sentiment="negative")
    _review(db_path, 1, ext="new", rating=1, written_days_ago=5, sentiment="negative")
    issues = models.get_top_issues(1, days=90)
    assert issues and issues[0]["count"] == 1


def test_the_topic_heatmap_uses_the_same_window(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="old", rating=1, written_days_ago=1000, sentiment="negative")
    rows = models.get_topic_heatmap(1, days=90)
    assert all((r.get("total") or 0) == 0 for r in rows)


# ── The negative-spike alert ───────────────────────────────────────────────

def test_a_backlog_imported_today_does_not_fire_a_spike(db_path):
    """The SMS read "4 negative reviews in the last 7 days" on a client's
    first day, about reviews up to three years old."""
    _restaurant(db_path)
    for i in range(4):
        _review(db_path, 1, ext=f"n{i}", rating=1, written_days_ago=200 + i,
                sentiment="negative")
    assert notify._neg_spike_count(1, db_path=db_path) == 0


def test_real_recent_negatives_still_fire_a_spike(db_path):
    _restaurant(db_path)
    for i in range(3):
        _review(db_path, 1, ext=f"n{i}", rating=1, written_days_ago=i + 1,
                sentiment="negative")
    assert notify._neg_spike_count(1, db_path=db_path) == 3


def test_a_deleted_review_does_not_count_toward_a_spike(db_path):
    _restaurant(db_path)
    for i in range(3):
        _review(db_path, 1, ext=f"n{i}", rating=1, written_days_ago=1, sentiment="negative")
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET deleted_at=datetime('now') WHERE external_id='n0'")
    conn.commit()
    conn.close()
    assert notify._neg_spike_count(1, db_path=db_path) == 2


# ── A review that failed analysis is still the owner's review ──────────────

def test_unanalysed_reviews_are_counted_and_named(db_path):
    """Measured before the fix: 25 reviews in the table, 20 shown, a 4.2
    average against a real 3.6, and five unanswered one-stars nowhere in the
    queue of reviews needing a reply."""
    _restaurant(db_path)
    for i in range(4):
        _review(db_path, 1, ext=f"ok{i}", rating=5, written_days_ago=3)
    for i in range(2):
        _review(db_path, 1, ext=f"bad{i}", rating=1, written_days_ago=3,
                processed=0, sentiment=None, cats=None)
    s = models.get_review_stats(1)
    assert s["total"] == 6
    assert s["unanalysed"] == 2
    assert s["sentiment_complete"] is False
    assert s["avg_rating"] == pytest.approx(round((4 * 5 + 2 * 1) / 6, 1))


def test_an_unanswered_unanalysed_review_appears_in_needs_response(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="u1", rating=1, written_days_ago=1, processed=0,
            sentiment=None, cats=None)
    assert models.get_review_stats(1)["needs_response"] == 1


def test_a_fully_analysed_restaurant_reports_complete(db_path):
    _restaurant(db_path)
    _review(db_path, 1, ext="a", rating=5, written_days_ago=1)
    s = models.get_review_stats(1)
    assert s["unanalysed"] == 0 and s["sentiment_complete"] is True


# ── An edited review is the guest changing their mind ──────────────────────

def _r(rid, ext, rating, text):
    return Review(restaurant_id=rid, platform="google", external_id=ext,
                  author="Ann", rating=rating, text=text)


def test_a_guest_raising_their_rating_is_picked_up(db_path):
    """save_reviews was insert-only, so a one-star the guest later raised to
    five stayed a one-star in the average and the reply queue forever."""
    _restaurant(db_path)
    save_reviews([_r(1, "x", 1, "Awful, never again.")], db_path=db_path)
    n, _ = save_reviews([_r(1, "x", 5, "They fixed it — five stars now.")], db_path=db_path)
    assert n == 0, "the edit is not a new review"
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT rating, text, original_rating, processed, edited_at "
                       "FROM reviews WHERE external_id='x'").fetchone()
    conn.close()
    assert row["rating"] == 5
    assert "five stars now" in row["text"]
    assert row["original_rating"] == 1
    assert row["processed"] == 0, "changed text must be re-analysed"
    assert row["edited_at"]


def test_an_unchanged_refetch_touches_nothing(db_path):
    _restaurant(db_path)
    save_reviews([_r(1, "x", 4, "Good.")], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET processed=1, sentiment='positive' WHERE external_id='x'")
    conn.commit()
    conn.close()
    save_reviews([_r(1, "x", 4, "Good.")], db_path=db_path)
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT processed, edited_at FROM reviews WHERE external_id='x'").fetchone()
    conn.close()
    assert row["processed"] == 1 and row["edited_at"] is None


def test_an_edit_does_not_throw_away_a_reply_already_published(db_path):
    _restaurant(db_path)
    save_reviews([_r(1, "x", 1, "Bad.")], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET response_status='posted', draft_response='Sorry!' "
                 "WHERE external_id='x'")
    conn.commit()
    conn.close()
    save_reviews([_r(1, "x", 5, "Actually great.")], db_path=db_path)
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT rating, response_status, draft_response FROM reviews "
                       "WHERE external_id='x'").fetchone()
    conn.close()
    assert row["rating"] == 5
    assert row["response_status"] == "posted"
    assert row["draft_response"] == "Sorry!"


def test_save_reviews_returns_the_row_id(db_path):
    """review.id was None on every newly saved review, so alert_log rows were
    written with a null review_id and nothing could re-read the batch."""
    _restaurant(db_path)
    _n, new = save_reviews([_r(1, "x", 4, "Good.")], db_path=db_path)
    assert new and new[0].id is not None


def test_get_reviews_by_ids_is_scoped_to_one_restaurant(db_path):
    _restaurant(db_path, 1)
    _restaurant(db_path, 2)
    _n, mine = save_reviews([_r(1, "x", 4, "Mine.")], db_path=db_path)
    save_reviews([_r(2, "y", 4, "Theirs.")], db_path=db_path)
    got = models.get_reviews_by_ids(2, [mine[0].id], db_path=db_path)
    assert got == []
