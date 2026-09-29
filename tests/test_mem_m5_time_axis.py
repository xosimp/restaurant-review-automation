"""Memory fix round M5, time_axis (memory audit 9/29/26, CROSS-18).

Marketing's review signal used its own time axis and the server's clock:
`COALESCE(review_date, fetched_at)` read a backfilled review stored with an
empty review_date as this fortnight's praise, and datetime.now() started the
window a day off for a restaurant west of the server. It now reads the ONE
review time axis (models.REVIEW_TIME_AXIS_BARE) from the restaurant's own
today, and the post-attribution review count does the same and leaves
removed reviews out.
"""
from datetime import datetime, timedelta

import pytest

import marketing_signals as ms
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _review(db_path, rid, ext, rating, text, review_date, fetched_at, deleted=False, categories='["service"]'):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                 "fetched_at, processed, categories, deleted_at) VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                 (rid, "google", ext, "G", rating, text, review_date, fetched_at, categories,
                  "2026-09-20 10:00:00" if deleted else None))
    conn.commit()
    conn.close()


def test_an_empty_review_date_is_no_date_and_the_fetch_stamp_decides(db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="Axis Diner", owner_email="axis@x.test"), db_path=db_path)
    monkeypatch.setattr(ms, "_local_today", lambda r: datetime(2026, 9, 29).date())
    # Backfilled a month ago with review_date '' — not this fortnight.
    _review(db_path, rid, "old", 5, "Loved the brisket back in August", "", "2026-08-29 12:00:00")
    # A recent one, and a recent one Google removed.
    _review(db_path, rid, "new", 5, "Great patio night", "2026-09-25", "2026-09-26 03:00:00")
    _review(db_path, rid, "gone", 5, "Spam praise", "2026-09-26", "2026-09-26 04:00:00", deleted=True)
    sig = ms.review_signal(rid, db_path=db_path)
    assert sig["reviews_read"] == 1
    assert sig["best_quote"] == "Great patio night"


def test_the_window_ends_on_the_restaurants_own_today(db_path, monkeypatch):
    rid = create_restaurant(Restaurant(name="West Diner", owner_email="west@x.test"), db_path=db_path)
    # 14 days before the restaurant's own 9/29 is 9/15: a review on 9/15
    # counts, one on 9/14 does not — whatever the server's date is.
    _review(db_path, rid, "edge-in", 5, "In the window", "2026-09-15", "2026-09-15 20:00:00")
    _review(db_path, rid, "edge-out", 4, "Just outside", "2026-09-14", "2026-09-14 20:00:00")
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda r, naive=False: datetime(2026, 9, 29, 21, 0))
    sig = ms.review_signal(rid, db_path=db_path)
    assert sig["reviews_read"] == 1 and sig["best_quote"] == "In the window"


def test_the_source_reads_the_one_axis_not_its_own():
    import inspect
    src = inspect.getsource(ms.review_signal) + inspect.getsource(ms._beyond_sales)
    assert "COALESCE(review_date, fetched_at) >=" not in src and "review_date >= ?" not in src
    assert "REVIEW_TIME_AXIS_BARE" in src
    assert "datetime.now() - timedelta" not in inspect.getsource(ms.review_signal)


def test_post_attribution_counts_live_reviews_on_the_one_axis(db_path):
    rid = create_restaurant(Restaurant(name="Dish Diner", owner_email="dish@x.test"), db_path=db_path)
    posted = datetime(2026, 9, 10, 18, 0)
    _review(db_path, rid, "a", 5, "The brisket tacos were perfect", "2026-09-12", "2026-09-12 20:00:00")
    _review(db_path, rid, "b", 5, "brisket tacos again, removed", "2026-09-13", "2026-09-13 20:00:00", deleted=True)
    # No review_date at all: the fetch stamp places it in the window.
    _review(db_path, rid, "c", 4, "brisket tacos worth the wait", "", "2026-09-14 20:00:00")

    class _R(dict):
        def keys(self):
            return super().keys()
    row = _R(id=0, menu_item_id=None, occasion=None, post_kind=None, topic="Brisket tacos tonight")
    out = ms._beyond_sales(rid, row, posted, ["2026-09-10"], 1, db_path)
    assert out["reviews_mentioning"] == 2
