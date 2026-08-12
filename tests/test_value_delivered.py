"""value_delivered.py — the "Total Value Delivered" figure and its daily
snapshot history (see mobile_api.py's _do_mobile_home, which calls both on
every Home-tab load)."""
import pytest

import models
from models import create_restaurant, Restaurant, Review, save_reviews, get_conn
from value_delivered import compute_total_value_delivered, record_value_snapshot, get_value_history


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """compute_total_value_delivered() calls models.get_review_stats(), which
    has no db_path param and always resolves models.py's own module-level
    get_conn — patching it here is the only way to keep that nested call
    scoped to the test db. See test_mobile_api.py's _redirect_db for the
    identical gotcha."""
    real_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real_get_conn(db_path))


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Value Test Co"), owner_email="v@x.com", **kw), db_path=db_path)


def _responded_review(db_path, rid, external_id="rev1"):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=external_id,
                          author="Ann", rating=5, text="Great!")], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE reviews SET response_status='approved', processed=1 WHERE restaurant_id=? AND external_id=?",
        (rid, external_id)
    )
    conn.commit()
    conn.close()


def _reviews_only(db_path, **kw):
    """Isolates the reviews-value term — labor/inventory fall back to synthetic
    sample-data savings (matching the web dashboard) whenever a restaurant has
    no live CSV connected, so leaving those modules on would couple these
    reviews-specific assertions to that unrelated sample-data constant."""
    return _restaurant(db_path, module_labor=0, module_inventory=0, module_marketing=0, **kw)


def test_fresh_restaurant_has_zero_value_delivered(db_path):
    rid = _reviews_only(db_path)
    assert compute_total_value_delivered(rid, db_path=db_path) == 0


def test_responded_reviews_count_at_5_dollars_each(db_path):
    rid = _reviews_only(db_path)
    _responded_review(db_path, rid, "r1")
    _responded_review(db_path, rid, "r2")
    assert compute_total_value_delivered(rid, db_path=db_path) == 10


def test_reviews_value_excluded_when_module_disabled(db_path):
    rid = _reviews_only(db_path, module_reviews=0)
    _responded_review(db_path, rid, "r1")
    assert compute_total_value_delivered(rid, db_path=db_path) == 0


def test_unknown_restaurant_returns_zero(db_path):
    assert compute_total_value_delivered(999999, db_path=db_path) == 0


def test_snapshot_round_trips_through_history(db_path):
    rid = _restaurant(db_path)
    record_value_snapshot(rid, 250, db_path=db_path)
    history = get_value_history(rid, db_path=db_path)
    assert len(history) == 1
    assert history[0]["value"] == 250


def test_snapshot_upserts_same_day_instead_of_duplicating(db_path):
    rid = _restaurant(db_path)
    record_value_snapshot(rid, 100, db_path=db_path)
    record_value_snapshot(rid, 150, db_path=db_path)
    history = get_value_history(rid, db_path=db_path)
    assert len(history) == 1
    assert history[0]["value"] == 150


def test_history_scoped_to_own_restaurant(db_path):
    rid_a = _restaurant(db_path, name="A")
    rid_b = _restaurant(db_path, name="B")
    record_value_snapshot(rid_a, 500, db_path=db_path)
    history_b = get_value_history(rid_b, db_path=db_path)
    assert history_b == []
