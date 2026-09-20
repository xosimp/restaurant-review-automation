"""Two notifications the audit found missing, both of which the product had
already computed and never told anyone about.

  closing_summary — the morning brief says how YESTERDAY went. Nothing said
                    how TODAY went, while the owner could still picture the
                    room.
  outcome_achieved — outcomes.evaluate_due has worked out what a change was
                    worth, daily, since outcomes shipped. The owner was
                    never told. It is the only notification in the product
                    about money already made.
"""
from datetime import date, timedelta

import pytest

import intraday
import models
import strategy_jobs


@pytest.fixture
def rid(db_path):
    return models.create_restaurant(
        models.Restaurant(name="Simple EJ's", owner_email="erik@x.test"), db_path=db_path)


def _capture(db_path, rid, day, hour, net, weekday=None):
    conn = models.get_conn(db_path)
    conn.execute(
        "INSERT INTO pos_intraday (restaurant_id, business_date, captured_hour, weekday, "
        "net_sales, provider) VALUES (?,?,?,?,?,?)",
        (rid, day.isoformat(), hour, weekday or day.strftime("%A"), net, "toast"))
    conn.commit()
    conn.close()


# ── closing summary ─────────────────────────────────────────────────────────

def test_a_close_is_measured_against_the_same_weekday(db_path, rid):
    today = date(2026, 9, 18)                      # a Friday
    for weeks_back in (1, 2, 3):
        past = today - timedelta(weeks=weeks_back)
        _capture(db_path, rid, past, 16, 3000)
        _capture(db_path, rid, past, 22, 6000)     # the day's total
    _capture(db_path, rid, today, 22, 7500)

    out = intraday.closing_summary(rid, day=today, db_path=db_path)

    assert out["available"] is True
    assert out["net_sales"] == 7500
    assert out["typical"] == 6000
    assert out["pct"] == 25.0
    assert out["direction"] == "ahead"
    assert out["samples"] == 3


def test_a_close_withholds_the_comparison_until_there_is_one(db_path, rid):
    """'You're 30% down' built on one previous Friday is noise with a
    percentage sign on it."""
    today = date(2026, 9, 18)
    _capture(db_path, rid, today - timedelta(weeks=1), 22, 6000)
    _capture(db_path, rid, today, 22, 4000)

    out = intraday.closing_summary(rid, day=today, db_path=db_path)

    assert out["available"] is False
    assert out["net_sales"] == 4000          # still says what it measured
    assert "1 past Friday" in out["reason"]


def test_a_night_with_nothing_to_say_sends_nothing(db_path, rid):
    """No POS reading and no close-out is a night Cavnar has nothing to say
    about. Sending anyway is how a summary becomes something people turn
    off."""
    title, body = strategy_jobs._closing_text({"available": False}, None)
    assert title is None and body is None


def test_the_close_out_handover_rides_along(db_path, rid):
    summary = {"available": True, "weekday": "Friday", "net_sales": 7500.0,
               "typical": 6000.0, "pct": 25.0, "direction": "ahead", "samples": 3}
    note = {"went_wrong": "Fryer down from 8pm", "eighty_sixed": "Branzino",
            "callouts": "", "went_well": "Patio full"}

    title, body = strategy_jobs._closing_text(summary, note)

    assert "25% ahead of a typical Friday" in title
    assert "$7,500" in title
    assert "Fryer down from 8pm" in body
    assert "Branzino" in body
    assert "Call-outs" not in body           # empty fields drop out


def test_a_handover_alone_is_still_worth_sending(db_path, rid):
    title, body = strategy_jobs._closing_text(
        {"available": False}, {"went_wrong": "Walk-in at 41 degrees"})
    assert title == "Tonight's handover is in"
    assert "Walk-in at 41 degrees" in body


def test_a_normal_night_is_named_as_one(db_path, rid):
    """Under 5% off is a normal night, and saying "3% behind" about one
    teaches an owner the number means nothing."""
    title, _ = strategy_jobs._closing_text(
        {"available": True, "weekday": "Tuesday", "net_sales": 6100.0,
         "typical": 6000.0, "pct": 1.7, "direction": "ahead", "samples": 4}, None)
    assert "about a normal Tuesday" in title


# ── outcome achieved ────────────────────────────────────────────────────────

def _with_a_phone(monkeypatch, pushed):
    """An owner who has the app. _reach pushes to them and emails nobody."""
    monkeypatch.setattr("push.fire_push",
                        lambda rid_, kind, title, body, **k: pushed.append((title, body)))
    monkeypatch.setattr("push.get_device_tokens",
                        lambda *a, **k: [{"user_id": 1, "id": 1}])
    monkeypatch.setattr("morning_brief.recipients",
                        lambda *a, **k: [{"id": 1, "email": "erik@x.test"}])
    monkeypatch.setattr("notify.record_notification", lambda *a, **k: None)


def test_only_a_real_win_is_worth_a_notification(db_path, rid, monkeypatch):
    pushed = []
    _with_a_phone(monkeypatch, pushed)

    told = strategy_jobs._tell_owners_what_worked([
        {"restaurant_id": rid, "verdict": "improved", "dollars_monthly": 40.0,
         "title": "Barely moved", "metric": "food_cost_pct"},
        {"restaurant_id": rid, "verdict": "worsened", "dollars_monthly": -900.0,
         "title": "Went backwards", "metric": "labor_pct"},
    ], db_path)

    assert told == 0 and pushed == []


def test_the_biggest_win_is_the_one_told(db_path, rid, monkeypatch):
    """Five 'this worked' pushes on the same morning is how a win becomes
    noise."""
    pushed = []
    _with_a_phone(monkeypatch, pushed)

    told = strategy_jobs._tell_owners_what_worked([
        {"restaurant_id": rid, "verdict": "improved", "dollars_monthly": 320.0,
         "title": "Reprice the burrata", "metric_label": "Food cost %"},
        {"restaurant_id": rid, "verdict": "improved", "dollars_monthly": 1450.0,
         "title": "Cut Tuesday prep hours", "metric_label": "Labor %"},
    ], db_path)

    assert told == 1
    title, body = pushed[0]
    assert "$1,450/month" in title
    assert "Cut Tuesday prep hours" in body
    # Never claimed as proven cause — the same caveat every outcome surface
    # in the product carries.
    assert "not proven cause" in body


def test_an_owner_without_the_app_is_emailed_instead(db_path, rid, monkeypatch):
    """The win was push-only, so an owner without the app never learned a
    change had paid off. morning_brief.deliver's push-OR-email pattern is
    the one this follows."""
    sent = []
    monkeypatch.setattr("push.fire_push", lambda *a, **k: pytest.fail("should not push"))
    monkeypatch.setattr("push.get_device_tokens", lambda *a, **k: [])
    monkeypatch.setattr("morning_brief.recipients",
                        lambda *a, **k: [{"id": 1, "email": "erik@x.test"}])
    monkeypatch.setattr("notify.record_notification", lambda *a, **k: None)

    class _Ok:
        ok = True
    monkeypatch.setattr("emails.deliver",
                        lambda **kw: sent.append(kw["payload"]) or _Ok())

    told = strategy_jobs._tell_owners_what_worked([
        {"restaurant_id": rid, "verdict": "improved", "dollars_monthly": 900.0,
         "title": "Cut Tuesday prep", "metric_label": "Labor %"}], db_path)

    assert told == 1 and len(sent) == 1
    assert "paid off" in sent[0]["subject"]
    assert sent[0]["preheader"]


def test_a_win_with_nobody_to_tell_is_not_sent(db_path, rid, monkeypatch):
    pushed = []
    monkeypatch.setattr("push.fire_push", lambda *a, **k: pushed.append(a))
    monkeypatch.setattr("push.get_device_tokens", lambda *a, **k: [])
    monkeypatch.setattr("morning_brief.recipients", lambda *a, **k: [])

    told = strategy_jobs._tell_owners_what_worked([
        {"restaurant_id": rid, "verdict": "improved", "dollars_monthly": 900.0,
         "title": "x", "metric_label": "Labor %"}], db_path)

    assert told == 0 and pushed == []
