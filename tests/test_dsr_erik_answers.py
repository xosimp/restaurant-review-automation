"""Erik's answers (Simple EJ's, 9/24/26) — the DSR set up the way his sheet
and his Back Office are.

  "It's a mixture of 4 and 5 week periods. It must line up with Back Office.
   Also our weeks start on Wednesday."           → 4-4-5 (and 4-5-4, 5-4-4,
                                                   or the exact lengths)
  "Gross sales — everything included. Net sales = gross − comps, voids, tax
   etc."                                          → dsr_gross_basis "all"
  "I want to set budget daily."                   → dsr_budgets is per day
  "Influence = what was going on, Cubs, Bears, World Cup, event."
"""
import sys
from datetime import date
from types import SimpleNamespace as NS

import pytest

import auth
import closeout
import models
import pos
import dsr
from dsr import block_sales, fiscal, rollup, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


EJS = NS(fiscal_week_start_dow=2, fiscal_year_start="2025-12-31", fiscal_period_scheme="445")


@pytest.mark.parametrize("day, label", [
    ("2025-12-31", "Period 1 · Week 1"),
    ("2026-02-25", "Period 3 · Week 1"),       # the first 5-week period
    ("2026-08-26", "Period 9 · Week 1"),       # the date on Erik's sheet
    ("2026-09-22", "Period 9 · Week 4"),
    ("2026-09-29", "Period 9 · Week 5"),       # period 9 is a 5-week one
    ("2026-09-30", "Period 10 · Week 1"),
    ("2026-12-30", "Period 1 · Week 1"),       # the next fiscal year
])
def test_a_4_4_5_year_from_wednesday_12_31_25_matches_his_sheet(day, label):
    assert fiscal.label(EJS, day) == label


def test_the_fiscal_year_is_named_for_the_year_that_holds_most_of_it():
    assert fiscal.position(EJS, "2025-12-31")["fiscal_year"] == 2026
    assert fiscal.position(EJS, "2026-12-30")["fiscal_year"] == 2027
    older = NS(fiscal_week_start_dow=2, fiscal_year_start="2026-01-14", fiscal_period_scheme="4x13")
    assert fiscal.position(older, "2026-08-26")["fiscal_year"] == 2026


def test_patterns_and_exact_lengths():
    assert fiscal.period_lengths("454")[:3] == [4, 5, 4] and fiscal.period_lengths("544")[:3] == [5, 4, 4]
    exact = "4,4,5,4,4,5,4,4,5,4,4,6"
    assert fiscal.period_lengths(exact) is None                    # 6-week periods aren't a thing
    assert fiscal.period_lengths("4,4,5,4,4,5,4,4,5,4,4,5") == [4, 4, 5] * 4
    assert fiscal.period_lengths("4,4,5,4,4,5,4,4,5,4,4,5,1") is None
    assert fiscal.period_lengths("4,4,5,4,4,5,4,4,5,4,5,5") == [4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 5, 5]   # a 53-week year
    assert fiscal.period_lengths("nonsense") is None and not fiscal.valid_scheme("")


def test_period_nine_runs_five_weeks_in_the_rollup():
    assert rollup.period_bounds(EJS, date(2026, 9, 16)) == (date(2026, 8, 26), date(2026, 9, 29))


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


DAY_DATA = {"gross": 7415.0, "net": 6975.0, "transactions": 212, "guests": 288, "discounts": 260.0,
            "comps": 180.0, "voids": 95.0, "refunds": 0.0, "tax": 589.0, "by_department": {"FOOD": 6975.0},
            "by_hour": {"19": 6975.0}, "items": [], "net_deductions": ["discounts", "comps"], "source_checks": {}}


def _block(db, basis, **data):
    rid = create_restaurant(Restaurant(name="EJ's", owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"dsr_gross_basis": basis}, db_path=db)
    r = get_restaurant(rid, db_path=db)
    assert r.dsr_gross_basis == basis                          # through the whitelist and back
    ctx = dsr.Context(r, date(2026, 9, 22), db_path=db)
    return block_sales._ready(ctx, dict(DAY_DATA, **data), "rpower", "pos")


def test_everything_included_gross_adds_tax_and_voids_and_net_is_unchanged(db):
    b = _block(db, "all")
    m = b["metrics"]
    assert m["gross"] == 7415.0 + 589.0 + 95.0 and m["gross_items"] == 7415.0 and m["net"] == 6975.0
    assert b["detail"]["definition"]["gross_basis"] == "all"
    assert "tax" in b["detail"]["definition"]["gross"] and "voids" in b["detail"]["definition"]["net"]


def test_items_gross_is_unchanged_by_default(db):
    m = _block(db, "items")["metrics"]
    assert m["gross"] == 7415.0 == m["gross_items"]


def test_an_everything_gross_without_the_tax_is_not_measured(db):
    b = _block(db, "all", tax=None)
    assert b["metrics"]["gross"] is None and b["metrics"]["net"] == 6975.0
    assert b["detail"]["definition"]["gross_missing"] == ["tax"]


def test_a_new_restaurant_defaults_to_items(db):
    rid = create_restaurant(Restaurant(name="New", owner_email="n@x.com"), db_path=db)
    assert get_restaurant(rid, db_path=db).dsr_gross_basis == "items"


def test_the_settings_route_takes_the_calendar_and_the_gross_basis(db, monkeypatch):
    from flask import Flask
    from strategy_routes import strategy_bp
    rid = create_restaurant(Restaurant(name="EJ's", owner_email="e@x.com"), db_path=db)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "role": "client",
                                                           "is_admin": 0, "username": "o", "email": "o@x"})
    app = Flask(__name__)
    app.register_blueprint(strategy_bp)
    c = app.test_client()
    ok = c.post("/api/dsr/settings", json={"fiscal_week_start_dow": 2, "fiscal_year_start": "2025-12-31",
                                           "fiscal_period_scheme": "4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 4, 5",
                                           "dsr_gross_basis": "all"})
    assert ok.status_code == 200, ok.get_json()
    r = get_restaurant(rid, db_path=db)
    assert (r.fiscal_period_scheme, r.dsr_gross_basis) == ("4,4,5,4,4,5,4,4,5,4,4,5", "all")
    assert c.get("/api/dsr/settings").get_json()["settings"]["dsr_gross_basis"] == "all"
    assert c.post("/api/dsr/settings", json={"fiscal_period_scheme": "4,4,6"}).status_code == 400
    assert c.post("/api/dsr/settings", json={"dsr_gross_basis": "most"}).status_code == 400
    assert c.post("/api/dsr/settings", json={"fiscal_period_scheme": "454"}).status_code == 200


def test_influence_is_worded_the_way_erik_uses_it():
    assert closeout.LABELS["influence"].startswith("Influence — what was going on")
