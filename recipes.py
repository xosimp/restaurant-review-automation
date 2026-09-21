"""Recipe drafts from the POS — the one-time build that blocks food-cost
accuracy, done by the model and confirmed by the owner.

inventory_ledger creates a menu_items row the first time a Toast item
sells, and the recipe behind it stayed empty until someone typed it — the
automation audit's largest one-time entry, and the thing recipe coverage
(and therefore every plate cost and margin) rests on. This drafts the
recipe for each unrecipe'd dish from the restaurant's OWN ingredient list
(the model may only name ingredients that exist — anything else is
dropped), stores it as a draft, and the owner accepts, edits or rejects.
Nothing here writes a recipe row until they accept.

Bounded: RECIPE_DRAFT_LIMIT dishes per run, one run a week, only where
Food Cost is on and a POS is feeding menu items.
"""
import json
import os

from models import get_conn, DB_PATH

MODEL = os.getenv("RECIPE_MODEL", "claude-sonnet-5")
RECIPE_DRAFT_LIMIT = 8

_SCHEMA = {
    "type": "object",
    "properties": {
        "ingredients": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "qty": {"type": "number"},
                    "unit": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["name", "qty", "unit", "confidence"],
                "additionalProperties": False,
            },
        },
        "note": {"type": ["string", "null"]},
    },
    "required": ["ingredients", "note"],
    "additionalProperties": False,
}


def _recipe_of(item):
    for k in ("recipe", "ingredients", "recipe_ingredients", "lines"):
        v = item.get(k)
        if isinstance(v, list):
            return v
    return []


def missing_recipes(restaurant_id, db_path=DB_PATH):
    """Active menu items with no recipe and no pending draft."""
    import inventory_ledger
    items = inventory_ledger.list_menu_items_with_recipes(restaurant_id) or []
    conn = get_conn(db_path)
    try:
        pending = {r["menu_item_id"] for r in conn.execute(
            "SELECT menu_item_id FROM recipe_drafts WHERE restaurant_id=? AND status='pending'",
            (restaurant_id,)).fetchall()}
    finally:
        conn.close()
    return [m for m in items if not _recipe_of(m) and m.get("id") not in pending]


def _prompt(item_name, ingredients, context):
    names = "\n".join(f"- {i['name']} (unit: {i.get('unit') or 'each'})" for i in ingredients)
    return (
        f"You are costing a restaurant menu. Draft the recipe for ONE plate of: {item_name}\n\n"
        f"Use ONLY ingredients from this list, spelled exactly as given, with the quantity per plate "
        f"in the ingredient's own unit. Leave out anything you would have to invent. "
        f"Mark each line's confidence honestly.\n\nIngredients on hand:\n{names}\n\n"
        + (f"Restaurant context: {context}\n" if context else "")
        + "Return the JSON only."
    )


def draft_missing(restaurant_id, limit=RECIPE_DRAFT_LIMIT, client=None, db_path=DB_PATH):
    """Draft up to `limit` recipes. Returns {"drafted": n, "skipped": n}."""
    import inventory_ledger
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r or not getattr(r, "module_inventory", 0):
        return {"drafted": 0, "skipped": 0}
    ingredients = [i for i in (inventory_ledger.list_ingredients(restaurant_id) or []) if i.get("name")]
    if len(ingredients) < 3:
        return {"drafted": 0, "skipped": 0, "reason": "fewer than three ingredients on file"}
    by_name = {i["name"].strip().lower(): i for i in ingredients}
    todo = missing_recipes(restaurant_id, db_path=db_path)[:limit]
    if not todo:
        return {"drafted": 0, "skipped": 0}
    import anthropic
    from ai_utils import create_with_retry
    client = client or anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    context = (getattr(r, "menu_notes", None) or "")[:600]
    drafted = skipped = 0
    for item in todo:
        try:
            msg = create_with_retry(
                client, restaurant_id=restaurant_id, action="recipe_draft",
                model=MODEL, max_tokens=1200,
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
                messages=[{"role": "user", "content": _prompt(item["name"], ingredients, context)}])
            text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
            out = json.loads(text)
        except Exception as e:
            import ops
            ops.capture(e, job="recipe_draft", context=f"restaurant_id={restaurant_id} item={item.get('id')}")
            skipped += 1
            continue
        lines = []
        for ln in out.get("ingredients") or []:
            ing = by_name.get(str(ln.get("name") or "").strip().lower())
            try:
                qty = float(ln.get("qty"))
            except (TypeError, ValueError):
                continue
            if not ing or qty <= 0:
                continue          # never an ingredient the restaurant does not have
            lines.append({"ingredient_id": ing["id"], "name": ing["name"], "qty": round(qty, 4),
                          "unit": ing.get("unit") or ln.get("unit") or "",
                          "confidence": ln.get("confidence") or "low"})
        if not lines:
            skipped += 1
            continue
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO recipe_drafts (restaurant_id, menu_item_id, menu_item_name, lines_json, note) "
                         "VALUES (?,?,?,?,?)",
                         (restaurant_id, item["id"], item["name"], json.dumps(lines), (out.get("note") or "")[:300] or None))
            conn.commit()
        finally:
            conn.close()
        drafted += 1
    return {"drafted": drafted, "skipped": skipped}


def list_drafts(restaurant_id, status="pending", db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM recipe_drafts WHERE restaurant_id=? AND status=? ORDER BY id DESC LIMIT 50",
                            (restaurant_id, status)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["lines"] = json.loads(d.pop("lines_json") or "[]")
        except Exception:
            d["lines"] = []
        out.append(d)
    return out


def accept(restaurant_id, draft_id, lines=None, user_id=None, db_path=DB_PATH):
    """Write the recipe. `lines` (optional) are the owner's edited lines —
    [{ingredient_id, qty}] — otherwise the draft's own. Returns
    {"ok", "written", "skipped"}."""
    import inventory_ledger
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM recipe_drafts WHERE id=? AND restaurant_id=? AND status='pending'",
                           (draft_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "That draft is gone or already answered."}
    use = lines if isinstance(lines, list) and lines else json.loads(row["lines_json"] or "[]")
    written = skipped = 0
    for ln in use:
        try:
            rid_ = inventory_ledger.add_recipe_ingredient(restaurant_id, int(row["menu_item_id"]),
                                                          int(ln["ingredient_id"]), float(ln["qty"]))
        except (KeyError, TypeError, ValueError):
            rid_ = 0
        if rid_:
            written += 1
        else:
            skipped += 1
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE recipe_drafts SET status=?, answered_at=datetime('now'), answered_by=? WHERE id=?",
                     ("accepted" if written else "rejected", user_id, draft_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": written > 0, "written": written, "skipped": skipped,
            "error": None if written else "None of those lines could be written."}


def reject(restaurant_id, draft_id, user_id=None, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE recipe_drafts SET status='rejected', answered_at=datetime('now'), answered_by=? "
                           "WHERE id=? AND restaurant_id=? AND status='pending'", (user_id, draft_id, restaurant_id))
        conn.commit()
        return {"ok": cur.rowcount == 1}
    finally:
        conn.close()


def import_csv(restaurant_id, text, db_path=DB_PATH):
    """Bulk recipes from CSV: menu_item, ingredient, qty. A missing dish is
    created; a missing ingredient is skipped and named — never invented."""
    import csv, io
    import inventory_ledger
    rows = list(csv.DictReader(io.StringIO(text or "")))
    if not rows:
        return {"ok": False, "error": "No rows — the first line must be: menu_item, ingredient, qty"}
    cols = {c.strip().lower() for c in (rows[0].keys() or [])}
    if not {"menu_item", "ingredient", "qty"} <= cols:
        return {"ok": False, "error": "Columns must be: menu_item, ingredient, qty"}
    ingredients = {i["name"].strip().lower(): i for i in (inventory_ledger.list_ingredients(restaurant_id) or []) if i.get("name")}
    dishes = {m["name"].strip().lower(): m for m in (inventory_ledger.list_menu_items_with_recipes(restaurant_id) or []) if m.get("name")}
    written, skipped, unknown = 0, 0, []
    for raw in rows[:2000]:
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items() if k}
        dish, ing, qty = row.get("menu_item", ""), row.get("ingredient", ""), row.get("qty", "")
        if not dish or not ing:
            skipped += 1
            continue
        ingredient = ingredients.get(ing.lower())
        if not ingredient:
            unknown.append(ing)
            skipped += 1
            continue
        m = dishes.get(dish.lower())
        if not m:
            mid = inventory_ledger.create_menu_item(restaurant_id, dish)
            m = {"id": mid, "name": dish}
            dishes[dish.lower()] = m
        try:
            ok = inventory_ledger.add_recipe_ingredient(restaurant_id, int(m["id"]), int(ingredient["id"]), float(qty))
        except (TypeError, ValueError):
            ok = 0
        if ok:
            written += 1
        else:
            skipped += 1
    return {"ok": written > 0, "written": written, "skipped": skipped,
            "unknown_ingredients": sorted(set(unknown))[:20],
            "error": None if written else "Nothing written — check the ingredient names match your list."}
