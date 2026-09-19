"""Engagement: the product could say how many notifications it SENT and
nothing about whether any of them were worth sending.

The one design rule here: this never suppresses anything on its own.
Reading a banner on a lock screen IS engagement and leaves no tap behind,
so inferring "they ignore these" from taps alone would quietly switch off
alerts someone reads every day. It produces a SUGGESTION.
"""
import pytest

import models
import notify


@pytest.fixture
def rid(db_path):
    return models.create_restaurant(
        models.Restaurant(name="Simple EJ's", owner_email="erik@x.test"), db_path=db_path)


def _sent(db_path, rid, alert_type, n, days_ago=1):
    conn = models.get_conn(db_path)
    for _ in range(n):
        conn.execute(
            "INSERT INTO alert_log (restaurant_id, alert_type, fired_at) "
            "VALUES (?,?, datetime('now', ?))", (rid, alert_type, f"-{days_ago} days"))
    conn.commit()
    conn.close()


def test_opens_are_counted_against_sends(db_path, rid):
    _sent(db_path, rid, "5star", 12)
    models.record_notification_open(rid, "5star", user_id=1, db_path=db_path)
    models.record_notification_open(rid, "5star", user_id=1, db_path=db_path)

    rows = {r["alert_type"]: r for r in models.notification_engagement(rid, db_path=db_path)}

    assert rows["5star"]["delivered"] == 12
    assert rows["5star"]["opened"] == 2


def test_a_type_nobody_opens_becomes_a_suggestion(db_path, rid):
    _sent(db_path, rid, "5star", 34)

    suggestions = notify.engagement_report(rid, db_path=db_path)

    assert len(suggestions) == 1
    assert suggestions[0]["alert_type"] == "5star"
    assert suggestions[0]["delivered"] == 34
    # It has to name the switch the owner would flip, or it is an
    # observation rather than something they can act on.
    assert suggestions[0]["push_column"] == "al_5star_push"


def test_one_open_is_enough_to_say_nothing(db_path, rid):
    _sent(db_path, rid, "5star", 34)
    models.record_notification_open(rid, "5star", db_path=db_path)
    assert notify.engagement_report(rid, db_path=db_path) == []


def test_thin_evidence_makes_no_suggestion(db_path, rid):
    """A suggestion made on three notifications is worse than none."""
    _sent(db_path, rid, "5star", notify.ENGAGEMENT_MIN_DELIVERED - 1)
    assert notify.engagement_report(rid, db_path=db_path) == []


def test_a_health_alert_is_never_suggested_away(db_path, rid):
    """An owner who hasn't opened a health alert in sixty days has had a
    good sixty days."""
    _sent(db_path, rid, "health", 40)
    assert notify.engagement_report(rid, db_path=db_path) == []


def test_the_morning_brief_is_never_suggested_away(db_path, rid):
    _sent(db_path, rid, "morning_brief", 55)
    assert notify.engagement_report(rid, db_path=db_path) == []


def test_old_sends_fall_out_of_the_window(db_path, rid):
    _sent(db_path, rid, "5star", 40, days_ago=notify.ENGAGEMENT_WINDOW_DAYS + 5)
    assert notify.engagement_report(rid, db_path=db_path) == []


def test_engagement_is_scoped_to_one_restaurant(db_path, rid):
    other = models.create_restaurant(
        models.Restaurant(name="Somebody Else", owner_email="x@y.test"), db_path=db_path)
    _sent(db_path, rid, "5star", 30)
    _sent(db_path, other, "1star", 30)

    types = {r["alert_type"] for r in models.notification_engagement(rid, db_path=db_path)}
    assert types == {"5star"}
