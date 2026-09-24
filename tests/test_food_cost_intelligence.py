"""Audit #14 — the Food Cost CFO layer.

The finding this file exists for: the module held four financial engines and
the AI saw one of them. cogs.build_food_cost_pct computed actual food cost %
against the restaurant's own target, menu_profitability computed plate margin
and contribution, recipe_coverage said how far any of it could be trusted —
and none reached the model, so an AI titled "food cost consultant" narrated
waste and could not say whether food cost was over target.

These tests hold the new machinery to the standard the module already held its
counts to: a claim only exists when the evidence for it does, and a projection
built on a substituted zero is worse than no projection because it looks
exactly like a real one.
"""
import inspect
import json
from datetime import date, timedelta

import pytest

import cogs
import food_cost_intelligence as fci
import inventory
import inventory_ledger as il
import models


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, fci):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(fci, "DB_PATH", db_path)
    yield


def _restaurant(db_path, rid=1, **kw):
    conn = models.get_conn(db_path)
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test", "module_inventory": 1}
    cols.update(kw)
    keys = ",".join(cols)
    marks = ",".join("?" for _ in cols)
    conn.execute(f"INSERT INTO restaurants ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _ingredient(db_path, rid, name, unit_cost=10.0, unit="lb", supplier=None, **kw):
    conn = models.get_conn(db_path)
    cols = {"restaurant_id": rid, "name": name, "unit": unit, "unit_cost": unit_cost,
            "par_level": 10, "current_stock": 10, "avg_daily_usage": 2,
            "last_order_qty": 20, "waste_last_week": 0, "is_active": 1}
    if supplier:
        cols["supplier_name"] = supplier
    cols.update(kw)
    keys = ",".join(cols)
    marks = ",".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO ingredients ({keys}) VALUES ({marks})", tuple(cols.values()))
    conn.commit()
    iid = cur.lastrowid
    conn.close()
    return iid


def _event(db_path, rid, iid, kind, qty, days_ago=1, source="manual"):
    conn = models.get_conn(db_path)
    conn.execute(
        "INSERT INTO ingredient_stock_events "
        "(restaurant_id, ingredient_id, event_type, qty, event_date, source) VALUES (?,?,?,?,?,?)",
        (rid, iid, kind, qty, (date.today() - timedelta(days=days_ago)).isoformat(), source))
    conn.commit()
    conn.close()


def _snapshot(db_path, rid, days_ago, waste, inv_value=None, items=None):
    conn = models.get_conn(db_path)
    conn.execute(
        "INSERT INTO inventory_history (restaurant_id, waste_json, week_end, items_json, inv_value, source) "
        "VALUES (?,?,?,?,?,'scheduled')",
        (rid, json.dumps({"total_waste_cost": waste, "top_items": []}),
         (date.today() - timedelta(days=days_ago)).isoformat(),
         json.dumps(items) if items else None, inv_value))
    conn.commit()
    conn.close()


# ── P0-1 · the history is written on a schedule, not on a page view ─────────

def test_the_snapshot_writer_is_a_scheduled_job_not_a_page_render():
    """inventory_history had exactly one writer in production: the AI insight
    function, on page render. The weekly waste series, the multi-week price
    trends, the price-spike alert and the opening/closing values behind food
    cost % all read that table — so all four were functions of whether the
    owner happened to open the tab, and a week nobody looked at is ABSENT
    from the series rather than zero in it."""
    import scheduler
    src = inspect.getsource(scheduler)
    assert 'claim_period("food_cost_snapshots"' in src
    body = inspect.getsource(scheduler.run_food_cost_snapshots)
    assert "weekly_snapshot" in body
    # Every active restaurant, through the bounded, resumable sweep — this
    # pinned a bare `for row in rows:` loop, the unbounded shape MOD-FC-19 removed.
    assert "resumable_sweep(" in body
    assert "except Exception as e:" in body, "one restaurant failing must not end the sweep"


def test_the_snapshot_records_the_stock_value_cogs_needs(db_path, monkeypatch):
    """cogs.inventory_value_near needs a counted inventory value to bracket a
    COGS window. Before the column existed it could only be recovered by
    re-summing items_json, so a snapshot without per-item detail contributed
    no opening or closing value and food cost % silently refused to compute."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "analysis_for",
                        lambda r: ([{"item": "Ribeye"}], True,
                                   {"total_waste_cost_week": 120.0, "waste_items": [],
                                    "total_stock_value": 4500.0}))
    out = fci.weekly_snapshot(rid, db_path=db_path)
    assert out["ok"] is True
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT inv_value, source FROM inventory_history WHERE restaurant_id=?",
                       (rid,)).fetchone()
    conn.close()
    assert row["inv_value"] == 4500.0
    assert row["source"] == "scheduled"


def test_the_snapshot_refuses_sample_data(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], False, {}))
    out = fci.weekly_snapshot(rid, db_path=db_path)
    assert out["ok"] is False
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) c FROM inventory_history").fetchone()["c"] == 0
    conn.close()


# ── P0-2 · "vs last week" is a week ─────────────────────────────────────────

def test_the_insight_takes_week_over_week_from_the_iso_week_series():
    """week_end is TODAY's date, written on every render, so the old query
    ("week_end < date('now','-1 day') ORDER BY week_end DESC LIMIT 1")
    returned whenever the owner last opened the tab. An owner who looked on
    Monday and again on Wednesday saw a two-day delta labelled "vs last week"
    — and the dollar forecast was computed from it."""
    src = inspect.getsource(inventory.get_claude_insights)
    assert "from waste_trend import build_waste_trend" in src
    assert "wow_delta" in src
    assert "vs last week (ISO weeks)" in src
    # And the old row-recency query is gone. Comments stripped: the comment
    # above the fix names the old query on purpose, and matching that would
    # let this test pass on a file that still ran it.
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    assert "week_end < date('now','-1 day')" not in code


def test_an_outlier_week_is_never_projected_from():
    """A single anomalous week extrapolated is not a forecast. The forecast
    is the four-week mean now (the momentum projection this test used to
    gate lost to "same as last week" — CA2 probe W), and an outlier week is
    left out of that mean rather than becoming the level."""
    import forecast_log
    src = inspect.getsource(inventory.get_claude_insights)
    assert "_latest_is_anomaly" in src and "Do not project from it." in src
    assert "waste_next_week(" in src and "_anoms" in src
    # the flagged week does not move the forecast
    assert forecast_log.waste_next_week([400, 410, 390, 1400], anomalies={3}) == 400.0
    assert forecast_log.waste_next_week([400, 410, 390, 1400]) == 650.0


def test_a_page_render_never_moves_the_inventory_value_cogs_brackets_on(db_path, monkeypatch):
    """inv_value is the anchor cogs.inventory_value_near brackets a COGS
    window on. The render-time snapshot was rewriting it, so the food cost
    percentage moved because somebody opened a tab — and the write landed
    BEFORE the CFO evidence was assembled in the same call, so a render
    changed the closing inventory its own food cost % was then computed from.
    Two consecutive loads produced 7.6% and 31.4% from identical underlying
    data. Caught in end-to-end verification, not by a unit test."""
    rid = _restaurant(db_path)
    _snapshot(db_path, rid, days_ago=0, waste=100.0, inv_value=9000.0)
    src = inspect.getsource(inventory.get_claude_insights)
    assert "inv_value=COALESCE(inv_value, ?)" in src, \
        "the render-time write must not clobber the scheduled value"

    # And the scheduled writer still sets it on a row that has none.
    conn = models.get_conn(db_path)
    conn.execute("UPDATE inventory_history SET inv_value=NULL WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(inventory, "analysis_for",
                        lambda r: ([{"item": "X"}], True,
                                   {"total_waste_cost_week": 10.0, "waste_items": [],
                                    "total_stock_value": 7777.0}))
    fci.weekly_snapshot(rid, db_path=db_path)
    conn = models.get_conn(db_path)
    got = conn.execute("SELECT inv_value FROM inventory_history WHERE restaurant_id=?",
                       (rid,)).fetchone()["inv_value"]
    conn.close()
    assert got == 7777.0


def test_the_insight_no_longer_runs_ddl_on_the_render_path():
    """CREATE TABLE plus two ALTER TABLEs ran inside the insight on every
    single view, despite models.init_db declaring the table. waste_trend
    removed exactly this cost with its own _SCHEMA_ENSURED guard."""
    src = inspect.getsource(inventory.get_claude_insights)
    assert "CREATE TABLE IF NOT EXISTS inventory_history" not in src
    assert "ALTER TABLE inventory_history" not in src


# ── P1-1/P1-2/P1-3 · the AI sees the other engines ─────────────────────────

def test_the_prompt_carries_food_cost_percent_margins_coverage_and_cross_module():
    src = inspect.getsource(inventory.get_claude_insights)
    assert "import food_cost_intelligence as _fci" in src
    for token in ("FOOD COST POSITION", "PROFITABILITY", "WHERE THE MONEY IS",
                  "HOW FAR THESE FIGURES CAN BE TRUSTED",
                  "WHAT THE OTHER MODULES RECORDED OVER THE SAME PERIOD"):
        assert token in src, f"the prompt no longer carries {token}"


def test_the_prompt_forbids_inventing_a_cause_and_reranking():
    src = inspect.getsource(inventory.get_claude_insights)
    assert "Never state a cause that is not in the ROOT-CAUSE READ" in src
    assert "ALREADY RANKED" in src
    assert "Do not promote a cheaper or easier item above a more expensive one" in src


def test_the_insight_reads_diagnoses_rather_than_generating_them():
    """Producing one is a Sonnet call over the ranked drivers and belongs on
    the scheduler, not on the critical path of a page load."""
    src = inspect.getsource(inventory.get_claude_insights)
    assert "get_diagnosis(restaurant_id, include_stale=True)" in src
    assert "_fci.diagnose(" not in src and "_fci2.diagnose(" not in src


# ── P1-5 · the benchmark denominator ────────────────────────────────────────

def test_the_waste_rate_uses_real_windowed_purchases_when_available():
    """total_purchased summed each item's LAST order, whenever that was. A
    restaurant ordering fortnightly had a denominator covering two weeks
    against a numerator covering one, so its waste rate read roughly half the
    truth — and that figure drives the Excellent/Concerning label."""
    items = [{"item": "Ribeye", "category": "protein", "par_level": 10, "current_stock": 5,
              "unit_cost": 10.0, "avg_daily_usage": 2, "last_order_qty": 100,
              "waste_last_week": 4, "case_size": 1}]
    loose = inventory.analyse_inventory(items)
    tight = inventory.analyse_inventory(items, purchases_window=200.0)
    # $40 of waste against $1,000 of "last orders" vs against $200 really received.
    assert loose["waste_rate_pct"] == 4.0
    assert tight["waste_rate_pct"] == 20.0
    assert "delivery ledger" in tight["purchases_basis"]
    assert "varies by item" in loose["purchases_basis"]


def test_analysis_for_supplies_the_real_purchases_and_count_dates(db_path, monkeypatch):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Ribeye")
    _event(db_path, rid, iid, "receiving", 30, days_ago=2)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE ingredients SET last_recount_at=? WHERE id=?",
                 ((date.today() - timedelta(days=3)).isoformat(), iid))
    conn.commit()
    conn.close()
    seen = {}
    monkeypatch.setattr(inventory, "analyse_inventory",
                        lambda items, **kw: seen.update(kw) or {"waste_items": [], "critical_low": [],
                                                                "reorder_soon": []})
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant",
                        lambda r: ([{"item": "Ribeye"}], True))
    inventory.analysis_for(rid)
    assert seen["purchases_window"] == 300.0, "30 units at $10 received in the window"
    assert seen["counted_to"] is not None, "the real count date must reach the window label"


# ── P1-6 / P2-3 · one annualization, and figures that agree ────────────────

def test_the_displayed_annual_is_the_displayed_monthly_times_twelve():
    """monthly was rounded to whole dollars and annual was computed from the
    UNROUNDED monthly, so the two shown side by side on the iOS hero
    disagreed by up to several dollars."""
    items = [{"item": "Herbs", "category": "produce", "par_level": 10, "current_stock": 5,
              "unit_cost": 1.37, "avg_daily_usage": 1, "last_order_qty": 10,
              "waste_last_week": 7, "case_size": 1}]
    a = inventory.analyse_inventory(items)
    assert a["annual_recoverable"] == round(a["recoverable_monthly"] * 12, 2)
    assert a["annual_waste_projection"] == round(a["monthly_waste_projection"] * 12, 2)


def test_the_projection_says_it_is_one_week_extrapolated():
    a = inventory.analyse_inventory([{"item": "X", "category": "pantry", "par_level": 1,
                                      "current_stock": 1, "unit_cost": 1, "avg_daily_usage": 1,
                                      "last_order_qty": 1, "waste_last_week": 0, "case_size": 1}])
    assert "a single week, not a trend" in a["projection_basis"]


# ── P2-10 · the window label ────────────────────────────────────────────────

def test_count_age_is_reported_separately_from_the_waste_window():
    """Two different windows, and the first attempt at this fix conflated
    them. week_start/week_end label the period the WASTE figures cover, and
    for a ledger restaurant that genuinely is the trailing seven days —
    recompute_rollups sums waste over exactly today-6..today. Relabelling it
    with the count dates renamed a correct seven-day window after a single
    count day. The count date is a different fact: how far the CURRENT STOCK
    figures can be trusted. Caught in self-review, not by a test."""
    items = [{"item": "X", "category": "pantry", "par_level": 1, "current_stock": 1,
              "unit_cost": 1, "avg_daily_usage": 1, "last_order_qty": 1,
              "waste_last_week": 0, "case_size": 1}]
    old = (date.today() - timedelta(days=21)).isoformat()
    a = inventory.analyse_inventory(items, counted_from=old, counted_to=old)
    # The waste window stays the trailing seven days...
    assert a["week_end"] == date.today().strftime("%-m/%-d/%y")
    assert a["week_start"] == (date.today() - timedelta(days=6)).strftime("%-m/%-d/%y")
    # ...and the count age is its own, separate, reported fact.
    assert a["window_from_counts"] is True
    assert a["window_age_days"] == 21
    assert "21 days ago" in a["stock_basis"]

    bare = inventory.analyse_inventory(items)
    assert bare["window_from_counts"] is False
    assert bare["window_age_days"] == 0
    assert "No count dates on file" in bare["stock_basis"], \
        "an absent count must never read as a fresh one"


# ── P3-3 · the portion-variance engine ──────────────────────────────────────

def test_the_inferred_gap_is_attributed_to_ingredients_not_aggregated_away(db_path):
    """record_recount already wrote the theoretical-vs-actual gap per
    ingredient. waste_sources aggregated it into one restaurant-wide dollar
    figure and discarded which ingredient produced it — so the module held
    the measurement of over-portioning and could not name it."""
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Ribeye", unit_cost=12.0)
    _event(db_path, rid, iid, "depletion", 100, days_ago=3)
    _event(db_path, rid, iid, "waste", 20, days_ago=2, source="inferred")
    out = il.inferred_variance(rid)
    assert len(out["ingredients"]) == 1
    e = out["ingredients"][0]
    assert e["ingredient"] == "Ribeye"
    assert e["variance_pct"] == 20.0
    assert e["cost"] == 240.0
    assert e["material"] is True
    assert out["material_monthly_cost"] > 0


def test_a_small_or_cheap_variance_is_not_called_material(db_path):
    """A 30% variance on $4 of parsley is a distraction from a 9% variance on
    the ribeye."""
    rid = _restaurant(db_path)
    cheap = _ingredient(db_path, rid, "Parsley", unit_cost=0.20)
    _event(db_path, rid, cheap, "depletion", 100, days_ago=3)
    _event(db_path, rid, cheap, "waste", 30, days_ago=2, source="inferred")
    noisy = _ingredient(db_path, rid, "Flour", unit_cost=50.0)
    _event(db_path, rid, noisy, "depletion", 1000, days_ago=3)
    _event(db_path, rid, noisy, "waste", 20, days_ago=2, source="inferred")
    out = il.inferred_variance(rid)
    assert out["material"] == [], "30% of $6 and 2% of anything are both below the floor"


def test_a_variance_with_no_theoretical_usage_reports_no_percentage(db_path):
    """No recipe depleted this ingredient, so there is no baseline to call
    the gap large or small against. Reported with pct=None rather than
    dropped or divided by zero."""
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Truffle", unit_cost=200.0)
    _event(db_path, rid, iid, "waste", 2, days_ago=2, source="inferred")
    out = il.inferred_variance(rid)
    e = out["ingredients"][0]
    assert e["variance_pct"] is None
    assert e["material"] is False


def test_the_variance_names_the_dishes_that_consume_the_ingredient(db_path):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Ribeye", unit_cost=12.0)
    _event(db_path, rid, iid, "depletion", 100, days_ago=3)
    _event(db_path, rid, iid, "waste", 20, days_ago=2, source="inferred")
    conn = models.get_conn(db_path)
    mid = conn.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)",
                       (rid, "Ribeye Steak")).lastrowid
    conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) "
                 "VALUES (?,?,?)", (mid, iid, 1.0))
    conn.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                 "VALUES (?,?,?,?)", (rid, mid, (date.today() - timedelta(days=2)).isoformat(), 80))
    conn.commit()
    conn.close()
    e = il.inferred_variance(rid)["material"][0]
    assert e["dishes"][0]["dish"] == "Ribeye Steak"
    assert e["dishes"][0]["share"] == 1.0


def test_waste_sources_now_carries_the_ingredient_breakdown(db_path):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Ribeye", unit_cost=12.0)
    _event(db_path, rid, iid, "depletion", 100, days_ago=3)
    _event(db_path, rid, iid, "waste", 20, days_ago=2, source="inferred")
    out = il.waste_sources(rid)
    assert out["inferred"] == 240.0
    assert out["top_inferred"], "the single dollar figure was the one number nobody could act on"
    assert out["top_inferred"][0]["ingredient"] == "Ribeye"


# ── Cost drivers: ranked in Python ──────────────────────────────────────────

def test_drivers_are_ranked_by_dollars_then_confidence_then_ease():
    """The prompt said "ranked by dollar impact" over an unordered
    concatenation of three loops, and nothing checked the order that came
    back. Ranking is arithmetic; it belongs in Python."""
    src = inspect.getsource(fci.cost_drivers)
    assert 'drivers.sort(key=lambda d: (-d["dollars_monthly"]' in src
    assert "_CONF.get" in src and "_DIFF.get" in src


def test_every_driver_carries_evidence_confidence_difficulty_and_consequence(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {
        "waste_items": [{"item": "Ribeye", "recoverable_cost": 60.0, "waste_cost": 120.0,
                         "waste_pct": 30, "waste_tolerance_pct": 15}],
        "order_reduction": [], "overstock": []}))
    out = fci.cost_drivers(rid, db_path=db_path)
    assert out["available"] is True
    d = out["drivers"][0]
    for key in ("dollars_monthly", "confidence", "difficulty", "evidence", "if_ignored"):
        assert d.get(key), f"a driver without {key} cannot answer 'why this first'"
    assert d["dollars_monthly"] == round(60.0 * 52 / 12, 2)


def test_a_failed_driver_source_is_reported_not_swallowed(db_path, monkeypatch):
    """Each of the four driver blocks ended in a bare `except Exception: pass`.
    The ranking is the product here, so a silently dropped source does not
    merely lose a line — it can change which opportunity an owner is told to
    do first, with nothing anywhere saying a source was missing."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {
        "waste_items": [{"item": "Ribeye", "recoverable_cost": 60.0, "waste_cost": 120.0,
                         "waste_pct": 30, "waste_tolerance_pct": 15}],
        "order_reduction": [], "overstock": []}))

    def boom(*a, **k):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(il, "inferred_variance", boom)

    out = fci.cost_drivers(rid, db_path=db_path)
    assert "portion" in out["degraded_sources"]
    assert out["complete"] is False
    # And the prompt block says the ranking is incomplete rather than
    # presenting a partial list as the whole picture.
    assert "incomplete" in fci._drivers_block(out)


def test_a_complete_run_reports_no_degraded_sources(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {
        "waste_items": [{"item": "Ribeye", "recoverable_cost": 60.0, "waste_cost": 120.0,
                         "waste_pct": 30, "waste_tolerance_pct": 15}],
        "order_reduction": [], "overstock": []}))
    out = fci.cost_drivers(rid, db_path=db_path)
    assert out["degraded_sources"] == []
    assert out["complete"] is True
    assert "incomplete" not in fci._drivers_block(out)


def test_a_driver_below_the_dollar_floor_is_not_reported(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {
        "waste_items": [{"item": "Parsley", "recoverable_cost": 1.0, "waste_cost": 2.0,
                         "waste_pct": 30, "waste_tolerance_pct": 15}],
        "order_reduction": [], "overstock": []}))
    out = fci.cost_drivers(rid, db_path=db_path)
    assert out["drivers"] == []
    assert "floor" in out["reason"]


def test_sample_data_produces_no_drivers(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], False, {}))
    out = fci.cost_drivers(rid, db_path=db_path)
    assert out["available"] is False
    assert "sample data" in out["reason"]


def test_cost_drivers_returns_one_shape_on_every_path(db_path, monkeypatch):
    """An early return with a narrower shape is how fix_first ended up meaning
    two different things depending on which branch produced it. Every caller
    reads one contract."""
    rid = _restaurant(db_path)
    keys = {"available", "drivers", "total_monthly", "min_driver_dollars",
            "degraded_sources", "complete", "basis", "reason"}
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], False, {}))
    assert keys <= set(fci.cost_drivers(rid, db_path=db_path)), "sample-data path"
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {
        "waste_items": [], "order_reduction": [], "overstock": []}))
    assert keys <= set(fci.cost_drivers(rid, db_path=db_path)), "empty-but-live path"
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {
        "waste_items": [{"item": "Ribeye", "recoverable_cost": 60.0, "waste_cost": 120.0,
                         "waste_pct": 30, "waste_tolerance_pct": 15}],
        "order_reduction": [], "overstock": []}))
    assert keys <= set(fci.cost_drivers(rid, db_path=db_path)), "populated path"


# ── Profitability: the number an owner asks for ─────────────────────────────

def test_no_profitability_projection_without_every_input(db_path):
    """A projection built on a substituted zero looks exactly like a real
    one."""
    rid = _restaurant(db_path)
    out = fci.profitability_projection(rid, db_path=db_path)
    assert out["available"] is False
    assert out.get("reason")
    assert "prime cost, not net profit" in out["basis"]


def test_a_projection_too_early_in_the_month_is_refused(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(fci, "MIN_DAYS_FOR_PROJECTION", 99)
    out = fci.profitability_projection(rid, db_path=db_path)
    assert out["available"] is False
    assert "into the month" in out["reason"]


def test_the_projection_is_labelled_a_forecast(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: (60000.0, None))
    monkeypatch.setattr(cogs, "build_food_cost_pct",
                        lambda r, days=28, db_path=None, today=None: {
                            "ok": True, "cogs": 18000.0, "pct": 30.0, "target": 28.0})
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant",
                        lambda r: {"is_live": True, "total_sales": 60000.0,
                                   "overall_labor_pct": 30.0})
    out = fci.profitability_projection(rid, db_path=db_path)
    assert out["available"] is True
    assert out["claim_kind"] == "forecast"
    assert out["prime_cost_pct"] == 60.0
    assert out["projected_sales"] > 0


# ── Weekday, seasonal and supplier intelligence ─────────────────────────────

def test_a_thin_weekday_bucket_reports_counts_and_withholds_its_share(db_path):
    """A single Friday event at 100% of that day's waste is not a Friday
    pattern."""
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Ribeye", unit_cost=10.0)
    _event(db_path, rid, iid, "waste", 5, days_ago=2)
    out = fci.weekday_waste(rid, db_path=db_path)
    day = out["by_weekday"][0]
    assert day["events"] == 1
    assert day["share"] is None
    assert day["below_floor"] is True
    assert out["worst_day"] is None


def test_a_concentrated_weekday_is_measured_against_a_seven_day_week(db_path):
    """Measuring against days-with-data inverted the test: waste landing on
    exactly two days gave a baseline of 50%, so Thursday carrying 69% of
    every waste dollar failed to clear it and the module reported "spread
    fairly evenly across the week" — a statement that was not merely
    unhelpful but false, hiding exactly the concentration this exists to
    find. Caught in live verification, not by a unit test."""
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Salmon", unit_cost=10.0)
    # Two weekdays only, one clearly dominant.
    thursdays = [d for d in range(1, 40)
                 if (date.today() - timedelta(days=d)).strftime("%a") == "Thu"][:4]
    fridays = [d for d in range(1, 40)
               if (date.today() - timedelta(days=d)).strftime("%a") == "Fri"][:4]
    for d in thursdays:
        _event(db_path, rid, iid, "waste", 10, days_ago=d)
    for d in fridays:
        _event(db_path, rid, iid, "waste", 4, days_ago=d)
    out = fci.weekday_waste(rid, db_path=db_path)
    assert out["worst_day"] is not None, "69% of waste on one day is a concentration"
    assert out["worst_day"]["weekday"] == "Thursday"
    assert out["even_share"] == round(1 / 7, 3), "the baseline is a seven-day week"
    assert out["days_with_waste"] == 2
    block = fci._pattern_block(out, {"available": False, "reason": "x"})
    assert "spread fairly evenly" not in block
    assert "only 2 days of the week" in block


def test_waste_on_a_couple_of_days_is_never_called_an_even_spread(db_path):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Salmon", unit_cost=10.0)
    mondays = [d for d in range(1, 40)
               if (date.today() - timedelta(days=d)).strftime("%a") == "Mon"][:3]
    tuesdays = [d for d in range(1, 40)
                if (date.today() - timedelta(days=d)).strftime("%a") == "Tue"][:3]
    for d in mondays + tuesdays:
        _event(db_path, rid, iid, "waste", 5, days_ago=d)
    out = fci.weekday_waste(rid, db_path=db_path)
    block = fci._pattern_block(out, {"available": False, "reason": "x"})
    assert "spread fairly evenly" not in block
    assert "only 2 days of the week" in block


def test_seasonal_refuses_without_a_real_prior_year_window(db_path):
    rid = _restaurant(db_path)
    for i in range(4):
        _snapshot(db_path, rid, days_ago=i * 7, waste=100)
    out = fci.seasonal_baseline(rid, db_path=db_path)
    assert out["available"] is False
    assert "last year" in out["reason"]


def test_supplier_comparison_needs_two_suppliers_at_the_same_unit(db_path):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Ribeye", unit_cost=10.0, unit="lb", supplier="A")
    _ingredient(db_path, rid, "Ribeye", unit_cost=13.0, unit="lb", supplier="B")
    out = fci.supplier_comparison(rid, db_path=db_path)
    assert out["available"] is True
    c = out["comparisons"][0]
    assert c["cheapest_supplier"] == "A" and c["dearest_supplier"] == "B"
    assert c["spread_pct"] == 30.0


def test_supplier_comparison_refuses_across_mismatched_units(db_path):
    """ingredients.unit is free text and nothing converts between a case and
    a pound, so comparing across them is meaningless."""
    rid = _restaurant(db_path)
    _ingredient(db_path, rid, "Ribeye", unit_cost=10.0, unit="lb", supplier="A")
    _ingredient(db_path, rid, "Ribeye", unit_cost=130.0, unit="case", supplier="B")
    assert fci.supplier_comparison(rid, db_path=db_path)["available"] is False


# ── Forecasts that get scored ───────────────────────────────────────────────

def test_a_forecast_is_stored_and_later_scored(db_path):
    """A forecast nobody checks costs nothing to get wrong, which is the
    opposite of what a projection is for."""
    rid = _restaurant(db_path)
    # Last week: closed, so scorable. (This used today's date, and the old
    # scorer scored a week still in progress against its partial figure.)
    last_week = date.today() - timedelta(days=7)
    fci.record_forecast(rid, "waste_week", last_week.isoformat(), 400.0, basis="test", db_path=db_path)
    _snapshot(db_path, rid, days_ago=7, waste=500.0)
    out = fci.score_forecasts(rid, db_path=db_path)
    assert out["scored"] == 1
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT predicted, actual, error_pct, signed_error_pct FROM forecast_log").fetchone()
    conn.close()
    assert row["predicted"] == 400.0 and row["actual"] == 500.0
    # One denominator — the actual — for both error figures (CA2 #5).
    assert row["error_pct"] == 20.0 and row["signed_error_pct"] == -20.0


def test_a_week_still_in_progress_is_never_scored(db_path):
    rid = _restaurant(db_path)
    fci.record_forecast(rid, "waste_week", date.today().isoformat(), 400.0, basis="test", db_path=db_path)
    _snapshot(db_path, rid, days_ago=0, waste=500.0)
    assert fci.score_forecasts(rid, db_path=db_path)["scored"] == 0


def test_forecast_accuracy_says_nothing_until_it_has_scored_enough(db_path):
    rid = _restaurant(db_path)
    out = fci.forecast_accuracy(rid, db_path=db_path)
    assert out["available"] is False
    assert "not enough scored forecasts" in out["reason"]


# ── The diagnosis ───────────────────────────────────────────────────────────

def test_a_diagnosis_naming_a_driver_it_was_not_given_is_rejected():
    """A CFO's paragraph is only worth more than a summary because every
    claim in it traces to a measured line."""
    with pytest.raises(ValueError, match="named no driver"):
        fci._validate_diagnosis(
            {"headline": "Costs are up", "cause": "the lobster is over-portioned",
             "confidence": "high"},
            ["Ribeye", "Salmon"], prompt="", restaurant_id=1)


def test_a_diagnosis_citing_a_real_driver_is_kept():
    out = fci._validate_diagnosis(
        {"headline": "Ribeye waste is the story", "cause": "Ribeye is wasting above tolerance",
         "alternative_cause": "a bad count", "what_would_confirm": "recount on Friday",
         "recommended_action": "drop the par", "expected_outcome": "waste falls in two weeks",
         "confidence": "medium"},
        ["Ribeye", "Salmon"], prompt="", restaurant_id=1)
    assert out["cited_drivers"] == ["Ribeye"]
    assert out["confidence"] == "medium"


def test_a_diagnosis_stating_an_unsourced_figure_is_flagged_and_downgraded():
    out = fci._validate_diagnosis(
        {"headline": "Ribeye is costing $9,400 a month", "cause": "Ribeye over-portioning",
         "confidence": "high"},
        ["Ribeye"], prompt="Ribeye: $240/month recoverable", restaurant_id=1)
    assert out["unsupported_figures"], "$9,400 was never in the input"
    assert out["confidence"] == "low"


def test_an_unknown_confidence_falls_to_low():
    out = fci._validate_diagnosis(
        {"headline": "x", "cause": "Ribeye", "confidence": "certain"},
        ["Ribeye"], prompt="", restaurant_id=1)
    assert out["confidence"] == "low"


def test_the_diagnose_prompt_requires_an_alternative_and_forbids_reranking():
    p = fci.DIAGNOSE_PROMPT
    assert "alternative_cause" in p
    assert "what_would_confirm" in p
    assert "Do not re-rank them" in p
    assert "State no figure" in p
    assert "An owner acting on a confident guess about their food cost loses real money" in p


def test_an_empty_cross_module_block_says_there_is_no_data():
    """An empty block reads to a model as 'nothing notable happened', which
    is a different claim from 'we have no data'."""
    block = fci._operational_block({"labor": None, "reviews": None, "marketing": None, "notes": []})
    assert "NO operational evidence" in block
    assert "medium" in block


def test_sample_labor_is_refused_rather_than_reported(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant",
                        lambda r: {"is_live": False, "overall_labor_pct": 31.0})
    ctx = fci.operational_context(rid, db_path=db_path)
    assert ctx["labor"] is None
    assert any("sample data" in n for n in ctx["notes"])


def test_a_missing_food_cost_percent_tells_the_model_not_to_estimate_one():
    block = fci._position_block({"ok": False, "missing": [
        {"component": "closing inventory", "why": "no count in range"}]})
    assert "Do not state or estimate a food cost percentage" in block
    assert "closing inventory" in block


def test_a_missing_projection_tells_the_model_not_to_estimate_one():
    block = fci._profit_block({"available": False, "reason": "net sales missing"})
    assert "Do not state or estimate a profitability figure" in block


# ── The executive brief ─────────────────────────────────────────────────────

def test_the_brief_answers_the_eight_questions_or_says_why_not(db_path):
    rid = _restaurant(db_path)
    out = fci.executive_brief(rid, db_path=db_path)
    for key in ("what_changed", "why", "fix_first", "money_involved", "improved",
                "worsened", "needs_attention_now", "can_wait", "trust"):
        assert key in out, f"the morning brief cannot answer '{key}'"
    # With no data every answer is an explicit absence, never a fabricated one.
    assert out["why"]["cause"] is None
    assert out["money_involved"].get("monthly_at_stake") is None
    assert out["money_involved"].get("reason")


def test_fix_first_has_one_shape_on_every_branch(db_path, monkeypatch):
    """`now[0]` carried "what" and the raw-driver fallback carried "label", so
    a consumer reading fix_first["what"] — the Ask Cavnar tool and the morning
    brief both do — got nothing on exactly the path where every driver was
    expensive but hard to fix, which is the case an owner most needs answered.
    Caught in self-review, not by a test."""
    rid = _restaurant(db_path)
    # A driver that is neither low-effort nor over $200: lands in `can_wait`,
    # leaving `needs_attention_now` empty.
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {
        "waste_items": [], "order_reduction": [], "overstock": []}))
    monkeypatch.setattr(fci, "cost_drivers", lambda r, db_path=None: {
        "available": True, "total_monthly": 90.0, "reason": None,
        "drivers": [{"kind": "menu", "label": "Risotto runs at 44% food cost",
                     "dollars_monthly": 90.0, "confidence": "high", "difficulty": "high",
                     "evidence": "e", "if_ignored": "i"}]})
    out = fci.executive_brief(rid, db_path=db_path)
    assert out["needs_attention_now"] is None
    assert out["fix_first"] is not None
    assert out["fix_first"]["what"] == "Risotto runs at 44% food cost"
    assert out["fix_first"]["dollars_monthly"] == 90.0


def test_the_projection_says_the_labor_share_came_from_another_period(db_path, monkeypatch):
    """The labor share comes from the shift analysis's own period, not the
    days being projected. That assumption is the weakest link in the whole
    projection, and an owner reading "prime cost 39%" is entitled to know
    which half of it was measured over these days."""
    rid = _restaurant(db_path)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: (60000.0, None))
    monkeypatch.setattr(cogs, "build_food_cost_pct",
                        lambda r, days=28, db_path=None, today=None: {
                            "ok": True, "cogs": 18000.0, "pct": 30.0, "target": 28.0})
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant",
                        lambda r: {"is_live": True, "total_sales": 60000.0,
                                   "overall_labor_pct": 30.0,
                                   "date_range": {"start": "2026-08-01", "end": "2026-08-28"}})
    out = fci.profitability_projection(rid, db_path=db_path)
    assert "not a labor measurement of these specific days" in out["basis"]
    assert "not net profit" in out["basis"]
    # M/D/YY in owner-facing text (T7, B6#13); the ISO bounds travel apart.
    assert out["labor_period"] == "8/1/26 to 8/28/26"
    assert (out["labor_period_start"], out["labor_period_end"]) == ("2026-08-01", "2026-08-28")
    # And the month-over-month comparison says which half of prime cost moved.
    if out.get("dollars_vs_last_month") is not None:
        assert "food cost component only" in out["comparison_basis"]


def test_the_brief_separates_what_needs_attention_now_from_what_can_wait(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {
        "waste_items": [{"item": "Ribeye", "recoverable_cost": 60.0, "waste_cost": 120.0,
                         "waste_pct": 30, "waste_tolerance_pct": 15}],
        "order_reduction": [], "overstock": []}))
    out = fci.executive_brief(rid, db_path=db_path)
    assert out["needs_attention_now"], "a low-effort driver is this week's work"
    assert out["fix_first"]["what"] == "Ribeye waste above tolerance"


# ── Tenant isolation ────────────────────────────────────────────────────────

def test_current_stock_can_be_scoped_to_a_restaurant(db_path):
    """It read and returned a financial quantity keyed on nothing but an
    integer id — one unguarded future caller from computing one restaurant's
    stock from another's events."""
    rid_a = _restaurant(db_path, rid=1)
    rid_b = _restaurant(db_path, rid=2)
    iid = _ingredient(db_path, rid_a, "Ribeye", current_stock=7)
    conn = models.get_conn(db_path)
    got_right = il._compute_current_stock(conn, iid, rid_a)
    got_wrong = il._compute_current_stock(conn, iid, rid_b)
    conn.close()
    assert got_right == 7
    assert got_wrong == 0.0, "another restaurant must read nothing, not this one's stock"


def test_the_variance_query_never_crosses_tenants(db_path):
    rid_a = _restaurant(db_path, rid=1)
    rid_b = _restaurant(db_path, rid=2)
    iid = _ingredient(db_path, rid_a, "Ribeye", unit_cost=12.0)
    _event(db_path, rid_a, iid, "depletion", 100, days_ago=3)
    _event(db_path, rid_a, iid, "waste", 20, days_ago=2, source="inferred")
    assert il.inferred_variance(rid_b)["ingredients"] == []


# ── Performance ─────────────────────────────────────────────────────────────

def test_rollups_read_the_ledger_in_one_aggregate_not_six_queries():
    """Six statements x every ingredient x every restaurant, every night, was
    the dominant cost of the nightly depletion sync."""
    src = inspect.getsource(il.recompute_rollups)
    assert src.count("conn.execute(") <= 4, "the per-ingredient fan-out is back"
    assert "n_depletion" in src and "dep_days" in src and "recv_qty" in src


def test_menu_profitability_is_bounded(db_path):
    """It selected every active menu item, joined every recipe row and every
    sale in the window, and serialised the lot to a phone."""
    src = inspect.getsource(il.menu_profitability)
    assert "LIMIT ?" in src
    assert "MENU_PAGE_SIZE" in src
    out = il.menu_profitability(_restaurant(db_path))
    assert out["truncated"] is False
    assert out["page_size"] == il.MENU_PAGE_SIZE
