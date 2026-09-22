"""Owner-set closed days (SCHED-1): saved beside the rules, kept when the
rules are saved again, read into the week's constraints and the prompt."""
import pytest

import models
import schedule_rules as sr
from models import create_restaurant, Restaurant


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return create_restaurant(Restaurant(name="Closed Mondays", owner_email="c@x.com"), db_path=db_path)


WEEK = ["2026-12-21", "2026-12-22", "2026-12-23", "2026-12-24", "2026-12-25", "2026-12-26", "2026-12-27"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def test_closed_weekdays_and_dates_reach_the_constraints_and_the_prompt(rid, db_path):
    sr.save_closures(rid, closed_weekdays=["monday"], closed_dates=["2026-12-25", "not a date"], db_path=db_path)
    c = sr.build_constraints(rid, WEEK, DAYS, db_path=db_path)
    assert c.closed_dates == {"2026-12-21", "2026-12-25"}
    block = sr.prompt_block(c)
    assert "CLOSED on Monday 12/21, Friday 12/25" in block


def test_saving_the_rules_keeps_the_closures(rid, db_path):
    sr.save_closures(rid, closed_weekdays=["Tuesday"], db_path=db_path)
    sr.save_compliance(rid, {"min_rest_hours": 9}, db_path=db_path)
    assert sr.closures(models.get_restaurant(rid, db_path))["closed_weekdays"] == ["Tuesday"]
    assert sr.compliance(models.get_restaurant(rid, db_path))["min_rest_hours"] == 9


def test_closed_every_day_is_refused(rid, db_path):
    with pytest.raises(ValueError):
        sr.save_closures(rid, closed_weekdays=DAYS, db_path=db_path)
