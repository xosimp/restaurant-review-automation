"""The ingredient stock ledger (inventory_ledger.py) at the edges the MOD
audit found untested: a nightly re-sync after the owner counts, a dish that
stops selling, two counts of one item at once, negative receiving and a
recipe typed in the wrong unit, count-sheet dates, a NaN count, a crash in
the middle of a depletion pass, refunds — plus how analyse_inventory treats
negative stock and zero usage, which is where those ledger errors land.

POS reads go through pos.fetch_order_selections, stubbed. Dates are frozen
by replacing inventory_ledger.date. Confirmed defects are strict xfails."""
import os
import sys
import threading
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, get_conn

# Imported at collection, while models.get_conn is still the real one, so a
# lazy import inside a test never binds that test's redirect for good.
import inventory, inventory_ledger as il, pos, strategy_routes  # noqa: E401,F401

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _rid(db_path):
    return create_restaurant(Restaurant(name="Ledger Co", owner_email="l@x.com", module_inventory=1), db_path=db_path)


def _ingredient(db_path, rid, name="Dough", stock=100.0, par=40, usage=0.0, unit="lb", cost=2.0, anchor=None):
    c = get_conn(db_path)
    iid = c.execute(
        "INSERT INTO ingredients (restaurant_id,name,category,unit,par_level,unit_cost,case_size,current_stock,"
        "avg_daily_usage,last_order_qty,waste_last_week,is_active) VALUES (?,?,?,?,?,?,1,?,?,?,0,1)",
        (rid, name, "Pantry", unit, par, cost, stock, usage, par)).lastrowid
    c.execute("INSERT INTO ingredient_stock_events (restaurant_id,ingredient_id,event_type,qty,event_date,source) "
              "VALUES (?,?,'recount',?,?,'migration')", (rid, iid, stock, (anchor or date.today()).isoformat()))
    c.commit(); c.close()
    return iid


def _dish(db_path, rid, iid, guid="g1", qty_per_unit=1.0, name="Pizza"):
    c = get_conn(db_path)
    mid = c.execute("INSERT INTO menu_items (restaurant_id,toast_guid,name) VALUES (?,?,?)", (rid, guid, name)).lastrowid
    c.execute("INSERT INTO recipe_ingredients (menu_item_id,ingredient_id,qty_per_unit) VALUES (?,?,?)",
              (mid, iid, qty_per_unit))
    c.commit(); c.close()
    return mid


def _stock(rid, iid):
    return {r["id"]: r for r in il.list_ingredients(rid)}[iid]


def _waste_rows(db_path, iid):
    c = get_conn(db_path)
    rows = c.execute("SELECT qty FROM ingredient_stock_events WHERE ingredient_id=? AND event_type='waste'",
                     (iid,)).fetchall()
    c.close()
    return [r["qty"] for r in rows]


# ── A5 ledger #11 / MOD-FC-4: a re-sync after a recount ─────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-4: re-synced depletion rows get new ids after the recount and are subtracted again")
def test_a_resync_after_a_recount_does_not_deplete_twice(db_path, monkeypatch):
    rid = _rid(db_path)
    today = date.today()
    d1, d2 = today - timedelta(days=1), today - timedelta(days=2)
    iid = _ingredient(db_path, rid, stock=100, anchor=d2 - timedelta(days=1))
    _dish(db_path, rid, iid)
    sales = {d2.isoformat(): 10, d1.isoformat(): 10}
    monkeypatch.setattr(pos, "fetch_order_selections",
                        lambda r, d: ([{"item": {"guid": "g1"}, "quantity": sales.get(d.isoformat(), 0)}], "toast"))
    il.compute_daily_depletion(rid, d2)
    il.compute_daily_depletion(rid, d1)
    assert _stock(rid, iid)["current_stock"] == 80
    il.record_recount(rid, iid, 80, event_date=d1)          # the owner counts after close
    for d in (d2, d1, today):                                # next night's 3-day window
        il.compute_daily_depletion(rid, d)
    assert _stock(rid, iid)["current_stock"] == 80


def test_re_running_one_business_date_is_idempotent(db_path, monkeypatch):
    rid = _rid(db_path)
    d1 = date.today() - timedelta(days=1)
    iid = _ingredient(db_path, rid, stock=100, anchor=d1 - timedelta(days=1))
    _dish(db_path, rid, iid)
    monkeypatch.setattr(pos, "fetch_order_selections", lambda r, d: ([{"item": {"guid": "g1"}, "quantity": 10}], "toast"))
    il.compute_daily_depletion(rid, d1)
    il.compute_daily_depletion(rid, d1)
    assert _stock(rid, iid)["current_stock"] == 90


# ── A5 ledger #12 / MOD-FC-13: a dish that stops selling ────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-13: rollups are recomputed only for ingredients depleted that day, so usage freezes")
def test_an_ingredient_whose_dish_stops_selling_decays_to_zero_usage(db_path, monkeypatch):
    import datetime as _dt
    real_today = _dt.date.today()

    class FakeDate(_dt.date):
        NOW = real_today

        @classmethod
        def today(cls):
            return cls.NOW
    monkeypatch.setattr(il, "date", FakeDate)
    rid = _rid(db_path)
    oil = _ingredient(db_path, rid, name="Truffle Oil", stock=500, anchor=real_today - timedelta(days=31))
    flour = _ingredient(db_path, rid, name="Flour", stock=500, anchor=real_today - timedelta(days=31))
    _dish(db_path, rid, oil, guid="g1", name="Truffle Fries")
    _dish(db_path, rid, flour, guid="g2", name="Pizza")
    dish86 = real_today - timedelta(days=20)
    monkeypatch.setattr(pos, "fetch_order_selections", lambda r, d: (
        [{"item": {"guid": "g2"}, "quantity": 10}] + ([{"item": {"guid": "g1"}, "quantity": 8}] if d < dish86 else []),
        "toast"))
    for k in range(30, -1, -1):
        day = real_today - timedelta(days=k)
        FakeDate.NOW = FakeDate(day.year, day.month, day.day)
        il.compute_daily_depletion(rid, FakeDate.NOW - timedelta(days=1))
    assert _stock(rid, flour)["avg_daily_usage"] == 10
    assert _stock(rid, oil)["avg_daily_usage"] == 0


# ── A5 ledger #13 / MOD-FC-14: two counts of one item at once ───────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-14: record_recount reads the expectation outside a write lock; both counts infer the full gap")
def test_two_simultaneous_recounts_of_one_ingredient_infer_waste_once(db_path, monkeypatch):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Flour", stock=100)
    barrier = threading.Barrier(2)
    real = il._compute_current_stock

    def read_then_wait(conn, ingredient_id, restaurant_id=None):
        v = real(conn, ingredient_id, restaurant_id)
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return v
    monkeypatch.setattr(il, "_compute_current_stock", read_then_wait)
    errors = []

    def count():
        try:
            il.record_recount(rid, iid, 90, source="count_sheet")
        except Exception as e:  # a refused duplicate is an acceptable outcome
            errors.append(e)
    threads = [threading.Thread(target=count) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    assert _waste_rows(db_path, iid) == [10.0]


def test_a_recount_below_expectation_infers_the_gap_as_waste(db_path):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Flour", stock=100)
    out = il.record_recount(rid, iid, 90)
    assert out["inferred_waste_qty"] == 10.0 and _waste_rows(db_path, iid) == [10.0]


# ── A5 ledger #14, #15 / MOD-FC-12: negative stock ──────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-12: record_receiving accepts any sign; -500 drives stock to -400")
def test_negative_receiving_is_refused(db_path):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, stock=100)
    il.record_receiving(rid, iid, -500)
    assert _stock(rid, iid)["current_stock"] == 100


@pytest.mark.xfail(strict=True, reason="MOD-FC-12: a recipe typed in ounces on a per-pound ingredient over-depletes with no sanity check")
def test_a_recipe_in_the_wrong_unit_never_drives_stock_below_zero(db_path, monkeypatch):
    rid = _rid(db_path)
    d1 = date.today() - timedelta(days=1)
    iid = _ingredient(db_path, rid, name="Beef", stock=30, anchor=d1 - timedelta(days=1))
    _dish(db_path, rid, iid, qty_per_unit=8.0, name="Burger")          # 8 "oz" on a per-lb ingredient
    monkeypatch.setattr(pos, "fetch_order_selections", lambda r, d: ([{"item": {"guid": "g1"}, "quantity": 60}], "toast"))
    il.compute_daily_depletion(rid, d1)
    assert _stock(rid, iid)["current_stock"] >= 0


def _item(**kw):
    d = dict(item="Beef", category="Protein", par_level=20, current_stock=10, unit_cost=5,
             avg_daily_usage=3, last_order_qty=20, waste_last_week=0, case_size=1)
    d.update(kw)
    return d


@pytest.mark.xfail(strict=True, reason="MOD-FC-12: analyse_inventory orders par*1.5 - current_stock, so negative stock inflates the order and the stock value")
def test_negative_stock_orders_no_more_than_an_empty_shelf_would(db_path):
    empty = inventory.analyse_inventory([_item(current_stock=0)])
    negative = inventory.analyse_inventory([_item(current_stock=-485)])
    qty = lambda a: max([i["suggested_order_qty"] for i in a["critical_low"] + a["reorder_soon"]] or [0])
    assert qty(negative) <= qty(empty)
    assert negative["total_stock_value"] >= 0


# ── analyse_inventory #13 / MOD-FC-13: zero usage ───────────────────────────

@pytest.mark.xfail(strict=True, reason="MOD-FC-13: avg_daily_usage 0 gives days_remaining 99, so an empty, below-par item never reaches the order")
def test_an_empty_below_par_item_reaches_the_order_even_when_usage_reads_zero():
    a = inventory.analyse_inventory([_item(current_stock=0, avg_daily_usage=0)])
    assert [i["item"] for i in a["critical_low"] + a["reorder_soon"]] == ["Beef"]


def test_an_item_with_usage_that_is_about_to_run_out_is_critical():
    a = inventory.analyse_inventory([_item(current_stock=2, avg_daily_usage=3)])
    assert [i["item"] for i in a["critical_low"]] == ["Beef"]


# ── A5 ledger #16 / MOD-FC-15: the count-sheet date ─────────────────────────

def _count_sheet(monkeypatch, rid, body):
    import strategy_routes as sr
    monkeypatch.setattr(sr, "_sees_food", lambda u: True)
    monkeypatch.setattr(sr, "_body", lambda: body)
    return sr._do_count_sheet_save({"id": 1, "restaurant_id": rid, "username": "o"})


@pytest.mark.parametrize("bad_date", [
    pytest.param("9/21/26", id="m-d-yy"),
    pytest.param("2099-12-31", id="future"),
])
@pytest.mark.xfail(strict=True, reason="MOD-FC-15: the count-sheet date is passed straight to record_recount with no validation")
def test_a_count_sheet_with_a_non_iso_or_future_date_is_refused(db_path, monkeypatch, bad_date):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Basil", stock=10)
    out, status = _count_sheet(monkeypatch, rid, {"date": bad_date, "items": [{"ingredient_id": iid, "counted": 4}]})
    assert status == 400
    c = get_conn(db_path)
    n = c.execute("SELECT COUNT(*) FROM ingredient_stock_events WHERE event_date=?", (bad_date,)).fetchone()[0]
    c.close()
    assert n == 0


def test_a_count_sheet_with_an_iso_date_is_written(db_path, monkeypatch):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Basil", stock=10)
    day = (date.today() - timedelta(days=1)).isoformat()
    out, status = _count_sheet(monkeypatch, rid, {"date": day, "items": [{"ingredient_id": iid, "counted": 4}]})
    assert status == 200 and out["written"] == 1
    assert _stock(rid, iid)["current_stock"] == 4


# ── A5 ledger #17: a NaN count ──────────────────────────────────────────────

def test_a_nan_recount_is_refused_and_stock_is_unchanged(db_path):
    rid = _rid(db_path)
    iid = _ingredient(db_path, rid, name="Rice", stock=10)
    try:
        il.record_recount(rid, iid, float("nan"))
    except Exception:
        pass
    assert _stock(rid, iid)["current_stock"] == 10
    c = get_conn(db_path)
    n = c.execute("SELECT COUNT(*) FROM ingredient_stock_events WHERE ingredient_id=?", (iid,)).fetchone()[0]
    c.close()
    assert n == 1  # only the anchor


# ── A5 ledger #18: a crash in the middle of a depletion pass ────────────────

def test_a_crash_mid_depletion_leaves_the_day_as_it_was(db_path, monkeypatch):
    rid = _rid(db_path)
    d1 = date.today() - timedelta(days=1)
    iid = _ingredient(db_path, rid, stock=100, anchor=d1 - timedelta(days=1))
    _dish(db_path, rid, iid)
    monkeypatch.setattr(pos, "fetch_order_selections", lambda r, d: ([{"item": {"guid": "g1"}, "quantity": 10}], "toast"))
    il.compute_daily_depletion(rid, d1)
    assert _stock(rid, iid)["current_stock"] == 90

    def boom(*a, **k):
        raise RuntimeError("crash after the DELETE")
    monkeypatch.setattr(il, "recompute_rollups", boom)
    with pytest.raises(RuntimeError):
        il.compute_daily_depletion(rid, d1)
    c = get_conn(db_path)
    rows = c.execute("SELECT qty FROM ingredient_stock_events WHERE ingredient_id=? AND event_type='depletion'",
                     (iid,)).fetchall()
    c.close()
    assert [r["qty"] for r in rows] == [10.0]


# ── A5 ledger #19: a refund in the same business day ────────────────────────

def test_a_sale_and_its_refund_on_one_day_deplete_nothing_net(db_path, monkeypatch):
    """A negative selection quantity is netted against the day's sales of the
    same item. (Whether a stand-alone refund should return stock is not
    specified by the audit and is not asserted here.)"""
    rid = _rid(db_path)
    d1 = date.today() - timedelta(days=1)
    iid = _ingredient(db_path, rid, stock=100, anchor=d1 - timedelta(days=1))
    _dish(db_path, rid, iid)
    monkeypatch.setattr(pos, "fetch_order_selections", lambda r, d: (
        [{"item": {"guid": "g1"}, "quantity": 2}, {"item": {"guid": "g1"}, "quantity": -2}], "toast"))
    out = il.compute_daily_depletion(rid, d1)
    assert out["units_sold"] == 0
    assert _stock(rid, iid)["current_stock"] == 100
