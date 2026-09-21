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


def test_alert_config_fields_set_at_creation_are_not_silently_dropped(db_path):
    """create_restaurant()'s INSERT predated the al_*/urgent_via_*/alert_*
    channel-toggle columns entirely — a non-default value passed to
    Restaurant() at creation (e.g. al_1star_sms=1, the SMS toggle for
    1-star alerts, which defaults to 0) was silently ignored, and the new
    row got whatever the column's own ALTER TABLE ... DEFAULT is instead.
    Doesn't affect most signups today since the defaults happen to be
    sensible, but any future flow that lets someone choose a non-default
    value at creation time (e.g. opting out of SMS at signup) would have
    that choice vanish without error."""
    rid = create_restaurant(Restaurant(
        name="Alert Config Co", owner_email="ac@x.com",
        alert_1star=0, alert_2star=1, alert_health=0, alert_neg_spike=0,
        alert_negative_trend=0, alert_no_response=1, alert_5star=1,
        alert_rating_threshold=1, alert_rating_floor=3.5, alert_labor_over=1,
        alert_any_review=1, alert_resp_approved=1,
        urgent_via_email=0, urgent_via_sms=1,
        al_health_email=0, al_health_sms=1, al_health_push=0,
        al_1star_email=0, al_1star_sms=1, al_1star_push=0,
        al_2star_email=0, al_2star_sms=1, al_2star_push=0,
        al_5star_email=1, al_5star_sms=1, al_5star_push=0,
        al_spike_email=0, al_spike_sms=1, al_spike_push=0,
        al_unres_email=0, al_unres_sms=1, al_unres_push=0,
    ), db_path=db_path)

    r = get_restaurant(rid, db_path=db_path)

    assert r.alert_1star == 0 and r.alert_2star == 1 and r.alert_health == 0
    assert r.alert_neg_spike == 0 and r.alert_negative_trend == 0
    assert r.alert_no_response == 1 and r.alert_5star == 1
    assert r.alert_rating_threshold == 1 and r.alert_rating_floor == 3.5
    assert r.alert_labor_over == 1 and r.alert_any_review == 1 and r.alert_resp_approved == 1
    assert r.urgent_via_email == 0 and r.urgent_via_sms == 1
    assert r.al_health_email == 0 and r.al_health_sms == 1 and r.al_health_push == 0
    assert r.al_1star_email == 0 and r.al_1star_sms == 1 and r.al_1star_push == 0
    assert r.al_2star_email == 0 and r.al_2star_sms == 1 and r.al_2star_push == 0
    assert r.al_5star_email == 1 and r.al_5star_sms == 1 and r.al_5star_push == 0
    assert r.al_spike_email == 0 and r.al_spike_sms == 1 and r.al_spike_push == 0
    assert r.al_unres_email == 0 and r.al_unres_sms == 1 and r.al_unres_push == 0


def test_alert_config_defaults_still_apply_when_not_specified(db_path):
    """The fix must not break the common case: a plain Restaurant() with
    no alert kwargs should still get the dataclass's own sensible
    defaults (declared in the same block, models.py:~409-441), not NULL
    or some other value introduced by making the INSERT explicit."""
    rid = create_restaurant(Restaurant(name="Defaults Co", owner_email="def@x.com"), db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert r.alert_1star == 1 and r.alert_health == 1
    assert r.urgent_via_email == 1 and r.urgent_via_sms == 0
    assert r.al_health_push == 1 and r.al_1star_sms == 0


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


# ── the four touch points, enforced ──────────────────────────────────────────

def test_every_alert_preference_column_is_writable_through_update_restaurant():
    """A restaurants column needs four touch points (dataclass, migration,
    whitelist, hydration). al_3star_email/sms/push had three: they were in
    the DDL, the dataclass and get_restaurant, but not in update_restaurant's
    `allowed`, so a write silently no-op'd. Asserted against the source so
    it holds for every al_*/alert_* field, not one fixture."""
    import dataclasses, inspect
    src = inspect.getsource(update_restaurant)
    missing = [f.name for f in dataclasses.fields(Restaurant)
               if (f.name.startswith("al_") or f.name.startswith("alert_")) and f'"{f.name}"' not in src]
    assert missing == [], missing


def test_three_star_alert_preferences_round_trip(db_path):
    rid = create_restaurant(Restaurant(name="Three Star Co", owner_email="ts@x.com"), db_path=db_path)
    update_restaurant(rid, {"al_3star_email": 0, "al_3star_sms": 1, "al_3star_push": 0}, db_path=db_path)
    r = get_restaurant(rid, db_path=db_path)
    assert (r.al_3star_email, r.al_3star_sms, r.al_3star_push) == (0, 1, 0)


def test_a_direct_restaurants_write_drops_the_request_memo(db_path):
    """get_restaurant is memoised per Flask request. update_restaurant
    invalidates; the direct UPDATE sites (last_fetched_at, deletion request,
    organisation, activity) must too, or a read after the write in the same
    request returns the row from before it."""
    import models
    from flask import Flask
    rid = create_restaurant(Restaurant(name="Memo Co", owner_email="memo@x.com"), db_path=db_path)
    with Flask(__name__).app_context():
        before = get_restaurant(rid, db_path=db_path)
        assert before.last_fetched_at is None
        assert get_restaurant(rid, db_path=db_path) is before          # memoised
        models.update_last_fetched(rid, db_path=db_path)
        after = get_restaurant(rid, db_path=db_path)
        assert after is not before and after.last_fetched_at
        models.log_activity(rid, "labor", db_path=db_path)
        assert get_restaurant(rid, db_path=db_path).last_active_tab == "labor"
