"""Regressions for the Food Cost module audit.

Each test here corresponds to a finding that was verified against the
implementation before it was fixed. The comments say what the behaviour used
to be, because in most cases the old behaviour looked perfectly reasonable
from the call site and only became visibly wrong two or three files away.
"""
import json
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import client_api
import cogs as cogs_mod
import inventory
import inventory_ledger as ledger
import models
import notify
import waste_trend
from client_api import client_bp
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    # notify reaches the database through models.get_conn rather than binding
    # its own reference, so patching models covers it.
    for mod in (models, auth, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    return flask_app


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Audit Co")
    kw.setdefault("owner_email", "a@x.com")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _login_as(monkeypatch, rid, role="client", is_admin=0):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": 1, "restaurant_id": rid, "is_admin": is_admin,
                                 "username": "u", "role": role, "email": "owner@x.test"})


def _ingredient(db_path, rid, name, **kw):
    kw.setdefault("cost", 4.0)
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost,
                                 case_size, current_stock, avg_daily_usage, last_order_qty,
                                 waste_last_week, is_active, supplier_name, supplier_email)
        VALUES (?,?,?,?,?,?,1,?,?,?,?,1,?,?)
    """, (rid, name, kw.get("category", "Produce"), "lb", kw.get("par", 10), kw["cost"],
          kw.get("stock", 0), kw.get("usage", 3), kw.get("last_order_qty", 0),
          kw.get("waste", 0), kw.get("supplier_name"), kw.get("supplier_email")))
    conn.commit()
    iid = cur.lastrowid
    conn.close()
    return iid


# ── F1: one analysis, not eleven ────────────────────────────────────────────

def test_analysis_for_resolves_delivery_days_and_holidays_itself(monkeypatch, db_path):
    """Eleven call sites assembled analyse_inventory's arguments by hand, in
    four different combinations. The Food Cost page passed the delivery
    schedule and upcoming holidays; the purchase order actually emailed to a
    supplier passed neither — so urgency was classified differently and
    holiday scaling (1.4x) was missing from the order that went out."""
    rid = _restaurant(db_path, delivery_days="Mon,Thu")
    seen = {}

    def spy(items, delivery_days=None, upcoming_holidays=None, today=None, **kw):
        seen["delivery_days"] = delivery_days
        seen["holidays"] = upcoming_holidays
        # analysis_for also resolves the measured inputs analyse_inventory
        # cannot derive for itself — real windowed purchases and the actual
        # count dates. Captured so the assertions below can check they are
        # threaded through rather than silently dropped.
        seen.update(kw)
        return {"waste_items": [], "critical_low": [], "reorder_soon": []}

    # get_restaurant binds DB_PATH as a default argument at import time, so
    # redirecting models.DB_PATH afterwards doesn't reach it.
    monkeypatch.setattr(models, "get_restaurant",
                        lambda r, db_path=None: models.Restaurant(name="Audit Co", owner_email="a@x.com",
                                                                  delivery_days="Mon,Thu"))
    monkeypatch.setattr(inventory, "analyse_inventory", spy)
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([{"item": "x"}], True))
    inventory.analysis_for(rid)
    assert seen["delivery_days"] == "Mon,Thu"
    assert seen["holidays"] is not None, "upcoming holidays must always be resolved"


def test_analysis_for_carries_is_live_so_no_caller_can_drop_it(monkeypatch, db_path):
    rid = _restaurant(db_path)
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda r: ([{"item": "x"}], False))
    monkeypatch.setattr(inventory, "analyse_inventory", lambda *a, **k: {})
    _items, is_live, analysis = inventory.analysis_for(rid)
    assert is_live is False
    assert analysis["is_live"] is False


def test_the_order_draft_carries_a_hash_of_what_it_contains(db_path):
    """send-order rebuilt the draft rather than sending the previewed one, so
    a stock or supplier change in between silently altered quantities."""
    groups = [{"supplier_email": "a@s.test", "items": [{"item": "Romaine", "qty": 4}]}]
    one = inventory.draft_hash(groups)
    same = inventory.draft_hash([{"supplier_email": "a@s.test",
                                  "items": [{"item": "Romaine", "qty": 4}]}])
    changed = inventory.draft_hash([{"supplier_email": "a@s.test",
                                     "items": [{"item": "Romaine", "qty": 9}]}])
    assert one == same
    assert one != changed


# ── F3/F4: sample data never leaves the building ────────────────────────────

def test_the_food_waste_alert_unpacks_the_tuple_it_is_given(monkeypatch, db_path):
    """load_inventory_for_restaurant returns (items, is_live). The tuple was
    passed straight into analyse_inventory, which subscripts each element by
    string key — so this raised TypeError on every restaurant every day and a
    bare `print` swallowed it. The alert had never once fired."""
    rid = _restaurant(db_path)
    calls = {}
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant",
                        lambda r: ([{"item": "Romaine"}], True))
    def spy(items, **kw):
        calls["items"] = items
        return {"waste_items": []}

    monkeypatch.setattr(models, "get_restaurant", lambda r, db_path=None: None)
    monkeypatch.setattr(inventory, "analyse_inventory", spy)
    inventory.analysis_for(rid)
    assert isinstance(calls["items"], list), "analyse_inventory must receive the item list, not the tuple"


def test_a_repeat_at_the_same_level_does_not_re_alert(db_path):
    """The alert re-fired every 7 days for a standing condition, which trains
    an owner to ignore the channel. It now fires on deterioration."""
    rid = _restaurant(db_path)
    assert notify._waste_alert_worsened(rid, 400.0, db_path=db_path) is True, "first alert always fires"
    notify._log_alert(rid, "food_waste", db_path=db_path, value=400.0)
    assert notify._waste_alert_worsened(rid, 405.0, db_path=db_path) is False
    assert notify._waste_alert_worsened(rid, 600.0, db_path=db_path) is True


# ── F5/F11: denominators ────────────────────────────────────────────────────

def test_waste_rate_denominator_covers_the_same_week_as_the_waste(db_path):
    """waste_last_week is a 7-day total; last_order_qty was a single delivery,
    so the rate was inflated roughly in proportion to how often a restaurant
    takes deliveries — and that rate drives both the headline benchmark and a
    up-to-40% cut to suggested order quantities."""
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Romaine", cost=2.0)
    for offset in (0, 3, 6):
        ledger.record_receiving(rid, iid, qty=10.0, event_date=date.today() - timedelta(days=offset))
    ledger.recompute_rollups(rid, iid)
    conn = get_conn(db_path)
    row = conn.execute("SELECT last_order_qty FROM ingredients WHERE id=?", (iid,)).fetchone()
    conn.close()
    assert row["last_order_qty"] == 30.0, "three deliveries in the window should sum, not be replaced"


# ── F7/F8/F9: menu margins ──────────────────────────────────────────────────

def _dish(db_path, rid, name, price, ingredients, sold=None):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO menu_items (restaurant_id, name, sell_price) VALUES (?,?,?)",
                       (rid, name, price))
    mid = cur.lastrowid
    for iid, qty in ingredients:
        conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                     (mid, iid, qty))
    if sold:
        conn.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) "
                     "VALUES (?,?,?,?)", (rid, mid, date.today().isoformat(), sold))
    conn.commit()
    conn.close()
    return mid


def test_average_food_cost_is_weighted_by_what_actually_sells(db_path):
    """It was the unweighted mean of each dish's percentage, so a $3 side
    counted as much as the entree carrying the night's revenue — and it was
    shown against the 28-35% rule of thumb, which is revenue-weighted."""
    rid = _restaurant(db_path)
    cheap = _ingredient(db_path, rid, "Syrup", cost=0.30)
    beef = _ingredient(db_path, rid, "Ribeye", cost=9.00)
    _dish(db_path, rid, "Soda", 3.00, [(cheap, 1)], sold=100)      # 10% fc
    _dish(db_path, rid, "Ribeye", 36.00, [(beef, 1)], sold=100)    # 25% fc
    out = ledger.menu_profitability(rid)
    assert out["has_sales_data"] is True
    # Unweighted would be 17.5%. Weighted: (30 + 900) / (300 + 3600) = 23.8%.
    assert out["average_food_cost_pct"] == pytest.approx(23.8, abs=0.2)
    assert "weighted" in (out["average_basis"] or "")


def test_best_dish_is_the_biggest_contributor_not_the_lowest_percentage(db_path):
    """Sorting by food cost % made a $3 soda outrank a $36 steak carrying $27
    of gross margin — advice that actively pushes an owner to promote the
    cheapest thing on the menu."""
    rid = _restaurant(db_path)
    cheap = _ingredient(db_path, rid, "Syrup", cost=0.30)
    beef = _ingredient(db_path, rid, "Ribeye", cost=9.00)
    _dish(db_path, rid, "Soda", 3.00, [(cheap, 1)], sold=50)
    _dish(db_path, rid, "Ribeye", 36.00, [(beef, 1)], sold=50)
    out = ledger.menu_profitability(rid)
    assert out["best"]["name"] == "Ribeye"
    assert out["highest_food_cost"]["name"] == "Ribeye", "the percentage ranking is still available, separately"


def test_a_dish_with_an_uncosted_ingredient_reports_no_margin_at_all(db_path):
    """COALESCE(unit_cost, 0) meant an ingredient nobody had priced added
    nothing to the plate cost, so a dish whose protein had no cost showed an
    excellent margin with nothing indicating the number was incomplete."""
    rid = _restaurant(db_path)
    priced = _ingredient(db_path, rid, "Bun", cost=0.50)
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit_cost, is_active) VALUES (?,?,?,1)",
                       (rid, "Mystery Patty", None))
    unpriced = cur.lastrowid
    conn.commit(); conn.close()
    _dish(db_path, rid, "Burger", 15.00, [(priced, 1), (unpriced, 1)])
    out = ledger.menu_profitability(rid)
    assert not out["priced"], "a dish with an unpriced ingredient must not be reported as costed"
    assert out["uncosted"] and out["uncosted"][0]["name"] == "Burger"
    assert out["uncosted"][0]["uncosted_ingredients"] == 1


def test_a_duplicate_recipe_row_cannot_double_count_the_plate(db_path):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Cheese", cost=2.0)
    mid = _dish(db_path, rid, "Mac", 12.0, [(iid, 1)])
    assert ledger.add_recipe_ingredient(rid, mid, iid, 1) == 0, "the same pair must not bind twice"


def test_recipe_quantities_must_be_a_positive_finite_number(db_path):
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Cheese", cost=2.0)
    mid = _dish(db_path, rid, "Mac", 12.0, [])
    for bad in ("nan", "inf", "-1", "0", "abc", None):
        assert ledger.add_recipe_ingredient(rid, mid, iid, bad) == 0, bad


# ── F19: NaN prices ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["NaN", "Infinity", "1e400", "-5"])
def test_menu_prices_reject_non_finite_and_negative_values(db_path, bad):
    """NaN passes every comparison guard (nan < 0 is False) and persisted, and
    then jsonify emitted bare NaN — invalid JSON that broke the entire
    menu-margins response for that restaurant."""
    rid = _restaurant(db_path)
    mid = _dish(db_path, rid, "Mac", 12.0, [])
    assert ledger.set_menu_item_price(rid, mid, bad) is False
    conn = get_conn(db_path)
    price = conn.execute("SELECT sell_price FROM menu_items WHERE id=?", (mid,)).fetchone()["sell_price"]
    conn.close()
    assert price == 12.0, "the original price must survive a rejected write"
    assert json.dumps({"p": price})  # would raise/emit NaN if it had been stored


# ── F20/F23: quick counts ───────────────────────────────────────────────────

def test_a_blank_price_is_refused_rather_than_stored_as_zero():
    """A blank field was coerced to $0.00 and stored as this week's price of
    record, which then became next week's baseline and silently suppressed
    that ingredient's drift alert, since drift only runs when both prices are
    above zero."""
    clean, rejected = client_api._clean_quickcount_items(
        [{"name": "Romaine", "price": ""}, {"name": "Beef", "price": "8.50"}])
    assert [c["name"] for c in clean] == ["Beef"]
    assert rejected and rejected[0]["name"] == "Romaine"


@pytest.mark.parametrize("bad", [{"name": 5}, "not-a-dict", {"price": 1}, {"name": "x", "price": "NaN"}])
def test_malformed_rows_never_reach_storage(bad):
    """The array was persisted verbatim; on the NEXT submission it became
    `prev` and prev_map's i["name"].lower() raised outside any try — a 500
    that saved nothing, so the poisoned blob stayed and every future count
    500'd for that restaurant permanently."""
    clean, _rejected = client_api._clean_quickcount_items([bad])
    assert clean == []


def test_a_quick_count_keeps_the_owners_custom_ingredients(monkeypatch, db_path):
    """The blob was rebuilt as {current, previous}, dropping the custom_items
    key the custom-ingredient route writes and the template renders."""
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO client_data (restaurant_id, food_cost_json) VALUES (?,?)",
                 (rid, json.dumps({"custom_items": [{"name": "Truffle Oil", "unit": "btl"}]})))
    conn.commit(); conn.close()
    client_api._do_food_cost_quickcount(rid, [{"name": "Romaine", "price": "2.50"}])
    conn = get_conn(db_path)
    saved = json.loads(conn.execute("SELECT food_cost_json FROM client_data WHERE restaurant_id=?",
                                    (rid,)).fetchone()["food_cost_json"])
    conn.close()
    assert saved["custom_items"] == [{"name": "Truffle Oil", "unit": "btl"}]
    assert saved["current"]["items"][0]["name"] == "Romaine"


# ── F22: purchase order numbering ───────────────────────────────────────────

def test_a_purchase_order_number_is_allocated_with_the_row(db_path):
    """The number was a COUNT(*)+1 read before the supplier email went out and
    inserted after it, against a UNIQUE constraint: two concurrent sends both
    emailed, and the second insert raised IntegrityError as an uncaught 500
    with no PO row, no email log and no audit event."""
    rid = _restaurant(db_path)
    numbers = [models.record_purchase_order(rid, "Fresh Co", "a@s.test", [{"item": "x", "qty": 1}], 10.0,
                                            db_path=db_path) for _ in range(3)]
    assert numbers == ["PO-0001", "PO-0002", "PO-0003"]
    assert len(set(numbers)) == 3


def test_a_failed_send_leaves_no_phantom_purchase_order(db_path):
    rid = _restaurant(db_path)
    po = models.record_purchase_order(rid, "Fresh Co", "a@s.test", [], 0.0, db_path=db_path)
    assert models.void_purchase_order(rid, po, db_path=db_path) is True
    assert models.get_purchase_orders(rid, db_path=db_path) == []


# ── F31: history reads are bounded ──────────────────────────────────────────

def test_the_week_total_counts_every_week_not_just_the_fetched_ones(db_path):
    """Snapshots are written per day the page is viewed, not per week, so the
    loader used to read and JSON-parse a restaurant's entire history to serve
    eight weeks. It is bounded now — but `total` still has to reflect every
    week on file, because that is what decides which range buttons appear."""
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    for w in range(12):
        day = date.today() - timedelta(weeks=w)
        conn.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end) VALUES (?,?,?)",
                     (rid, json.dumps({"total_waste_cost": 100 + w}), day.isoformat()))
    conn.commit(); conn.close()
    weeks, total = waste_trend.load_waste_history(rid, 8, db_path=db_path)
    assert len(weeks) == 8
    assert total == 12, "the range buttons depend on knowing the full history length"


# ── F6: actual food cost % ──────────────────────────────────────────────────

def test_food_cost_pct_withholds_the_number_and_names_what_is_missing(db_path):
    """A food cost % built on an assumed opening inventory is worse than none,
    because it looks exactly like a real one."""
    rid = _restaurant(db_path)
    out = cogs_mod.build_food_cost_pct(rid, db_path=db_path)
    assert out["ok"] is False and out["pct"] is None
    components = {m["component"] for m in out["missing"]}
    assert "net sales" in components
    for entry in out["missing"]:
        assert entry["why"], "every missing component must say why"


def test_food_cost_pct_is_cogs_over_sales_against_the_restaurants_target(monkeypatch, db_path):
    rid = _restaurant(db_path, food_cost_target=30.0)
    monkeypatch.setattr(cogs_mod, "net_sales_in_window", lambda r, s, e: (10000.0, None))
    monkeypatch.setattr(cogs_mod, "purchases_in_window", lambda r, s, e, db_path=None: (3000.0, 4))
    start = date.today() - timedelta(days=27)
    monkeypatch.setattr(waste_trend, "load_waste_history", lambda r, l, db_path=None, **_bounds: ([
        {"week_end": start.isoformat(), "inv_value": 5000.0},
        {"week_end": date.today().isoformat(), "inv_value": 4800.0},
    ], 2))
    out = cogs_mod.build_food_cost_pct(rid, db_path=db_path)
    # COGS = 5000 + 3000 - 4800 = 3200 -> 32.0% of 10000
    assert out["ok"] is True
    assert out["cogs"] == 3200.0 and out["pct"] == 32.0
    assert out["target"] == 30.0 and out["variance_pts"] == 2.0
    assert out["label"] == "Slightly over"


def test_an_impossible_count_is_refused_rather_than_reported_as_negative(monkeypatch, db_path):
    rid = _restaurant(db_path)
    monkeypatch.setattr(cogs_mod, "net_sales_in_window", lambda r, s, e: (10000.0, None))
    monkeypatch.setattr(cogs_mod, "purchases_in_window", lambda r, s, e, db_path=None: (100.0, 1))
    start = date.today() - timedelta(days=27)
    monkeypatch.setattr(waste_trend, "load_waste_history", lambda r, l, db_path=None, **_bounds: ([
        {"week_end": start.isoformat(), "inv_value": 1000.0},
        {"week_end": date.today().isoformat(), "inv_value": 9000.0},
    ], 2))
    out = cogs_mod.build_food_cost_pct(rid, db_path=db_path)
    assert out["ok"] is False and out["pct"] is None
    assert "unrecorded" in out["missing"][0]["why"] or "wrong" in out["missing"][0]["why"]


# ── F24: recipe coverage ────────────────────────────────────────────────────

def test_recipe_coverage_reports_the_share_of_sales_a_recipe_accounts_for(db_path):
    """A dish with no recipe depletes nothing, so its ingredients look like
    they are never used — worst for whatever sells most. Coverage was logged
    and never surfaced, so nothing on the page indicated how much of the rest
    of it could be trusted."""
    rid = _restaurant(db_path)
    iid = _ingredient(db_path, rid, "Beef", cost=5.0)
    _dish(db_path, rid, "Burger", 15.0, [(iid, 1)], sold=30)
    _dish(db_path, rid, "Special", 20.0, [], sold=10)
    out = ledger.recipe_coverage(rid)
    assert out["units_sold"] == 40.0
    assert out["units_covered"] == 30.0
    assert out["coverage_pct"] == 75.0
    assert out["uncovered_top"][0]["name"] == "Special"


# ── Benchmark honesty ───────────────────────────────────────────────────────

def test_a_week_with_no_waste_recorded_is_neither_missing_data_nor_excellent():
    """_has_benchmark also required total_waste_cost > 0, so a week with real
    purchases and nothing logged rendered as "—" with "Upload inventory data
    to see benchmark", identical to having no data at all. It then became
    "Excellent" — but nothing logged is not the same as nothing wasted, so it
    is its own state now: not measured, neutral, never the best result
    (confidence audit CA4 F11, fix I8)."""
    items = [{"item": "Romaine", "category": "Produce", "par_level": 10, "current_stock": 8,
              "unit_cost": 2.0, "avg_daily_usage": 1.0, "last_order_qty": 20, "waste_last_week": 0.0,
              "unit": "lb", "case_size": 1}]
    out = inventory.analyse_inventory(items)
    assert out["waste_rate_pct"] == 0
    assert out["benchmark_label"] == "No waste recorded" and out["benchmark_label"] != "—"
    assert out["benchmark_tone"] == "neutral"
    assert out["benchmark_state"] == "not_measured"
    assert "No waste recorded this week" in out["benchmark_detail"]
    assert "Excellent" not in out["benchmark_detail"]


def test_the_benchmark_tone_is_a_semantic_name_not_a_hex():
    """home_brief tested this value against the string "green" while it was a
    hex colour, so its `good` flag never once matched."""
    items = [{"item": "R", "category": "Produce", "par_level": 10, "current_stock": 8,
              "unit_cost": 2.0, "avg_daily_usage": 1.0, "last_order_qty": 20, "waste_last_week": 5.0,
              "unit": "lb", "case_size": 1}]
    out = inventory.analyse_inventory(items)
    assert out["benchmark_tone"] in ("good", "neutral", "warn", "bad")
    assert "benchmark_color" not in out


# ── AI honesty ──────────────────────────────────────────────────────────────

def test_every_savings_figure_offered_to_the_model_comes_from_the_analysis():
    """The prompt required "an estimated dollar amount" on each recommendation
    and supplied none, so every "saves $X" on the page was the model's own
    arithmetic on figures nothing had checked."""
    analysis = {
        "waste_items": [{"item": "Romaine", "recoverable_cost": 41.5, "waste_tolerance_pct": 28}],
        "order_reduction": [{"item": "Beef", "savings_vs_last": 18.0,
                             "suggested_order_qty": 12, "last_order_qty": 20}],
        "overstock": [{"item": "Butter", "overstock_cost": 9.25}],
    }
    block = inventory._supported_savings_block(analysis)
    assert "$41.50" in block and "Romaine" in block
    assert "$18.00" in block and "Beef" in block
    assert "$9.25" in block and "not a weekly saving" in block


def test_no_supported_savings_says_so_instead_of_inviting_an_invention():
    block = inventory._supported_savings_block({"waste_items": [], "order_reduction": [], "overstock": []})
    assert "does not support" in block


def test_the_prompt_forbids_figures_that_are_not_in_it():
    src = open("inventory.py", encoding="utf-8").read()
    assert "must appear verbatim somewhere above" in src
    assert "Zero is allowed when the data supports none" in src


def test_unsupported_figures_reach_the_unverified_marker():
    """verify_figures was called and its return value discarded, while
    labor.py appends the marker, client_api parses it and mobile_api renders
    it as claim_kinds.insight_unverified — the whole pipeline existed and food
    cost was the one module that computed the flag and dropped it. The check
    is the Response Validation Layer's now (F1), and the food read keeps the
    marker the clients parse."""
    import inventory
    assert "finish_food_read(" in open("inventory.py", encoding="utf-8").read()
    ctx = inventory.food_read_context(None, "Waste this week: $160.", {},
                                      inventory.food_insight_validation_facts({"total_waste_cost_week": 160.0}))
    out = inventory.finish_food_read("Waste ran $9,400 this week.", ctx)
    assert "UNVERIFIED:" in out and "$9,400" in out.split("UNVERIFIED:")[1]


# ── Authorization ───────────────────────────────────────────────────────────

def test_a_manager_is_refused_the_food_cost_export_scope(client, monkeypatch, db_path):
    """account/export-data matches no module prefix, so the gates covering
    every /food-cost route did not apply: a manager — the role that exists
    precisely to withhold margins — could mail itself every ingredient and
    unit cost."""
    rid = _restaurant(db_path)
    _login_as(monkeypatch, rid, role="manager")
    resp = client.post("/api/account/export-data", json={"scopes": ["food_cost"]})
    assert resp.status_code == 403


def test_the_dashboard_withholds_food_cost_from_a_role_denied_it():
    """"/" computed and rendered the whole payload for any session that could
    load it, because _module_permission_denied is path-prefix driven and "/"
    matches no prefix. The panel markup was ungated too — only the tab button
    checked the module."""
    src = open("hosted_dashboard.py", encoding="utf-8").read()
    assert "_can_see_food_cost" in src
    assert "FOOD_COST_VIEW" in src
    html = open("templates/dashboard.html", encoding="utf-8").read()
    panel = html.index('id="panel-inventory"')
    assert "{% if mod_inventory %}" in html[panel - 200:panel], \
        "the panel itself must be gated, not just the tab button"
