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

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports): a
    bound copy kept whatever models.get_conn was when this module was first
    imported — in a test run, an earlier test's database."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

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


# ── units: the card's, converted in Python (H6) ─────────────────────────────
#
# A photographed card's "8 oz" was stored as 8 in the ingredient's own unit —
# 8 lb when the ingredient is kept in pounds — because the line took
# `ing.get("unit") or ln.get("unit")`. The card's unit is kept now and the
# quantity converted here; a unit that cannot be converted (ounces of an
# ingredient kept by the each) is flagged, never guessed at.
_UNIT_EXTRA = {"tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp", "tbsp": "tbsp", "tbs": "tbsp",
               "tablespoon": "tbsp", "tablespoons": "tbsp", "cup": "cup", "cups": "cup", "c": "cup",
               "pt": "pt", "pint": "pt", "pints": "pt", "ml": "ml", "milliliter": "ml", "milliliters": "ml",
               "fl oz": "floz", "floz": "floz", "fl. oz": "floz", "quarts": "qt", "grams": "g", "gram": "g",
               "kilogram": "kg", "kilograms": "kg", "liters": "l", "litres": "l", "lb.": "lb", "oz.": "oz"}
# Each unit's size in its dimension's base unit: grams, millilitres, pieces.
_UNIT_SIZE = {"g": ("mass", 1.0), "kg": ("mass", 1000.0), "oz": ("mass", 28.349523125),
              "lb": ("mass", 453.59237),
              "ml": ("volume", 1.0), "l": ("volume", 1000.0), "tsp": ("volume", 4.92892159375),
              "tbsp": ("volume", 14.78676478125), "floz": ("volume", 29.5735295625),
              "cup": ("volume", 236.5882365), "pt": ("volume", 473.176473), "qt": ("volume", 946.352946),
              "gal": ("volume", 3785.411784),
              "each": ("count", 1.0), "dozen": ("count", 12.0)}


def norm_unit(u):
    """A unit written any usual way, as one token (invoices._unit plus the
    kitchen measures a recipe card uses), or None when there is none."""
    raw = " ".join(str(u or "").strip().lower().split())
    if not raw:
        return None
    if raw in _UNIT_EXTRA:
        return _UNIT_EXTRA[raw]
    import invoices
    base = invoices._unit(raw)
    return _UNIT_EXTRA.get(base, base)


def convert_qty(qty, from_unit, to_unit):
    """`qty` of `from_unit` expressed in `to_unit`, or None when the two are
    not the same kind of measure (weight, volume, count) or either is
    unknown. Same unit is the quantity unchanged."""
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return None
    a, b = norm_unit(from_unit), norm_unit(to_unit)
    if not a or not b:
        return None
    if a == b:
        return q
    if a not in _UNIT_SIZE or b not in _UNIT_SIZE or _UNIT_SIZE[a][0] != _UNIT_SIZE[b][0]:
        return None
    return q * _UNIT_SIZE[a][1] / _UNIT_SIZE[b][1]


# ── what past drafts taught (H6) ────────────────────────────────────────────
#
# The owner's edits to accepted drafts were stored (accepted_lines_json,
# edited_lines) and never read. Where the owner has rewritten most of the
# lines on past drafts of the same kind of dish, a new draft's confidence
# steps down one level, and the draft says why.
DISH_TYPES = (("pizza", ("pizza", "flatbread", "calzone")), ("burger", ("burger", "slider")),
              ("sandwich", ("sandwich", "panini", "sub", "wrap", "melt", "club")),
              ("pasta", ("pasta", "spaghetti", "linguine", "penne", "rigatoni", "ravioli", "lasagna",
                         "carbonara", "alfredo", "gnocchi", "fettuccine")),
              ("salad", ("salad", "caesar")), ("soup", ("soup", "chowder", "bisque", "chili")),
              ("taco", ("taco", "burrito", "quesadilla", "enchilada", "nachos")),
              ("breakfast", ("pancake", "waffle", "omelet", "omelette", "benedict", "french toast", "hash")),
              ("dessert", ("cake", "pie", "brownie", "cookie", "sundae", "tiramisu", "cheesecake", "gelato")),
              ("drink", ("cocktail", "margarita", "martini", "spritz", "sangria", "mojito", "latte", "smoothie")))
EDIT_HISTORY_MIN_DRAFTS = 3         # accepted drafts of that type before their edits count
EDIT_HISTORY_LOWER_AT = 0.5         # share of drafted lines the owner changed or removed
_CONF_STEP = {"high": "medium", "medium": "low", "low": "low"}
_CONF_RANK = {"low": 0, "medium": 1, "high": 2}

# The most one plate plausibly holds of an ingredient, per kind of measure
# (R13, B5 #16): 6 lb of salmon a plate was accepted as-is. Past this a line
# is flagged and reads low.
PLATE_MAX = {"mass": (2.0, "lb"), "volume": (1.0, "qt"), "count": (12.0, None)}


def _lowest(*bands):
    return min((b for b in bands if b in _CONF_RANK), key=lambda b: _CONF_RANK[b], default="low")


def line_confidence(model_band, qty, said_unit, ing_unit, converted, *, estimate, per_plate=True):
    """(band, note) for one recipe line, decided in code (R9, B5 #9): what
    code can check sets the ceiling, and the model's own band only lowers it.

    * An estimate (a draft with no card) is never high: nothing measured it.
    * No unit given, or a unit that does not convert to the ingredient's:
      low, with a note (a missing unit used to become the ingredient's unit
      silently and read "ok" — R13).
    * A converted unit (oz on the card, lb in stock): at most medium.
    * A per-plate quantity past PLATE_MAX for its measure: low, with a note.
    """
    ceiling, note = ("medium" if estimate else "high"), None
    if not str(said_unit or "").strip():
        return "low", f"no unit was given; {'it' if not ing_unit else 'the ingredient'} is kept in {ing_unit or 'no unit'}"
    if converted is None or converted <= 0:
        return "low", None
    if norm_unit(said_unit) != norm_unit(ing_unit):
        ceiling = _lowest(ceiling, "medium")
    if per_plate:
        kind = (_UNIT_SIZE.get(norm_unit(ing_unit)) or (None,))[0]
        limit = PLATE_MAX.get(kind)
        if limit:
            cap, unit = limit
            per = convert_qty(converted, ing_unit, unit) if unit else converted
            if per is not None and per > cap:
                return "low", f"{converted:g} {ing_unit} a plate is more than one plate usually holds"
    return _lowest(ceiling, model_band if model_band in _CONF_RANK else "low"), note


def dish_type(name):
    """A coarse kind of dish read from its name ("Margherita Pizza" → pizza),
    or "other"."""
    low = f" {str(name or '').lower()} "
    for kind, words in DISH_TYPES:
        if any(re.search(rf"(?<![a-z]){re.escape(w)}s?(?![a-z])", low) for w in words):
            return kind
    return "other"


def draft_edit_history(restaurant_id, kind, db_path=DB_PATH) -> dict:
    """{"drafts", "lines", "edited", "rate"} over this restaurant's accepted
    ESTIMATED drafts of dishes of this kind (photographed cards excluded —
    those are transcriptions, not estimates)."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT menu_item_name, lines_json, edited_lines, note FROM recipe_drafts WHERE restaurant_id=? "
            "AND status='accepted' AND accepted_lines_json IS NOT NULL ORDER BY id DESC LIMIT 60",
            (restaurant_id,)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    drafts = lines = edited = 0
    for r in rows:
        if str(r["note"] or "").startswith("From a photographed recipe card"):
            continue
        if dish_type(r["menu_item_name"]) != kind:
            continue
        try:
            n = len(json.loads(r["lines_json"] or "[]"))
        except Exception:
            continue
        if n <= 0:
            continue
        drafts += 1
        lines += n
        edited += min(n, int(r["edited_lines"] or 0))
    return {"drafts": drafts, "lines": lines, "edited": edited,
            "rate": round(edited / lines, 2) if lines else None}


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
            import data_health
            msg = create_with_retry(
                client, restaurant_id=restaurant_id, action="recipe_draft",
                # Rests on no data source: recipe drafts the owner confirms line by line.
                readiness=data_health.NOT_APPLICABLE,
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
        # What the owner's edits to past drafts of this kind of dish said
        # (H6): most lines rewritten → this draft's confidence steps down.
        kind = dish_type(item.get("name"))
        hist = draft_edit_history(restaurant_id, kind, db_path=db_path)
        lower = bool(hist["drafts"] >= EDIT_HISTORY_MIN_DRAFTS and hist["rate"] is not None
                     and hist["rate"] >= EDIT_HISTORY_LOWER_AT)
        lines = []
        for ln in out.get("ingredients") or []:
            ing = by_name.get(str(ln.get("name") or "").strip().lower())
            try:
                qty = float(ln.get("qty"))
            except (TypeError, ValueError):
                continue
            if not ing or qty <= 0:
                continue          # never an ingredient the restaurant does not have
            ing_unit = ing.get("unit") or ""
            given_unit = str(ln.get("unit") or "").strip()
            said_unit = given_unit or ing_unit
            converted = convert_qty(qty, said_unit, ing_unit) if ing_unit else None
            # The line's confidence is code's (R9): unit given and
            # convertible, a plausible per-plate amount, an estimate never
            # high — the model's band only lowers it; the owner's edit
            # history steps it down.
            conf, conf_note = line_confidence(ln.get("confidence"), qty, given_unit, ing_unit, converted,
                                              estimate=True)
            if lower:
                conf = _CONF_STEP[conf]
            line = {"ingredient_id": ing["id"], "name": ing["name"], "unit": ing_unit,
                    "confidence": conf, "source": "estimate", "per": "plate"}
            if not given_unit:
                # A missing unit is flagged, never silently the ingredient's
                # (R13, B5 #16 / p11).
                line.update(qty=round(qty, 4), unit_ok=False, unit_note=conf_note)
            elif conf_note and converted is not None and converted > 0:
                line.update(qty=round(converted, 4), unit_ok=False, unit_note=conf_note)
                if norm_unit(said_unit) != norm_unit(ing_unit):
                    line["card_qty"], line["card_unit"] = round(qty, 4), said_unit
            elif converted is not None and converted > 0:
                line["qty"] = round(converted, 4)
                line["unit_ok"] = True
                if norm_unit(said_unit) != norm_unit(ing_unit):
                    line["card_qty"], line["card_unit"] = round(qty, 4), said_unit
            else:
                # The model answered in a unit the ingredient is not kept
                # in and cannot be converted to: kept, flagged, never
                # written by an unedited accept.
                line.update(qty=round(qty, 4), unit=said_unit, unit_ok=False,
                            unit_note=f"estimated in {said_unit or 'no unit'}; {ing['name']} is kept in "
                                      f"{ing_unit or 'no unit'}")
            lines.append(line)
        if not lines:
            skipped += 1
            continue
        note_bits = ["Estimated by Cavnar AI — check each quantity before accepting"]
        if lower:
            note_bits.append(f"confidence lowered: you changed {int(hist['rate'] * 100)}% of the lines on "
                             f"{hist['drafts']} past {kind} drafts")
        if out.get("note"):
            note_bits.append(str(out["note"])[:160])
        conn = get_conn(db_path)
        try:
            conn.execute("INSERT INTO recipe_drafts (restaurant_id, menu_item_id, menu_item_name, lines_json, note) "
                         "VALUES (?,?,?,?,?)",
                         (restaurant_id, item["id"], item["name"], json.dumps(lines), " · ".join(note_bits)[:300]))
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
        "yield": {"type": ["number", "null"]},
        "ingredients": _SCHEMA["properties"]["ingredients"],
        "note": {"type": ["string", "null"]},
    },
    "required": ["menu_item_name", "yield", "ingredients", "note"],
    "additionalProperties": False,
}

_PHOTO_PROMPT = (
    "This is a photo of a recipe card, prep sheet or handwritten recipe from a restaurant kitchen. "
    "Transcribe it: the dish name as written, and every ingredient line with its quantity and unit "
    "exactly as the card writes them (per the batch or plate the card describes — do not scale, do not "
    "convert units). `yield` is how many plates or portions the card says it makes, only if the card "
    "says so; null when it does not. Where the card names an "
    "ingredient that is on the restaurant's list below, use the list's spelling exactly; otherwise keep "
    "the card's words. Mark confidence low for anything you had to guess at. Return the JSON only.\n\n"
    "Ingredients on the restaurant's list:\n{names}"
)


class RecipePhotoError(ValueError):
    pass


def _card_line(ing, qty, card_unit, confidence, card_yield=None):
    """One transcribed card line as a draft line: the card's own quantity
    and unit kept (card_qty / card_unit), the quantity converted to the
    ingredient's unit in Python, divided by the card's yield when it states
    one (per plate) or marked per batch when it does not. A unit that does
    not convert is flagged (unit_ok False) — an unedited accept never
    writes it (H6)."""
    ing_unit = ing.get("unit") or ""
    card_unit = (card_unit or "").strip()
    converted = convert_qty(qty, card_unit or ing_unit, ing_unit) if ing_unit and (card_unit or ing_unit) else None
    # Code's band, the model's only lowering it (R9): a transcription can
    # read high when the unit is the card's own and converts cleanly.
    conf, _note = line_confidence(confidence, qty, card_unit, ing_unit, converted, estimate=False,
                                  per_plate=bool(card_yield))
    line = {"ingredient_id": ing["id"], "name": ing["name"], "unit": ing_unit, "confidence": conf,
            "source": "transcribed", "card_qty": round(float(qty), 4), "card_unit": card_unit or None,
            "per": "plate" if card_yield else "batch"}
    if converted is None or converted <= 0 or not card_unit:
        line.update(qty=round(float(qty), 4), unit_ok=False,
                    unit_note=(f"the card gives no unit; {ing['name']} is kept in {ing_unit or 'no unit'}"
                               if not card_unit else
                               f"the card says {qty:g} {card_unit}; {ing['name']} is kept in "
                               f"{ing_unit or 'no unit'} and the two don't convert"))
        return line
    line["qty"] = round(converted / card_yield if card_yield else converted, 4)
    line["unit_ok"] = True
    return line


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
    import data_health
    msg = create_with_retry(
        client, restaurant_id=restaurant_id, action="recipe_photo",
        # Rests on no data source: OCR of a recipe photo the owner confirms.
        readiness=data_health.NOT_APPLICABLE,
        model=MODEL, max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": _PHOTO_SCHEMA}},
        messages=[{"role": "user", "content": [invoices._content_block(data, media_type),
                                               {"type": "text", "text": _PHOTO_PROMPT.format(names=names)}]}])
    if getattr(msg, "stop_reason", None) == "refusal":
        raise RecipePhotoError("The card couldn't be read. Try a clearer photo.")
    if getattr(msg, "stop_reason", None) == "max_tokens":
        # The transcription ran out of room, not out of legibility: telling
        # the owner to retake a sharp photo of a long card sends them round
        # in circles (AI-26).
        raise RecipePhotoError("That card is too long to read in one go — photograph it "
                               "a section at a time.")
    text = next((b.text for b in msg.content if getattr(b, "type", "") == "text"), "")
    try:
        out = json.loads(text)
    except ValueError:
        raise RecipePhotoError("The card couldn't be read. Try a clearer photo.")
    dish = (out.get("menu_item_name") or "").strip()[:120]
    if not dish:
        raise RecipePhotoError("No dish name could be read from the card — write it at the top and try again.")
    try:
        card_yield = float(out.get("yield")) if out.get("yield") is not None else None
    except (TypeError, ValueError):
        card_yield = None
    card_yield = card_yield if card_yield and card_yield > 0 else None
    lines, unmatched = [], []
    for ln in out.get("ingredients") or []:
        nm = str(ln.get("name") or "").strip()
        try:
            qty = float(ln.get("qty"))
        except (TypeError, ValueError):
            qty = None
        ing = by_name.get(nm.lower())
        if ing and qty and qty > 0:
            lines.append(_card_line(ing, qty, ln.get("unit"), ln.get("confidence"), card_yield))
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
    if card_yield:
        note_bits.append(f"the card makes {card_yield:g} — quantities are per plate")
    elif any(l.get("per") == "batch" for l in lines):
        note_bits.append("the card doesn't say how many plates it makes — enter the yield before accepting")
    bad_units = [l["name"] for l in lines if not l.get("unit_ok")]
    if bad_units:
        note_bits.append("units to check: " + ", ".join(bad_units[:4]))
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
            "note": " · ".join(note_bits)[:300], "unmatched": unmatched, "menu_item_matched": matched,
            **_draft_flags(lines)}


def _draft_flags(lines) -> dict:
    """What a client needs to say about a draft before Accept (H6): whether
    it is an estimate or a transcription, whether it still needs the card's
    yield, which lines' units could not be converted, and the confidence
    levels its lines carry (high, medium and low are distinct values)."""
    lines = [l for l in (lines or []) if isinstance(l, dict)]
    return {"is_estimate": any(l.get("source") == "estimate" for l in lines),
            "needs_yield": any(l.get("per") == "batch" for l in lines),
            "unit_warnings": [{"name": l.get("name"), "note": l.get("unit_note")}
                              for l in lines if l.get("unit_ok") is False],
            "confidence_levels": sorted({l.get("confidence") for l in lines if l.get("confidence") in _CONF_STEP},
                                        key=["high", "medium", "low"].index)}


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
        d.update(_draft_flags(d["lines"]))
        out.append(d)
    return out


def accept(restaurant_id, draft_id, lines=None, user_id=None, db_path=DB_PATH, yield_count=None):
    """Write the recipe. `lines` (optional) are the owner's edited lines —
    [{ingredient_id, qty}] — otherwise the draft's own. Returns
    {"ok", "written", "skipped"}.

    A photographed card that never said how many plates it makes is a
    BATCH: its quantities are refused as one plate's until the owner gives
    `yield_count` (the draft's per-batch lines are divided by it) or types
    the lines themselves. An unedited accept never writes a line whose unit
    could not be converted (unit_ok False) — it is skipped and named (H6)."""
    import inventory_ledger
    owner_lines = isinstance(lines, list) and bool(lines)
    try:
        yc = float(yield_count) if yield_count not in (None, "") else None
    except (TypeError, ValueError):
        yc = None
    yc = yc if yc and yc > 0 else None
    if not owner_lines:
        conn = get_conn(db_path)
        try:
            peek = conn.execute("SELECT lines_json FROM recipe_drafts WHERE id=? AND restaurant_id=? AND status='pending'",
                                (draft_id, restaurant_id)).fetchone()
        finally:
            conn.close()
        try:
            peek_lines = json.loads(peek["lines_json"] or "[]") if peek else []
        except Exception:
            peek_lines = []
        if any(isinstance(l, dict) and l.get("per") == "batch" for l in peek_lines) and not yc:
            return {"ok": False, "written": 0, "skipped": 0, "needs_yield": True,
                    "error": "This card is a batch recipe — say how many plates it makes, or enter each "
                             "quantity per plate, before accepting."}
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
    drafted = json.loads(row["lines_json"] or "[]")
    unit_skipped = []
    if owner_lines:
        use = lines
    else:
        use = []
        for d in drafted:
            if not isinstance(d, dict):
                continue
            if d.get("unit_ok") is False:
                unit_skipped.append(d.get("name"))
                continue
            if d.get("per") == "batch" and yc:
                try:
                    d = dict(d, qty=round(float(d["qty"]) / yc, 4), per="plate")
                except (KeyError, TypeError, ValueError):
                    unit_skipped.append(d.get("name"))
                    continue
            use.append(d)
    # Suggested vs chosen (audit #41) and provenance (audit #35): a line the
    # owner accepted exactly as drafted is 'draft_accepted' — the model's
    # quantity, unreviewed; one they changed or added is 'draft_edited'.
    by_ing = {}
    for d in drafted:
        try:
            # A batch line divided by the yield the owner gave is still the
            # card's own quantity, not an owner edit of it.
            div = yc if (yc and d.get("per") == "batch") else 1.0
            by_ing[int(d.get("ingredient_id"))] = round(float(d.get("qty")) / div, 4)
        except (TypeError, ValueError, AttributeError):
            continue
    written = skipped = edited = 0
    accepted_lines = []
    for ln in use:
        try:
            ing_id, qty = int(ln["ingredient_id"]), float(ln["qty"])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        same = ing_id in by_ing and abs(by_ing[ing_id] - qty) < 1e-9
        rid_ = inventory_ledger.add_recipe_ingredient(restaurant_id, int(row["menu_item_id"]), ing_id, qty,
                                                      source="draft_accepted" if same else "draft_edited")
        if rid_:
            written += 1
            edited += 0 if same else 1
            accepted_lines.append({"ingredient_id": ing_id, "qty": qty, "edited": not same})
        else:
            skipped += 1
    # Lines the draft had that the owner removed are edits too.
    kept = {a["ingredient_id"] for a in accepted_lines}
    edited += sum(1 for i in by_ing if i not in kept)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE recipe_drafts SET accepted_lines_json=?, edited_lines=? WHERE id=? AND restaurant_id=?",
                     (json.dumps(accepted_lines), edited, draft_id, restaurant_id))
        conn.commit()
    except Exception as e:
        print(f"[recipes] accepted lines not recorded: {e}")
    finally:
        conn.close()
    if not written:
        # Nothing could be bound: the claim becomes the answer it really is.
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE recipe_drafts SET status='rejected' WHERE id=? AND restaurant_id=? "
                         "AND status='accepted'", (draft_id, restaurant_id))
            conn.commit()
        finally:
            conn.close()
    return {"ok": written > 0, "written": written, "skipped": skipped + len(unit_skipped), "edited": edited,
            "unit_skipped": [n for n in unit_skipped if n],
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
