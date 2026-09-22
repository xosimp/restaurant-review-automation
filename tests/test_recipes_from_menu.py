"""Recipes drafted from the pasted menu, and the Simple EJ's Food Cost seed.

The photographed recipe card is gone from the web: nobody has one to
photograph. What every owner has is the menu — one paste creates the
dishes, keeps the prices and drafts a recipe per dish from the ingredient
list, behind the same accept gate.
"""
import os
import sqlite3

import pytest

import models
import recipes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(db_path, monkeypatch):
    """conftest's db_path (init_db + ensure_columns, the real boot schema),
    pointed at by models.DB_PATH for the helpers that read it bare."""
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _rid(db, name="Menu Test"):
    from models import Restaurant
    return models.create_restaurant(Restaurant(name=name, owner_email="o@x.test", module_inventory=1), db_path=db)


# ── the parser ─────────────────────────────────────────────────────────────

def test_parse_menu_text_reads_every_way_a_menu_pastes():
    got = recipes.parse_menu_text(
        "Classic Burger, 14\n- Caesar Salad — $11\nShrimp Tacos $16.50\nRibeye: 32\n"
        "Loaded Fries\n\nSalmon Plate - 24\nWings\t12\nKids Mac   7\n"
    )
    assert got == [("Classic Burger", 14.0), ("Caesar Salad", 11.0), ("Shrimp Tacos", 16.5),
                   ("Ribeye", 32.0), ("Loaded Fries", None), ("Salmon Plate", 24.0),
                   ("Wings", 12.0), ("Kids Mac", 7.0)]


def test_parse_menu_text_keeps_a_number_that_is_part_of_the_name():
    assert recipes.parse_menu_text("Wings 12\n7 Layer Dip") == [("Wings 12", None), ("7 Layer Dip", None)]


def test_parse_menu_text_is_bounded():
    assert len(recipes.parse_menu_text("\n".join(f"Dish {i}, 9" for i in range(200)))) == recipes.MENU_LINE_LIMIT


# ── draft_from_menu ────────────────────────────────────────────────────────

def test_draft_from_menu_creates_prices_and_drafts_only_what_needs_it(db, monkeypatch):
    rid = _rid(db)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name, sell_price) VALUES (?,NULL,'Caesar Salad',9.5)", (rid,))
    conn.commit(); conn.close()

    calls = {}
    monkeypatch.setattr(recipes, "missing_recipes", lambda r, db_path=None: [
        {"id": row[0], "name": row[1]} for row in sqlite3.connect(db).execute(
            "SELECT id, name FROM menu_items WHERE restaurant_id=? AND name != 'Caesar Salad'", (r,))])
    monkeypatch.setattr(recipes, "draft_missing", lambda r, limit, client, db_path, items: calls.setdefault("items", items) and {"drafted": len(items), "skipped": 0})

    out = recipes.draft_from_menu(rid, "Classic Burger, 14\nCaesar Salad, 11\nLoaded Fries", db_path=db)
    assert out["ok"] and out["created"] == 2 and out["priced"] == 1 and out["drafted"] == 2
    assert out["already_covered"] == 1
    assert sorted(i["name"] for i in calls["items"]) == ["Classic Burger", "Loaded Fries"]
    rows = dict(sqlite3.connect(db).execute("SELECT name, sell_price FROM menu_items WHERE restaurant_id=?", (rid,)).fetchall())
    assert rows == {"Caesar Salad": 9.5, "Classic Burger": 14.0, "Loaded Fries": None}, "a price already set is never overwritten"

    # pasting the same menu again adds nothing
    calls.clear()
    out2 = recipes.draft_from_menu(rid, "Classic Burger, 14\nCaesar Salad, 11\nLoaded Fries", db_path=db)
    assert out2["created"] == 0 and out2["priced"] == 0
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM menu_items WHERE restaurant_id=?", (rid,)).fetchone()[0] == 3


def test_draft_from_menu_refuses_an_empty_paste(db):
    out = recipes.draft_from_menu(_rid(db), "   \n\n", db_path=db)
    assert out["ok"] is False and "one dish a line" in out["error"]


def test_draft_missing_takes_an_explicit_item_list():
    import inspect
    assert "items=None" in inspect.signature(recipes.draft_missing).__str__()


# ── the web surface ────────────────────────────────────────────────────────

def test_the_web_drafts_from_the_menu_and_no_longer_asks_for_a_photo():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "fc2ScanRecipe" not in src and "fc2-photo-file" not in src and "Photograph a recipe" not in src
    assert "function fc2DraftRecipes(btn,missingOnly)" in src
    assert "/api/food-cost/recipes/draft" in src
    assert 'id="fc2-menu-text"' in src
    for rule in (".fc2-menu-draft{", ".fc2-tool-empty{"):
        assert rule in src


def test_the_tools_render_a_state_block_not_a_bare_sentence():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    assert "el.innerHTML='No menu items mapped to recipes yet.'" not in src
    assert "el.innerHTML='Nothing to order — assign suppliers to ingredients first.'" not in src
    assert src.count('class="fc2-tool-empty"') == 2


def test_the_route_is_registered_for_web_and_mobile():
    import strategy_routes
    paths = {p for p, _m, _f, _e in strategy_routes._ROUTES}
    assert "/food-cost/recipes/draft" in paths


# ── the Simple EJ's seed ───────────────────────────────────────────────────

def test_simple_ejs_seed_gives_food_cost_something_to_compute(db):
    import demo_seed
    rid = demo_seed._seed_simple_ejs(db)
    conn = sqlite3.connect(db)
    q = lambda sql: conn.execute(sql, (rid,)).fetchone()[0]
    assert q("SELECT COUNT(*) FROM ingredients WHERE restaurant_id=?") == len(demo_seed._EJS_PANTRY)
    assert q("SELECT COUNT(*) FROM ingredients WHERE restaurant_id=? AND supplier_email LIKE '%.example'") == len(demo_seed._EJS_PANTRY)
    assert q("SELECT COUNT(*) FROM menu_items WHERE restaurant_id=? AND sell_price > 0") == len(demo_seed._EJS_MENU)
    assert q("SELECT COUNT(*) FROM recipe_ingredients WHERE menu_item_id IN (SELECT id FROM menu_items WHERE restaurant_id=?)") \
        == sum(len(lines) for _, _, lines in demo_seed._EJS_MENU)
    assert q("SELECT COUNT(*) FROM inventory_history WHERE restaurant_id=? AND items_json IS NOT NULL AND inv_value > 0") == 6
    assert q("SELECT COUNT(*) FROM ingredient_stock_events WHERE restaurant_id=? AND event_type='receiving'") == 5 * len(demo_seed._EJS_PANTRY)
    # sales reach today, so a 28-day food-cost window is fully covered
    assert q("SELECT MAX(date) FROM labor_daily_history WHERE restaurant_id=?") == __import__("datetime").date.today().isoformat()
    # the anchoring recount is the newest event for every ingredient
    newest = conn.execute("SELECT event_type FROM ingredient_stock_events WHERE restaurant_id=? ORDER BY id DESC LIMIT 1", (rid,)).fetchone()[0]
    assert newest == "recount"
    conn.close()

    # a second boot changes nothing
    demo_seed._seed_simple_ejs(db)
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM ingredients WHERE restaurant_id=?", (rid,)).fetchone()[0] == len(demo_seed._EJS_PANTRY)
    assert conn.execute("SELECT COUNT(*) FROM inventory_history WHERE restaurant_id=?", (rid,)).fetchone()[0] == 6
    conn.close()


def test_simple_ejs_food_cost_seed_never_touches_an_uploaded_pantry(db):
    import demo_seed
    rid = _rid(db, "Simple EJ's")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE restaurants SET is_demo=1 WHERE id=?", (rid,))
    conn.commit(); conn.close()
    models.save_client_data(rid, "inventory", "item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week\nX,P,1,1,1,1,1,0", source="upload", db_path=db)
    demo_seed._seed_ejs_food_cost(rid, db)
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM ingredients WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
