"""Shared fixtures: every test gets a real, throwaway SQLite database built by
the same init_db() the app uses, so schema drift is caught here instead of in
production."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from models import init_db, ensure_columns, create_restaurant, save_reviews, Restaurant, Review


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test_reviews.db")
    init_db(db_path=path)
    # Real app boot (hosted_dashboard.py) calls both init_db() AND
    # ensure_columns() — they're two separate migration paths (the latter
    # covers columns like alert_quiet_start/alert_max_per_day). Skipping
    # ensure_columns() here meant tests had a schema real production never has.
    ensure_columns(db_path=path)
    return path


@pytest.fixture
def two_restaurants(db_path):
    """Two restaurants with one review each — the minimum world in which
    cross-tenant bugs (IDOR) are observable."""
    rid_a = create_restaurant(Restaurant(name="Alpha Cafe", owner_email="a@x.com"), db_path=db_path)
    rid_b = create_restaurant(Restaurant(name="Bravo Bistro", owner_email="b@x.com"), db_path=db_path)
    save_reviews([
        Review(restaurant_id=rid_a, platform="google", external_id="ext-a1",
               author="Ann", rating=2, text="Cold food and a long wait."),
        Review(restaurant_id=rid_b, platform="google", external_id="ext-b1",
               author="Bob", rating=5, text="Fantastic dinner, will be back."),
    ], db_path=db_path)
    return {"db_path": db_path, "rid_a": rid_a, "rid_b": rid_b}
