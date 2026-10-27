"""
pos_food_waste.py — food made and not sold, read from the POS (owner,
10/16/26: "are these biggest waste items actually accurate using ej's real
kitchen data? wire them up if not").

A restaurant with no waste log and no kitchen count still records its waste
on the POS: a dish comped or voided because it was made wrong, sent back or
had something in it ("Error Made", "Unhappy Customer", "Object in Food") was
cooked and thrown away. A line voided before it was made ("Error Not Made"),
a manager's test, a check moved ("Wrong Check") or a walk-out is not food
waste and is left out.

Each dish is costed at its plate cost from the restaurant's Back Office
recipe book (backoffice_recipes.plates — invoice prices), never its menu
price: what the kitchen lost is the food, not the sale. A dish with no
costed recipe is counted and named, and left out of the dollars.

The window ends on the restaurant's last complete business date
(pos.local_complete_through), never the server's date: after 7pm Central a
UTC host's "yesterday" was the restaurant's today, still unarchived, so the
7-day list covered 6 days under a 7-day label (food & bar audit 10/16/26
POS-18, FC-CALC-24). The plate costs are kept per process on their inputs
(the recipe book, the invoice lines, the archived nights they price) — the
dashboard asked for every plate on every page load (SCALE-06).
"""
import logging
import re
import threading
from datetime import date, timedelta

log = logging.getLogger("pos_food_waste")

# The reason says the food was made: remade, sent back, wrong, spoiled.
MADE_RE = re.compile(r"error made|\bmade wrong|remake|re-?fire|unhappy|object in|sent back|send back|burn|dropp|"
                     r"spill|quality|cold\b|over ?cooked|under ?cooked|wrong (?:temp|item|food|dish|order made)|"
                     r"\bwaste", re.I)
# ...and never when it says it wasn't, or wasn't about food.
NOT_MADE_RE = re.compile(r"not made|test|wrong check|walk ?out|duplicate|training|price|discount|tax", re.I)
_NOT_FOOD_DEPT = re.compile(r"beer|liquor|wine|beverage|bar\b|discount|gift|merch|other", re.I)
# A POS marker ("{=}**TO GO*{=}") or an open-price button is not a dish.
_NOT_DISH = re.compile(r"\*\*|\{=\}|^open\b", re.I)


def get_conn(db_path=None):
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


def is_waste_reason(reason) -> bool:
    r = str(reason or "")
    return bool(MADE_RE.search(r)) and not NOT_MADE_RE.search(r)


# {(restaurant_id, db_path): (inputs stamp, {name: cost})}, bounded.
_PLATE_CACHE = {}
import process_caches as _process_caches  # noqa: E402 (registered beside each cache, A04-13)
_process_caches.register("pos_food_waste._PLATE_CACHE", _PLATE_CACHE)
_PLATE_CACHE_MAX = 256
_plate_lock = threading.Lock()


def _plate_stamp(restaurant_id, today, db_path=None):
    """What backoffice_recipes.plates reads, as one comparable value: the
    recipe book's load, the invoice lines' count and newest write, and the
    archived nights its sales window prices (count, newest archive, lines)."""
    conn = get_conn(db_path)
    try:
        rid = int(restaurant_id)
        out = [str(today)]
        for sql, args in (
                ("SELECT loaded_at FROM bo_recipe_books WHERE restaurant_id=?", (rid,)),
                ("SELECT COUNT(*), MAX(updated_at) FROM bo_invoice_lines WHERE restaurant_id=?", (rid,)),
                ("SELECT COUNT(*), MAX(archived_at), SUM(lines) FROM pos_archive_days WHERE restaurant_id=? "
                 "AND business_date>=?", (rid, (today - timedelta(days=40)).isoformat()))):
            row = conn.execute(sql, args).fetchone()
            out.append(tuple(row) if row else None)
        return tuple(out)
    finally:
        conn.close()


def _plate_costs(restaurant_id, db_path=None) -> dict:
    """{POS name lower: plate cost} for every complete, believable Back
    Office recipe matched to a POS button; {} without a recipe book.
    Priced as of the restaurant's own date, and kept until one of plates'
    inputs changes (_plate_stamp)."""
    try:
        import pos
        today = pos.local_complete_through(restaurant_id) + timedelta(days=1)
        stamp = _plate_stamp(restaurant_id, today, db_path)
    except Exception as e:
        import ops
        ops.capture(e, job="pos_food_waste", context=f"restaurant_id={restaurant_id} plate stamp",
                    restaurant_id=restaurant_id)
        return {}
    key = (int(restaurant_id), db_path)
    with _plate_lock:
        hit = _PLATE_CACHE.get(key)
    if hit and hit[0] == stamp:
        return dict(hit[1])
    try:
        import backoffice_recipes
        pl = backoffice_recipes.plates(restaurant_id, today=today, db_path=db_path)
    except Exception as e:
        # A failure is not "no recipes" (SCALE-11): it reaches the console,
        # and the card names every dish uncosted rather than guessing.
        import ops
        ops.capture(e, job="pos_food_waste", context=f"restaurant_id={restaurant_id} plates",
                    restaurant_id=restaurant_id)
        return {}
    out = {}
    for p in pl.get("plates") or []:
        if not p.get("complete") or p.get("suspect") or not p.get("cost"):
            continue
        for n in p.get("pos_names") or []:
            out[" ".join(str(n).lower().split())] = float(p["cost"])
    with _plate_lock:
        if len(_PLATE_CACHE) >= _PLATE_CACHE_MAX:
            _PLATE_CACHE.pop(next(iter(_PLATE_CACHE)), None)
        _PLATE_CACHE[key] = (stamp, out)
    return dict(out)


def remakes(restaurant_id, days=7, today=None, db_path=None, end=None) -> dict:
    """{available, why_not?, window {from, to, days}, items: [{name, qty,
    plate_cost, cost, value, reason}], total_cost, costed_qty, uncosted:
    [{name, qty}], lines}. Items by cost, largest first; `value` is the
    menu price the POS recorded, for reference only.

    The window is the `days` business dates ending `end`; with no `end`, the
    day before `today` (the restaurant's own date) or, with neither, the
    restaurant's last complete business date (pos.local_complete_through)."""
    if end is None:
        if today is not None:
            end = today - timedelta(days=1)
        else:
            import pos
            end = pos.local_complete_through(restaurant_id)
    start = end - timedelta(days=days - 1)
    try:
        from inventory_ledger import _drink_categories
        drinks = _drink_categories(restaurant_id)
    except Exception as e:                       # the department rule still keeps drinks out
        import ops
        ops.capture(e, job="pos_food_waste", context=f"restaurant_id={restaurant_id} drink categories",
                    restaurant_id=restaurant_id)
        drinks = set()
    conn = get_conn(db_path)
    try:
        try:
            # A modifier is part of the dish it modifies: one linked to its
            # parent (parent_line_id) or, on a night whose links were not
            # read (pos_archive_days.links_ok 0, POS-12), one the menu files
            # as a modifier — "Add Bacon" is never a wasted dish of its own.
            rows = conn.execute(
                "SELECT item_name, reason, qty, loss_amount, department, sales_category FROM pos_ticket_lines "
                "WHERE restaurant_id=? AND kind IN ('comp','void') AND business_date>=? AND business_date<=? "
                "AND (parent_line_id IS NULL OR parent_line_id='') AND COALESCE(item_kind, '') != 'modifier'",
                (int(restaurant_id), start.isoformat(), end.isoformat())).fetchall()
        except Exception as e:
            import ops
            ops.capture(e, job="pos_food_waste", context=f"restaurant_id={restaurant_id} lines",
                        restaurant_id=restaurant_id)
            rows = []
    finally:
        conn.close()
    window = {"from": start.isoformat(), "to": end.isoformat(), "days": days}
    lines = []
    for r in rows:
        if not is_waste_reason(r["reason"]):
            continue
        if _NOT_FOOD_DEPT.search(str(r["department"] or "")):
            continue
        if str(r["sales_category"] or "").strip().lower() in drinks:
            continue
        lines.append(r)
    if not rows:
        return {"available": False, "why_not": "No POS comps or voids on file for these days.", "window": window}
    costs = _plate_costs(restaurant_id, db_path)
    by = {}
    for r in lines:
        name = str(r["item_name"] or "").strip()
        if not name or _NOT_DISH.search(name):
            continue
        it = by.setdefault(name, {"name": name, "qty": 0.0, "value": 0.0, "reasons": {}})
        q = abs(float(r["qty"] or 0)) or 1.0
        it["qty"] += q
        it["value"] += abs(float(r["loss_amount"] or 0))
        it["reasons"][r["reason"]] = it["reasons"].get(r["reason"], 0) + q
    items, uncosted = [], []
    for it in by.values():
        pc = costs.get(" ".join(it["name"].lower().split()))
        reason = max(it["reasons"].items(), key=lambda kv: kv[1])[0]
        row = {"name": it["name"], "qty": round(it["qty"], 1), "value": round(it["value"], 2), "reason": reason}
        if pc is None:
            uncosted.append(row)
            continue
        row.update(plate_cost=round(pc, 2), cost=round(pc * it["qty"], 2))
        items.append(row)
    items.sort(key=lambda x: (-x["cost"], x["name"]))
    uncosted.sort(key=lambda x: (-x["qty"], x["name"]))
    return {"available": True, "window": window, "items": items, "uncosted": uncosted,
            "total_cost": round(sum(x["cost"] for x in items), 2),
            "costed_qty": round(sum(x["qty"] for x in items), 1), "lines": len(lines)}
