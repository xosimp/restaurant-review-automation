"""
inventory_ledger.py — persistent per-ingredient stock ledger

Replaces the old inventory_csv blob (re-parsed fresh on every read, no
stable per-ingredient identity) with real ingredients rows plus an
append-only ingredient_stock_events log (recount / receiving / depletion /
waste). current_stock, avg_daily_usage, and waste_last_week are cached on
the ingredients row and recomputed from the ledger any time it changes —
never incrementally mutated in isolation, so a re-run can never silently
double-count.

A 'recount' event stores the absolute counted quantity and is the ledger's
anchor: current_stock = latest recount.qty + receiving since - depletion
since - waste since, where "since" means "inserted after that recount"
(ordered by row id, not event_date — entries are almost always logged
same-day as they occur, and id ordering sidesteps same-day tie-breaking
entirely). Every ingredient always has at least one recount because CSV
migration inserts one, so there's no "no recount yet" special case.

db_conn/get_conn are imported lazily inside each function (not at module
top level) so tests that monkeypatch models.get_conn to redirect at a
throwaway DB actually take effect here — a top-level `from models import
get_conn` would bind an import-time reference immune to that patch (see
tests/test_ask_cavnar.py's _redirect_db fixture for the exact gotcha).
"""
import logging
from datetime import date, timedelta

log = logging.getLogger("inventory_ledger")

_TREND_WINDOW_DAYS = 7


def _as_date_str(d) -> str:
    if d is None:
        return date.today().isoformat()
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def _compute_current_stock(conn, ingredient_id: int) -> float:
    recount = conn.execute(
        "SELECT id, qty FROM ingredient_stock_events "
        "WHERE ingredient_id=? AND event_type='recount' ORDER BY id DESC LIMIT 1",
        (ingredient_id,)
    ).fetchone()
    if not recount:
        row = conn.execute("SELECT current_stock FROM ingredients WHERE id=?", (ingredient_id,)).fetchone()
        return row["current_stock"] if row else 0.0

    stock = recount["qty"]
    deltas = conn.execute(
        "SELECT event_type, COALESCE(SUM(qty),0) AS total FROM ingredient_stock_events "
        "WHERE ingredient_id=? AND id>? AND event_type IN ('receiving','depletion','waste') "
        "GROUP BY event_type",
        (ingredient_id, recount["id"])
    ).fetchall()
    for d in deltas:
        if d["event_type"] == "receiving":
            stock += d["total"]
        else:
            stock -= d["total"]
    return stock


def recompute_rollups(restaurant_id: int, ingredient_id: int, conn=None) -> None:
    """Refresh current_stock (always), and avg_daily_usage/waste_last_week
    (only when this ingredient actually has ledger depletion/waste history
    — otherwise a manually-entered value would get silently zeroed out for
    an ingredient that's never been recipe-mapped)."""
    _own_conn = conn is None
    if _own_conn:
        from models import get_conn
        conn = get_conn()
    try:
        current_stock = _compute_current_stock(conn, ingredient_id)

        recount = conn.execute(
            "SELECT event_date FROM ingredient_stock_events "
            "WHERE ingredient_id=? AND event_type='recount' ORDER BY id DESC LIMIT 1",
            (ingredient_id,)
        ).fetchone()

        window_start = (date.today() - timedelta(days=_TREND_WINDOW_DAYS - 1)).isoformat()

        has_depletion = conn.execute(
            "SELECT 1 FROM ingredient_stock_events WHERE ingredient_id=? AND event_type='depletion' LIMIT 1",
            (ingredient_id,)
        ).fetchone() is not None
        has_waste = conn.execute(
            "SELECT 1 FROM ingredient_stock_events WHERE ingredient_id=? AND event_type='waste' LIMIT 1",
            (ingredient_id,)
        ).fetchone() is not None
        last_receiving = conn.execute(
            "SELECT qty FROM ingredient_stock_events "
            "WHERE ingredient_id=? AND event_type='receiving' ORDER BY id DESC LIMIT 1",
            (ingredient_id,)
        ).fetchone()

        sets, params = ["current_stock=?", "updated_at=datetime('now')"], [current_stock]
        if recount:
            sets.append("last_recount_at=?")
            params.append(recount["event_date"])
        if has_depletion:
            depletion_sum = conn.execute(
                "SELECT COALESCE(SUM(qty),0) AS total FROM ingredient_stock_events "
                "WHERE ingredient_id=? AND event_type='depletion' AND event_date>=?",
                (ingredient_id, window_start)
            ).fetchone()["total"]
            sets.append("avg_daily_usage=?")
            params.append(round(depletion_sum / _TREND_WINDOW_DAYS, 3))
        if has_waste:
            waste_sum = conn.execute(
                "SELECT COALESCE(SUM(qty),0) AS total FROM ingredient_stock_events "
                "WHERE ingredient_id=? AND event_type='waste' AND event_date>=?",
                (ingredient_id, window_start)
            ).fetchone()["total"]
            sets.append("waste_last_week=?")
            params.append(round(waste_sum, 3))
        if last_receiving:
            sets.append("last_order_qty=?")
            params.append(last_receiving["qty"])
        params.append(ingredient_id)
        conn.execute(f"UPDATE ingredients SET {', '.join(sets)} WHERE id=?", params)
        if _own_conn:
            conn.commit()
    finally:
        if _own_conn:
            conn.close()


def record_recount(restaurant_id: int, ingredient_id: int, counted_qty: float,
                    event_date=None, source: str = "manual", note: str = None) -> dict:
    """Insert an absolute recount event. If the ledger expected more stock
    than was actually counted, auto-insert an inferred 'waste' event for
    the gap — a recount finding MORE than expected (a prior undercount,
    typically) never fabricates negative waste, it's just logged."""
    from models import db_conn
    event_date_str = _as_date_str(event_date)
    with db_conn() as conn:
        expected = _compute_current_stock(conn, ingredient_id)
        gap = round(expected - counted_qty, 3)

        # The waste event (if any) must be inserted BEFORE the recount, so
        # its row id is lower than the recount's — it explains why the
        # count came in below expectation, it isn't stock that vanished
        # AFTER the recount. _compute_current_stock only sums events with
        # id > the anchor recount's id, so inserting waste afterward would
        # double-subtract the same gap the recount's own qty already bakes in.
        inferred_waste = 0.0
        if gap > 0:
            conn.execute(
                "INSERT INTO ingredient_stock_events "
                "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (restaurant_id, ingredient_id, "waste", gap, event_date_str, "inferred",
                 f"inferred from recount gap ({expected} expected vs {counted_qty} counted)")
            )
            inferred_waste = gap
        else:
            log.info(f"[inventory_ledger] recount for ingredient {ingredient_id}: "
                     f"counted {counted_qty} >= expected {expected}, no waste inferred")

        cur = conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "recount", counted_qty, event_date_str, source, note)
        )
        recount_id = cur.lastrowid

        recompute_rollups(restaurant_id, ingredient_id, conn=conn)
        conn.commit()

    return {"recount_id": recount_id, "inferred_waste_qty": inferred_waste}


def record_receiving(restaurant_id: int, ingredient_id: int, qty: float,
                      event_date=None, source: str = "manual", note: str = None) -> int:
    from models import db_conn
    event_date_str = _as_date_str(event_date)
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "receiving", qty, event_date_str, source, note)
        )
        event_id = cur.lastrowid
        recompute_rollups(restaurant_id, ingredient_id, conn=conn)
        conn.commit()
    return event_id


def record_depletion_from_sale(restaurant_id: int, ingredient_id: int, qty: float,
                                event_date, source: str = "toast") -> int:
    """Records the event only — does NOT roll up current_stock/avg_daily_usage.
    compute_daily_depletion calls this in a batch per business date and
    rolls up once per ingredient afterward; a standalone caller must call
    recompute_rollups() itself to see the change reflected."""
    from models import db_conn
    event_date_str = _as_date_str(event_date)
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source) "
            "VALUES (?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "depletion", qty, event_date_str, source)
        )
        event_id = cur.lastrowid
        conn.commit()
    return event_id


def compute_daily_depletion(restaurant_id: int, business_date) -> dict:
    """Pull one Toast business date's sold items, match against
    recipe_ingredients via menu_items.toast_guid, and record depletion.
    Idempotent: any existing toast-sourced depletion rows for this exact
    business date are deleted before the freshly computed set is inserted,
    inside the same transaction — safe to re-run (nightly retry, manual
    resync) without double-depleting stock."""
    import toast as _toast
    from models import db_conn

    business_date_str = _as_date_str(business_date)
    real_date = business_date if hasattr(business_date, "isoformat") else date.fromisoformat(business_date_str)
    selections = _toast.fetch_order_selections(restaurant_id, real_date)

    sold_by_guid = {}
    for sel in selections:
        guid = (sel.get("item") or {}).get("guid")
        if not guid:
            continue
        sold_by_guid[guid] = sold_by_guid.get(guid, 0) + float(sel.get("quantity", 0) or 0)

    ingredients_updated = set()
    unmapped = []

    with db_conn() as conn:
        conn.execute(
            "DELETE FROM ingredient_stock_events "
            "WHERE restaurant_id=? AND event_date=? AND event_type='depletion' AND source='toast'",
            (restaurant_id, business_date_str)
        )

        for guid, qty_sold in sold_by_guid.items():
            menu_item = conn.execute(
                "SELECT id, name FROM menu_items WHERE restaurant_id=? AND toast_guid=? AND is_active=1",
                (restaurant_id, guid)
            ).fetchone()
            if not menu_item:
                unmapped.append({"toast_guid": guid, "qty_sold": qty_sold, "reason": "menu item not discovered"})
                continue

            recipe_rows = conn.execute(
                "SELECT ingredient_id, qty_per_unit FROM recipe_ingredients WHERE menu_item_id=?",
                (menu_item["id"],)
            ).fetchall()
            if not recipe_rows:
                unmapped.append({"toast_guid": guid, "menu_item": menu_item["name"],
                                  "qty_sold": qty_sold, "reason": "no recipe configured"})
                continue

            for r in recipe_rows:
                conn.execute(
                    "INSERT INTO ingredient_stock_events "
                    "(restaurant_id, ingredient_id, event_type, qty, event_date, source) "
                    "VALUES (?,?,?,?,?,?)",
                    (restaurant_id, r["ingredient_id"], "depletion",
                     r["qty_per_unit"] * qty_sold, business_date_str, "toast")
                )
                ingredients_updated.add(r["ingredient_id"])

        for ingredient_id in ingredients_updated:
            recompute_rollups(restaurant_id, ingredient_id, conn=conn)
        conn.commit()

    if unmapped:
        log.warning(f"[inventory_ledger] restaurant {restaurant_id} on {business_date_str}: "
                    f"{len(unmapped)} unmapped selection(s) — {unmapped}")

    return {"ingredients_updated": len(ingredients_updated), "unmapped_selections": unmapped}


def discover_menu_items(restaurant_id: int, days: int = 7) -> dict:
    """Scan a recent window of real Toast business dates (via
    fetch_business_days, so we only hit days Toast actually reports rather
    than guessing) and upsert every distinct item.guid seen in their order
    selections into menu_items — the one-time-ish admin action that seeds
    the recipe editor without a separate Toast Menus API integration.
    Safe to re-run: upserts by (restaurant_id, toast_guid), never duplicates."""
    import toast as _toast
    from models import db_conn

    end = date.today()
    # fetch_business_days isn't demo-mode aware (only fetch_order_selections
    # is), so a demo/showcase restaurant would otherwise throw trying a real
    # HTTP call with a fake token. _demo_order_selections returns the same
    # fixed menu regardless of date, so one representative date is enough.
    business_dates = [end.isoformat()] if _toast._is_demo(restaurant_id) else \
        sorted(_toast.fetch_business_days(restaurant_id, end - timedelta(days=days), end).keys())

    seen = {}  # guid -> display name
    for bd_str in business_dates:
        bd = date.fromisoformat(bd_str)
        for sel in _toast.fetch_order_selections(restaurant_id, bd):
            guid = (sel.get("item") or {}).get("guid")
            if not guid:
                continue
            seen[guid] = sel.get("displayName") or seen.get(guid) or guid

    discovered, updated = 0, 0
    with db_conn() as conn:
        for guid, name in seen.items():
            existing = conn.execute(
                "SELECT id, name FROM menu_items WHERE restaurant_id=? AND toast_guid=?",
                (restaurant_id, guid)
            ).fetchone()
            if existing:
                if existing["name"] != name:
                    conn.execute("UPDATE menu_items SET name=? WHERE id=?", (name, existing["id"]))
                    updated += 1
                continue
            conn.execute(
                "INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,?,?)",
                (restaurant_id, guid, name)
            )
            discovered += 1
        conn.commit()

    return {"discovered": discovered, "updated": updated, "total_seen": len(seen)}


def import_csv_to_ingredients(restaurant_id: int) -> dict:
    """One-time admin migration: parses client_data.inventory_csv via the
    existing load_inventory(), upserts one ingredients row per item keyed
    on (restaurant_id, name) — safe to re-run, already-imported names are
    skipped rather than duplicated. inventory_csv itself is left untouched
    as an archival fallback."""
    from inventory import load_inventory
    from models import get_client_data, db_conn

    data = get_client_data(restaurant_id)
    if not data or not data.get("inventory_csv"):
        return {"imported": 0, "skipped_existing": 0, "error": "No inventory_csv found for this restaurant"}

    items = load_inventory(csv_string=data["inventory_csv"])
    today_str = date.today().isoformat()
    imported, skipped = 0, 0

    with db_conn() as conn:
        for item in items:
            existing = conn.execute(
                "SELECT id FROM ingredients WHERE restaurant_id=? AND name=? AND is_active=1",
                (restaurant_id, item["item"])
            ).fetchone()
            if existing:
                skipped += 1
                continue

            cur = conn.execute(
                "INSERT INTO ingredients "
                "(restaurant_id, name, category, unit, par_level, unit_cost, case_size, "
                " current_stock, avg_daily_usage, last_order_qty, waste_last_week, last_recount_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (restaurant_id, item["item"], item.get("category", ""), item.get("unit", ""),
                 item["par_level"], item["unit_cost"], item["case_size"],
                 item["current_stock"], item["avg_daily_usage"], item["last_order_qty"],
                 item["waste_last_week"], today_str)
            )
            ingredient_id = cur.lastrowid
            conn.execute(
                "INSERT INTO ingredient_stock_events "
                "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
                "VALUES (?,?,?,?,?,?,?)",
                (restaurant_id, ingredient_id, "recount", item["current_stock"], today_str,
                 "migration", "initial anchor from CSV import")
            )
            imported += 1
        conn.commit()

    return {"imported": imported, "skipped_existing": skipped}


# ── Admin CRUD helpers (thin routes in admin_routes.py delegate here so DB
#    access always goes through the lazily-imported get_conn/db_conn above) ──

def list_ingredients(restaurant_id: int) -> list:
    from models import get_conn
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM ingredients WHERE restaurant_id=? AND is_active=1 ORDER BY name",
        (restaurant_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_ingredient(restaurant_id: int, name: str, category: str = "", unit: str = "",
                       par_level: float = 0, unit_cost: float = 0, case_size: float = 1.0,
                       current_stock: float = 0) -> int:
    """Creates the ingredient and its initial recount anchor in one call —
    every ingredient must have at least one recount (see module docstring)."""
    from models import db_conn
    today_str = date.today().isoformat()
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, "
            "unit_cost, case_size, current_stock, last_recount_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (restaurant_id, name, category, unit, par_level, unit_cost, case_size or 1.0,
             current_stock, today_str)
        )
        ingredient_id = cur.lastrowid
        conn.execute(
            "INSERT INTO ingredient_stock_events "
            "(restaurant_id, ingredient_id, event_type, qty, event_date, source, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, ingredient_id, "recount", current_stock, today_str,
             "admin", "initial count at creation")
        )
        conn.commit()
    return ingredient_id


def update_ingredient(ingredient_id: int, **fields) -> None:
    """Updates static attributes only (name/category/unit/par_level/unit_cost/
    case_size) plus avg_daily_usage/waste_last_week as a manual override —
    current_stock is deliberately not editable here, it can only change via
    record_recount/record_receiving so the ledger stays the source of truth."""
    from models import db_conn
    allowed = {"name", "category", "unit", "par_level", "unit_cost", "case_size",
               "avg_daily_usage", "waste_last_week"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    sets = ", ".join(f"{k}=?" for k in updates) + ", updated_at=datetime('now')"
    with db_conn() as conn:
        conn.execute(f"UPDATE ingredients SET {sets} WHERE id=?", [*updates.values(), ingredient_id])
        conn.commit()


def deactivate_ingredient(ingredient_id: int) -> None:
    """Soft-delete only — recipe_ingredients/ingredient_stock_events may
    still reference this ingredient, never hard-delete it."""
    from models import db_conn
    with db_conn() as conn:
        conn.execute("UPDATE ingredients SET is_active=0, updated_at=datetime('now') WHERE id=?",
                     (ingredient_id,))
        conn.commit()


def list_menu_items_with_recipes(restaurant_id: int) -> list:
    from models import get_conn
    conn = get_conn()
    menu_items = conn.execute(
        "SELECT * FROM menu_items WHERE restaurant_id=? AND is_active=1 ORDER BY name",
        (restaurant_id,)
    ).fetchall()
    result = []
    for mi in menu_items:
        recipe_rows = conn.execute(
            "SELECT ri.id, ri.ingredient_id, ri.qty_per_unit, i.name AS ingredient_name, i.unit "
            "FROM recipe_ingredients ri JOIN ingredients i ON i.id = ri.ingredient_id "
            "WHERE ri.menu_item_id=?",
            (mi["id"],)
        ).fetchall()
        result.append({**dict(mi), "recipe": [dict(r) for r in recipe_rows]})
    conn.close()
    return result


def add_recipe_ingredient(menu_item_id: int, ingredient_id: int, qty_per_unit: float) -> int:
    from models import db_conn
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
            (menu_item_id, ingredient_id, qty_per_unit)
        )
        conn.commit()
        return cur.lastrowid


def delete_recipe_ingredient(recipe_ingredient_id: int) -> None:
    from models import db_conn
    with db_conn() as conn:
        conn.execute("DELETE FROM recipe_ingredients WHERE id=?", (recipe_ingredient_id,))
        conn.commit()
