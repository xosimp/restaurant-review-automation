"""Plate costs from Back Office recipes (backoffice_recipes, 10/15/26): each
ingredient priced from the latest invoice line read the way the supplier
bills it (a catch-weight case is a price per pound), else Back Office's
last price; a price no item could cost is refused and named; a plate with
an unpriced ingredient is a floor, never a percentage."""
import json
import os
import subprocess
import sys
from datetime import date, timedelta

import backoffice_ap as bo
import backoffice_recipes as br
from tests.test_backoffice_ap import MOBILE, TODAY, _app, _as, _conn, _rid, db  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _v(**kw):
    v = {"vendor": "GORDON FOOD SERVICE", "sku": "1", "purchase_unit": "Case", "per_pack": "", "pack_unit": "",
         "price": "$10.00", "yield": "100%", "last_purchased": TODAY.strftime("%m/%d/%Y")}
    v.update(kw)
    return v


BOOK = {
    "items": [
        {"name": "BEEF BRISKET", "gl": "4030 MEAT",
         "vendors": [_v(sku="197761", per_pack="55.00", pack_unit="Pound", price="$5.94")]},
        {"name": "DAIRY BUTTER BLEND", "gl": "4020 DAIRY",
         "vendors": [_v(sku="648560", per_pack="36.00", pack_unit="Pound", price="$72.00",
                        volume_per_unit=[32.0, "Tbsp"])]},
        {"name": "PRODUCE ONIONS SPANISH YELLOW", "gl": "4010 PRODUCE",
         "vendors": [_v(sku="591122", purchase_unit="Pound", price="$39.26")]},
        {"name": "BUN BRIOCHE", "gl": "4050 DRY GOODS",
         "vendors": [_v(sku="5", per_pack="48", pack_unit="Each", price="$24.00")]},
        {"name": "BEEF PATTY 4 OZ PUCK", "gl": "4030 MEAT",
         "vendors": [_v(sku="523859", per_pack="10.00", pack_unit="Pound", price="$7.26")]},
    ],
    "recipes": [
        {"name": "SMOKE BEEF BRISKET", "group": "PREP ITEM>RECIPES", "batch_yield": {"weight": "11.00", "weight_unit": "Pound"},
         "ingredients": [{"name": "BEEF BRISKET", "qty": "22.00", "unit": "Pound", "item": 0}]},
        {"name": "COCA COLA ONIONS", "group": "PREP ITEM>RECIPES", "batch_yield": {"weight": "5.00", "weight_unit": "Pound"},
         "ingredients": [{"name": "PRODUCE ONIONS SPANISH YELLOW", "qty": "10.00", "unit": "Pound", "item": 2}]},
        {"name": "MELTY BRISKET SAMMY", "group": "MENU ITEM>PLATES", "batch_yield": {},
         "ingredients": [{"name": "SMOKE BEEF BRISKET", "qty": "6.00", "unit": "Ounce", "prep": "SMOKE BEEF BRISKET"},
                         {"name": "DAIRY BUTTER BLEND", "qty": "2.00", "unit": "Tbsp", "item": 1},
                         {"name": "BUN BRIOCHE", "qty": "1.00", "unit": "Each", "item": 3}]},
        {"name": "SMASHBURGER- EJ'S STYLE", "group": "MENU ITEM>PLATES", "batch_yield": {},
         "ingredients": [{"name": "BEEF PATTY 4 OZ PUCK", "qty": "8.00", "unit": "Ounce", "item": 4},
                         {"name": "COCA COLA ONIONS", "qty": "2.00", "unit": "Ounce", "prep": "COCA COLA ONIONS"},
                         {"name": "BUN BRIOCHE", "qty": "1.00", "unit": "Each", "item": 3}]},
        {"name": "MOD- ADD SHRIMP", "group": "MENU ITEM>MODS", "batch_yield": {}, "ingredients": []},
    ],
    "pos_names": {"MELTY BRISKET SAMMY": ["Brisket Sammy"], "SMASHBURGER- EJ'S STYLE": ["EJ's Smash Burger"]},
}


def _invoice(db, rid):
    """EJ's GFS invoice 9041344199: brisket billed by the pound (128.6 lb
    at $5.94 under a 'Case' unit), onions $39.26 a 'Pound' (the sack)."""
    bo.save_lines(rid, "GORDON FOOD SERVICE", "9041344199", [
        {"item_code": "197761", "description": "BEEF BRISKET", "gl": "4030", "purchase_unit": "Case", "qty": 128.6,
         "unit_amount": 5.94, "ext_amount": 763.88},
        {"item_code": "591122", "description": "PRODUCE ONIONS SPANISH YELLOW", "gl": "4010", "purchase_unit": "Pound",
         "qty": 4.0, "unit_amount": 39.26, "ext_amount": 157.04}],
        invoice_date=(TODAY - timedelta(days=2)).isoformat(), db_path=db)


def _sold(db, rid, name, units, price):
    c = _conn(db)
    for i in range(units):
        c.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_name, kind, qty, "
                  "sales) VALUES (?, 'rpower', ?, ?, ?, 'sale', 1, ?)",
                  (rid, f"{name}-{i}", (TODAY - timedelta(days=1 + i % 20)).isoformat(), name, price))
    c.execute("INSERT INTO pos_ticket_lines (restaurant_id, provider, line_id, business_date, item_name, kind, qty, "
              "sales) VALUES (?, 'rpower', ?, ?, ?, 'void', 1, ?)", (rid, f"{name}-void", TODAY.isoformat(), name, 99.0))
    c.commit()
    c.close()


def test_a_catch_weight_case_is_a_price_per_pound_and_impossible_prices_are_refused():
    inv = {"197761": {"purchase_unit": "Case", "qty": 128.6, "unit_amount": 5.94, "invoice_date": "2026-10-15"},
           "591122": {"purchase_unit": "Pound", "qty": 4.0, "unit_amount": 39.26, "invoice_date": "2026-10-15"}}
    p = br.item_price(BOOK["items"][0], inv, TODAY)
    assert p["source"] == "invoice" and p["base"] == "Pound" and abs(p["per_base"] - 5.94) < 1e-9
    assert p["flag"] == "billed by weight", "not $5.94 over a 55-pound case"
    assert "case price" in br.item_price(BOOK["items"][2], inv, TODAY)["error"], "onions at $39.26 a pound"
    assert "too cheap for meat" in br.item_price(BOOK["items"][4], inv, TODAY)["error"], "patties at 73 cents a pound"
    b = br.item_price(BOOK["items"][1], inv, TODAY)
    assert b["source"] == "back_office" and abs(b["per_base"] - 2.0) < 1e-9
    whole = {"648560": {"purchase_unit": "Case", "qty": 1.0, "unit_amount": 90.0, "invoice_date": "2026-10-15"}}
    assert abs(br.item_price(BOOK["items"][1], whole, TODAY)["per_base"] - 2.5) < 1e-9, "a whole case over its pack"
    old = dict(BOOK["items"][3], vendors=[_v(sku="5", per_pack="48", pack_unit="Each", price="$24.00",
                                              last_purchased="1/5/2026")])
    assert "four months" in br.item_price(old, {}, date(2026, 10, 15))["flag"]


def test_a_kitchen_unit_converts_by_the_items_own_conversion():
    v = BOOK["items"][1]["vendors"][0]
    assert abs(br._base_per(2.0, "Tbsp", "Pound", v) - 2 / 32) < 1e-9, "1 Pound = 32 Tbsp"
    assert abs(br._base_per(8.0, "Ounce", "Pound", v) - 0.5) < 1e-9
    assert br._base_per(1.0, "Cup", "Each", BOOK["items"][3]["vendors"][0]) is None


def test_plates_cost_each_dish_and_a_dish_with_an_unpriced_ingredient_is_only_a_floor(db):
    rid = _rid(db)
    _invoice(db, rid)
    _sold(db, rid, "Brisket Sammy", 20, 18.0)
    _sold(db, rid, "EJ's Smash Burger", 30, 13.0)
    assert br.plates(rid, today=TODAY, db_path=db)["available"] is False
    r = br.load(rid, BOOK, db_path=db)
    assert r == {"recipes": 5, "items": 5, "menu": 3, "mapped": 2}
    out = br.plates(rid, today=TODAY, db_path=db)
    names = [p["name"] for p in out["plates"]]
    assert names == ["SMASHBURGER- EJ'S STYLE", "MELTY BRISKET SAMMY"], "by sales; an unmatched mod is left out"
    sammy = out["plates"][1]
    # 22 lb at $5.94 yields 11 lb: $11.88 a pound, 6 oz $4.455; 2 Tbsp butter
    # at $2/lb and 32 Tbsp a pound $0.125; a bun $0.50.
    assert sammy["complete"] and sammy["cost"] == round(4.455 + 0.125 + 0.5, 2)
    assert sammy["price"] == 18.0 and sammy["units"] == 20, "the void is not a sale"
    assert sammy["cost_pct"] == round(100 * (4.455 + 0.125 + 0.5) / 18.0, 1)
    smash = out["plates"][0]
    assert not smash["complete"] and smash["cost_pct"] is None and smash["margin"] is None
    whys = {(m["name"], m.get("via")) for m in smash["missing"]}
    assert ("BEEF PATTY 4 OZ PUCK", None) in whys and ("PRODUCE ONIONS SPANISH YELLOW", "COCA COLA ONIONS") in whys
    assert out["summary"]["cost_pct"] == sammy["cost_pct"], "only fully priced dishes make the figure"
    assert out["summary"]["covered_sales"] == 360.0 and out["summary"]["mapped_sales"] == 750.0
    assert out["prices"] == {"invoice": 1, "back_office": 2, "unpriced": 2}


def test_a_plate_far_below_its_price_is_flagged_as_a_recipe_missing_its_main_item(db):
    rid = _rid(db)
    b = json.loads(json.dumps(BOOK))
    b["recipes"].append({"name": "TOMAHAWK PORK CHOP", "group": "MENU ITEM>PLATES", "batch_yield": {},
                         "ingredients": [{"name": "BUN BRIOCHE", "qty": "1.00", "unit": "Each", "item": 3}]})
    b["pos_names"]["TOMAHAWK PORK CHOP"] = ["Pork Chop"]
    br.load(rid, b, db_path=db)
    _sold(db, rid, "Pork Chop", 5, 54.99)
    _sold(db, rid, "Brisket Sammy", 20, 18.0)
    out = br.plates(rid, today=TODAY, db_path=db)
    chop = next(p for p in out["plates"] if p["name"] == "TOMAHAWK PORK CHOP")
    assert chop["complete"] and "missing its main item" in chop["suspect"]
    sammy = next(p for p in out["plates"] if p["name"] == "MELTY BRISKET SAMMY")
    assert out["summary"]["cost_pct"] == sammy["cost_pct"] and out["summary"]["suspect"] == 1


def test_the_plates_route_web_and_mobile(db, monkeypatch):
    rid = _rid(db)
    br.load(rid, BOOK, db_path=db)
    cl = _app().test_client()
    _as(monkeypatch, rid, role="client", uid=11)
    r = cl.get("/api/food-cost/plates")
    assert r.status_code == 200 and r.get_json()["ok"] and r.get_json()["available"]
    r = cl.get("/mobile/api/food-cost/plates", headers=MOBILE)
    assert r.status_code == 200 and len(r.get_json()["plates"]) == 2


def test_ejs_recipe_book_is_whole_and_every_pos_match_names_a_real_recipe():
    b = json.load(open(os.path.join(ROOT, "docs/clients/simple-ejs/back_office_recipes.json")))
    br.validate(b)
    names = {r["name"] for r in b["recipes"]}
    assert set(b["pos_names"]) <= names
    assert sum(1 for r in b["recipes"] if r["group"].startswith("MENU ITEM")) >= 140
    out = subprocess.run([sys.executable, os.path.join(ROOT, "scripts/backoffice_import.py"), "--rid", "1",
                          "--file", os.path.join(ROOT, "docs/clients/simple-ejs/back_office_recipes.json")],
                         capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert out.returncode == 0 and json.loads(out.stdout)["kind"] == "recipe_book", out.stderr[-400:]


def test_the_food_cost_page_draws_the_plate_card_and_never_calls_it_food_cost():
    html = open(os.path.join(ROOT, "templates/dashboard.html")).read()
    assert 'id="fc2-pl"' in html and "fetch('/api/food-cost/plates'" in html
    js = html[html.index("function fc2LoadPlates"):html.index("function fc2LoadCfo")]
    assert "food cost" not in js.lower()
