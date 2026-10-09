"""The weekly budget, planned (owner, 10/9/26): the owner's figures beside
Cavnar AI's suggestion, by day or by the week (dsr/budget_plan.py).

The rules it keeps: a suggestion is never saved as a budget; every
suggestion says what it rests on and a measured confidence % (None below
the floor, never a word); a night with nothing to go on is blank and says
why, never 0; weekly mode splits one figure by the restaurant's own weekday
mix so each night's report still reads "vs budget"."""
from datetime import date

import pytest

from dsr import budget_plan as bp
from dsr import store
from models import Restaurant, create_restaurant

MON = date(2026, 10, 12)
TODAY = date(2026, 10, 9)
MIX = {"Monday": 0.10, "Tuesday": 0.10, "Wednesday": 0.12, "Thursday": 0.14, "Friday": 0.20,
       "Saturday": 0.22, "Sunday": 0.12}


@pytest.fixture
def rid(db_path):
    return create_restaurant(Restaurant(name="Budget Grill", owner_email="b@x.test", timezone="America/Chicago"),
                             db_path=db_path)


def test_a_week_figure_splits_by_the_weekday_mix_and_adds_back_to_the_total():
    out = bp.split_week(10000, bp.week_dates(MON), MIX)
    assert out["2026-10-17"] == 2200 and out["2026-10-12"] == 1000
    assert round(sum(out.values()), 2) == 10000
    # A closed weekday (no share) gets nothing; the rest still add up.
    closed = dict(MIX, Monday=0.0)
    out = bp.split_week(9000, bp.week_dates(MON), closed)
    assert "2026-10-12" not in out and round(sum(out.values()), 2) == 9000
    assert bp.split_week(None, bp.week_dates(MON), MIX) == {}


def test_the_week_view_puts_each_saved_budget_beside_its_suggestion_and_never_saves_one(rid, db_path, monkeypatch):
    store.set_budget(rid, MON, gross=1200, net=1100, db_path=db_path)
    monkeypatch.setattr(bp, "gross_ratio", lambda *a, **k: (1.08, 20))
    monkeypatch.setattr(bp, "forecast_accuracy", lambda *a, **k: (86, 30))
    monkeypatch.setattr(bp, "weekday_mix", lambda *a, **k: (MIX, "the mix"))

    def fake_forecast(r, day, db_path=None):
        if day.strftime("%A") == "Tuesday":
            return {"available": False, "reason": "only 2 past Tuesdays on file (needs 4)"}
        return {"available": True, "typical_sales": 4321.0, "samples": 8, "low": 3900, "high": 4800}
    import demand
    monkeypatch.setattr(demand, "forecast_net", fake_forecast)
    v = bp.week_view(rid, MON, today=TODAY, db_path=db_path)
    mon, tue = v["days"][0], v["days"][1]
    assert mon["budget_net"] == 1100 and mon["suggest"]["net"] == 4320 and mon["suggest"]["gross"] == round(4320 * 1.08, -1)
    assert mon["suggest"]["confidence_pct"] == 86 and "the last 8 Mondays" in mon["suggest"]["basis"]
    assert tue["suggest"] is None and "2 past Tuesdays" in tue["why_none"]          # blank and says why, never 0
    assert v["budget"]["net"] == 1100 and v["suggest"]["net"] == 4320 * 6
    # Nothing was written for the suggested nights.
    assert set(store.budgets_for(rid, MON, date(2026, 10, 18), db_path=db_path)) == {"2026-10-12"}


def test_no_confidence_figure_below_the_floor(rid, db_path):
    pct, n = bp.forecast_accuracy(rid, TODAY, db_path)
    assert pct is None and n < bp.ACCURACY_MIN_NIGHTS


def test_weekly_save_writes_every_night_and_a_blank_figure_clears_it(rid, db_path, monkeypatch):
    monkeypatch.setattr(bp, "weekday_mix", lambda *a, **k: (MIX, "the mix"))
    saved = bp.save_week(rid, MON, net=10000, gross=None, today=TODAY, db_path=db_path)
    got = store.budgets_for(rid, MON, date(2026, 10, 18), db_path=db_path)
    assert round(sum(x["net"] for x in got.values()), 2) == 10000 and len(saved) == 7
    assert all(x.get("gross") is None for x in got.values())


def test_weekly_save_refuses_without_a_pattern_to_split_by(rid, db_path, monkeypatch):
    monkeypatch.setattr(bp, "weekday_mix", lambda *a, **k: ({}, "no weeks"))
    monkeypatch.setattr(bp, "week_view", lambda *a, **k: {"mix": {}})
    with pytest.raises(ValueError):
        bp.save_week(rid, MON, net=10000, today=TODAY, db_path=db_path)


def test_the_mode_reads_day_until_the_owner_picks_week(rid, db_path):
    from models import update_restaurant
    assert bp.mode_of(rid, db_path) == "day"
    update_restaurant(rid, {"dsr_budget_mode": "week"}, db_path=db_path)
    assert bp.mode_of(rid, db_path) == "week"
