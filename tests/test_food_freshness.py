"""Data Freshness audit, workstream F (food, inventory and money) — Top-50
#8, #10, #11, #13, #38 (food), #42, #47 (F side), #48.

Each test pins one finding from scratchpad dh-reports (DH1-1, DH1-3,
DH1-4, DH1-5, DH1-6, DH1-14, DH1-18, DH3-3, DH3-4, DH3-15, DH4-16): the
figure is dated by the data it rests on, and withheld or aged when that
data is old, never presented as current."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

import cogs
import data_freshness as df
import demand
import food_cost_intelligence as fci
import forecast_log
import inventory
import inventory_ledger as il
import models
import notify
import value_delivered


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, fci):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(fci, "DB_PATH", db_path)
    yield


TODAY = date.today()


def _ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


def _restaurant(db_path, rid=1, **kw):
    conn = models.get_conn(db_path)
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test", "module_inventory": 1}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _ingredient(db_path, rid, name, **kw):
    cols = {"restaurant_id": rid, "name": name, "unit": "lb", "unit_cost": 10.0, "par_level": 10,
            "current_stock": 10, "avg_daily_usage": 2, "last_order_qty": 20, "waste_last_week": 0,
            "is_active": 1, "last_recount_at": _ago(1)}
    cols.update(kw)
    conn = models.get_conn(db_path)
    iid = conn.execute(f"INSERT INTO ingredients ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                       tuple(cols.values())).lastrowid
    conn.commit()
    conn.close()
    return iid


def _event(db_path, rid, iid, kind, qty, day, unit_cost=None):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, "
                 "source, unit_cost) VALUES (?,?,?,?,?,'manual',?)", (rid, iid, kind, qty, day, unit_cost))
    conn.commit()
    conn.close()


def _row(db_path, rid):
    return models.get_restaurant(rid, db_path=db_path)


# ── #8 · waste is summed from dated events, not the undated rollup ─────────

def test_week_waste_is_summed_from_dated_events_not_the_cached_rollup(db_path):
    """DH1-1: $400 of produce logged on 9/1 and never touched again read
    "waste this week" on 9/24 in the food read, Ask, the alert, the
    snapshot and the opportunity."""
    rid = _restaurant(db_path)
    old = _ingredient(db_path, rid, "Lettuce", waste_last_week=40.0)      # the stale cached rollup
    fresh = _ingredient(db_path, rid, "Salmon", waste_last_week=0.0)
    _event(db_path, rid, old, "waste", 40.0, _ago(23))
    _event(db_path, rid, fresh, "waste", 3.0, _ago(2))
    _event(db_path, rid, fresh, "waste", 9.0, _ago(12))                     # outside the week
    items, live, a = inventory.analysis_for(rid)
    by = {i["item"]: i for i in items}
    assert live is True
    assert by["Lettuce"]["waste_last_week"] == 0.0 and by["Lettuce"]["waste_basis"] == "logged"
    assert by["Salmon"]["waste_last_week"] == 3.0
    assert a["total_waste_cost_week"] == 30.0
    local = il.local_today(rid)
    assert a["waste_window"] == {"start": (local - timedelta(days=6)).isoformat(), "end": local.isoformat()}
    assert a["waste_source"]["last_waste_at"] == _ago(2)


def test_a_manual_waste_figure_is_named_as_undated(db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Flour", waste_last_week=5.0)                 # never logged: typed in
    items, _live, a = inventory.analysis_for(rid)
    assert items[0]["waste_basis"] == "manual" and items[0]["waste_last_week"] == 5.0
    assert a["waste_source"]["manual_items"] == 1
    _w, line = inventory.food_prompt_data_lines(a)
    assert "undated waste figure typed in" in line


def test_recompute_stamps_the_rollup_time(db_path):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Beef")
    il.recompute_rollups(rid, iid)
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT rollup_at FROM ingredients WHERE id=?", (iid,)).fetchone()
    conn.close()
    assert row["rollup_at"]


def test_the_waste_source_is_dated_by_the_last_waste_event(db_path):
    rid = _restaurant(db_path)
    r = _row(db_path, rid)
    assert df.source_state(r, "waste", db_path=db_path)["state"] == "not_connected"
    iid = _ingredient(db_path, rid, "Beef")
    _event(db_path, rid, iid, "waste", 1.0, _ago(30))
    s = df.source_state(r, "waste", db_path=db_path)
    assert s["as_of_iso"] == _ago(30) and s["state"] == "stale"
    assert "Last waste logged" in s["basis"]
    for m in ("food", "food_cost", "inventory"):
        assert {"purchases", "waste"} <= set(df.MODULE_SOURCES[m]), m


def test_the_snapshot_and_the_prompt_carry_the_logged_week(db_path):
    rid = _restaurant(db_path)
    old = _ingredient(db_path, rid, "Lettuce", waste_last_week=40.0)
    _event(db_path, rid, old, "waste", 40.0, _ago(23))
    fci.weekly_snapshot(rid, db_path=db_path)
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT waste_json, items_json FROM inventory_history WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert json.loads(row["waste_json"])["total_waste_cost"] == 0.0
    assert json.loads(row["items_json"])[0]["waste_last_week"] == 0.0
    src = open("inventory.py", encoding="utf-8").read()
    assert "- Waste this week:" not in src and "- Waste logged {analysis.get('week_start'" in src


# ── #10 · each item's own count ─────────────────────────────────────────────

def test_critical_low_is_judged_on_each_items_own_count(db_path):
    """DH3-3: a recount of lettuce today made a 40-day-old chicken count
    "fresh", so chicken's projected 0.5 days fired a running-out push."""
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Lettuce", last_recount_at=TODAY.isoformat())
    _ingredient(db_path, rid, "Chicken", current_stock=0.5, avg_daily_usage=2, last_recount_at=_ago(40))
    _items, _live, a = inventory.analysis_for(rid)
    chicken = next(x for x in a["critical_low"] if x["item"] == "Chicken")
    assert chicken["count_stale"] is True
    cf = a["count_freshness"]
    assert cf["counted_recent"] == 1 and cf["items_total"] == 2 and cf["oldest_count_at"] == _ago(40)
    _w, line = inventory.food_prompt_data_lines(a)
    assert "1 of 2 items counted in the last 7 days" in line and "oldest count" in line


def test_the_food_reads_stale_sources_come_from_the_registry(db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Lettuce", last_recount_at=TODAY.isoformat())
    _ingredient(db_path, rid, "Chicken", last_recount_at=_ago(40))
    ds = inventory.food_stale_sources(rid, db_path=db_path)
    assert ds["stale_sources"] and ds["stale_sources"][0].startswith("stock count")


def test_a_legacy_inventory_csv_reads_unknown_not_not_connected(db_path):
    """DH1-6: live stock and waste from an upload with no count date."""
    rid = _restaurant(db_path)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO client_data (restaurant_id, inventory_csv, updated_at) VALUES (?,?,?)",
                 (rid, "item,current_stock\nx,1\n", "2026-03-02 10:00:00"))
    conn.commit()
    conn.close()
    s = df.source_state(_row(db_path, rid), "inventory", db_path=db_path)
    assert s["state"] == "unknown" and s["pct"] == 0
    assert "uploaded 3/2/26, count date unknown" in s["basis"]


# ── #11 · deliveries and prices ─────────────────────────────────────────────

def test_food_cost_is_withheld_naming_the_last_delivery(db_path):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Beef")
    _event(db_path, rid, iid, "receiving", 10.0, _ago(45))
    fc = cogs.build_food_cost_pct(rid, db_path=db_path)
    why = next(m["why"] for m in fc["missing"] if m["component"] == "purchases")
    from time_utils import mdy
    assert why == f"deliveries not logged since {mdy(_ago(45))}"
    assert fc["ok"] is False and fc["pct"] is None


def test_the_prices_source_is_dated_by_the_newest_invoice(db_path):
    rid = _restaurant(db_path)
    r = _row(db_path, rid)
    assert df.source_state(r, "prices", db_path=db_path)["state"] == "not_connected"
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO invoice_imports (restaurant_id, invoice_date, applied_at) VALUES (?,?,?)",
                 (rid, _ago(3), _ago(2) + " 09:00:00"))
    conn.commit()
    conn.close()
    s = df.source_state(r, "prices", db_path=db_path)
    assert s["as_of_iso"] == _ago(3) and s["last_invoice_iso"] == _ago(3) and s["state"] == "current"


def test_a_price_driver_carries_the_last_invoice_date(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Beef", avg_daily_usage=5)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO invoice_imports (restaurant_id, invoice_date, applied_at) VALUES (?,?,?)",
                 (rid, _ago(3), _ago(3) + " 09:00:00"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(inventory, "compute_item_trends", lambda *a, **k: {})
    monkeypatch.setattr(inventory, "build_price_watch", lambda *a, **k: [
        {"item": "Beef", "is_big_8": True, "change_pct": 20.0, "old_price": 5.0, "new_price": 6.0, "weeks": 3}])
    d = next(x for x in fci.cost_drivers(rid, db_path=db_path)["drivers"] if x["kind"] == "price")
    from time_utils import mdy
    assert d["last_invoice_at"] == _ago(3) and f"last invoice {mdy(_ago(3))}" in d["evidence"]


# ── #38 (food) · alerts on current data ─────────────────────────────────────

def _alert_restaurant(db_path):
    rid = models.create_restaurant(models.Restaurant(name="Alert Co", owner_email="a@x.test", module_inventory=1),
                                   db_path=db_path)
    models.update_restaurant(rid, {"alert_food_waste": 1}, db_path=db_path)
    return rid


def test_the_food_waste_alert_waits_for_current_counts(db_path, monkeypatch):
    """DH4-16: "$X of waste flagged this week" went out on counts nobody had
    taken in weeks."""
    rid = _alert_restaurant(db_path)
    _ingredient(db_path, rid, "Beef", last_recount_at=_ago(40))
    items = [{"item": f"Item{i}", "waste_cost": 60.0} for i in range(4)]
    analysis = {"waste_items": items, "waste_items_total": 240.0, "waste_items_count": 4,
                "week_start": "9/18/26", "week_end": "9/24/26"}
    monkeypatch.setattr(inventory, "analysis_for", lambda rid_: ([{"item": "x"}], True, analysis))
    monkeypatch.setattr(notify, "_waste_alert_worsened", lambda *a, **k: True)
    fired = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid_, t, sms, subj, lines=None, **k: fired.append((t, sms)))
    notify.check_extra_daily_alerts(db_path=db_path)
    assert not [f for f in fired if f[0] == "food_waste"]
    conn = models.get_conn(db_path)
    conn.execute("UPDATE ingredients SET last_recount_at=? WHERE restaurant_id=?", (_ago(1), rid))
    conn.commit()
    conn.close()
    notify.check_extra_daily_alerts(db_path=db_path)
    sms = next(f[1] for f in fired if f[0] == "food_waste")
    assert "logged 9/18/26–9/24/26" in sms and "this week" not in sms


def test_the_price_alert_waits_for_a_recent_invoice(db_path, monkeypatch):
    """DH3-4: a five-week-old price climb texted as news."""
    rid = _alert_restaurant(db_path)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO invoice_imports (restaurant_id, invoice_date, applied_at) VALUES (?,?,?)",
                 (rid, _ago(35), _ago(35) + " 09:00:00"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(inventory, "analysis_for", lambda *a, **k: ([], False, {}))
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda *a, **k: ([{"item": "Salmon"}], True))
    monkeypatch.setattr(inventory, "compute_item_trends", lambda *a, **k: [])
    monkeypatch.setattr(inventory, "build_price_watch", lambda *a, **k: [
        {"item": "Salmon", "is_big_8": True, "change_pct": 12.0, "old_price": 10.0, "new_price": 11.2}])
    monkeypatch.setattr(notify, "_price_spike_impact", lambda *a, **k: (None, []))
    fired = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid_, t, sms, subj, **k: fired.append((t, sms)) or True)
    notify.check_extra_daily_alerts(db_path=db_path)
    assert not [f for f in fired if f[0] == "price_spike"]


# ── #13 · prime cost on a current labor period ──────────────────────────────

def _prime_inputs(monkeypatch, end):
    monkeypatch.setattr(fci, "MIN_DAYS_FOR_PROJECTION", 1)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: (60000.0, None))
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda r, days=28, db_path=None, today=None: {
        "ok": True, "cogs": 18000.0, "pct": 30.0, "target": 28.0})
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: {
        "is_live": True, "total_sales": 60000.0, "overall_labor_pct": 30.0,
        "date_range": {"start": (date.fromisoformat(end) - timedelta(days=27)).isoformat(), "end": end,
                       "days": 28}})


def test_prime_cost_refuses_a_months_old_labor_share(db_path, monkeypatch):
    """DH1-3: shifts last uploaded 7/15 put July's labor % in every
    September email."""
    rid = _restaurant(db_path)
    _prime_inputs(monkeypatch, _ago(40))
    out = fci.profitability_projection(rid, db_path=db_path)
    assert out["available"] is False
    why = next(m["why"] for m in out["missing"] if m["component"] == "labor")
    from time_utils import mdy
    assert f"shifts on file end {mdy(_ago(40))}" in why
    assert out["labor_from"].startswith("labor from ")


def test_prime_cost_names_its_labor_period(db_path, monkeypatch):
    rid = _restaurant(db_path)
    _prime_inputs(monkeypatch, _ago(1))
    out = fci.profitability_projection(rid, db_path=db_path)
    from time_utils import mdy
    assert out["available"] is True
    assert out["labor_from"] == f"labor from {mdy(_ago(28))}–{mdy(_ago(1))}"
    assert "pp['labor_from']" in open("morning_brief.py", encoding="utf-8").read()


# ── #42 · the opportunity figure is dated ───────────────────────────────────

def test_an_opportunity_on_a_stale_labor_period_is_withheld(db_path, monkeypatch):
    """DH1-4: "$1,900/mo available" from a June labor period."""
    rid = _restaurant(db_path, module_labor=1, module_inventory=0)
    labor_a = {"is_live": True, "potential_savings_monthly": 1900.0,
               "date_range": {"start": _ago(90), "end": _ago(60), "days": 30}}
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: labor_a)
    out = value_delivered.opportunity(rid, db_path=db_path)
    assert out["items"] == [] and out["monthly"] == 0.0
    assert out["withheld"][0]["key"] == "labor" and out["withheld"][0]["as_of_iso"] == _ago(60)
    assert "monthly" not in out["withheld"][0]
    labor_a["date_range"] = {"start": _ago(30), "end": _ago(1), "days": 30}
    out = value_delivered.opportunity(rid, db_path=db_path)
    assert out["items"][0]["monthly"] == 1900.0 and out["items"][0]["as_of_iso"] == _ago(1)
    assert out["items"][0]["aged"] is False


# ── #47 · the restaurant's own day ──────────────────────────────────────────

def test_day_boundaries_are_the_restaurants_local_date(db_path, monkeypatch):
    """DH1-14: on a UTC host, after 7pm Central "today" is tomorrow."""
    rid = _restaurant(db_path)
    local = TODAY - timedelta(days=1)            # the restaurant is still on "yesterday"
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda r, naive=False: datetime(local.year, local.month, local.day, 22, 0,
                                                        tzinfo=timezone.utc))
    assert cogs.build_food_cost_pct(rid, db_path=db_path)["end"] == local.isoformat()
    assert demand.forecast_day(rid, db_path=db_path)["day"] == local.isoformat()
    assert il.local_today(rid) == local
    assert fci._local_today(rid) == local


# ── #48 · forecasts scored and stated on current data ───────────────────────

def test_waste_is_unscorable_for_a_week_nothing_was_logged(db_path):
    rid = _restaurant(db_path)
    end = date(2026, 9, 20)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end, source) VALUES (?,?,?,?)",
                 (rid, json.dumps({"total_waste_cost": 0}), end.isoformat(), "scheduled"))
    conn.commit()
    conn.close()
    assert forecast_log._waste_actual(rid, end - timedelta(days=6), end, {}, db_path) is None


def test_reach_is_unscorable_while_the_metrics_sync_fails(db_path):
    rid = _restaurant(db_path)
    end = date(2026, 9, 20)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, post_id, reach, created_at) "
                 "VALUES (?, 'post', 'p1', 50, '2026-09-18 12:00:00')", (rid,))
    conn.execute("INSERT INTO job_cursors (key, value) VALUES (?, ?)",
                 (f"metrics_sync:{rid}", json.dumps({"last_attempt_at": "2026-09-22 03:00:00",
                                                     "last_ok_at": "2026-09-19 03:00:00", "error": "401"})))
    conn.commit()
    conn.close()
    assert forecast_log._reach_actual(rid, end - timedelta(days=6), end, {}, db_path) is None


def _nights(db_path, rid, weekday_dates):
    conn = models.get_conn(db_path)
    for d in weekday_dates:
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                     (rid, d.isoformat(), d.strftime("%A"), 1000.0))
    conn.commit()
    conn.close()


def test_the_day_forecast_names_its_newest_night_and_withholds_old_ones(db_path):
    """DH1-18: after the POS stops, "a typical Saturday" came from three
    Saturdays six to eight weeks back."""
    from time_utils import mdy
    rid = _restaurant(db_path)
    day = date(2026, 9, 26)
    _nights(db_path, rid, [day - timedelta(weeks=w) for w in (6, 7, 8)])
    out = demand.forecast_day(rid, day, db_path=db_path)
    assert out["available"] is False and out["stale"] is True
    assert mdy((day - timedelta(weeks=6)).isoformat()) in out["reason"]
    _nights(db_path, rid, [day - timedelta(weeks=1)])
    out = demand.forecast_day(rid, day, db_path=db_path)
    assert out["available"] is True and out["newest_sample"] == (day - timedelta(weeks=1)).isoformat()
    assert f"the newest {mdy((day - timedelta(weeks=1)).isoformat())}" in out["range_basis"]
