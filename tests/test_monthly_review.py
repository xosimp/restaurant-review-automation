"""The month read like a P&L, not counted (workflow audit #12)."""
from datetime import date, timedelta

import pytest

import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import monthly_review, metrics, outcomes, goals, business_intelligence
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, monthly_review, metrics, outcomes, goals, business_intelligence):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Month Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("module_labor", 1)
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _day(db_path, rid, d, sales, labor_cost):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, "
                 "labor_pct) VALUES (?,?,?,?,?,?)",
                 (rid, d.isoformat(), d.strftime("%A"), sales, labor_cost, labor_cost / sales * 100))
    conn.commit(); conn.close()


def _month(db_path, rid, year, month, sales, labor_cost, days=28):
    for i in range(days):
        _day(db_path, rid, date(year, month, i + 1), sales, labor_cost)


def test_the_month_is_compared_with_the_one_before(db_path):
    import monthly_review
    rid = _rid(db_path)
    _month(db_path, rid, 2026, 7, sales=4000, labor_cost=1200)     # 30%
    _month(db_path, rid, 2026, 8, sales=4000, labor_cost=1400)     # 35%
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    assert review["month"] == "August 2026" and review["compared_with"] == "July"
    labour = next(m for m in review["metrics"] if m["key"] == "labor_pct")
    assert labour["value"] == 35.0 and labour["previous"] == 30.0
    assert labour["verdict"] == "worsened"
    assert "Labour %" in monthly_review.headline(review)


def test_an_unmeasurable_month_says_so_instead_of_reporting_no_change(db_path):
    import monthly_review
    rid = _rid(db_path)
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    labour = next(m for m in review["metrics"] if m["key"] == "labor_pct")
    assert labour["value"] is None and labour["verdict"] == "unknown"
    assert "not measurable" in " ".join(monthly_review.lines(review))
    assert "Not enough data" in monthly_review.headline(review)


def test_a_first_month_is_not_compared_with_nothing(db_path):
    import monthly_review
    rid = _rid(db_path)
    _month(db_path, rid, 2026, 8, sales=4000, labor_cost=1200)
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    text = " ".join(monthly_review.lines(review))
    assert "no July figure to compare with" in text


def test_a_steady_month_is_called_steady(db_path):
    import monthly_review
    rid = _rid(db_path)
    _month(db_path, rid, 2026, 7, sales=4000, labor_cost=1200)
    _month(db_path, rid, 2026, 8, sales=4000, labor_cost=1204)
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    assert "steady month" in monthly_review.headline(review)


def test_modules_the_restaurant_does_not_have_are_left_out(db_path):
    import monthly_review
    rid = _rid(db_path, module_inventory=0, module_reviews=0)
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    assert {m["key"] for m in review["metrics"]} == {"sales", "labor_pct"}


def test_the_email_carries_the_review_and_survives_a_broken_block(db_path, monkeypatch):
    import emails, monthly_review
    rid = _rid(db_path)
    _month(db_path, rid, 2026, 7, sales=4000, labor_cost=1200)
    _month(db_path, rid, 2026, 8, sales=4400, labor_cost=1200)
    import goals
    monkeypatch.setattr(goals, "progress", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    html = "".join(emails._monthly_review_sections(rid))
    assert "The month against July" in html and "Sales per day" in html
