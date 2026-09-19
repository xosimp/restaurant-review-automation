"""
menu_intelligence.py — the dish-level view nobody else can build.

Two features, both of which need data that lives in different modules:

DISH SCORECARD — plate margin (recipes x ingredient costs), popularity (POS
sales mix) and guest sentiment (reviews naming the dish), on one row. The POS
knows what sells; it does not know the plate cost or what guests said. The
review tool knows what guests said; it knows nothing about margin. A star dish
drawing complaints is urgent, a "dog" drawing complaints is simply a cut, and
only the combination can tell those apart.

REPRICE — when an ingredient's price rises, which dishes lost how much margin,
and what menu price would restore each dish's food cost %. An invoice tool
knows the price rose; it doesn't know your recipes or how many plates you sell.

Everything here is computed; no model runs. Every recommendation carries the
evidence it rests on, and every floor below is there so a pattern is not read
into one review or one plate.
"""
import json
import math
from datetime import date, timedelta

from models import get_conn, DB_PATH, REVIEW_TIME_AXIS_BARE

# A dish is flagged on sentiment only past this many mentions in the window.
# One unhappy guest naming the risotto is an anecdote.
MIN_MENTIONS = 2
SENTIMENT_WINDOW_DAYS = 90

# Reprice floors: below these the price move is real but not worth an owner's
# attention or a menu reprint.
MIN_PLATE_INCREASE = 0.10          # dollars per plate
MIN_MONTHLY_MARGIN_LOST = 10.0     # dollars per month
PRICE_ROUNDING = 0.25              # menu prices land on quarters


def _dish_mentions(restaurant_id, db_path=DB_PATH):
    """{normalised dish name as written: {"pos", "neg", "complaints", "review_ids"}}"""
    since = (date.today() - timedelta(days=SENTIMENT_WINDOW_DAYS)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT id, sentiment, entities, specific_complaint FROM reviews "
            f"WHERE restaurant_id=? AND deleted_at IS NULL AND processed=1 "
            f"AND entities IS NOT NULL AND entities != '' "
            f"AND date({REVIEW_TIME_AXIS_BARE}) >= ?", (restaurant_id, since)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            ents = json.loads(r["entities"] or "{}")
        except Exception:
            continue
        for dish in (ents.get("dishes") or []) if isinstance(ents, dict) else []:
            if isinstance(dish, str) and dish.strip():
                out.append({"dish": dish.strip(), "sentiment": r["sentiment"],
                            "complaint": r["specific_complaint"], "review_id": r["id"]})
    return out


def _attach_sentiment(items, mentions):
    """Match what guests wrote to menu items with the same word-level matcher
    the cross-module layer uses ("the ribeye" -> "Ribeye Steak")."""
    from business_intelligence import _same_thing
    for it in items:
        it.update({"positive_mentions": 0, "negative_mentions": 0,
                   "complaints": [], "review_ids": []})
    for m in mentions:
        hits = [it for it in items if _same_thing(m["dish"], it["name"])]
        # A mention that matches several dishes ("chicken" vs three chicken
        # dishes) is not evidence about any one of them.
        if len(hits) != 1:
            continue
        it = hits[0]
        if m["sentiment"] == "negative":
            it["negative_mentions"] += 1
            if m["complaint"] and len(it["complaints"]) < 3:
                it["complaints"].append(m["complaint"])
        elif m["sentiment"] == "positive":
            it["positive_mentions"] += 1
        if len(it["review_ids"]) < 8:
            it["review_ids"].append(m["review_id"])


def _verdict(it):
    """What an owner should do with this dish, and why — or None."""
    q = it.get("quadrant")
    neg, pos = it["negative_mentions"], it["positive_mentions"]
    complaints_matter = neg >= MIN_MENTIONS and neg > pos
    if q == "stars" and complaints_matter:
        return ("urgent", "Protect it: this is a top earner and guests are complaining about it.")
    if q == "plowhorses":
        if complaints_matter:
            return ("fix", "Sells well, earns little, and draws complaints — fix the plate before repricing it.")
        return ("reprice", "Sells well, earns little: reprice or re-cost the plate.")
    if q == "puzzles":
        if pos >= MIN_MENTIONS:
            return ("promote", "Earns well and guests who order it like it — it needs visibility, not changes.")
        return ("promote", "Earns well but sells little: promote or reposition it.")
    if q == "dogs":
        if complaints_matter:
            return ("cut", "Low sales, low margin, and guests complain about it — a candidate to cut.")
        return ("review", "Low sales and low margin: review whether it earns its place.")
    if complaints_matter:
        return ("fix", "Guests are naming this dish in complaints.")
    return None


def dish_scorecard(restaurant_id, db_path=DB_PATH):
    """One row per costed, priced dish: margin, popularity, sentiment, verdict."""
    import inventory_ledger
    data = inventory_ledger.menu_profitability(restaurant_id)
    priced = [dict(e) for e in (data.get("priced") or [])]
    if not priced:
        return {"available": False,
                "reason": ("no dishes have both a recipe and a price yet"
                           if (data.get("unpriced") or data.get("unmapped")) else
                           "no menu items on file")}
    eng = data.get("menu_engineering") or {}
    quad = {}
    for key in ("stars", "plowhorses", "puzzles", "dogs"):
        for name in eng.get(key) or []:
            quad[name] = key
    for it in priced:
        it["quadrant"] = quad.get(it["name"])

    _attach_sentiment(priced, _dish_mentions(restaurant_id, db_path))
    for it in priced:
        v = _verdict(it)
        it["action"], it["why"] = (v if v else (None, None))

    order = {"urgent": 0, "fix": 1, "cut": 2, "reprice": 3, "promote": 4, "review": 5, None: 9}
    priced.sort(key=lambda it: (order.get(it["action"], 9), -(it.get("total_contribution") or 0)))
    return {
        "available": True,
        "dishes": priced,
        "has_sales_data": bool(data.get("has_sales_data")),
        "sentiment_window_days": SENTIMENT_WINDOW_DAYS,
        "min_mentions": MIN_MENTIONS,
        "note": (None if data.get("has_sales_data") else
                 "No sales mix yet, so dishes cannot be sorted into stars/plowhorses/puzzles/dogs; "
                 "margins and guest mentions are still real."),
        "unpriced_count": len(data.get("unpriced") or []),
        "unmapped_count": len(data.get("unmapped") or []),
    }


# ── reprice ────────────────────────────────────────────────────────────────

def _round_up(value, step=PRICE_ROUNDING):
    return round(math.ceil(value / step - 1e-9) * step, 2)


def reprice_suggestions(restaurant_id, db_path=DB_PATH):
    """For every ingredient whose price has risen, the dishes it hit, the
    monthly margin they lost, and the price that restores their food cost %.

    ASSUMPTION, stated to the owner with every result: ingredients.unit_cost
    already carries the NEW price (it is what the plate cost is computed from,
    and it updates when a count, CSV or invoice lands). The increase itself is
    read from the price history, so the pre-increase plate cost is the current
    one minus that increase.
    """
    from inventory import load_inventory_for_restaurant, compute_item_trends, build_price_watch
    items, is_live = load_inventory_for_restaurant(restaurant_id)
    if not is_live:
        return {"available": False, "reason": "no real inventory data — prices are sample figures"}
    try:
        watch = build_price_watch(compute_item_trends(restaurant_id, items or []))
    except Exception as e:
        return {"available": False, "reason": f"price history unavailable: {e}"}
    rises = [w for w in watch if (w.get("change_pct") or 0) > 0
             and w.get("old_price") and w.get("new_price")
             and w["new_price"] > w["old_price"]]
    if not rises:
        return {"available": True, "suggestions": [], "reason": "no ingredient prices have risen"}

    import inventory_ledger
    menu = {e["id"]: e for e in (inventory_ledger.menu_profitability(restaurant_id).get("priced") or [])}

    conn = get_conn(db_path)
    try:
        ingredients = [dict(r) for r in conn.execute(
            "SELECT id, name, unit FROM ingredients WHERE restaurant_id=?", (restaurant_id,)).fetchall()]
        recipes = [dict(r) for r in conn.execute(
            "SELECT ri.menu_item_id, ri.ingredient_id, ri.qty_per_unit FROM recipe_ingredients ri "
            "JOIN menu_items m ON m.id=ri.menu_item_id WHERE m.restaurant_id=? AND m.is_active=1",
            (restaurant_id,)).fetchall()]
    finally:
        conn.close()

    from invoices import match_ingredient
    by_dish = {}
    for w in rises:
        # Exact name first; otherwise the strict matcher invoices use (every
        # word of the ingredient's name present, one unique winner). A shared
        # word is NOT enough here: "chicken" alone would reprice every
        # chicken-breast dish off a chicken-thigh price rise.
        exact = [i for i in ingredients if i["name"].strip().lower() == w["item"].strip().lower()]
        ing = exact[0] if len(exact) == 1 else (None if exact else match_ingredient(w["item"], ingredients))
        if not ing:
            continue          # ambiguous or unknown ingredient — not evidence
        per_unit = w["new_price"] - w["old_price"]
        for rec in (r for r in recipes if r["ingredient_id"] == ing["id"]):
            dish = menu.get(rec["menu_item_id"])
            if not dish:
                continue
            inc = per_unit * float(rec["qty_per_unit"] or 0)
            d = by_dish.setdefault(dish["id"], {
                "dish": dish["name"], "menu_item_id": dish["id"],
                "sell_price": dish["sell_price"], "plate_cost": dish["plate_cost"],
                "units_sold_30d": dish.get("units_sold"), "increase_per_plate": 0.0,
                "drivers": []})
            d["increase_per_plate"] += inc
            d["drivers"].append({"ingredient": ing["name"], "old_price": w["old_price"],
                                 "new_price": w["new_price"], "change_pct": w["change_pct"],
                                 "per_plate": round(inc, 2)})

    out = []
    for d in by_dish.values():
        inc = d["increase_per_plate"]
        if inc < MIN_PLATE_INCREASE:
            continue
        before_cost = d["plate_cost"] - inc
        price = d["sell_price"]
        if not price or before_cost <= 0:
            continue
        before_fc = before_cost / price
        suggested = _round_up(d["plate_cost"] / before_fc) if before_fc > 0 else None
        units = d["units_sold_30d"]
        monthly = round(inc * units, 2) if units else None
        if monthly is not None and monthly < MIN_MONTHLY_MARGIN_LOST:
            continue
        d.update({
            "increase_per_plate": round(inc, 2),
            "food_cost_pct_before": round(before_fc * 100, 1),
            "food_cost_pct_now": round(d["plate_cost"] / price * 100, 1),
            "suggested_price": suggested,
            "price_change": round(suggested - price, 2) if suggested else None,
            "monthly_margin_lost": monthly,
            "monthly_basis": ("units sold over the last 30 days" if units else
                              "no sales mix yet — per-plate figure only"),
        })
        out.append(d)
    # Dollars a month first; per-plate-only rows (no sales mix) after them —
    # the two are different units and must not be sorted against each other.
    out.sort(key=lambda d: (d["monthly_margin_lost"] is None,
                            -(d["monthly_margin_lost"] or d["increase_per_plate"])))
    return {
        "available": True,
        "suggestions": out,
        "assumption": ("Assumes your ingredient costs already reflect the new price. The "
                       "suggested price restores each dish's food cost % to what it was before "
                       "the increase, rounded up to the nearest 25¢ — a starting point, not a "
                       "rule: re-costing the plate or changing a supplier are alternatives."),
    }
