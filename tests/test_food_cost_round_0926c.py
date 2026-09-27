"""Owner round, 9/26/26 late: Approvals, the most likely cause from its AI
read, the Food Cost hero's figures and headings, the count and waste flow,
Send to suppliers folding, a null dish list, and recipes from the inventory
system."""
import sys
import types
from datetime import date

import pytest

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


def test_reviews_says_approvals_on_both_clients():
    assert "<span>Approvals</span>" in SRC and "How your replies were approved" not in SRC
    ios = open("ios/CavnarAI/CavnarAI/Features/Reviews/ResponseRingsChart.swift", encoding="utf-8").read()
    assert 'title: "Approvals"' in ios


def test_the_most_likely_cause_opens_from_its_ai_read():
    assert "#review-diagnosis,#lb2-diag,#guest-diag{display:none!important}" in SRC
    assert "var CAUSE_HOSTS=['review-diagnosis','lb2-diag','guest-diag'];" in SRC
    sync = _between("function causeSync(id){", "\n}\n")
    assert "What\\\\u2019s the most likely cause?" in sync or "most likely cause?" in sync
    opn = _between("function causeOpen(id){", "\n}\n")
    assert "cModal.open('cause-modal',{close:'causeClose'})" in opn and "bd.appendChild(host.firstChild)" in opn
    assert "new MutationObserver(function(){causeSync(id);})" in SRC


def test_the_hero_figures_share_a_line_and_headings_sit_under_them():
    fcp = _between("function fc2LoadFoodCostPct(", "\n}\n") if "function fc2LoadFoodCostPct(" in SRC else SRC
    assert "host.innerHTML='<div class=\"v '+tone+'\"><span class=\"hb-num\">'+d.pct+'%</span></div>'\n          +'<div class=\"k\">Food cost</div>'" in SRC
    assert "host.innerHTML='<div class=\"v neutral\"><span class=\"hb-num\">—</span></div>'\n        +'<div class=\"k\">Food cost</div>'" in SRC
    assert ".fc2-hero-fcp .v,.fc2-hero-nums .fc2-waste-n .big{font-size:clamp(34px,4vw,50px);line-height:1;margin:0}" in SRC
    assert ".fc2-waste-n .big{grid-row:1;" in SRC and ".fc2-waste-n .fc2-num-lbl{grid-row:2;margin-top:10px" in SRC
    assert ".fc2-hero-fcp .k{font-size:12.5px;letter-spacing:.16em;color:var(--ember)}" in SRC
    assert "color:var(--ember2)}" not in SRC[SRC.index(".fc2-waste-n .fc2-num-lbl{"):][:200]


def test_save_count_sits_under_the_last_ingredient_and_logged_waste_lands_at_once():
    place = _between("function fc2PlaceCountFt(){", "\n}\n")
    assert "ft.style.gridColumn=n?String(((n-1)%cols)+1):''" in place
    log = _between("function fc2LogWaste(btn){", "\n}\n")
    assert "fc2ApplyWaste(d.waste);" in log
    apply = _between("function fc2ApplyWaste(w){", "\n}\n")
    for part in ("inv-week-waste", "fc2-chip-waste", "fc2-waste-items-total", "fc2InvalidateStaticCards()",
                 "loadWasteTrend(true)", "fc2LoadCfo()"):
        assert part in apply, part
    api = open("client_api.py", encoding="utf-8").read()
    assert 'out["waste"] = {' in api


def test_send_to_suppliers_folds_to_one_line():
    assert '<details class="hb-results fc2-work fc2-send-d" id="fc2-order-send-d">' in SRC
    assert "whenSeen('fc2-order-send-d',loadOrderDraft);" in SRC
    assert "var fold=document.getElementById('fc2-order-send-d');if(fold)fold.open=true;" in SRC
    assert "#fc2-orders .hd+.fc2-orders,#fc2-orders .hd+.fc2-ok{margin-top:28px}" in SRC


def test_no_dishes_is_a_null_dash():
    menu = _between("function loadFcMenu(){", "\n  }\n")
    assert "fc2-null hb-num" in menu and "hb-empty\">'+_fcEsc(d.reason" not in menu
    assert ".fc2-null{font-family:'Space Grotesk',sans-serif;font-size:44px" in SRC


def test_recipes_load_from_the_inventory_system_only():
    blk = _between("function fc2LoadRecipeDrafts(){", "function fc2ConfidentDrafts(ds){")
    assert "'/api/food-cost/recipes'" in blk and "synced" in blk
    assert "recipe-drafts" not in blk and "data-recipe-accept" not in blk
    routes = open("strategy_routes.py", encoding="utf-8").read()
    assert '("/food-cost/recipes", ["GET"], _do_recipes_list, "recipes_list")' in routes


@pytest.fixture
def synced(db_path, monkeypatch):
    import models
    import inventory_sync
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = models.create_restaurant(models.Restaurant(name="Recipe Co", owner_email="r@x.test", module_inventory=1),
                                   db_path=db_path)
    mod = types.ModuleType("fake_rcp_provider")
    mod.label = "Fake Office"
    mod.is_connected = lambda r: r == rid
    mod.connected_ids = lambda: [rid]
    mod.fetch_inventory = lambda r: {
        "items": [{"name": "Dough", "unit": "each", "unit_cost": 0.8, "ref": "I1"},
                  {"name": "Mozzarella", "unit": "lb", "unit_cost": 4.1, "ref": "I2"}],
        "recipes": [{"dish": "Margherita", "ref": "M1", "sell_price": 14,
                     "lines": [{"ref": "I1", "qty": 1}, {"name": "mozzarella", "qty": 0.25}, {"name": "Nope", "qty": 1}]}],
    }
    monkeypatch.setitem(sys.modules, "fake_rcp_provider", mod)
    monkeypatch.setattr(inventory_sync, "PROVIDERS", {"fake": "fake_rcp_provider"})
    return rid, real, db_path


def test_a_sync_writes_every_dish_and_its_ingredients(synced):
    import inventory_sync
    import inventory_ledger
    rid, real, db_path = synced
    out = inventory_sync.sync_restaurant(rid)
    assert out["ok"] and out["recipes"] == 1
    menu = inventory_ledger.list_menu_items_with_recipes(rid)
    dish = [m for m in menu if m["name"] == "Margherita"][0]
    assert dish["sell_price"] == 14 and dish["external_ref"] == "M1"
    assert sorted((r["ingredient_name"], r["qty_per_unit"]) for r in dish["recipe"]) == [("Dough", 1.0), ("Mozzarella", 0.25)]
    # A second sync replaces, never doubles.
    inventory_sync.sync_restaurant(rid)
    dish = [m for m in inventory_ledger.list_menu_items_with_recipes(rid) if m["name"] == "Margherita"][0]
    assert len(dish["recipe"]) == 2
