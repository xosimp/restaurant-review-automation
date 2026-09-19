"""The morning surface (workflow audit #2, #3, #6, #13, #14).

The brief is the one thing an owner reads before service, so everything the
product already knows that changes what they do today has to be in it — and
the same lines have to be what Home and Ask open with.
"""
from datetime import date, datetime, timedelta

import pytest

import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import morning_brief, demand, issues, outcomes, goals, metrics, reporter
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, morning_brief, demand, issues, outcomes, goals, metrics):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Morning Co")
    kw.setdefault("owner_email", "o@x.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _lines(rid, db_path, **kw):
    import morning_brief
    r = models.get_restaurant(rid, db_path=db_path)
    return {l["key"]: l for l in morning_brief.build(rid, restaurant=r, db_path=db_path, **kw)["lines"]}


def _review(db_path, rid, rating, status="drafted"):
    import uuid
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                 "review_date, fetched_at, processed, response_status) "
                 "VALUES (?,'google',?,'A',?,'text',date('now'),datetime('now'),1,?)",
                 (rid, uuid.uuid4().hex[:12], rating, status))
    conn.commit(); conn.close()


def test_reviews_waiting_on_a_reply_are_in_the_brief(db_path):
    rid = _rid(db_path, module_reviews=1)
    _review(db_path, rid, 2)
    _review(db_path, rid, 5)
    _review(db_path, rid, 4, status="posted")
    line = _lines(rid, db_path)["reviews"]
    assert "2 reviews waiting" in line["text"] and "1 of them 2 stars or worse" in line["text"]
    assert line["ask"]


def test_what_the_kitchen_is_about_to_run_out_of_is_in_the_brief(db_path, monkeypatch):
    import morning_brief
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(morning_brief, "_critical_low",
                        lambda rid_: ["Chicken Breast", "Romaine", "Mozzarella", "Basil"])
    line = _lines(rid, db_path)["stock"]
    assert "Chicken Breast, Romaine, Mozzarella and 1 more" in line["text"]


def test_sample_inventory_never_tells_the_owner_to_order(db_path):
    """_critical_low reads live data only — sample figures order nothing."""
    import morning_brief
    rid = _rid(db_path, module_inventory=1)
    assert morning_brief._critical_low(rid) == []
    assert "stock" not in _lines(rid, db_path)


def test_next_weeks_schedule_is_chased_from_thursday(db_path, monkeypatch):
    import morning_brief
    rid = _rid(db_path, module_labor=1)
    monkeypatch.setattr(morning_brief, "_schedule_drafted_recently", lambda *a, **k: False)
    thursday, tuesday = date(2026, 9, 24), date(2026, 9, 22)
    assert "schedule" in _lines(rid, db_path, today=thursday)
    assert "schedule" not in _lines(rid, db_path, today=tuesday), "not on a Tuesday"
    monkeypatch.setattr(morning_brief, "_schedule_drafted_recently", lambda *a, **k: True)
    assert "schedule" not in _lines(rid, db_path, today=thursday), "already built"


def test_the_day_line_carries_the_weather_and_the_calendar(db_path, monkeypatch):
    import morning_brief, weather, marketing
    rid = _rid(db_path, module_labor=1)
    monkeypatch.setattr(weather, "get_forecast_for_week", lambda r, days: [
        {"short_forecast": "Sunny", "high_f": 88, "precip_pct": 0}])
    monkeypatch.setattr(marketing, "get_upcoming_holidays", lambda when: "Labor Day (Sep 07)")
    text = _lines(rid, db_path, today=date(2026, 9, 7))["today"]["text"]
    assert "88° and sunny" in text and "Labor Day" in text


def test_a_slow_weekday_is_named_with_a_way_to_act_on_it(db_path, monkeypatch):
    import demand
    rid = _rid(db_path, module_labor=1)
    monkeypatch.setattr(demand, "slow_days", lambda *a, **k: {
        "available": True, "slow_days": [{"day": "Tuesday", "vs_average_pct": -22, "samples": 8}]})
    line = _lines(rid, db_path)["slow_day"]
    assert "Tuesdays run about 22% under" in line["text"] and "Tuesday" in line["ask"]


def test_a_manager_without_food_cost_sees_neither_stock_nor_prices(db_path, monkeypatch):
    import morning_brief
    rid = _rid(db_path, module_inventory=1, module_reviews=1)
    monkeypatch.setattr(morning_brief, "_critical_low", lambda rid_: ["Chicken Breast"])
    mgr = {"id": 5, "role": "manager", "is_admin": 0, "grants": frozenset()}
    assert "stock" not in _lines(rid, db_path, viewer=mgr)
    assert "stock" in _lines(rid, db_path)


def test_every_email_line_offers_to_be_asked_about(db_path):
    import morning_brief
    brief = {"date": "2026-09-21", "lines": [
        {"key": "yesterday", "text": "Yesterday: $4,100", "tone": "good",
         "ask": "Why was yesterday strong?"}]}
    html = morning_brief._email_html(brief, "Simple EJ's")
    assert "Ask about this" in html
    assert "?ask=Why%20was%20yesterday%20strong%3F" in html


def test_the_web_answers_an_ask_link():
    """The email's link lands on Home and asks the question once."""
    page = open("templates/dashboard.html").read()
    assert "hbAskFromUrl" in page and "[?&]ask=" in page
    assert "replaceState" in page, "the question must not re-fire on refresh"
