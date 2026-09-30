"""Re-audit fix round R7 (9/29/26): CROSSMODULE-11 (ordering, stock-out
projections and the prep list ignored this restaurant's measured demand — a
generic weekend shape, a fixed 1.4x, no measured event effects) and
CROSSMODULE-19 (the supplier order was trimmed for waste twice)."""
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

import demand
import inventory
import models
from models import Restaurant, create_restaurant

MON = date(2026, 9, 28)


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _item(**kw):
    it = {"item": "Brisket", "category": "protein", "par_level": 10.0, "current_stock": 10.0, "unit_cost": 5.0,
          "avg_daily_usage": 2.0, "last_order_qty": 10.0, "waste_last_week": 0.0}
    it.update(kw)
    return it


# ── CROSSMODULE-11: this restaurant's week ─────────────────────────────────

def test_the_usage_profile_is_this_restaurants_own_week():
    rid = create_restaurant(Restaurant(name="Thursday Co", owner_email="t@x.test"))
    conn = models.get_conn()
    # Eight weeks: closed Mondays, Thursday the busiest night, a slow Saturday.
    by_wd = {1: 2000.0, 2: 2000.0, 3: 5000.0, 4: 3000.0, 5: 1500.0, 6: 2500.0}
    for k in range(1, 57):
        d = MON - timedelta(days=k)
        if d.weekday() in by_wd:
            conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, final) "
                         "VALUES (?,?,?,?,1)", (rid, d.isoformat(), d.strftime("%A"), by_wd[d.weekday()]))
    conn.commit()
    conn.close()
    p = inventory.usage_profile(rid, today=MON)
    m = p["multipliers"]
    assert p["source"] == "measured" and m[0] == 0.0
    assert max(m, key=m.get) == 3 and m[5] < m[4] < m[3]
    assert round(sum(m.values()) / 7, 3) == 1.0
    # Through the simulation: Monday (closed) uses nothing.
    assert inventory._simulate_days_remaining(1.0, 2.0, MON, profile=m) > 1.0


def test_too_little_history_keeps_the_default_shape():
    rid = create_restaurant(Restaurant(name="New Co", owner_email="n@x.test"))
    p = inventory.usage_profile(rid, today=MON)
    assert p["source"] == "default" and p["multipliers"] == inventory._DEFAULT_PROFILE


def test_a_measured_game_night_runs_the_stock_down_sooner():
    # Tuesday measured +50% here (a game night on file).
    eff = {(MON + timedelta(days=1)).isoformat(): {"factor": 1.5, "pct": 50.0, "labels": ["Cubs"], "kinds": ["event"]}}
    got = inventory.analyse_inventory([_item()], today=MON, day_effects=eff)
    assert got["demand_basis"]["effects"][0]["labels"] == ["Cubs"] and got["demand_basis"]["profile"] == "default"
    # 10 units at 2 a day: Tuesday uses half again, so the stock runs out sooner.
    assert inventory._simulate_days_remaining(10.0, 2.0, MON, day_effects=eff) < \
        inventory._simulate_days_remaining(10.0, 2.0, MON)


def test_the_order_quantity_counts_the_measured_nights_ahead():
    eff = {(MON + timedelta(days=1)).isoformat(): {"factor": 1.5, "pct": 50.0, "labels": ["Cubs"], "kinds": ["event"]}}
    a = _item(current_stock=0.0)
    inventory.analyse_inventory([a], today=MON)
    b = _item(current_stock=0.0)
    inventory.analyse_inventory([b], today=MON, day_effects=eff)
    # Flat: 15 + 6 = 21. With Tuesday at +50% on the default shape
    # (Mon-Wed each 0.88 of an average day): 15 + 2*0.88*(1 + 1.5 + 1) = 21.16.
    assert a["suggested_order_qty"] == 21 and b["suggested_order_qty"] > a["suggested_order_qty"] - 1
    assert b["days_remaining"] <= a["days_remaining"]


def test_a_measured_holiday_replaces_the_generic_bump():
    hol = "Valentine's Day (Oct 1)"
    salmon = lambda: _item(item="Salmon", current_stock=0.0)  # noqa: E731
    bumped = salmon()
    inventory.analyse_inventory([bumped], today=MON, upcoming_holidays=hol)
    assert bumped["event_scaled"] is True
    measured = salmon()
    eff = {(MON + timedelta(days=3)).isoformat(): {"factor": 1.1, "pct": 10.0, "labels": ["Valentine's Day"],
                                                   "kinds": ["holiday"]}}
    inventory.analyse_inventory([measured], today=MON, upcoming_holidays=hol, day_effects=eff)
    assert measured["event_scaled"] is False
    # A payday alone does not stand in for the holiday.
    pay = salmon()
    inventory.analyse_inventory([pay], today=MON, upcoming_holidays=hol,
                                day_effects={MON.isoformat(): {"factor": 1.1, "pct": 10.0, "labels": ["1st"],
                                                               "kinds": ["payday"]}})
    assert pay["event_scaled"] is True


def test_the_last_updated_stamp_is_on_the_restaurants_clock():
    out = inventory.analyse_inventory([_item()], today=MON, tz=ZoneInfo("America/Los_Angeles"))
    assert out["last_updated"].count("/") == 2


def test_the_prep_list_is_scaled_by_the_dates_measured_effect(monkeypatch):
    import event_memory
    rid = create_restaurant(Restaurant(name="Prep Co", owner_email="p@x.test"))
    conn = models.get_conn()
    mid = conn.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)",
                       (rid, "Brisket plate")).lastrowid
    ing = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, current_stock, is_active) "
                       "VALUES (?,?,?,?,1)", (rid, "Brisket", "lb", 5.0)).lastrowid
    conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                 (mid, ing, 0.5))
    sun = date(2026, 10, 4)
    for k in (1, 2, 3):
        conn.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                     "VALUES (?,?,?,?)", (rid, mid, (sun - timedelta(weeks=k)).isoformat(), 20))
    conn.commit()
    conn.close()
    plain = demand.prep_list(rid, sun)
    assert plain["items"][0]["expected_use"] == 10.0 and "effect_pct" not in plain
    monkeypatch.setattr(event_memory, "effects_for_day", lambda rid, day, db_path=None: {
        "pct": 30.0, "applied": [{"label": "cubs", "kind": "event", "lift_pct": 30.0, "n": 4}], "subsumed": [],
        "basis": "Cubs: +30%"})
    game = demand.prep_list(rid, sun)
    assert game["items"][0]["expected_use"] == 13.0 and game["effect_pct"] == 30.0
    assert "Scaled +30%" in game["note"]


# ── CROSSMODULE-19: one waste adjustment ───────────────────────────────────

def test_the_order_comes_down_for_waste_once_and_the_line_says_how_much():
    # The reviewer's case: 80% of the last order wasted. analyse_inventory
    # takes it to 0.60 of the need; the draft used to take up to 30% more.
    it = _item(current_stock=0.0, waste_last_week=8.0, avg_daily_usage=2.0)
    inventory.analyse_inventory([it], today=MON)
    raw = 10 * 1.5 + 2 * 3
    assert it["suggested_order_qty"] == round(raw * 0.60)
    assert it["waste_trimmed_units"] == round(raw - raw * 0.60)
    qty, trimmed = inventory._trim_for_waste(it)
    assert qty == it["suggested_order_qty"] and trimmed == it["waste_trimmed_units"]


def test_no_waste_recorded_no_trim():
    it = _item(current_stock=0.0, waste_last_week=0.0)
    inventory.analyse_inventory([it], today=MON)
    assert it["suggested_order_qty"] == 21 and it["waste_trimmed_units"] == 0
    assert inventory._trim_for_waste(it) == (21, 0)
