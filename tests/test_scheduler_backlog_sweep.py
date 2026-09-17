"""run_daily_fetch()'s analyse/draft backlog sweep must not depend on this
cycle having fetched anything new from Google — see scheduler.py.

A review stuck at processed=0 or response_status='pending' (a failed Haiku
call, a rate limit, or one inserted outside the normal fetch path — a seed
script, a manual import) used to get swept up only on a cycle where this
SAME restaurant also happened to receive a genuinely new review that day.
Zero new reviews that cycle meant the backlog sat there forever, fixable
only by a manual "Draft a reply" click.
"""
import functools

import pytest

import analyser
import drafter
import fetcher
import models
import scheduler


@pytest.fixture(autouse=True)
def _redirect_to_test_db(monkeypatch, db_path):
    # run_daily_fetch() calls these without a db_path, so they default to
    # the real DB_PATH — bind each one to the test db instead. get_conn()
    # itself is called both directly (with no path) and indirectly, by the
    # functions below, with their own already-bound db_path — so it only
    # substitutes the test path when none was given, rather than being
    # replaced by a fixed-arg partial (which would collide with those
    # already-explicit calls).
    orig_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda path=None: orig_get_conn(path or db_path))
    for name in ("get_restaurant", "save_reviews",
                 "get_pending_analysis", "get_pending_drafts",
                 "update_last_fetched", "get_approved_examples"):
        orig = getattr(models, name)
        monkeypatch.setattr(models, name, functools.partial(orig, db_path=db_path))

    monkeypatch.setattr(fetcher, "fetch_google", lambda *a, **k: [])
    monkeypatch.setattr(analyser, "analyse_review",
                         lambda review_id, rating, text, restaurant_id=None:
                         models.update_analysis(review_id, sentiment="positive", categories=[],
                                                 summary="", urgency="normal", db_path=db_path))
    monkeypatch.setattr(drafter, "draft_response",
                         lambda review_id, *a, **k:
                         models.update_draft(review_id, "Thanks so much for the kind words!", db_path=db_path))


def _seed_pending_review(db_path, rid):
    conn = models.get_conn(db_path)
    conn.execute("""
        INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text,
                              review_date, fetched_at, sentiment, urgency, response_status, processed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
    """, (rid, "google", "backlog-1", "Pat", 5, "Loved it, will be back!",
          "2026-09-10", "2026-09-10 12:00:00", "positive", "normal", "pending"))
    conn.commit()
    conn.close()


def _review_row(db_path, rid):
    conn = models.get_conn(db_path)
    row = conn.execute(
        "SELECT response_status, draft_response FROM reviews WHERE restaurant_id=?", (rid,)
    ).fetchone()
    conn.close()
    return row


def test_pending_backlog_gets_drafted_even_when_this_cycle_fetches_nothing_new(db_path):
    rid = models.create_restaurant(models.Restaurant(
        name="Backlog Co", owner_email="backlog@x.com",
        reviews_live=1, google_place_id="places/dummy",
    ), db_path=db_path)
    _seed_pending_review(db_path, rid)

    scheduler.run_daily_fetch()

    row = _review_row(db_path, rid)
    assert row["response_status"] == "drafted"
    assert row["draft_response"] == "Thanks so much for the kind words!"


def test_a_restaurant_with_no_backlog_and_no_new_reviews_is_a_quiet_no_op(db_path):
    rid = models.create_restaurant(models.Restaurant(
        name="Quiet Co", owner_email="quiet@x.com",
        reviews_live=1, google_place_id="places/dummy",
    ), db_path=db_path)

    scheduler.run_daily_fetch()  # must not raise with nothing to sweep

    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 0
