"""notify.py's push wiring: the new al_*_push columns feeding into
fire_review_alerts()'s blast() and check_no_response_alerts(), and the
design decision that push (unlike SMS/email) has no global on/off switch —
a restaurant that's turned off urgent_via_sms and urgent_via_email entirely
must still get push if the per-type toggle is on."""
import pytest

import notify
from models import (
    create_restaurant, Restaurant, Review, save_reviews, get_conn, update_restaurant,
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # Guards _send_alert_email() into a no-op print instead of a real Resend call.
    monkeypatch.setattr(notify, "RESEND_API_KEY", "")


def _restaurant(db_path, **kw):
    return create_restaurant(
        Restaurant(name=kw.pop("name", "Push Alert Co"), owner_email="p@x.com", **kw), db_path=db_path
    )


def _review(rid, **kw):
    return Review(
        restaurant_id=rid, platform="google", external_id=kw.pop("external_id", "r1"),
        author="Ann", rating=kw.pop("rating", 1), text=kw.pop("text", "Bad experience."),
        id=1, **kw,
    )


def test_fire_review_alerts_calls_push_for_1star_when_enabled(db_path, monkeypatch):
    rid = _restaurant(db_path)
    calls = []
    monkeypatch.setattr("push.fire_push", lambda *a, **kw: calls.append(a))

    notify.fire_review_alerts(rid, "Push Alert Co", [_review(rid, rating=1)], db_path=db_path)

    assert len(calls) == 1
    assert calls[0][1] == "1star"  # fire_push(restaurant_id, alert_type, ...)


def test_fire_review_alerts_skips_push_when_type_disabled(db_path, monkeypatch):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"al_1star_push": 0}, db_path=db_path)
    calls = []
    monkeypatch.setattr("push.fire_push", lambda *a, **kw: calls.append(a))

    notify.fire_review_alerts(rid, "Push Alert Co", [_review(rid, rating=1)], db_path=db_path)

    assert calls == []


def test_fire_review_alerts_still_pushes_with_sms_and_email_globally_off(db_path, monkeypatch):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"urgent_via_sms": 0, "urgent_via_email": 0}, db_path=db_path)
    calls = []
    monkeypatch.setattr("push.fire_push", lambda *a, **kw: calls.append(a))

    notify.fire_review_alerts(rid, "Push Alert Co", [_review(rid, rating=1)], db_path=db_path)

    assert len(calls) == 1


def test_fire_review_alerts_no_push_when_everything_off(db_path, monkeypatch):
    rid = _restaurant(db_path)
    update_restaurant(rid, {
        "urgent_via_sms": 0, "urgent_via_email": 0, "al_1star_push": 0,
    }, db_path=db_path)
    calls = []
    monkeypatch.setattr("push.fire_push", lambda *a, **kw: calls.append(a))

    notify.fire_review_alerts(rid, "Push Alert Co", [_review(rid, rating=1)], db_path=db_path)

    assert calls == []


def test_check_no_response_alerts_fires_push_with_sms_and_email_off(db_path, monkeypatch):
    rid = _restaurant(db_path)
    update_restaurant(rid, {"urgent_via_sms": 0, "urgent_via_email": 0, "alert_no_response": 1}, db_path=db_path)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="old1",
                          author="Bob", rating=1, text="Terrible.")], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute(
        "UPDATE reviews SET sentiment='negative', fetched_at=datetime('now','-3 days') WHERE restaurant_id=?",
        (rid,)
    )
    conn.commit()
    conn.close()

    calls = []
    monkeypatch.setattr("push.fire_push", lambda *a, **kw: calls.append(a))
    notify.check_no_response_alerts(db_path=db_path)

    assert len(calls) == 1
    assert calls[0][1] == "no_response"
