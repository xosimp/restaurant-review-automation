"""Food Cost round (owner, 9/26/26): the hero, the waste trend shell, the
order of the lists, the waste row's controls, suppliers reaching the order
at once, the demo's three lists, and the two false "Unverified" notes."""
import re
from datetime import date, timedelta

import pytest

import response_validation as rv
import inventory

SRC = open("templates/dashboard.html", encoding="utf-8").read()
PANEL = SRC[SRC.index('id="panel-inventory"'):SRC.index("/panel-inventory")]


# ── the two false "Unverified" notes ────────────────────────────────────────

def _check(text, analysis):
    facts = inventory.food_insight_validation_facts(analysis)
    ctx = rv.ValidationContext(restaurant_id=None, surface="food_insight", facts=facts, context_text="",
                               policy={"action": "inventory_insight"})
    return [(f["rule"], f["span"]) for f in rv.validate(text, ctx).findings]


def test_a_saving_stated_as_less_is_not_a_contradiction():
    a = {"total_waste_cost_week": None, "order_reduction": [{"item": "Olive Oil", "savings_vs_last": 61.0}],
         "waste_items": [], "overstock": []}
    got = _check("Order 3 units of Olive Oil instead of 8 — about $61.00 less per order.", a)
    assert not [r for r in got if r[0] == "X1"], got
    # The wrong direction is still caught.
    got = _check("Order 3 units of Olive Oil instead of 8 — about $61.00 more per order.", a)
    assert ("X1", "$61.00") in got


def test_a_figure_in_parentheses_takes_its_own_period():
    wk = 47.899
    a = {"total_waste_cost_week": wk, "monthly_waste_projection": round(wk * 52 / 12, 2), "order_reduction": [],
         "waste_items": [], "overstock": []}
    got = _check("Waste sits at $47.90 for the week ($207.56 projected for the month, one week stretched).", a)
    assert not [r for r in got if r[0] in ("F3", "F1")], got


# ── the page ────────────────────────────────────────────────────────────────

def test_the_hero_says_less_and_colours_the_weeks_waste_by_its_target():
    assert 'id="fc2-today"' not in PANEL
    assert "{% set _wtone = 'good' if (inv.is_live and not _inv_stale and _wl in ['Under target','Near target'])" in PANEL
    assert "('bad' if (inv.is_live and not _inv_stale and _wl in ['Over target','Well over target'])" in PANEL
    # The dark .fc2-pos surface no longer paints the hero's food cost block.
    assert '.fc2-hero-fcp.fc2-pos,[data-theme="dark"] .fc2-hero-fcp.fc2-pos{background:none;' in SRC
    # Row for row: one grid, both blocks on its rows.
    assert ".fc2-hero-nums>#fc2-fcp{grid-column:1;grid-row:1/-1;display:grid;grid-template-rows:subgrid" in SRC
    assert ".fc2-waste-n .fc2-num-lbl{grid-row:4" in SRC and ".fc2-waste-n .delta{grid-row:5" in SRC


def test_the_waste_trend_shell_is_darker_than_its_card():
    assert '<section class="lb2-hero fc2-wt-shell" aria-label="Waste trend">' in PANEL
    assert ".lb2-hero.fc2-wt-shell .fc2-wt{margin-top:0;background:var(--surface)}" in SRC
    assert '[data-theme="dark"] .lb2-hero.fc2-wt-shell{background:#171411}' in SRC


def test_the_waste_and_over_par_lists_sit_under_the_order():
    assert PANEL.index('id="fc2-orders"') < PANEL.index('id="fc2-ledger-waste"') < PANEL.index('id="fc2-menu"')
    assert PANEL.index('id="fc2-ledger-stock"') < PANEL.index('id="fc2-menu"')


def test_the_waste_row_is_one_size_of_control_and_no_rule():
    load = SRC[SRC.index("function fc2LoadCountSheet(){"):SRC.index("function fc2LogWaste(btn){")]
    assert load.count("fc2-ctl") >= 3 and "fc2-ctl-btn" in load and 'style="max-width:90px"' not in load
    css = SRC[SRC.index(".fc2-waste-log{"):SRC.index(".fc2-waste-log{") + 400]
    assert "border-top" not in css.split("}")[0]
    assert "height:42px" in SRC[SRC.index(".fc2-block .fc2-ctl,#panel-inventory .fc2-ctl{"):][:300]
    assert "::-webkit-inner-spin-button{margin-left:10px}" in SRC


def test_a_supplier_change_reaches_send_to_suppliers_without_a_reload():
    assert "window.fc2ReloadOrderDraft=function(){if(document.getElementById('so-body'))loadOrderDraft();};" in SRC
    for fn in ("function fc2SupplierPick(sel){", "function fc2SupplierSaveNew(btn){", "function fc2AssignAllUnassigned(btn){"):
        body = SRC[SRC.index(fn):SRC.index("\n}\n", SRC.index(fn))]
        assert "fc2ReloadOrderDraft()" in body, fn
    sup = SRC[SRC.index("function fc2LoadSuppliers(){"):SRC.index("function fc2SupplierPost(")]
    assert 'class="ac-select fc2-ctl" data-sup-ing=' in sup


# ── the demo's three lists ──────────────────────────────────────────────────

@pytest.fixture
def demo(db_path, monkeypatch):
    import models
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = models.create_restaurant(models.Restaurant(name="Simple EJ's", owner_email="d@x.test", is_demo=1,
                                                     module_inventory=1), db_path=db_path)
    conn = real(db_path)
    today = date.today().isoformat()
    for i in range(12):
        # name, par, unit cost, stock (all comfortably between par and 1.3x par), usage, weekly order
        cur = conn.execute(
            "INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost, case_size, current_stock, "
            " avg_daily_usage, last_order_qty, waste_last_week, last_recount_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, f"Item {i}", "produce", "lb", 40.0, 2.0 + i, 1.0, 44.0, 4.0, 30.0, 0.0, today))
        conn.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
                     "VALUES (?,?,?,?,?,?)", (rid, cur.lastrowid, "recount", 44.0, (date.today() - timedelta(days=10)).isoformat(), "seed"))
    conn.commit()
    conn.close()
    return rid, db_path, real


def test_the_demo_gets_waste_an_urgent_order_and_cash_over_par_once_a_week(demo):
    import demo_seed
    rid, db_path, real = demo
    demo_seed._seed_ejs_food_showcase(rid, db_path)
    conn = real(db_path)
    week_ago = (date.today() - timedelta(days=6)).isoformat()
    waste = conn.execute("SELECT COUNT(DISTINCT ingredient_id) n FROM ingredient_stock_events WHERE restaurant_id=? "
                         "AND event_type='waste' AND event_date>=?", (rid, week_ago)).fetchone()["n"]
    rows = [dict(r) for r in conn.execute("SELECT current_stock, par_level, avg_daily_usage FROM ingredients "
                                          "WHERE restaurant_id=?", (rid,)).fetchall()]
    n_events = conn.execute("SELECT COUNT(*) n FROM ingredient_stock_events WHERE restaurant_id=?", (rid,)).fetchone()["n"]
    conn.close()
    assert waste >= 3
    assert any(r["current_stock"] < r["par_level"] and r["current_stock"] <= 2 * r["avg_daily_usage"] for r in rows)
    assert any(r["current_stock"] > 1.3 * r["par_level"] for r in rows)
    # Once a week: a second boot writes nothing.
    demo_seed._seed_ejs_food_showcase(rid, db_path)
    conn = real(db_path)
    assert conn.execute("SELECT COUNT(*) n FROM ingredient_stock_events WHERE restaurant_id=?", (rid,)).fetchone()["n"] == n_events
    conn.close()
