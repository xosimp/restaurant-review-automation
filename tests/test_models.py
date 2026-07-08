"""Regression tests for the tenant-scoping (IDOR) fixes in models.py — a
client must never be able to approve or mutate another restaurant's rows by
guessing IDs."""
from models import approve_response, get_conn, create_restaurant, get_restaurant, update_restaurant, Restaurant


def _status(db_path, review_id):
    conn = get_conn(db_path)
    row = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()
    conn.close()
    return row["response_status"]


def _review_ids(db_path, rid):
    conn = get_conn(db_path)
    rows = conn.execute("SELECT id FROM reviews WHERE restaurant_id=?", (rid,)).fetchall()
    conn.close()
    return [r["id"] for r in rows]


def test_approve_scoped_to_owner(two_restaurants):
    w = two_restaurants
    review_a = _review_ids(w["db_path"], w["rid_a"])[0]
    approve_response(review_a, restaurant_id=w["rid_a"], db_path=w["db_path"])
    assert _status(w["db_path"], review_a) == "approved"


def test_approve_blocked_cross_tenant(two_restaurants):
    """Restaurant B passing Restaurant A's review id must be a silent no-op."""
    w = two_restaurants
    review_a = _review_ids(w["db_path"], w["rid_a"])[0]
    before = _status(w["db_path"], review_a)
    approve_response(review_a, restaurant_id=w["rid_b"], db_path=w["db_path"])
    assert _status(w["db_path"], review_a) == before != "approved"


def test_save_reviews_dedupes_by_external_id(two_restaurants):
    from models import save_reviews, Review
    w = two_restaurants
    added, _ = save_reviews([
        Review(restaurant_id=w["rid_a"], platform="google", external_id="ext-a1",
               author="Ann", rating=2, text="Cold food and a long wait."),
    ], db_path=w["db_path"])
    assert added == 0
    assert len(_review_ids(w["db_path"], w["rid_a"])) == 1


def test_restaurant_has_two_fa_pending_field(two_restaurants):
    """The 2FA anti-bruteforce fix depends on this column existing."""
    conn = get_conn(two_restaurants["db_path"])
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(restaurants)").fetchall()]
    conn.close()
    assert "two_fa_pending" in cols


def test_scheduling_fields_round_trip_through_get_restaurant(db_path):
    """These 5 fields were in the schema, ensure_columns, and
    update_restaurant's allowed set, but get_restaurant() never read them
    back — settings saved via the admin scheduling form (section count,
    daypart split, delivery %, role minimums, sched notes) silently had zero
    effect on AI-generated schedules, since every read returned the
    dataclass default (None) regardless of what was actually stored."""
    rid = create_restaurant(Restaurant(name="Round Trip Cafe", owner_email="rt@x.com"), db_path=db_path)
    update_restaurant(rid, {
        "sched_notes": "Never schedule solo closers on Sundays.",
        "section_count": 5,
        "daypart_split": "lunch 30%, dinner 70%",
        "delivery_pct": 15,
        "role_minimums_json": '{"Server": 2, "Cook": 2}',
    }, db_path=db_path)

    r = get_restaurant(rid, db_path=db_path)

    assert r.sched_notes == "Never schedule solo closers on Sundays."
    assert r.section_count == 5
    assert r.daypart_split == "lunch 30%, dinner 70%"
    assert r.delivery_pct == 15
    assert r.role_minimums_json == '{"Server": 2, "Cook": 2}'


def test_weather_fields_round_trip_through_get_restaurant(db_path):
    rid = create_restaurant(Restaurant(name="Weather Round Trip Co", owner_email="wrt@x.com"), db_path=db_path)
    update_restaurant(rid, {
        "latitude": 41.9,
        "longitude": -88.3,
        "weather_cache_json": '[{"temperature": 80}]',
        "weather_cached_at": "2026-07-07T12:00:00",
    }, db_path=db_path)

    r = get_restaurant(rid, db_path=db_path)

    assert r.latitude == 41.9
    assert r.longitude == -88.3
    assert r.weather_cache_json == '[{"temperature": 80}]'
    assert r.weather_cached_at == "2026-07-07T12:00:00"
