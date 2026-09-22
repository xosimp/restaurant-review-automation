"""Food cost % (cogs.py), the snapshot history it reads, Price Watch, menu
repricing and recipe import, and the Food Cost scheduler jobs, at the edges
the MOD audit found untested: a closed restaurant, a sales archive with a
hole, a window ending today, a price change after a delivery, a single
snapshot, a year of snapshots, dual-sourced and renamed ingredients, a
recipe CSV that cannot reach the POS, a double accept, the 28-vs-30-day
label, forecasts of zero, and jobs with no bound, cursor or per-restaurant
isolation.

The POS is stubbed at pos.fetch_business_days; no job reaches a real
provider or sends an email. Confirmed defects are strict xfails."""
import json
import os
import sys
import threading
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, get_conn

# Imported at collection, while models.get_conn is still the real one, so a
# lazy import inside a test never binds that test's redirect for good.
import cogs, food_cost_intelligence as fci, inventory, inventory_ledger as il, menu_intelligence  # noqa: E401,F401
import ops, pos, recipes, scheduler, waste_trend  # noqa: E401,F401

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODAY = date(2026, 9, 22)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if mod is not None and (getattr(mod, "get_conn", None) is real or
                                (f.startswith(_REPO) and callable(getattr(mod, "get_conn", None)))):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)


@pytest.fixture
def pos_calls(monkeypatch):
    calls = []

    def fake(rid, start, end):
        calls.append((str(start), str(end)))
        raise pos.POSCapabilityError("no POS connected")
    monkeypatch.setattr(pos, "fetch_business_days", fake)
    return calls


def _rid(db_path, name="Cogs Co"):
    return create_restaurant(Restaurant(name=name, owner_email="c@x.com", module_inventory=1), db_path=db_path)


def _sales(db_path, rid, days_and_sales):
    c = get_conn(db_path)
    for d, s in days_and_sales:
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales) VALUES (?,?,?)", (rid, d.isoformat(), s))
    c.commit(); c.close()


def _snapshot(db_path, rid, day, inv_value=1000.0, items=None, waste=0.0):
    c = get_conn(db_path)
    c.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end, items_json, inv_value, source) "
              "VALUES (?,?,?,?,?, 'scheduled')",
              (rid, json.dumps({"total_waste_cost": waste}), day.isoformat(),
               json.dumps(items) if items is not None else None, inv_value))
    c.commit(); c.close()


def _ingredient(db_path, rid, name="Beef", cost=5.0, stock=10):
    c = get_conn(db_path)
    iid = c.execute("INSERT INTO ingredients (restaurant_id,name,category,unit,par_level,unit_cost,current_stock,"
                    "avg_daily_usage,last_order_qty,waste_last_week,is_active) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                    (rid, name, "Protein", "lb", 20, cost, stock, 3, 20, 0)).lastrowid
    c.commit(); c.close()
    return iid


# ── A5 cogs #7: a closed restaurant, or zero sales ──────────────────────────

def test_a_window_with_zero_sales_is_unknown_not_a_division(db_path, monkeypatch):
    rid = _rid(db_path)
    _sales(db_path, rid, [(TODAY - timedelta(days=k), 0) for k in range(1, 30)])
    monkeypatch.setattr(pos, "fetch_business_days",
                        lambda r, s, e: ({(TODAY - timedelta(days=k)).isoformat(): 0 for k in range(1, 29)}, "toast"))
    total, why = cogs.net_sales_in_window(rid, TODAY - timedelta(days=27), TODAY)
    assert total is None and "no sales" in why
    out = cogs.build_food_cost_pct(rid, today=TODAY)
    assert out["ok"] is False and out["pct"] is None
    assert "net sales" in [m["component"] for m in out["missing"]]


# ── A5 cogs #8 / MOD-FC-16: a hole inside the archive's span ────────────────

def test_an_archive_with_a_three_week_hole_is_not_read_as_covering_the_window(db_path, monkeypatch):
    rid = _rid(db_path)
    _sales(db_path, rid, [(TODAY - timedelta(days=40), 1000), (TODAY - timedelta(days=27), 1000),
                          (TODAY - timedelta(days=5), 1000)])
    monkeypatch.setattr(pos, "fetch_business_days", lambda r, s, e: ({"x": 23000.0}, "toast"))
    total, _why = cogs.net_sales_in_window(rid, TODAY - timedelta(days=27), TODAY - timedelta(days=5))
    assert total == 23000.0


def test_an_archive_covering_every_day_is_read_without_the_pos(db_path, pos_calls):
    rid = _rid(db_path)
    _sales(db_path, rid, [(TODAY - timedelta(days=k), 1000) for k in range(5, 28)])
    total, _why = cogs.net_sales_in_window(rid, TODAY - timedelta(days=27), TODAY - timedelta(days=5))
    assert total == 23000.0 and pos_calls == []


# ── A5 cogs #9 / MOD-FC-17: a window ending today ───────────────────────────

def test_an_archive_complete_through_yesterday_serves_a_window_ending_today(db_path, pos_calls):
    rid = _rid(db_path)
    today = date.today()
    _sales(db_path, rid, [(today - timedelta(days=k), 1000) for k in range(1, 61)])
    cogs.net_sales_in_window(rid, today - timedelta(days=27), today)
    assert pos_calls == []


# ── A5 cogs #10 / MOD-FC-23: a price change after a delivery ────────────────

def test_a_price_change_after_a_delivery_does_not_reprice_that_delivery(db_path):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, cost=5.0)
    il.record_receiving(rid, iid, 10, event_date=TODAY - timedelta(days=10))
    before, _ = cogs.purchases_in_window(rid, TODAY - timedelta(days=27), TODAY)
    c = get_conn(db_path)
    c.execute("UPDATE ingredients SET unit_cost=6.0 WHERE id=?", (iid,))
    c.commit(); c.close()
    after, _ = cogs.purchases_in_window(rid, TODAY - timedelta(days=27), TODAY)
    assert before == 50.0 and after == 50.0


# ── A5 cogs #11: opening and closing are one snapshot ───────────────────────

def test_one_snapshot_cannot_be_both_opening_and_closing(db_path, pos_calls):
    rid = _rid(db_path)
    _snapshot(db_path, rid, TODAY - timedelta(days=1))
    out = cogs.build_food_cost_pct(rid, days=3, today=TODAY)
    assert out["ok"] is False and out["pct"] is None
    assert "a second inventory count" in [m["component"] for m in out["missing"]]


# ── A5 cogs #12, waste #9 / MOD-FC-18: a year of snapshots ──────────────────

def test_a_28_day_food_cost_read_parses_only_snapshots_near_its_window(db_path, pos_calls, monkeypatch):
    rid = _rid(db_path)
    items = [{"item": f"I{i}", "unit_cost": 2.5, "current_stock": 15, "waste_last_week": 1} for i in range(20)]
    for k in range(0, 120):
        _snapshot(db_path, rid, TODAY - timedelta(days=k), items=items)
    parsed = []
    real = waste_trend._week_from_row
    monkeypatch.setattr(waste_trend, "_week_from_row", lambda *a, **k: parsed.append(1) or real(*a, **k))
    cogs.build_food_cost_pct(rid, today=TODAY)
    assert len(parsed) <= 28 + 2 * cogs.SNAPSHOT_TOLERANCE_DAYS


def test_old_inventory_snapshots_are_pruned(db_path):
    rid = _rid(db_path)
    _snapshot(db_path, rid, TODAY - timedelta(days=400))
    c = get_conn(db_path)
    c.execute("UPDATE inventory_history SET saved_at=datetime('now','-400 days')")
    c.commit(); c.close()
    ops.prune_ledgers(db_path=db_path)
    c = get_conn(db_path)
    n = c.execute("SELECT COUNT(*) FROM inventory_history WHERE week_end < date('now','-300 days')").fetchone()[0]
    c.close()
    assert n == 0


# ── A5 waste #10: a corrupt snapshot row ────────────────────────────────────

def test_a_corrupt_snapshot_row_is_skipped_not_fatal(db_path):
    rid = _rid(db_path)
    _snapshot(db_path, rid, TODAY - timedelta(days=14), waste=40.0)
    c = get_conn(db_path)
    c.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end, items_json) VALUES (?,?,?,?)",
              (rid, "{not json", (TODAY - timedelta(days=7)).isoformat(), "[also not json"))
    c.commit(); c.close()
    weeks, total = waste_trend.load_waste_history(rid, None)
    assert [w["waste"] for w in weeks][:1] == [40.0]


# ── A5 Price Watch #7-#9 / MOD-FC-22 ────────────────────────────────────────

def _history(db_path, rid, weekly_items):
    """weekly_items: list oldest→newest of item lists, one snapshot per week."""
    n = len(weekly_items)
    for i, items in enumerate(weekly_items):
        _snapshot(db_path, rid, date.today() - timedelta(days=7 * (n - i)), items=items)


@pytest.mark.xfail(strict=True, reason="MOD-FC-22: price history is keyed by display name, so one supplier's +21% on a dual-sourced item is masked")
def test_a_price_rise_on_one_of_two_same_named_ingredients_is_alerted(db_path):
    rid = _rid(db_path)
    steady = [{"id": 1, "item": "Chicken Breast", "unit_cost": 5.80}, {"id": 2, "item": "Chicken Breast", "unit_cost": 6.40}]
    _history(db_path, rid, [steady] * 6)
    now = [{"id": 1, "item": "Chicken Breast", "unit_cost": 7.00, "avg_daily_usage": 5},
           {"id": 2, "item": "Chicken Breast", "unit_cost": 6.40, "avg_daily_usage": 4}]
    alerts = inventory.compute_item_trends(rid, now)["price_alerts"]
    assert any(a["old_price"] == 5.80 and a["new_price"] == 7.00 for a in alerts)


@pytest.mark.xfail(strict=True, reason="MOD-FC-22: renaming an ingredient starts its price history from zero")
def test_renaming_an_ingredient_keeps_its_price_trend(db_path):
    rid = _rid(db_path)
    _history(db_path, rid, [[{"id": 7, "item": "Ribeye", "unit_cost": p}] for p in (10.0, 11.0, 12.0)])
    now = [{"id": 7, "item": "Ribeye 12oz", "unit_cost": 13.0, "avg_daily_usage": 2}]
    assert inventory.compute_item_trends(rid, now)["trend_alerts"]


def test_a_rising_price_under_one_name_is_a_trend(db_path):
    rid = _rid(db_path)
    _history(db_path, rid, [[{"item": "Ribeye", "unit_cost": p}] for p in (10.0, 11.0, 12.0)])
    t = inventory.compute_item_trends(rid, [{"item": "Ribeye", "unit_cost": 13.0, "avg_daily_usage": 2}])
    assert t["trend_alerts"][0]["weeks"] == 3


def test_a_previous_price_of_zero_raises_no_alert_and_no_error(db_path):
    rid = _rid(db_path)
    _history(db_path, rid, [[{"item": "Salt", "unit_cost": 0}], [{"item": "Pepper"}]])
    t = inventory.compute_item_trends(rid, [{"item": "Salt", "unit_cost": 1.0}, {"item": "Pepper", "unit_cost": 2.0}])
    assert t["price_alerts"] == []
    assert inventory.build_price_watch(t) == [] or isinstance(inventory.build_price_watch(t), list)


# ── A5 menu #9-#11 / MOD-FC-24: recipe CSV import ───────────────────────────

def _menu_item(db_path, rid, name, guid=None):
    c = get_conn(db_path)
    mid = c.execute("INSERT INTO menu_items (restaurant_id,toast_guid,name) VALUES (?,?,?)", (rid, guid, name)).lastrowid
    c.commit(); c.close()
    return mid


@pytest.mark.xfail(strict=True, reason="MOD-FC-24: a CSV dish that matches no POS item is created unlinked and counted as written, silently")
def test_a_recipe_csv_dish_that_matches_no_pos_item_is_reported_as_unlinked(db_path):
    rid = _rid(db_path)
    _ingredient(db_path, rid, name="Mozzarella")
    _menu_item(db_path, rid, "Margherita Pizza", guid="pos-1")
    out = recipes.import_csv(rid, "menu_item,ingredient,qty\nMargherita,Mozzarella,0.25\n")
    assert [k for k in out if "unlinked" in k.lower() and out[k]], out


@pytest.mark.xfail(strict=True, reason="MOD-FC-24: rows past 2,000 are dropped with no count in the response")
def test_a_recipe_csv_over_two_thousand_rows_reports_the_truncation(db_path):
    rid = _rid(db_path)
    _ingredient(db_path, rid, name="Mozzarella")
    rows = "".join(f"Dish {i},Mozzarella,0.25\n" for i in range(2100))
    out = recipes.import_csv(rid, "menu_item,ingredient,qty\n" + rows)
    assert [k for k in out if "truncat" in k.lower() and out[k]], {k: v for k, v in out.items() if k != "unknown_ingredients"}


@pytest.mark.xfail(strict=True, reason="MOD-FC-24: a row for an existing (dish, ingredient) pair hits the unique index and is skipped; qty cannot be corrected")
def test_a_recipe_csv_corrects_an_existing_quantity(db_path):
    rid = _rid(db_path)
    _ingredient(db_path, rid, name="Mozzarella")
    recipes.import_csv(rid, "menu_item,ingredient,qty\nMargherita,Mozzarella,0.25\n")
    recipes.import_csv(rid, "menu_item,ingredient,qty\nMargherita,Mozzarella,0.30\n")
    c = get_conn(db_path)
    q = c.execute("SELECT qty_per_unit FROM recipe_ingredients").fetchall()
    c.close()
    assert [r[0] for r in q] == [0.30]


def test_a_recipe_csv_row_is_written_against_a_matching_dish(db_path):
    rid = _rid(db_path)
    _ingredient(db_path, rid, name="Mozzarella")
    _menu_item(db_path, rid, "Margherita", guid="pos-1")
    out = recipes.import_csv(rid, "menu_item,ingredient,qty\nmargherita,mozzarella,0.25\n")
    assert out["written"] == 1


# ── A5 menu #12 / MOD-FC-27: accepting a draft twice ────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-27: accept is not claim-first; a concurrent second accept marks a written recipe 'rejected'")
def test_two_simultaneous_accepts_leave_the_draft_accepted(db_path, monkeypatch):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Mozzarella")
    mid = _menu_item(db_path, rid, "Margherita", guid="pos-1")
    c = get_conn(db_path)
    did = c.execute("INSERT INTO recipe_drafts (restaurant_id, menu_item_id, menu_item_name, lines_json) VALUES (?,?,?,?)",
                    (rid, mid, "Margherita", json.dumps([{"ingredient_id": iid, "qty": 0.25}]))).lastrowid
    c.commit(); c.close()
    second_has_read, first_done = threading.Event(), threading.Event()
    real_add = il.add_recipe_ingredient

    def add(*a, **k):
        if threading.current_thread().name == "second":
            second_has_read.set()           # it read status='pending' before this call
            first_done.wait(2)
        else:
            second_has_read.wait(2)
        return real_add(*a, **k)
    monkeypatch.setattr(il, "add_recipe_ingredient", add)

    def first():
        recipes.accept(rid, did)
        first_done.set()
    t1 = threading.Thread(target=first, name="first")
    t2 = threading.Thread(target=lambda: recipes.accept(rid, did), name="second")
    t1.start(); t2.start(); t1.join(5); t2.join(5)
    c = get_conn(db_path)
    status = c.execute("SELECT status FROM recipe_drafts WHERE id=?", (did,)).fetchone()[0]
    c.close()
    assert status == "accepted"


def test_accepting_a_draft_writes_its_lines_once(db_path):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Mozzarella")
    mid = _menu_item(db_path, rid, "Margherita", guid="pos-1")
    c = get_conn(db_path)
    did = c.execute("INSERT INTO recipe_drafts (restaurant_id, menu_item_id, menu_item_name, lines_json) VALUES (?,?,?,?)",
                    (rid, mid, "Margherita", json.dumps([{"ingredient_id": iid, "qty": 0.25}]))).lastrowid
    c.commit(); c.close()
    assert recipes.accept(rid, did)["written"] == 1
    assert recipes.accept(rid, did)["ok"] is False  # already answered


# ── A5 menu #13 / MOD-FC-28: the monthly basis ──────────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-28: reprice multiplies 28 days of units and labels it 'the last 30 days'")
def test_reprice_monthly_margin_matches_its_stated_basis(db_path, monkeypatch):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Beef", cost=6.0)
    mid = _menu_item(db_path, rid, "Burger", guid="pos-1")
    il.add_recipe_ingredient(rid, mid, iid, 1.0)
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([{"item": "Beef", "unit_cost": 6.0}], True))
    monkeypatch.setattr(inventory, "compute_item_trends", lambda r, items: {})
    monkeypatch.setattr(inventory, "build_price_watch", lambda t: [
        {"item": "Beef", "old_price": 5.0, "new_price": 6.0, "change_pct": 20.0}])
    monkeypatch.setattr(il, "menu_profitability", lambda r: {"priced": [
        {"id": mid, "name": "Burger", "sell_price": 15.0, "plate_cost": 6.0, "units_sold": 28}]})
    s = menu_intelligence.reprice_suggestions(rid)["suggestions"][0]
    per_month = round(1.0 * 28 * 30.0 / il._POPULARITY_WINDOW_DAYS, 2)
    assert s["monthly_margin_lost"] == per_month or "28" in s["monthly_basis"]


# ── A5 CFO #10: forecasts of zero ───────────────────────────────────────────

def test_a_forecast_of_zero_against_an_actual_of_zero_is_scored_without_dividing(db_path):
    rid = _rid(db_path)
    horizon = date.today() - timedelta(days=3)
    _snapshot(db_path, rid, horizon, waste=0.0)
    c = get_conn(db_path)
    c.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted) VALUES (?,?,?,0)",
              (rid, "waste_week", horizon.isoformat()))
    c.commit(); c.close()
    assert fci.score_forecasts(rid)["scored"] == 1
    c = get_conn(db_path)
    row = c.execute("SELECT actual, error_pct FROM forecast_log WHERE restaurant_id=?", (rid,)).fetchone()
    c.close()
    assert row["actual"] == 0 and row["error_pct"] is None


# ── A5 scheduler #5-#7 / MOD-FC-19, MOD-FC-26 ───────────────────────────────

def _three_pos_restaurants(db_path, monkeypatch):
    rids = [_rid(db_path, name=n) for n in ("A", "B", "C")]
    monkeypatch.setattr(pos, "supports", lambda rid, cap: True)
    monkeypatch.setattr(pos, "fetch_business_days", lambda rid, s, e: ({str(e): 1000.0}, "toast"))
    return rids


def _cursor_keys(db_path):
    c = get_conn(db_path)
    keys = [r[0] for r in c.execute("SELECT key FROM job_cursors").fetchall()]
    c.close()
    return keys


@pytest.mark.xfail(strict=True, reason="MOD-FC-19: the nightly depletion sync walks every restaurant with no bound or cursor")
def test_the_depletion_sync_records_where_it_stopped(db_path, monkeypatch):
    _three_pos_restaurants(db_path, monkeypatch)
    monkeypatch.setattr(il, "compute_daily_depletion", lambda rid, d: {"unmapped_selections": []})
    scheduler.run_daily_depletion_sync()
    assert any("deplet" in k or "inventory" in k for k in _cursor_keys(db_path))


@pytest.mark.xfail(strict=True, reason="MOD-FC-19: a restart mid-pass starts the depletion sync from the first restaurant again")
def test_a_depletion_sync_interrupted_mid_pass_resumes_after_the_last_restaurant_done(db_path, monkeypatch):
    rids = _three_pos_restaurants(db_path, monkeypatch)
    seen, deployed = [], []

    class Deploy(BaseException):
        """A redeploy mid-pass: the process dies, no except clause runs."""

    def deplete(rid, d):
        seen.append(rid)
        if rid == rids[1] and not deployed:
            deployed.append(rid)
            raise Deploy()
        return {"unmapped_selections": []}
    monkeypatch.setattr(il, "compute_daily_depletion", deplete)
    with pytest.raises(Deploy):
        scheduler.run_daily_depletion_sync()
    seen.clear()
    scheduler.run_daily_depletion_sync()
    assert seen[0] == rids[1]


@pytest.mark.xfail(strict=True, reason="MOD-FC-19: the snapshot job walks every restaurant with no bound or cursor")
def test_the_snapshot_job_records_where_it_stopped(db_path, monkeypatch):
    _three_pos_restaurants(db_path, monkeypatch)
    monkeypatch.setattr(fci, "weekly_snapshot", lambda rid, **k: {"ok": True})
    monkeypatch.setattr(fci, "record_profitability_forecast", lambda rid, **k: {})
    monkeypatch.setattr(fci, "score_forecasts", lambda rid, **k: {"scored": 0})
    scheduler.run_food_cost_snapshots()
    assert any("snapshot" in k or "food_cost" in k for k in _cursor_keys(db_path))


def test_one_restaurant_failing_its_snapshot_does_not_stop_the_rest(db_path, monkeypatch):
    rids = _three_pos_restaurants(db_path, monkeypatch)
    done = []

    def snap(rid, **k):
        if rid == rids[0]:
            raise RuntimeError("bad data")
        done.append(rid)
        return {"ok": True}
    monkeypatch.setattr(fci, "weekly_snapshot", snap)
    monkeypatch.setattr(fci, "record_profitability_forecast", lambda rid, **k: {})
    monkeypatch.setattr(fci, "score_forecasts", lambda rid, **k: {"scored": 0})
    monkeypatch.setattr(scheduler._ops, "capture", lambda *a, **k: None)
    out = scheduler.run_food_cost_snapshots()
    assert done == rids[1:] and out["failed"] == 1


@pytest.mark.xfail(strict=True, reason="MOD-FC-26: one malformed updated_at aborts check_stale_inventory for every restaurant")
def test_one_malformed_timestamp_does_not_hide_the_other_stale_restaurants(db_path, monkeypatch):
    import resend
    bad, stale = _rid(db_path, name="Bad Stamp"), _rid(db_path, name="Really Stale")
    for rid in (bad, stale):
        _ingredient(db_path, rid)
    c = get_conn(db_path)
    c.execute("UPDATE ingredients SET updated_at='not a timestamp' WHERE restaurant_id=?", (bad,))
    c.execute("UPDATE ingredients SET updated_at=datetime('now','-10 days') WHERE restaurant_id=?", (stale,))
    c.commit(); c.close()
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "re_test")
    monkeypatch.setattr(pos, "connected_provider", lambda rid: (None, None))
    sent = []
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda p: sent.append(p)), raising=False)
    scheduler.check_stale_inventory()
    assert sent and "Really Stale" in sent[0]["html"]


def test_the_stale_inventory_check_reports_a_stale_ledger(db_path, monkeypatch):
    import resend
    stale = _rid(db_path, name="Really Stale")
    _ingredient(db_path, stale)
    c = get_conn(db_path)
    c.execute("UPDATE ingredients SET updated_at=datetime('now','-10 days') WHERE restaurant_id=?", (stale,))
    c.commit(); c.close()
    monkeypatch.setattr(scheduler, "_resend_key", lambda: "re_test")
    monkeypatch.setattr(pos, "connected_provider", lambda rid: (None, None))
    sent = []
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda p: sent.append(p)), raising=False)
    scheduler.check_stale_inventory()
    assert sent and "Really Stale" in sent[0]["html"]
