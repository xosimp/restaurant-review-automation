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
    labor = next(m for m in review["metrics"] if m["key"] == "labor_pct")
    assert labor["value"] == 35.0 and labor["previous"] == 30.0
    assert labor["verdict"] == "worsened"
    assert "Labor %" in monthly_review.headline(review)


def test_an_unmeasurable_month_says_so_instead_of_reporting_no_change(db_path):
    import monthly_review
    rid = _rid(db_path)
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    labor = next(m for m in review["metrics"] if m["key"] == "labor_pct")
    assert labor["value"] is None and labor["verdict"] == "unknown"
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


# ── year over year, and the quarter ──────────────────────────────────────────

def test_the_month_is_also_set_against_the_same_month_last_year(db_path):
    import monthly_review
    rid = _rid(db_path)
    _month(db_path, rid, 2025, 8, sales=4000, labor_cost=1400)     # 35% a year ago
    _month(db_path, rid, 2026, 7, sales=4000, labor_cost=1200)     # 30%
    _month(db_path, rid, 2026, 8, sales=4000, labor_cost=1200)     # 30% — steady MoM, better YoY
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    labor = next(m for m in review["metrics"] if m["key"] == "labor_pct")
    assert labor["year_ago"] == 35.0 and labor["yoy"]["verdict"] == "improved"
    clause = monthly_review.yoy_clause(labor)
    assert "last year" in clause and "under" in clause
    assert any("last year" in ln for ln in monthly_review.lines(review))


def test_without_a_year_of_history_there_is_no_yoy_clause(db_path):
    import monthly_review
    rid = _rid(db_path)
    _month(db_path, rid, 2026, 7, sales=4000, labor_cost=1200)
    _month(db_path, rid, 2026, 8, sales=4000, labor_cost=1300)
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path)
    labor = next(m for m in review["metrics"] if m["key"] == "labor_pct")
    assert labor["year_ago"] is None and labor["yoy"] is None
    assert monthly_review.yoy_clause(labor) == ""


def test_a_quarter_is_three_months_against_the_three_before(db_path):
    import monthly_review
    rid = _rid(db_path)
    for m in (3, 4, 5):
        _month(db_path, rid, 2026, m, sales=4000, labor_cost=1200)
    for m in (6, 7, 8):
        _month(db_path, rid, 2026, m, sales=4000, labor_cost=1400)
    review = monthly_review.build(rid, today=date(2026, 9, 1), db_path=db_path, months=3)
    assert review["months"] == 3
    assert review["month"] == "June–August 2026" and review["compared_with"] == "March–May"
    labor = next(m for m in review["metrics"] if m["key"] == "labor_pct")
    assert labor["value"] == 35.0 and labor["previous"] == 30.0 and labor["verdict"] == "worsened"
    assert any("last quarter" in ln for ln in monthly_review.lines(review))


def test_the_quarterly_email_is_the_same_build_over_three_months(db_path, monkeypatch):
    import emails
    rid = _rid(db_path)
    for m in (3, 4, 5):
        _month(db_path, rid, 2026, m, sales=4000, labor_cost=1200)
    for m in (6, 7, 8):
        _month(db_path, rid, 2026, m, sales=4400, labor_cost=1200)
    sent = {}
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(emails, "deliver", lambda **kw: sent.update(kw) or True)
    import monthly_review
    real_build = monthly_review.build
    monkeypatch.setattr(monthly_review, "build",
                        lambda rid_, **kw: real_build(rid_, today=date(2026, 9, 1), db_path=db_path, **kw))
    emails.send_quarterly_summary_email("o@x.test", "Month Co", restaurant_id=rid)
    assert sent["email_type"] == "send_quarterly_summary"
    html = sent["payload"]["html"]
    assert "Quarterly review" in html and "June–August 2026" in sent["payload"]["subject"]
    assert "Sales per day" in html


def test_the_quarterly_job_skips_a_paused_account_and_respects_the_monthly_switch(monkeypatch):
    import scheduler

    class _R:
        def __init__(self, status, enabled=1):
            self.id = 1; self.name = "R"; self.owner_email = "o@x.test"; self.owner_name = "O"
            self.billing_status = status; self.monthly_review_enabled = enabled
    calls = []
    monkeypatch.setattr(models, "get_all_restaurants",
                        lambda *a, **k: [_R("active"), _R("paused"), _R("active", enabled=0)])
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    import emails
    monkeypatch.setattr(emails, "send_quarterly_summary_email", lambda **kw: calls.append(kw))
    out = scheduler.run_quarterly_summaries()
    assert out == {"sent": 1, "skipped": 2, "failed": 0}


def test_the_quarterly_slot_fires_on_the_first_of_a_quarter_only():
    """Pinned at the source: the slot is the only place the quarter is defined."""
    import inspect, scheduler
    src = inspect.getsource(scheduler.scheduler_loop)
    assert "today.day == 1 and today.month in (1, 4, 7, 10)" in src
    assert 'claim_period("quarterly_summary"' in src
