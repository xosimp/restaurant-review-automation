"""inventory_ledger.py — the stock-event ledger that replaces inventory_csv.
Correctness here means the arithmetic (waste inference, rollups) is right
and re-running the nightly depletion sync can never double-count."""
from datetime import date, timedelta

import pytest

import models
import inventory_ledger as ledger
from models import create_restaurant, Restaurant, save_client_data, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """inventory_ledger.py resolves models.get_conn lazily per-call (see its
    module docstring) specifically so this works — same gotcha/fix as
    tests/test_ask_cavnar.py's _redirect_db fixture."""
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)


def _restaurant(db_path):
    rid = create_restaurant(Restaurant(name="Ledger Test Co", owner_email="ledger@x.com"), db_path=db_path)
    return rid


def _save_inventory_csv_raw(db_path, restaurant_id, csv_str):
    """Writes client_data.inventory_csv directly via SQL, bypassing
    models.save_client_data() and its auto-migration hook — for tests that
    want to exercise import_csv_to_ingredients() in isolation rather than
    the auto-trigger (that behavior has its own test class below)."""
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO client_data (restaurant_id, inventory_csv, inventory_source) VALUES (?,?,?)",
        (restaurant_id, csv_str, "upload")
    )
    conn.commit()
    conn.close()


def _ingredient(db_path, restaurant_id, **overrides):
    defaults = dict(name="Romaine Lettuce", category="Produce", unit="lb",
                     par_level=20, unit_cost=2.5, case_size=1.0,
                     current_stock=15, avg_daily_usage=3.0, last_order_qty=20,
                     waste_last_week=1.0)
    defaults.update(overrides)
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost, "
        "case_size, current_stock, avg_daily_usage, last_order_qty, waste_last_week) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (restaurant_id, defaults["name"], defaults["category"], defaults["unit"],
         defaults["par_level"], defaults["unit_cost"], defaults["case_size"],
         defaults["current_stock"], defaults["avg_daily_usage"], defaults["last_order_qty"],
         defaults["waste_last_week"])
    )
    ingredient_id = cur.lastrowid
    conn.execute(
        "INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
        "VALUES (?,?,?,?,?,?)",
        (restaurant_id, ingredient_id, "recount", defaults["current_stock"], date.today().isoformat(), "migration")
    )
    conn.commit()
    conn.close()
    return ingredient_id


def _row(db_path, ingredient_id):
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM ingredients WHERE id=?", (ingredient_id,)).fetchone()
    conn.close()
    return dict(row)


# ── Recount / waste inference ────────────────────────────────────────────

class TestRecordRecount:
    def test_positive_gap_infers_waste(self, db_path):
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=15)
        result = ledger.record_recount(rid, iid, counted_qty=10)  # expected 15, found 10
        assert result["inferred_waste_qty"] == 5.0
        row = _row(db_path, iid)
        assert row["current_stock"] == 10.0
        assert row["waste_last_week"] == 5.0

    def test_zero_gap_infers_no_waste(self, db_path):
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=15)
        result = ledger.record_recount(rid, iid, counted_qty=15)
        assert result["inferred_waste_qty"] == 0.0

    def test_negative_gap_infers_no_waste(self, db_path):
        # Counted MORE than expected -- a prior undercount, never fabricate
        # negative waste.
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=15)
        result = ledger.record_recount(rid, iid, counted_qty=20)
        assert result["inferred_waste_qty"] == 0.0
        row = _row(db_path, iid)
        assert row["current_stock"] == 20.0

    def test_recount_becomes_new_anchor(self, db_path):
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=15)
        ledger.record_recount(rid, iid, counted_qty=10)
        ledger.record_receiving(rid, iid, qty=5)
        row = _row(db_path, iid)
        assert row["current_stock"] == 15.0  # 10 (new anchor) + 5 received


class TestRecordReceiving:
    def test_increases_current_stock(self, db_path):
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=10)
        ledger.record_receiving(rid, iid, qty=8)
        assert _row(db_path, iid)["current_stock"] == 18.0

    def test_becomes_last_order_qty(self, db_path):
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=10, last_order_qty=999)
        ledger.record_receiving(rid, iid, qty=8)
        assert _row(db_path, iid)["last_order_qty"] == 8.0


class TestRecordDepletion:
    def test_decreases_current_stock_and_computes_avg_daily_usage(self, db_path):
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=20, avg_daily_usage=999)
        ledger.record_depletion_from_sale(rid, iid, qty=3.5, event_date=date.today())
        ledger.recompute_rollups(rid, iid)
        row = _row(db_path, iid)
        assert row["current_stock"] == 16.5
        # trailing-7-day sum (3.5) / 7
        assert row["avg_daily_usage"] == 0.5

    def test_manual_ingredient_never_touched_by_depletion_keeps_manual_avg_usage(self, db_path):
        # No depletion events ever recorded -> avg_daily_usage must stay
        # exactly what was manually entered, not get zeroed by a rollup.
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, avg_daily_usage=4.2)
        ledger.record_receiving(rid, iid, qty=1)  # triggers a rollup
        assert _row(db_path, iid)["avg_daily_usage"] == 4.2


# ── Manual menu-item creation (the non-Toast / not-yet-discovered path) ──

class TestCreateMenuItem:
    def test_creates_with_null_toast_guid(self, db_path):
        rid = _restaurant(db_path)
        menu_item_id = ledger.create_menu_item(rid, "Grandma's Meatloaf")
        conn = get_conn(db_path)
        row = conn.execute("SELECT * FROM menu_items WHERE id=?", (menu_item_id,)).fetchone()
        conn.close()
        assert row["name"] == "Grandma's Meatloaf"
        assert row["toast_guid"] is None
        assert row["restaurant_id"] == rid

    def test_multiple_manual_items_dont_collide_on_null_guid(self, db_path):
        """SQLite UNIQUE(restaurant_id, toast_guid) treats multiple NULLs as
        distinct — this is the whole reason manual creation is even
        possible without assigning fake guids."""
        rid = _restaurant(db_path)
        id1 = ledger.create_menu_item(rid, "Dish One")
        id2 = ledger.create_menu_item(rid, "Dish Two")
        assert id1 != id2
        conn = get_conn(db_path)
        n = conn.execute("SELECT COUNT(*) AS n FROM menu_items WHERE restaurant_id=?", (rid,)).fetchone()["n"]
        conn.close()
        assert n == 2

    def test_manually_created_item_appears_in_recipe_editor(self, db_path):
        rid = _restaurant(db_path)
        ledger.create_menu_item(rid, "House Salad")
        menu_items = ledger.list_menu_items_with_recipes(rid)
        assert any(mi["name"] == "House Salad" for mi in menu_items)


# ── compute_daily_depletion idempotency ──────────────────────────────────

class TestComputeDailyDepletion:
    def _setup_recipe(self, db_path, rid, iid):
        conn = get_conn(db_path)
        cur = conn.execute(
            "INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,?,?)",
            (rid, "guid-caesar", "Caesar Salad")
        )
        menu_item_id = cur.lastrowid
        conn.execute(
            "INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
            (menu_item_id, iid, 0.25)
        )
        conn.commit()
        conn.close()
        return menu_item_id

    def test_matches_sale_to_recipe_and_depletes(self, db_path, monkeypatch):
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=20)
        self._setup_recipe(db_path, rid, iid)

        monkeypatch.setattr(
            "toast.fetch_order_selections",
            lambda restaurant_id, business_date: [
                {"item": {"guid": "guid-caesar"}, "displayName": "Caesar Salad", "quantity": 4}
            ]
        )
        result = ledger.compute_daily_depletion(rid, date.today())
        assert result["ingredients_updated"] == 1
        assert result["unmapped_selections"] == []
        # 4 sold * 0.25 qty_per_unit = 1.0 depleted
        assert _row(db_path, iid)["current_stock"] == 19.0

    def test_rerunning_same_business_date_does_not_double_deplete(self, db_path, monkeypatch):
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=20)
        self._setup_recipe(db_path, rid, iid)
        business_date = date.today()

        monkeypatch.setattr(
            "toast.fetch_order_selections",
            lambda restaurant_id, bd: [
                {"item": {"guid": "guid-caesar"}, "displayName": "Caesar Salad", "quantity": 4}
            ]
        )
        ledger.compute_daily_depletion(rid, business_date)
        ledger.compute_daily_depletion(rid, business_date)  # re-run, same date

        assert _row(db_path, iid)["current_stock"] == 19.0  # not 18.0
        conn = get_conn(db_path)
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM ingredient_stock_events WHERE ingredient_id=? AND event_type='depletion'",
            (iid,)
        ).fetchone()["n"]
        conn.close()
        assert n == 1

    def test_unmapped_selection_is_reported_not_silently_dropped(self, db_path, monkeypatch):
        rid = _restaurant(db_path)
        _ingredient(db_path, rid)  # no recipe configured for anything

        monkeypatch.setattr(
            "toast.fetch_order_selections",
            lambda restaurant_id, bd: [
                {"item": {"guid": "guid-unknown"}, "displayName": "Mystery Dish", "quantity": 2}
            ]
        )
        result = ledger.compute_daily_depletion(rid, date.today())
        assert result["ingredients_updated"] == 0
        assert len(result["unmapped_selections"]) == 1
        assert result["unmapped_selections"][0]["toast_guid"] == "guid-unknown"

    def test_voided_selections_already_filtered_upstream_are_ignored(self, db_path, monkeypatch):
        # fetch_order_selections is responsible for filtering voids; this
        # just confirms compute_daily_depletion trusts whatever it returns.
        rid = _restaurant(db_path)
        iid = _ingredient(db_path, rid, current_stock=20)
        self._setup_recipe(db_path, rid, iid)
        monkeypatch.setattr("toast.fetch_order_selections", lambda restaurant_id, bd: [])
        result = ledger.compute_daily_depletion(rid, date.today())
        assert result["ingredients_updated"] == 0
        assert _row(db_path, iid)["current_stock"] == 20.0


# ── CSV migration ─────────────────────────────────────────────────────────

_SAMPLE_CSV = (
    "item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,"
    "waste_last_week,case_size\n"
    "Chicken Breast,Protein,30,22,5.8,6.0,30,3.5,10\n"
    "Heavy Cream,Dairy,12,9,3.8,1.8,12,1.5,\n"
)


class TestImportCsvToIngredients:
    def test_imports_all_rows_with_fields_intact(self, db_path):
        rid = _restaurant(db_path)
        _save_inventory_csv_raw(db_path, rid, _SAMPLE_CSV)
        result = ledger.import_csv_to_ingredients(rid)
        assert result["imported"] == 2
        assert result["skipped_existing"] == 0

        conn = get_conn(db_path)
        rows = {r["name"]: dict(r) for r in conn.execute(
            "SELECT * FROM ingredients WHERE restaurant_id=?", (rid,)).fetchall()}
        conn.close()
        assert rows["Chicken Breast"]["case_size"] == 10.0
        assert rows["Chicken Breast"]["current_stock"] == 22.0
        assert rows["Heavy Cream"]["case_size"] == 1.0  # defaulted, column blank in CSV

    def test_rerunning_is_idempotent_not_additive(self, db_path):
        rid = _restaurant(db_path)
        save_client_data(rid, "inventory", _SAMPLE_CSV, source="upload", db_path=db_path)
        ledger.import_csv_to_ingredients(rid)
        second = ledger.import_csv_to_ingredients(rid)
        assert second["imported"] == 0
        assert second["skipped_existing"] == 2

        conn = get_conn(db_path)
        n = conn.execute("SELECT COUNT(*) AS n FROM ingredients WHERE restaurant_id=?", (rid,)).fetchone()["n"]
        conn.close()
        assert n == 2

    def test_inventory_csv_left_untouched_after_migration(self, db_path):
        rid = _restaurant(db_path)
        save_client_data(rid, "inventory", _SAMPLE_CSV, source="upload", db_path=db_path)
        ledger.import_csv_to_ingredients(rid)
        conn = get_conn(db_path)
        row = conn.execute("SELECT inventory_csv FROM client_data WHERE restaurant_id=?", (rid,)).fetchone()
        conn.close()
        assert row["inventory_csv"] == _SAMPLE_CSV

    def test_each_imported_ingredient_has_an_anchor_recount(self, db_path):
        rid = _restaurant(db_path)
        save_client_data(rid, "inventory", _SAMPLE_CSV, source="upload", db_path=db_path)
        ledger.import_csv_to_ingredients(rid)
        conn = get_conn(db_path)
        ingredient_id = conn.execute(
            "SELECT id FROM ingredients WHERE restaurant_id=? AND name='Chicken Breast'", (rid,)
        ).fetchone()["id"]
        recounts = conn.execute(
            "SELECT COUNT(*) AS n FROM ingredient_stock_events WHERE ingredient_id=? AND event_type='recount'",
            (ingredient_id,)
        ).fetchone()["n"]
        conn.close()
        assert recounts == 1


# ── Automatic migration on every inventory CSV save ──────────────────────

class TestAutoMigrationOnCsvSave:
    def test_saving_inventory_csv_immediately_creates_ingredients(self, db_path):
        """The whole point: no separate 'import' click needed — saving the
        CSV (admin upload, client self-upload, or a future Toast-fed path)
        populates the ledger right away."""
        rid = _restaurant(db_path)
        save_client_data(rid, "inventory", _SAMPLE_CSV, source="upload", db_path=db_path)

        conn = get_conn(db_path)
        rows = {r["name"] for r in conn.execute(
            "SELECT name FROM ingredients WHERE restaurant_id=? AND is_active=1", (rid,)).fetchall()}
        conn.close()
        assert rows == {"Chicken Breast", "Heavy Cream"}

    def test_re_saving_a_csv_with_a_new_item_only_adds_the_new_one(self, db_path):
        """Idempotent-by-name means a re-upload adding one dish's ingredient
        doesn't touch or duplicate the ones already tracked."""
        rid = _restaurant(db_path)
        save_client_data(rid, "inventory", _SAMPLE_CSV, source="upload", db_path=db_path)

        updated_csv = _SAMPLE_CSV + "Butter,Dairy,10,8,4.5,1.5,10,0.5,1\n"
        save_client_data(rid, "inventory", updated_csv, source="upload", db_path=db_path)

        conn = get_conn(db_path)
        rows = {r["name"] for r in conn.execute(
            "SELECT name FROM ingredients WHERE restaurant_id=? AND is_active=1", (rid,)).fetchall()}
        conn.close()
        assert rows == {"Chicken Breast", "Heavy Cream", "Butter"}

    def test_shifts_csv_save_does_not_trigger_ingredient_import(self, db_path):
        rid = _restaurant(db_path)
        save_client_data(rid, "shifts", "date,employee,actual_hours,sales\n2026-08-20,Ann,8,500\n",
                         source="upload", db_path=db_path)
        conn = get_conn(db_path)
        n = conn.execute("SELECT COUNT(*) AS n FROM ingredients WHERE restaurant_id=?", (rid,)).fetchone()["n"]
        conn.close()
        assert n == 0


# ── load_inventory_for_restaurant resolution order ───────────────────────

class TestLoadInventoryResolutionOrder:
    def test_prefers_ingredients_table_when_present(self, db_path):
        import inventory
        rid = _restaurant(db_path)
        _save_inventory_csv_raw(db_path, rid, _SAMPLE_CSV)
        _ingredient(db_path, rid, name="Only In Ledger", current_stock=99)

        items, is_live = inventory.load_inventory_for_restaurant(rid)
        assert is_live is True
        names = {i["item"] for i in items}
        assert "Only In Ledger" in names
        assert "Chicken Breast" not in names  # CSV ignored once ingredients rows exist

    def test_falls_back_to_csv_when_no_ingredients_rows(self, db_path):
        import inventory
        rid = _restaurant(db_path)
        save_client_data(rid, "inventory", _SAMPLE_CSV, source="upload", db_path=db_path)
        items, is_live = inventory.load_inventory_for_restaurant(rid)
        assert is_live is True
        assert {i["item"] for i in items} == {"Chicken Breast", "Heavy Cream"}

    def test_falls_back_to_sample_when_neither_exists(self, db_path):
        import inventory
        rid = _restaurant(db_path)
        items, is_live = inventory.load_inventory_for_restaurant(rid)
        assert is_live is False
        assert len(items) > 0

    def test_every_item_dict_has_the_keys_analyse_inventory_requires(self, db_path):
        import inventory
        rid = _restaurant(db_path)
        _ingredient(db_path, rid)
        items, _ = inventory.load_inventory_for_restaurant(rid)
        required = {"item", "category", "par_level", "current_stock", "unit_cost",
                    "avg_daily_usage", "last_order_qty", "waste_last_week", "unit", "case_size"}
        assert required.issubset(items[0].keys())
        # Regression smoke test: analyse_inventory must accept this shape unchanged.
        analysis = inventory.analyse_inventory(items)
        assert "waste_items" in analysis


# ── Priority ingredients (the realistic recipe-setup scope) ─────────────

class TestPriorityIngredients:
    def test_ranks_by_weekly_spend_impact_and_flags_unmapped(self, db_path):
        rid = _restaurant(db_path)
        # High impact: $12/unit * 5/day = $60/day driven
        salmon_id = _ingredient(db_path, rid, name="Salmon", unit_cost=12, avg_daily_usage=5)
        # Low impact: $0.50/unit * 1/day
        napkins_id = _ingredient(db_path, rid, name="Napkins", unit_cost=0.5, avg_daily_usage=1)

        result = ledger.priority_ingredients(rid)
        names = [r["name"] for r in result]
        assert names[0] == "Salmon"  # highest impact first
        salmon_row = next(r for r in result if r["name"] == "Salmon")
        assert salmon_row["has_recipe"] is False

    def test_flags_has_recipe_true_once_mapped(self, db_path):
        rid = _restaurant(db_path)
        salmon_id = _ingredient(db_path, rid, name="Salmon", unit_cost=12, avg_daily_usage=5)
        conn = get_conn(db_path)
        cur = conn.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,?,?)",
                           (rid, "guid-salmon-dish", "Grilled Salmon"))
        menu_item_id = cur.lastrowid
        conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                    (menu_item_id, salmon_id, 0.5))
        conn.commit()
        conn.close()

        result = ledger.priority_ingredients(rid)
        salmon_row = next(r for r in result if r["name"] == "Salmon")
        assert salmon_row["has_recipe"] is True

    def test_empty_when_no_ingredients_exist(self, db_path):
        rid = _restaurant(db_path)
        assert ledger.priority_ingredients(rid) == []
