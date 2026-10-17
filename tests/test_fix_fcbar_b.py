"""Food Cost + Bar audit 10/16/26, workstream B — the food cost engine, its
intelligence and its evidence. One test (or a few) per finding fixed, each
citing its ID (register: /Users/simp/fc_bar_audit_1016/01-fc-calc.md,
04-pos-time.md, 05-ai.md, 07-sec-scale.md)."""
import inspect
import json
import os
import sys
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import models
from models import Restaurant, create_restaurant, get_conn

# Imported at collection, while models.get_conn is still the real one.
import backoffice_ap as bo  # noqa: E402
import backoffice_counts as bc  # noqa: E402
import cogs  # noqa: E402
import food_cost_intelligence as fci  # noqa: E402
import inventory  # noqa: E402
import inventory_ledger as il  # noqa: E402
import rec_trust  # noqa: E402
import waste_trend  # noqa: E402
import client_api  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T = date(2026, 9, 22)            # a past as-of day: the window ends on it


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)      # noqa: E731
    for mod in list(sys.modules.values()):
        f = str(getattr(mod, "__file__", None) or "")
        if mod is not None and (getattr(mod, "get_conn", None) is real or
                                (f.startswith(_REPO) and callable(getattr(mod, "get_conn", None)))):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path, name="Fix B Co", **kw):
    kw.setdefault("module_inventory", 1)
    return create_restaurant(Restaurant(name=name, owner_email="b@x.test", **kw), db_path=db_path)


def _x(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _sales(db_path, rid, first, last, value=1000.0, skip=(), final=1):
    d = first
    while d <= last:
        if d not in skip:
            _x(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, sales, final) VALUES (?,?,?,?)",
               (rid, d.isoformat(), value, final))
        d += timedelta(days=1)


def _snapshot(db_path, rid, day, stock, recount_day, waste=None, cost=5.0):
    items = [{"item": "Beef", "category": "Protein", "unit_cost": cost, "current_stock": stock,
              "last_recount_at": recount_day.isoformat() if recount_day else None, "waste_last_week": 0}]
    _x(db_path, "INSERT INTO inventory_history (restaurant_id, waste_json, week_end, items_json, inv_value, source) "
                "VALUES (?,?,?,?,?, 'scheduled')",
       (rid, json.dumps({"total_waste_cost": waste, "top_items": []}), day.isoformat(), json.dumps(items),
        stock * cost))


def _ledger_kitchen(db_path, counted=True, bev=None):
    """A ledger kitchen: Beef at $5, 100 lb at the close of T-7, 90 lb at T,
    10 lb received on T-3, $1,000 a night of sales; full counts on both
    edges when `counted`, else the ledger's own figures."""
    rid = _rid(db_path)
    iid = il.create_ingredient(rid, "Beef", category="Protein", unit="lb", unit_cost=5.0, current_stock=100)
    il.record_receiving(rid, iid, 10, event_date=T - timedelta(days=3))
    _sales(db_path, rid, T - timedelta(days=70), T)
    old = date(2026, 6, 1)
    _snapshot(db_path, rid, T - timedelta(days=7), 100, (T - timedelta(days=7)) if counted else old)
    _snapshot(db_path, rid, T, 90, T if counted else old)
    if bev:
        d = T - timedelta(days=70)
        while d <= T:
            for metric, v in (("sales.cat:Beer", bev), ("sales.cat:Food", 1000.0 - bev)):
                _x(db_path, "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                            "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, d.isoformat(), metric, v))
            d += timedelta(days=1)
    return rid


# ── FC-CALC-01: Back Office food cost honours the window it is asked for ──

PRODUCE_COA = (
    "GLAccountSubTypeName,FinancialStatementTypeName,CorporateID,MainAccountNumber,GLAccountDescription,"
    "UseForWebIndicator,UnitID\n"
    "Purchase,Income and Expense Statement,7101,4010,4010 PRODUCE,True,7101\n"
)


def _bo_kitchen(db_path, counts, nights=None, first=date(2026, 8, 1), last=date(2026, 10, 15)):
    rid = _rid(db_path)
    bo.read_file(rid, "coa.csv", PRODUCE_COA.encode(), db_path=db_path)
    d = first
    while d <= last:
        if nights is None or d.weekday() in nights:
            _x(db_path, "INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                        "VALUES (?,?,?,?, 'ready', 'rpower')", (rid, d.isoformat(), "sales.cat:Food", 1000.0))
        d += timedelta(days=1)
    bo.save_invoice_book(rid, {"invoices": [
        {"vendor": "GREENLEAF PRODUCE", "number": f"7700{i}", "date": day.isoformat(), "total": "$3,000.00",
         "items": [{"sku": "1", "name": "LETTUCE", "gl": "4010 PRODUCE", "unit": "Case", "qty": "100", "amt": "30",
                    "ext": "3000.00"}]}
        for i, day in enumerate([date(2026, 9, 5), date(2026, 9, 15), date(2026, 9, 25), date(2026, 10, 5),
                                 date(2026, 10, 12)])]}, db_path=db_path)

    def cnt(end, food):
        return {"start": (end - timedelta(days=6)).strftime("%m/%d/%Y"), "end": end.strftime("%m/%d/%Y"),
                "status": "Submitted", "gls": [{"gl": "4011", "name": "PRODUCE INVENTORY", "amount": f"{food:.2f}"}]}
    bc.save_counts(rid, {"inventory_counts": [cnt(e, f) for e, f in counts]}, db_path=db_path)
    return rid


def test_fc_calc_01_monthly_counts_never_answer_for_october_to_date(db_path):
    rid = _bo_kitchen(db_path, [(date(2026, 8, 31), 4000.0), (date(2026, 9, 30), 5000.0)])
    oct_ = cogs.build_food_cost_pct(rid, days=16, today=date(2026, 10, 16), db_path=db_path)
    assert oct_["ok"] is False and "bracket" in oct_["missing"][0]["why"]
    # September asked as September is measured, and covers what was asked.
    sep = cogs.build_food_cost_pct(rid, days=30, today=date(2026, 9, 30), db_path=db_path)
    assert sep["ok"] and (sep["start"], sep["end"]) == ("2026-09-01", "2026-09-30") and sep["covers_asked"]
    assert cogs.covers_window(sep, date(2026, 9, 1), date(2026, 9, 30))
    # The prime cost and outcomes callers refuse another period's COGS.
    got, why = cogs.window_cogs(sep, date(2026, 10, 1), date(2026, 10, 15))
    assert got is None and "bracket" in why
    # A week inside a monthly count period is not that week's figure.
    week = cogs.build_food_cost_pct(rid, days=7, today=date(2026, 9, 20), db_path=db_path)
    assert week["ok"] is False


def test_fc_calc_01_a_count_without_food_is_never_an_anchor(db_path):
    rid = _bo_kitchen(db_path, [(date(2026, 9, 29), 6000.0)])
    bc.save_counts(rid, {"inventory_counts": [{"start": "10/07/2026", "end": "10/13/2026", "status": "Submitted",
                                               "gls": [{"gl": "4321", "name": "BEER INVENTORY", "amount": "950"}]}]},
                   db_path=db_path)
    out = cogs.build_food_cost_pct(rid, days=14, today=date(2026, 10, 14), db_path=db_path)
    assert out["ok"] is False and out["closing"] is None


def test_fc_calc_01_the_projection_reports_cogs_missing_for_a_month_its_counts_dont_bracket(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(fci, "_local_today", lambda r: date(2026, 10, 17))
    monkeypatch.setattr(fci, "forecast_accuracy", lambda *a, **k: None)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: (16000.0, None))
    # September's count period answering for October: the old shape.
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda r, days=28, db_path=None, today=None: {
        "ok": True, "cogs": 8000.0, "pct": 26.7, "start": "2026-09-01", "end": "2026-09-30", "window_days": 30})
    out = fci.profitability_projection(rid, db_path=db_path)
    assert out["available"] is False
    assert "food cost for this month" in [m["component"] for m in out["missing"]]


# ── FC-CALC-16: trading days on Back Office ───────────────────────────────

def test_fc_calc_16_a_kitchen_closed_sunday_and_monday_is_measured(db_path):
    rid = _bo_kitchen(db_path, [(date(2026, 9, 16), 4000.0), (date(2026, 10, 14), 4500.0)],
                      nights={1, 2, 3, 4, 5})            # Tue-Sat
    out = cogs.build_food_cost_pct(rid, days=28, today=date(2026, 10, 14), db_path=db_path)
    assert out["ok"], out["missing"]
    assert out["food_sales"] == 20 * 1000.0 and out["sales_estimate"] is None


# ── FC-CALC-02: a ledger kitchen anchors only on full counts ──────────────

def test_fc_calc_02_the_ledgers_own_stock_is_theoretical_never_cogs(db_path):
    rid = _ledger_kitchen(db_path, counted=False)
    out = cogs.build_food_cost_pct(rid, days=7, today=T, db_path=db_path)
    assert out["ok"] is False and out["pct"] is None and out["counted"] is False
    comps = {m["component"] for m in out["missing"]}
    assert comps == {"a counted opening inventory", "a counted closing inventory"}
    th = out["theoretical"]
    assert th and th["label"] == "by recipe (theoretical)" and th["cogs"] == 500.0 + 50.0 - 450.0


def test_fc_calc_02_two_full_counts_measure_it(db_path):
    rid = _ledger_kitchen(db_path, counted=True)
    out = cogs.build_food_cost_pct(rid, days=7, today=T, db_path=db_path)
    assert out["ok"] and out["counted"] and out["cogs"] == 100.0
    assert out["pct"] == round(100.0 / 7000.0 * 100, 1)
    assert "counted opening inventory" in out["basis"]


def test_fc_calc_02_a_snapshot_is_a_count_only_when_its_items_were_recounted():
    items = [{"item": "Beef", "category": "Protein", "unit_cost": 5, "current_stock": 10,
              "last_recount_at": "2026-09-21"},
             {"item": "Onion", "category": "Produce", "unit_cost": 1, "current_stock": 5,
              "last_recount_at": "2026-08-01"}]
    w = waste_trend._week_from_row("2026-09-22", json.dumps({"total_waste_cost": 5}), json.dumps(items))
    assert w["counted"] and w["counted_on"] == "2026-09-21", "the 5am snapshot reads yesterday's count"
    items[0]["last_recount_at"] = "2026-08-01"
    w = waste_trend._week_from_row("2026-09-22", json.dumps({"total_waste_cost": 5}), json.dumps(items))
    assert not w["counted"] and not cogs.is_counted(w)


# ── FC-CALC-11: one food sales denominator on both paths ──────────────────

def test_fc_calc_11_the_inventory_path_divides_by_food_sales(db_path):
    rid = _ledger_kitchen(db_path, counted=True, bev=400.0)
    out = cogs.build_food_cost_pct(rid, days=7, today=T, db_path=db_path)
    assert out["ok"] and out["sales_basis"] == "net_less_beverage"
    assert out["net_sales"] == 7000.0 and out["food_sales"] == 7 * 600.0
    assert out["pct"] == round(100.0 / 4200.0 * 100, 1)
    assert "less the nightly report's drink sales" in out["basis"]


def test_fc_calc_11_without_drink_sales_on_file_the_basis_says_net(db_path):
    rid = _ledger_kitchen(db_path, counted=True)
    out = cogs.build_food_cost_pct(rid, days=7, today=T, db_path=db_path)
    assert out["sales_basis"] == "net_sales" and "drink sales aren't separated" in out["basis"]


# ── FC-CALC-22 / POS-15: purchases and sales over the counted period ──────

def test_fc_calc_22_purchases_run_from_the_day_after_the_opening_count(db_path, monkeypatch):
    rid = _ledger_kitchen(db_path, counted=True)
    seen = []
    real = cogs.purchases_in_window
    monkeypatch.setattr(cogs, "purchases_in_window",
                        lambda r, s, e, db_path=None: seen.append((s, e)) or real(r, s, e, db_path=db_path))
    out = cogs.build_food_cost_pct(rid, days=10, today=T, db_path=db_path)
    # Opening count at T-7 (3 days inside the asked window's edge): the
    # window is the counted period, T-6..T.
    assert seen == [(T - timedelta(days=6), T)]
    assert (out["start"], out["end"]) == ((T - timedelta(days=6)).isoformat(), T.isoformat())
    assert out["asked_start"] == (T - timedelta(days=9)).isoformat()


# ── FC-CALC-15 / POS-02 / POS-17: one window end, the last complete day ───

def test_fc_calc_15_the_window_ends_on_the_last_complete_business_day(db_path):
    rid = _rid(db_path)
    real = cogs.local_today(rid)
    assert cogs.last_complete_day(rid) < real
    assert cogs.last_complete_day(rid, T) == T, "a past as-of day is its own end"
    out = cogs.build_food_cost_pct(rid, db_path=db_path)
    assert out["end"] == cogs.last_complete_day(rid).isoformat()


def test_pos_02_the_live_path_sums_only_the_windows_complete_days(db_path, monkeypatch):
    import pos
    rid = _rid(db_path)
    today = cogs.local_today(rid)
    start = today - timedelta(days=27)
    live = {(start + timedelta(days=i)).isoformat(): 1000.0 for i in range(28)}   # today's half day included
    monkeypatch.setattr(pos, "fetch_business_days", lambda *a, **k: (dict(live), "square"))
    total, why = cogs.net_sales_in_window(rid, start, today)
    assert total == 27000.0 and why is None


# ── POS-04: a missing or provisional day is scaled and said, never $0 ─────

def test_pos_04_a_missing_trading_day_is_scaled_and_labelled(db_path):
    rid = _rid(db_path)
    _sales(db_path, rid, T - timedelta(days=80), T, skip={T - timedelta(days=5)})
    total, note = cogs.net_sales_in_window(rid, T - timedelta(days=27), T)
    assert total == 28000.0 and "27 of 28" in note and "estimate" in note


def test_pos_04_a_provisional_night_is_missing_not_whole(db_path):
    rid = _rid(db_path)
    _sales(db_path, rid, T - timedelta(days=80), T, skip={T - timedelta(days=5)})
    _x(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, sales, final) VALUES (?,?,?,0)",
       (rid, (T - timedelta(days=5)).isoformat(), 150.0))
    total, note = cogs.net_sales_in_window(rid, T - timedelta(days=27), T)
    assert total == 28000.0 and note


# ── FC-CALC-14 / POS-03 / AI-19: the month-end projection ─────────────────

def test_fc_calc_14_the_projection_scales_by_the_days_in_its_figures(db_path, monkeypatch):
    rid = _rid(db_path)
    _sales(db_path, rid, date(2026, 8, 1), date(2026, 10, 6))
    monkeypatch.setattr(fci, "_local_today", lambda r: date(2026, 10, 7))
    monkeypatch.setattr(fci, "forecast_accuracy", lambda *a, **k: None)
    monkeypatch.setattr(fci, "MIN_DAYS_FOR_PROJECTION", 1)
    asked = []
    real_ns = cogs.net_sales_in_window
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda r, s, e: asked.append((s, e)) or real_ns(r, s, e))
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda r, days=28, db_path=None, today=None: {
        "ok": True, "cogs": 1800.0, "pct": 30.0, "window_days": days,
        "start": (today - timedelta(days=days - 1)).isoformat(), "end": today.isoformat()})
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda r: {
        "is_live": True, "total_sales": 6000.0, "overall_labor_pct": 30.0,
        "date_range": {"start": "2026-09-08", "end": "2026-10-06", "days": 28}})
    monkeypatch.setattr(fci, "_labor_stale_why", lambda *a, **k: None)
    out = fci.profitability_projection(rid, db_path=db_path)
    assert asked[0] == (date(2026, 10, 1), date(2026, 10, 6)), "sales through the last complete day"
    assert out["days_elapsed"] == 6 and out["net_sales_mtd"] == 6000.0
    assert out["projected_sales"] == 31000, "6 days of $1,000 x 31/6 (a flat weekday pattern)"
    assert "weekday pattern" in out["basis"]
    # AI-19: labor carried as a share is an estimate, never "measured".
    facts = {f.key: f for f in fci.typed_facts(profitability=out)}
    assert out["labor_basis"] == "analysis_share"
    assert facts["food.profit.prime_cost_pct"].kind == "estimate"
    assert facts["food.profit.labor_pct"].kind == "estimate"


def test_ai_19_a_weekend_heavy_profile_weights_the_run_rate(db_path):
    rid = _rid(db_path)
    d = date(2026, 8, 1)
    while d <= date(2026, 10, 6):
        _x(db_path, "INSERT INTO labor_daily_history (restaurant_id, date, sales, final) VALUES (?,?,?,1)",
           (rid, d.isoformat(), 3000.0 if d.weekday() >= 4 else 1000.0))
        d += timedelta(days=1)
    scale, words = fci._month_scale(rid, date(2026, 10, 1), date(2026, 10, 6), 31, db_path=db_path)
    # 10/1-10/6 holds Fri/Sat/Sun (3 heavy days of 6): the month's weight over it.
    assert "weekday pattern" in words and scale < 31 / 6


# ── FC-CALC-12: an unlogged week is no figure, never $0 ───────────────────

def test_fc_calc_12_a_snapshot_with_nothing_logged_stores_null_and_leaves_the_trend(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: (
        [{"item": "Beef", "category": "Protein", "unit_cost": 5.0, "current_stock": 10, "waste_last_week": 0,
          "waste_basis": "none"}], True,
        {"total_waste_cost_week": 0, "waste_items": [], "total_stock_value": 50.0, "food_stock_value": 50.0}))
    out = fci.weekly_snapshot(rid, db_path=db_path)
    assert out["ok"] and out["waste"] is None
    row = get_conn(db_path).execute("SELECT waste_json FROM inventory_history WHERE restaurant_id=?",
                                    (rid,)).fetchone()
    assert json.loads(row["waste_json"])["total_waste_cost"] is None
    weeks, _t = waste_trend.load_waste_history(rid, db_path=db_path)
    assert weeks == [], "an unmeasured week is not charted"
    stats = waste_trend.waste_trend_stats([
        {"week_end": f"2026-09-{d:02d}", "label": "w", "start_label": "s", "waste": v}
        for d, v in ((6, 100.0), (13, 110.0), (20, None), (27, 105.0), (28, None))])
    assert stats["weeks"] == 3 and stats["best"]["waste"] == 100.0


# ── FC-CALC-23 / AI-03: the snapshot's stock is food only, never negative ─

def test_fc_calc_23_the_snapshot_writes_the_food_stock_value(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(inventory, "analysis_for", lambda r: (
        [{"item": "Tito's Vodka", "category": "Liquor"}], True,
        {"total_waste_cost_week": 0, "waste_items": [], "total_stock_value": 3080.0, "food_stock_value": 80.0}))
    assert fci.weekly_snapshot(rid, db_path=db_path)["inv_value"] == 80.0
    row = get_conn(db_path).execute("SELECT inv_value FROM inventory_history WHERE restaurant_id=?",
                                    (rid,)).fetchone()
    assert row["inv_value"] == 80.0


def test_fc_calc_23_the_loader_clamps_negative_stock():
    items = [{"item": "Beef", "category": "Protein", "unit_cost": 5, "current_stock": 10},
             {"item": "Onion", "category": "Produce", "unit_cost": 1, "current_stock": -40}]
    w = waste_trend._week_from_row("2026-09-22", json.dumps({"total_waste_cost": 1}), json.dumps(items), 999.0)
    assert w["inv_value"] == 50.0


# ── AI-07: complete, non-overlapping weeks ────────────────────────────────

def test_ai_07_the_newest_overlapping_week_is_never_compared():
    weeks = [{"week_end": "2026-09-20", "label": "9/20", "start_label": "9/14", "waste": 110.0},
             {"week_end": "2026-09-27", "label": "9/27", "start_label": "9/21", "waste": 100.0},
             {"week_end": "2026-10-04", "label": "10/4", "start_label": "9/28", "waste": 120.0},
             {"week_end": "2026-10-06", "label": "10/6", "start_label": "9/30", "waste": 400.0}]
    waste_trend._mark_partial(weeks)
    assert weeks[-1]["partial"] and not weeks[2].get("partial")
    stats = waste_trend.waste_trend_stats(weeks)
    assert stats["wow_delta"] == 20.0, "9/28-10/4 against 9/21-9/27, never Tue-Mon against Mon-Sun"
    obs = waste_trend.waste_trend_observations(stats, weeks)
    assert any("the week ending 10/4" in o["text"] for o in obs)
    import forecast_log
    assert forecast_log.waste_next_week([w["waste"] for w in waste_trend.complete_weeks(weeks)]) == 110.0
    src = inspect.getsource(inventory.get_claude_insights)
    assert 'if not w.get("partial")' in src, "the insight's forecast reads complete weeks"


# ── AI-04: a draft recipe never becomes an over-portioning cause ──────────

def test_ai_04_a_gap_against_a_draft_recipe_is_provisional(db_path, monkeypatch):
    v = {"material": [{"ingredient": "Brisket", "monthly_cost": 300.0, "variance_pct": 25.0, "gap_qty": 5,
                       "unit": "lb", "theoretical_qty": 20, "dishes": [], "recounts": 3,
                       "recipe_lines": 2, "recipe_unreviewed_lines": 2, "draft_only": True}]}
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {"waste_items": []}))
    monkeypatch.setattr(il, "inferred_variance", lambda r: v)
    rid = _rid(db_path)
    drv = fci.cost_drivers(rid, db_path=db_path)
    d = next(x for x in drv["drivers"] if x["kind"] == "portion")
    assert d["provisional"] and d["draft_only"] and "Cavnar AI draft" in d["evidence"]
    assert "provisional" in d["evidence_input"]["flags"]
    import confidence_engine as ce
    assert ce.evidence(**d["evidence_input"])["pct"] <= ce.PARTIAL_CAP
    block = fci._drivers_block(drv)
    assert "Do not name over-portioning as its cause" in block


def test_ai_04_the_ledger_reads_each_flagged_ingredients_recipe_provenance(db_path):
    rid = _rid(db_path)
    iid = il.create_ingredient(rid, "Brisket", category="Protein", unit="lb", unit_cost=8.0, current_stock=20)
    mid = _x(db_path, "INSERT INTO menu_items (restaurant_id, name, sell_price) VALUES (?,?,?)",
             (rid, "Brisket Sammy", 18))
    _x(db_path, "INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit, source) VALUES (?,?,?,?)",
       (mid, iid, 0.5, "draft_accepted"))
    today = il.local_today(rid)
    _x(db_path, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
                "VALUES (?,?,?,?,?,?)", (rid, iid, "depletion", 20.0, today.isoformat(), "pos"))
    _x(db_path, "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
                "VALUES (?,?,?,?,?,?)", (rid, iid, "waste", 6.0, today.isoformat(), "inferred"))
    out = il.inferred_variance(rid)
    e = next(x for x in out["ingredients"] if x["ingredient"] == "Brisket")
    assert e["material"] and (e["recipe_lines"], e["recipe_unreviewed_lines"], e["draft_only"]) == (1, 1, True)


# ── AI-05: one waste event is one offender week ───────────────────────────

def test_ai_05_one_sunday_event_is_one_week_not_two(db_path):
    rid = _rid(db_path)
    iid = il.create_ingredient(rid, "Brisket", category="Protein", unit="lb", unit_cost=8.0, current_stock=20)
    today = fci._local_today(rid)
    sunday = today - timedelta(days=today.weekday() + 8)          # a Sunday more than a week back
    il.record_waste(rid, iid, 2.0, event_date=sunday)
    for k in range(7):                                             # the 7 daily rows that carry it
        day = sunday + timedelta(days=k)
        _x(db_path, "INSERT INTO inventory_history (restaurant_id, week_end, waste_json) VALUES (?,?,?)",
           (rid, day.isoformat(), json.dumps({"total_waste_cost": 16.0, "top_items": ["Brisket"]})))
    ww = fci._waste_weeks(rid, db_path=db_path)
    assert ww["brisket"] == 1 and ww["_all"] == 1


# ── AI-06: weeks of counts are weeks with a count ─────────────────────────

def test_ai_06_eight_weeks_of_snapshots_and_one_count_is_one_week(db_path):
    rid = _rid(db_path)
    today = cogs.local_today(rid)
    counted_on = today - timedelta(days=20)
    for k in range(0, 56, 1):
        day = today - timedelta(days=k)
        _snapshot(db_path, rid, day, 50, counted_on, waste=10.0)
    assert waste_trend.count_weeks(rid, 8, db_path=db_path) == 1
    ev = client_api.food_read_evidence(rid)
    assert ev["n"] == 1 and "1 week with an inventory count" in ev["basis"]
    assert rec_trust.food_diagnosis_input(rid, {"confidence": "medium"}, db_path=db_path)["n"] == 1


# ── AI-13: a line's confidence is the driver it names ─────────────────────

def test_ai_13_a_line_binds_to_the_driver_it_names(db_path):
    rid = _rid(db_path)
    menu = {"kind": "menu", "item": "Brisket Sammy", "label": "Brisket Sammy runs at 44% food cost",
            "recipe_lines": 6, "recipe_unreviewed_lines": 5}
    waste = {"kind": "waste", "item": "Salmon", "label": "Salmon waste above tolerance", "weeks_of_data": 8}
    drivers = [menu, waste]
    assert client_api._line_driver("Reprice the Brisket Sammy this week.", drivers) is menu
    assert client_api._line_driver("Cut the salmon order by a case.", drivers) is waste
    assert client_api._line_driver("Count the walk-in Friday.", drivers) is None
    text = "1. Reprice the Brisket Sammy this week.\n2. Cut the salmon order by a case."
    items = client_api.insight_rec_items(rid, text, "insight_food", "food", "food", promote=False,
                                         evidence={"n": 8, "kind": "weeks", "basis": "8 weeks"}, drivers=drivers)
    by = {it["bound_driver"]: it["confidence"]["dimensions"]["evidence"]["pct"] for it in items}
    assert by[menu["label"]] < by[waste["label"]]


# ── AI-14: a low-band diagnosis is an association ─────────────────────────

def test_ai_14_a_low_confidence_read_is_never_most_likely(monkeypatch):
    dg = {"cause": "Over-portioning on the brisket", "confidence": "low", "stale": False}
    assert rec_trust.diagnosis_anchor_strength(dg, by_band=True) == "association"
    assert rec_trust.diagnosis_anchor_strength(dict(dg, confidence="medium"), by_band=True) == "likely"
    assert rec_trust.diagnosis_anchor_strength(dg) == "likely", "the review surfaces keep their own rule"
    monkeypatch.setattr(fci, "get_diagnosis", lambda r, include_stale=False, **k: dict(dg, as_of="10/16/26"))
    block, cause, alt = inventory.root_cause_block(1)
    assert "Most likely" not in block and "hypothesis" in block and cause == []


# ── AI-16: the ranking copy names what weighs it ──────────────────────────

def test_ai_16_the_ranking_says_answers_and_results():
    src = inspect.getsource(fci.cost_drivers) + fci.DIAGNOSE_SYSTEM + fci.DIAGNOSE_USER
    assert "how that kind of fix has measured here" not in src
    assert "answers" in fci.DIAGNOSE_SYSTEM and "answers" in fci.DIAGNOSE_USER


# ── AI-17: improved is a move; fix first is the top driver ────────────────

def _two_drivers(monkeypatch, easy_dollars):
    monkeypatch.setattr(inventory, "analysis_for", lambda r: ([], True, {"waste_items": []}))
    monkeypatch.setattr(fci, "cost_drivers", lambda r, db_path=None: {
        "available": True, "total_monthly": 190.0 + easy_dollars, "reason": None, "drivers": [
            {"kind": "menu", "label": "Risotto runs at 44% food cost", "dollars_monthly": 190.0,
             "confidence": "medium", "difficulty": "high", "evidence": "e", "if_ignored": "i"},
            {"kind": "waste", "label": "Parsley waste above tolerance", "dollars_monthly": easy_dollars,
             "confidence": "medium", "difficulty": "low", "evidence": "e", "if_ignored": "i"}]})


def test_ai_17_a_16_dollar_easy_driver_never_goes_first(db_path, monkeypatch):
    rid = _rid(db_path)
    _two_drivers(monkeypatch, 16.0)
    assert fci.executive_brief(rid, db_path=db_path)["fix_first"]["what"] == "Risotto runs at 44% food cost"
    _two_drivers(monkeypatch, 160.0)
    assert fci.executive_brief(rid, db_path=db_path)["fix_first"]["what"] == "Parsley waste above tolerance"


def test_ai_17_a_position_under_target_is_not_improved(db_path, monkeypatch):
    rid = _rid(db_path)
    _two_drivers(monkeypatch, 16.0)
    monkeypatch.setattr(fci, "build_evidence", lambda r, db_path=None: {
        "drivers": fci.cost_drivers(r), "food_cost": {"ok": True, "pct": 28.0, "target": 30.0, "variance_pts": -2.0,
                                                      "start": "2026-09-01", "window_days": 28},
        "profitability": {"available": True, "dollars_vs_last_month": 0, "prime_cost_pct": 60.0,
                          "prev_month_prime_pct": 60.0},
        "seasonal": {}, "coverage": {}, "waste_sources": {}, "forecast_accuracy": None})
    monkeypatch.setattr(cogs, "build_food_cost_pct", lambda *a, **k: {"ok": False})
    out = fci.executive_brief(rid, db_path=db_path)
    assert out["improved"] is None and out["worsened"] is None


# ── AI-18: the diagnosis prompt's date is M/D/YY ──────────────────────────

def test_ai_18_the_diagnosis_prompt_dates_m_d_yy():
    src = inspect.getsource(fci.diagnosis_plan)
    assert '"%B %d, %Y"' not in src and "_mdy_dx(" in src


# ── SCALE-07: the nightly diagnoses are queue rows ────────────────────────

def test_scale_07_the_pass_queues_and_the_briefs_wait_for_the_rows(db_path, monkeypatch):
    import job_queue
    import scheduler
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    ids = [_rid(db_path, name=f"Q{i}") for i in range(3)]
    out = fci.fan_out_diagnoses(ids, today=date(2026, 10, 16))
    assert out["queued"] == 3 and out["attempted"] == 3
    assert job_queue.HANDLERS[fci.DIAGNOSIS_QUEUE_KIND] == "food_cost_intelligence:run_queued_diagnosis"
    assert fci.diagnoses_pending("2026-10-16") == 3
    from datetime import datetime

    class _Ops:
        def settled(self, job, day):
            return True
    assert scheduler._morning_input_pending(_Ops(), datetime(2026, 10, 16, 6, 30)) == "food_cost_diagnoses"
    monkeypatch.setattr(fci, "diagnose", lambda r: {"ok": False, "reason": "nothing"})
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda r: False)
    job_queue.drain(("bulk",), "test-worker")
    assert fci.diagnoses_pending("2026-10-16") == 0
    assert scheduler._morning_input_pending(_Ops(), datetime(2026, 10, 16, 6, 30)) is None
    assert fci.run_queued_diagnosis(SimpleNamespace(payload={})) == {"skipped": "no restaurant"}


# ── SCALE-11: a degraded read is reported ─────────────────────────────────

def test_scale_11_a_failed_read_is_captured_before_it_degrades(monkeypatch):
    import ops
    caught = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: caught.append(k.get("job")))

    def boom(*a, **k):
        raise RuntimeError("no such column")
    monkeypatch.setattr(models, "get_conn", boom)
    assert cogs.beverage_sales_in_window(1, T, T) is None
    assert cogs._archived_net_sales(1, T - timedelta(days=6), T) is None
    assert caught == ["cogs.beverage_sales_in_window", "cogs.archived_net_sales"]
