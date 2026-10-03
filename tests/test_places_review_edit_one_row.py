"""A guest's edited Google review stays one review (owner, 10/2/26: "SIP"
showed twice at Simple EJ's — the same words, six hours apart). Places keys a
review by its time and the author's profile URL; an edit moves the time, so
the edit arrived under a new key and was stored as a second review. One
profile URL on one listing is one review, so the stored row takes the edit."""
import pytest

import models
from models import Restaurant, Review, create_restaurant, save_reviews

URL = "https://www.google.com/maps/contrib/117207949778659562640/reviews"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _rid(db_path):
    return create_restaurant(Restaurant(name="Simple EJ's", owner_email="o@x.test"), db_path=db_path)


def _rv(rid, when, author_part=URL, text="Great food and TVs.", rating=5, date="2026-10-01T22:58:59"):
    return Review(restaurant_id=rid, platform="google", external_id=f"google_{when}_{author_part}",
                  author="SIP", rating=rating, text=text, review_date=date)


def _rows(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, external_id, rating, text FROM reviews WHERE restaurant_id=? ORDER BY id", (rid,))]
    finally:
        conn.close()


def test_an_edited_review_under_a_new_time_is_the_same_row(db_path):
    rid = _rid(db_path)
    assert save_reviews([_rv(rid, 1790913539)], db_path=db_path)[0] == 1
    n, new = save_reviews([_rv(rid, 1790937133, text="Great food and TVs. Back again!",
                              date="2026-10-02T05:32:13")], db_path=db_path)
    assert n == 0 and not new
    rows = _rows(db_path, rid)
    assert len(rows) == 1
    assert rows[0]["external_id"].startswith("google_1790937133_") and rows[0]["text"].endswith("Back again!")
    # and the next fetch of the edit is an ordinary re-fetch
    assert save_reviews([_rv(rid, 1790937133, text="Great food and TVs. Back again!")], db_path=db_path)[0] == 0
    assert len(_rows(db_path, rid)) == 1


def test_a_lowered_rating_on_the_edit_is_still_reported(db_path):
    rid = _rid(db_path)
    save_reviews([_rv(rid, 1790913539)], db_path=db_path)
    down = []
    save_reviews([_rv(rid, 1790937133, rating=2, text="Went downhill.")], db_path=db_path, downgrades=down)
    assert len(down) == 1 and _rows(db_path, rid)[0]["rating"] == 2


def test_two_reviewers_are_never_merged_and_a_bare_name_is_not_trusted(db_path):
    rid = _rid(db_path)
    other = "https://www.google.com/maps/contrib/999/reviews"
    save_reviews([_rv(rid, 1790913539), _rv(rid, 1790913600, author_part=other)], db_path=db_path)
    assert len(_rows(db_path, rid)) == 2
    save_reviews([_rv(rid, 1790900000, author_part="Anonymous"),
                  _rv(rid, 1790900500, author_part="Anonymous")], db_path=db_path)
    assert len(_rows(db_path, rid)) == 4, "two 'Anonymous' reviews by name alone stay two"


def test_a_removed_duplicate_holding_the_new_key_never_breaks_the_batch(db_path):
    """Simple EJ's, 10/3/26: the second SIP row was removed; the next fetch
    brings the edit under that removed row's key. The batch must not fail."""
    rid = _rid(db_path)
    save_reviews([_rv(rid, 1790913539)], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                 "fetched_at, deleted_at) VALUES (?, 'google', ?, 'SIP', 5, 'x', '2026-10-02T05:32:13', '2026-10-02T12:03:23', "
                 "datetime('now'))",
                 (rid, f"google_1790937133_{URL}"))
    conn.commit(); conn.close()
    other = _rv(rid, 1790999999, author_part="https://www.google.com/maps/contrib/5/reviews")
    n, _ = save_reviews([_rv(rid, 1790937133), other], db_path=db_path)
    assert n == 1, "the other guest's review in the same batch still lands"
    live = [r for r in _rows(db_path, rid)]
    assert len(live) == 3
