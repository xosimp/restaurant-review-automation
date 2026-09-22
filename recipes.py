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
import re
from ai_utils import model_for

from models import get_conn, DB_PATH

MODEL = model_for("recipes")
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


def draft_missing(restaurant_id, limit=RECIPE_DRAFT_LIMIT, client=None, db_path=DB_PATH, items=None):
    """Draft up to `limit` recipes — every dish with none, or just `items`
    ([{id, name}]) when the caller has a list. Returns {"drafted": n,
    "skipped": n} plus `reason` when nothing could be attempted."""
    import inventory_ledger
    from models import get_restaurant
    r = get_restaurant(restaurant_id)
    if not r or not getattr(r, "module_inventory", 0):
        return {"drafted": 0, "skipped": 0, "reason": "Food Cost is not on for this restaurant"}
    ingredients = [i for i in (inventory_ledger.list_ingredients(restaurant_id) or []) if i.get("name")]
    if len(ingredients) < 3:
        return {"drafted": 0, "skipped": 0, "reason": "fewer than three ingredients on file"}
    by_name = {i["name"].strip().lower(): i for i in ingredients}
    todo = (list(items) if items is not None else missing_recipes(restaurant_id, db_path=db_path))[:limit]
    if not todo:
        return {"drafted": 0, "skipped": 0}
    from ai_utils import create_with_retry, get_client
    client = client or get_client()
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


# ── the menu, pasted ─────────────────────────────────────────────────────────
# Nobody photographs a recipe card, and a restaurant without a POS has no
# menu items for the Tuesday job to draft against. What every owner does
# have is the menu. One paste — a dish a line, a price after it if they
# like — creates the dishes, keeps the prices, and drafts a recipe for each
# from the restaurant's own ingredient list. Same accept gate as ever.

MENU_LINE_LIMIT = 40

_PRICE_TAIL = re.compile(r"^(.*?)\s*(?:[,;:|]|\s[-–—]\s|[-–—](?=\s*\$?\d))\s*\$?\s*(\d{1,4}(?:\.\d{1,2})?)\s*$")
_PRICE_SPACE = re.compile(r"^(.*?)\s+\$(\d{1,4}(?:\.\d{1,2})?)\s*$")


def parse_menu_text(text):
    """'Classic Burger, 14' · 'Classic Burger — $14' · 'Classic Burger $14' ·
    'Classic Burger' → [(name, price or None)], at most MENU_LINE_LIMIT."""
    out = []
    for line in (text or "").splitlines():
        # A tab or a run of spaces between the dish and its price is how a
        # menu pastes out of a spreadsheet or a PDF column.
        s = re.sub(r"\t+|\s{2,}", ", ", line.strip().lstrip("-•*·").strip())
        if not s:
            continue
        m = _PRICE_TAIL.match(s) or _PRICE_SPACE.match(s)
        if m and m.group(1).strip():
            name, price = m.group(1), float(m.group(2))
        else:
            name, price = s, None
        name = name.strip(" .,;:-–—$")[:80]
        if name:
            out.append((name, price))
    return out[:MENU_LINE_LIMIT]


def draft_from_menu(restaurant_id, text, user_id=None, client=None, db_path=DB_PATH, limit=MENU_LINE_LIMIT):
    """The pasted menu → dishes on file (created where missing, priced where
    a price was given and none was set) → a draft for every dish that has
    no recipe and no pending draft. Nothing else is written until Accept."""
    lines = parse_menu_text(text)
    if not lines:
        return {"ok": False, "error": "Paste your menu, one dish a line — a price after a comma is optional."}
    created = priced = 0
    targets = []
    conn = get_conn(db_path)
    try:
        existing = {r["name"].strip().lower(): dict(r) for r in conn.execute(
            "SELECT id, name, sell_price FROM menu_items WHERE restaurant_id=? AND is_active=1",
            (restaurant_id,)).fetchall()}
        for name, price in lines:
            row = existing.get(name.lower())
            if not row:
                cur = conn.execute("INSERT INTO menu_items (restaurant_id, toast_guid, name) VALUES (?,NULL,?)",
                                   (restaurant_id, name))
                row = {"id": cur.lastrowid, "name": name, "sell_price": None}
                existing[name.lower()] = row
                created += 1
            if price and not row.get("sell_price"):
                conn.execute("UPDATE menu_items SET sell_price=? WHERE id=? AND restaurant_id=?",
                             (price, row["id"], restaurant_id))
                row["sell_price"] = price
                priced += 1
            targets.append({"id": row["id"], "name": row["name"]})
        conn.commit()
    finally:
        conn.close()
    missing = {m["id"] for m in missing_recipes(restaurant_id, db_path=db_path)}
    todo = [t for t in targets if t["id"] in missing]
    out = (draft_missing(restaurant_id, limit=limit, client=client, db_path=db_path, items=todo)
           if todo else {"drafted": 0, "skipped": 0})
    return {"ok": True, "dishes": len(lines), "created": created, "priced": priced,
            "already_covered": len(targets) - len(todo), **out}


_PHOTO_SCHEMA = {
    "type": "object",
    "properties": {
        "menu_item_name": {"type": ["string", "null"]},
        "ingredients": _SCHEMA["properties"]["ingredients"],
        "note": {"type": ["string", "null"]},
    },
    "required": ["menu_item_name", "ingredients", "note"],
    "additionalProperties": False,
}

_PHOTO_PROMPT = (
    "This is a photo of a recipe card, prep sheet or handwritten recipe from a restaurant kitchen. "
    "Transcribe it: the dish name as written, and every ingredient line with its quantity and unit as "
    "written (per the batch or plate the card describes — do not scale). Where the card names an "
    "ingredient that is on the restaurant's list below, use the list's spelling exactly; otherwise keep "
    "the card's words. Mark confidence low for anything you had to guess at. Return the JSON only.\n\n"
    "Ingredients on the restaurant's list:\n{names}"
)


class RecipePhotoError(ValueError):
    pass


def extract_from_image(restaurant_id, data, media_type, user_id=None, client=None, db_path=DB_PATH):
    """A recipe card photographed → a pending draft the owner confirms
    (moat audit #3). The same gate as an invoice scan: nothing is written
    to a menu item until Accept. Lines naming an ingredient the restaurant
    does not have are kept on the draft and flagged, never invented into
    the ledger.

    Returns the draft dict, with `unmatched` (card lines with no ingredient)
    and `menu_item_matched` (False when the dish had to be created)."""
    import invoices, inventory_ledger
    invoices.check_upload(data, media_type)
    if media_type == invoices.PDF_TYPE:
        raise RecipePhotoError("Photograph the recipe card — a PDF is not a card.")
    ingredients = [i for i in (inventory_ledger.list_ingredients(restaurant_id) or []) if i.get("name")]
    by_name = {i["name"].strip().lower(): i for i in ingredients}
    names = "\n".join(f"- {i['name']} (unit: {i.get('unit') or 'each'})" for i in ingredients) or "- (none on file yet)"
    from ai_utils import create_with_retry, get_client
    client = client or get_client()
    msg = create_with_retry(
        client, restaurant_id=restaurant_id, action="recipe_photo",
        model=MODEL, max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": _PHOTO_SCHEMA}},
        messages=[{"role": "user", "content": [invoices._content_block(data, media_type),
                                               {"type": "text", "text": _PHOTO_PROMPT.format(names=names)}]}])
    if getattr(msg, "stop_reason", None) == "refusal":
        raise RecipePhotoError("The card couldn't be read. Try a clearer photo.")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    try:
        out = json.loads(text)
    except ValueError:
        raise RecipePhotoError("The card couldn't be read. Try a clearer photo.")
    dish = (out.get("menu_item_name") or "").strip()[:120]
    if not dish:
        raise RecipePhotoError("No dish name could be read from the card — write it at the top and try again.")
    lines, unmatched = [], []
    for ln in out.get("ingredients") or []:
        nm = str(ln.get("name") or "").strip()
        try:
            qty = float(ln.get("qty"))
        except (TypeError, ValueError):
            qty = None
        ing = by_name.get(nm.lower())
        if ing and qty and qty > 0:
            lines.append({"ingredient_id": ing["id"], "name": ing["name"], "qty": round(qty, 4),
                          "unit": ing.get("unit") or ln.get("unit") or "", "confidence": ln.get("confidence") or "low"})
        elif nm:
            unmatched.append({"name": nm, "qty": qty, "unit": ln.get("unit") or ""})
    if not lines:
        raise RecipePhotoError("None of the card's ingredients are on your list yet — add them under Ingredients first.")
    # The dish: an existing menu item by name, else a new inactive-price item.
    items = inventory_ledger.list_menu_items_with_recipes(restaurant_id) or []
    match = next((m for m in items if (m.get("name") or "").strip().lower() == dish.lower()), None)
    if match is None:
        # A looser match only when it is unambiguous: "margherita" → the one
        # "Margherita Pizza". A card that says "pizza" matches nothing rather
        # than the first of six pizzas, and becomes its own dish to rename.
        loose = [m for m in items if dish.lower() in (m.get("name") or "").lower()
                 or (m.get("name") or "").strip().lower() in dish.lower()]
        match = loose[0] if len(loose) == 1 else None
    matched = match is not None
    if match is None:
        conn = get_conn(db_path)
        try:
            cur = conn.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (restaurant_id, dish))
            conn.commit()
            item_id, item_name = cur.lastrowid, dish
        finally:
            conn.close()
    else:
        item_id, item_name = match["id"], match["name"]
    note_bits = ["From a photographed recipe card"]
    if out.get("note"):
        note_bits.append(str(out["note"])[:160])
    if unmatched:
        note_bits.append("not on your list: " + ", ".join(u["name"] for u in unmatched[:6]))
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO recipe_drafts (restaurant_id, menu_item_id, menu_item_name, lines_json, note) "
                           "VALUES (?,?,?,?,?)", (restaurant_id, item_id, item_name, json.dumps(lines), " · ".join(note_bits)[:300]))
        conn.commit()
        draft_id = cur.lastrowid
    finally:
        conn.close()
    return {"id": draft_id, "menu_item_id": item_id, "menu_item_name": item_name, "lines": lines,
            "note": " · ".join(note_bits)[:300], "unmatched": unmatched, "menu_item_matched": matched}


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
    # Claim first: the pending -> accepted flip is the one atomic step, and
    # only the request that makes it writes anything. Read-then-write let a
    # second accept (double tap, second device) read 'pending', find every
    # line already bound, write nothing — and mark the recipe the first had
    # just written 'rejected' (MOD-FC-27).
    conn = get_conn(db_path)
    try:
        cur = conn.execute("UPDATE recipe_drafts SET status='accepted', answered_at=datetime('now'), "
                           "answered_by=? WHERE id=? AND restaurant_id=? AND status='pending'",
                           (user_id, draft_id, restaurant_id))
        conn.commit()
        row = conn.execute("SELECT * FROM recipe_drafts WHERE id=? AND restaurant_id=?",
                           (draft_id, restaurant_id)).fetchone() if cur.rowcount == 1 else None
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
    if not written:
        # Nothing could be bound: the claim becomes the answer it really is.
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE recipe_drafts SET status='rejected' WHERE id=? AND restaurant_id=? "
                         "AND status='accepted'", (draft_id, restaurant_id))
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


IMPORT_MAX_ROWS = 2000


def import_csv(restaurant_id, text, db_path=DB_PATH):
    """Bulk recipes from CSV: menu_item, ingredient, qty. A missing dish is
    created; a missing ingredient is skipped and named — never invented.

    Everything that did not simply work is said (MOD-FC-24):
      unlinked_dishes — dishes written to that no POS item is linked to. A
                        recipe on one never depletes stock, and a CSV name
                        that differs from the POS ("Margherita" vs
                        "Margherita Pizza") used to create one silently.
      updated         — (dish, ingredient) pairs already on file whose qty
                        this file corrected; they used to hit the unique
                        index and be skipped, so a qty could not be fixed.
      truncated_rows  — rows past IMPORT_MAX_ROWS, not read."""
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
    written, updated, skipped, unknown, unlinked = 0, 0, 0, [], {}
    for raw in rows[:IMPORT_MAX_ROWS]:
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
            m = {"id": mid, "name": dish, "toast_guid": None}
            dishes[dish.lower()] = m
        try:
            ok = inventory_ledger.add_recipe_ingredient(restaurant_id, int(m["id"]), int(ingredient["id"]), float(qty))
            fixed = False
            if not ok:
                fixed = inventory_ledger.set_recipe_ingredient_qty(
                    restaurant_id, int(m["id"]), int(ingredient["id"]), float(qty))
        except (TypeError, ValueError):
            ok, fixed = 0, False
        if ok:
            written += 1
        elif fixed:
            updated += 1
        else:
            skipped += 1
        if (ok or fixed) and not m.get("toast_guid"):
            unlinked[m["name"]] = True
    total_rows = len(rows)
    written_any = written + updated
    return {"ok": written_any > 0, "written": written, "updated": updated, "skipped": skipped,
            "unknown_ingredients": sorted(set(unknown))[:20],
            "unlinked_dishes": sorted(unlinked)[:50],
            "truncated_rows": max(0, total_rows - IMPORT_MAX_ROWS),
            "error": None if written_any else "Nothing written — check the ingredient names match your list."}
