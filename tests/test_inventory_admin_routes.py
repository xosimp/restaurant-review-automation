"""admin_routes.py's new /admin/inventory/* routes — the CRUD/recipe-editor
surface for the Toast-driven food cost engine. Same test-client-free,
call-the-decorated-function-directly pattern as tests/test_admin_routes.py."""
import pytest
from flask import Flask

import admin_routes
import auth
import inventory_ledger
import models
from admin_routes import (
    admin_bp, import_csv_to_ingredients_route, list_ingredients_route,
    create_ingredient_route, update_ingredient_route, delete_ingredient_route,
    discover_menu_items_route, list_recipes_route, add_recipe_ingredient_route,
    delete_recipe_ingredient_route, record_recount_route, record_receiving_route,
    resync_depletion_route,
)
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """Same gotcha as tests/test_admin_routes.py — models/auth/admin_routes
    all bind their own get_conn reference at module level."""
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect)


@pytest.fixture(autouse=True)
def _admin_session(monkeypatch):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.register_blueprint(admin_bp)
    return flask_app


def _restaurant(db_path):
    return create_restaurant(Restaurant(name="Admin Ledger Test Co", owner_email="a@x.com"), db_path=db_path)


# ── Ingredient CRUD ───────────────────────────────────────────────────────

def test_create_list_update_delete_ingredient(app, db_path):
    rid = _restaurant(db_path)
    with app.test_request_context(f"/admin/inventory/ingredients/{rid}", method="POST",
                                   json={"name": "Salmon Fillet", "category": "Protein",
                                         "par_level": 15, "unit_cost": 12.5, "current_stock": 10}):
        resp = create_ingredient_route(rid)
    body = resp.get_json()
    assert body["ok"] is True
    ingredient_id = body["id"]

    with app.test_request_context(f"/admin/inventory/ingredients/{rid}"):
        listed = list_ingredients_route(rid).get_json()
    assert len(listed["ingredients"]) == 1
    assert listed["ingredients"][0]["name"] == "Salmon Fillet"
    assert listed["ingredients"][0]["current_stock"] == 10.0

    with app.test_request_context(f"/admin/inventory/ingredients/{rid}/{ingredient_id}", method="POST",
                                   json={"par_level": 20}):
        update_ingredient_route(rid, ingredient_id)
    with app.test_request_context(f"/admin/inventory/ingredients/{rid}"):
        listed = list_ingredients_route(rid).get_json()
    assert listed["ingredients"][0]["par_level"] == 20.0

    with app.test_request_context(f"/admin/inventory/ingredients/{rid}/{ingredient_id}/delete", method="POST"):
        delete_ingredient_route(rid, ingredient_id)
    with app.test_request_context(f"/admin/inventory/ingredients/{rid}"):
        listed = list_ingredients_route(rid).get_json()
    assert listed["ingredients"] == []  # soft-deleted, filtered from the active list

    conn = get_conn(db_path)
    row = conn.execute("SELECT is_active FROM ingredients WHERE id=?", (ingredient_id,)).fetchone()
    conn.close()
    assert row["is_active"] == 0  # still exists, not hard-deleted


def test_create_ingredient_requires_name(app, db_path):
    rid = _restaurant(db_path)
    with app.test_request_context(f"/admin/inventory/ingredients/{rid}", method="POST", json={}):
        resp = create_ingredient_route(rid)
    assert resp.get_json()["ok"] is False


def test_update_ingredient_cannot_set_current_stock_directly(app, db_path):
    """current_stock must only move through record_recount/record_receiving
    — the edit route silently drops it if someone tries to pass it, rather
    than letting the ledger's anchor drift out of sync with a raw UPDATE."""
    rid = _restaurant(db_path)
    ingredient_id = inventory_ledger.create_ingredient(rid, name="Butter", current_stock=5)
    with app.test_request_context(f"/admin/inventory/ingredients/{rid}/{ingredient_id}", method="POST",
                                   json={"current_stock": 999}):
        update_ingredient_route(rid, ingredient_id)
    row = inventory_ledger.list_ingredients(rid)[0]
    assert row["current_stock"] == 5.0


# ── Recount / receiving routes ───────────────────────────────────────────

def test_recount_route_infers_waste_and_updates_stock(app, db_path):
    rid = _restaurant(db_path)
    ingredient_id = inventory_ledger.create_ingredient(rid, name="Heavy Cream", current_stock=12)
    with app.test_request_context(f"/admin/inventory/recount/{rid}/{ingredient_id}", method="POST",
                                   json={"counted_qty": 8}):
        resp = record_recount_route(rid, ingredient_id)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["inferred_waste_qty"] == 4.0
    assert inventory_ledger.list_ingredients(rid)[0]["current_stock"] == 8.0


def test_recount_route_requires_counted_qty(app, db_path):
    rid = _restaurant(db_path)
    ingredient_id = inventory_ledger.create_ingredient(rid, name="Butter")
    with app.test_request_context(f"/admin/inventory/recount/{rid}/{ingredient_id}", method="POST", json={}):
        resp = record_recount_route(rid, ingredient_id)
    assert resp.get_json()["ok"] is False


def test_receiving_route_increases_stock(app, db_path):
    rid = _restaurant(db_path)
    ingredient_id = inventory_ledger.create_ingredient(rid, name="Flour", current_stock=5)
    with app.test_request_context(f"/admin/inventory/receiving/{rid}/{ingredient_id}", method="POST",
                                   json={"qty": 10}):
        resp = record_receiving_route(rid, ingredient_id)
    assert resp.get_json()["ok"] is True
    assert inventory_ledger.list_ingredients(rid)[0]["current_stock"] == 15.0


# ── CSV import route (now mostly a manual backfill — saving inventory CSV
#    auto-migrates on its own, see tests/test_inventory_ledger.py's
#    TestAutoMigrationOnCsvSave) ───────────────────────────────────────────

def test_import_csv_route_backfills_data_saved_before_auto_migration_existed(app, db_path):
    """Simulates a restaurant with inventory_csv written directly (as if
    from before the auto-migration hook existed) — the manual route is
    still the backfill path for that."""
    rid = _restaurant(db_path)
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO client_data (restaurant_id, inventory_csv, inventory_source) VALUES (?,?,?)",
        (rid, "item,category,par_level,current_stock,unit_cost,avg_daily_usage,"
              "last_order_qty,waste_last_week\nOnions,Produce,10,8,1.2,2,10,0.5\n", "upload")
    )
    conn.commit()
    conn.close()

    with app.test_request_context(f"/admin/inventory/import-csv/{rid}", method="POST"):
        resp = import_csv_to_ingredients_route(rid)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["imported"] == 1


def test_saving_inventory_csv_auto_migrates_without_the_route(app, db_path):
    from models import save_client_data
    rid = _restaurant(db_path)
    csv_str = ("item,category,par_level,current_stock,unit_cost,avg_daily_usage,"
              "last_order_qty,waste_last_week\nOnions,Produce,10,8,1.2,2,10,0.5\n")
    save_client_data(rid, "inventory", csv_str, source="upload", db_path=db_path)
    assert inventory_ledger.list_ingredients(rid)[0]["name"] == "Onions"

    # The route still works, but it's now an idempotent no-op.
    with app.test_request_context(f"/admin/inventory/import-csv/{rid}", method="POST"):
        resp = import_csv_to_ingredients_route(rid)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["imported"] == 0
    assert body["skipped_existing"] == 1


# ── Recipe editor routes ──────────────────────────────────────────────────

def test_discover_add_delete_recipe_flow(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    ingredient_id = inventory_ledger.create_ingredient(rid, name="Romaine Lettuce", current_stock=20)

    monkeypatch.setattr("toast.fetch_business_days", lambda rid, start, end: {"2026-08-20": 500.0})
    monkeypatch.setattr(
        "toast.fetch_order_selections",
        lambda rid, bd: [{"item": {"guid": "guid-caesar"}, "displayName": "Caesar Salad", "quantity": 3}]
    )
    with app.test_request_context(f"/admin/inventory/discover-menu-items/{rid}", method="POST"):
        resp = discover_menu_items_route(rid)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["discovered"] == 1

    with app.test_request_context(f"/admin/inventory/recipes/{rid}"):
        recipes = list_recipes_route(rid).get_json()
    assert len(recipes["menu_items"]) == 1
    menu_item_id = recipes["menu_items"][0]["id"]
    assert recipes["menu_items"][0]["recipe"] == []
    assert recipes["priority_ingredients"][0]["name"] == "Romaine Lettuce"
    assert recipes["priority_ingredients"][0]["has_recipe"] is False

    with app.test_request_context(f"/admin/inventory/recipes/{menu_item_id}", method="POST",
                                   json={"ingredient_id": ingredient_id, "qty_per_unit": 0.25}):
        add_resp = add_recipe_ingredient_route(menu_item_id)
    assert add_resp.get_json()["ok"] is True
    recipe_ingredient_id = add_resp.get_json()["id"]

    with app.test_request_context(f"/admin/inventory/recipes/{rid}"):
        recipes = list_recipes_route(rid).get_json()
    assert len(recipes["menu_items"][0]["recipe"]) == 1
    assert recipes["menu_items"][0]["recipe"][0]["ingredient_name"] == "Romaine Lettuce"

    with app.test_request_context(f"/admin/inventory/recipes/{menu_item_id}/{recipe_ingredient_id}/delete",
                                   method="POST"):
        delete_recipe_ingredient_route(menu_item_id, recipe_ingredient_id)
    with app.test_request_context(f"/admin/inventory/recipes/{rid}"):
        recipes = list_recipes_route(rid).get_json()
    assert recipes["menu_items"][0]["recipe"] == []


# ── Resync-depletion route ────────────────────────────────────────────────

def test_resync_depletion_route(app, db_path, monkeypatch):
    rid = _restaurant(db_path)
    ingredient_id = inventory_ledger.create_ingredient(rid, name="Shrimp", current_stock=20)
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,?,?)",
                       (rid, "guid-shrimp-scampi", "Shrimp Scampi"))
    menu_item_id = cur.lastrowid
    conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                (menu_item_id, ingredient_id, 0.3))
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "toast.fetch_order_selections",
        lambda rid, bd: [{"item": {"guid": "guid-shrimp-scampi"}, "displayName": "Shrimp Scampi", "quantity": 5}]
    )
    with app.test_request_context(f"/admin/inventory/resync-depletion/{rid}", method="POST",
                                   json={"business_date": "2026-08-20"}):
        resp = resync_depletion_route(rid)
    body = resp.get_json()
    assert body["ok"] is True
    assert body["ingredients_updated"] == 1
    assert inventory_ledger.list_ingredients(rid)[0]["current_stock"] == 18.5  # 20 - (5*0.3)


# ── Admin gate ─────────────────────────────────────────────────────────────

def test_non_admin_rejected_with_json_401(app, db_path, monkeypatch):
    # POST (not GET), so _wants_json_response() is True and the rejection
    # comes back as JSON+401 rather than a redirect — see auth.py's
    # _wants_json_response() and test_admin_routes.py's own split on this.
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 0})
    rid = _restaurant(db_path)
    with app.test_request_context(f"/admin/inventory/ingredients/{rid}", method="POST", json={"name": "x"}):
        resp, status = create_ingredient_route(rid)
    assert status == 401
    assert resp.get_json()["session_expired"] is True
