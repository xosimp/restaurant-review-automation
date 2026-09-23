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
        # units_sold comes from menu_profitability's popularity window, which
        # is 28 days, not 30: multiplied as-is it was a four-week figure
        # labelled "the last 30 days" (MOD-FC-28). Scaled to 30 and said so.
        # (The key keeps its old name; clients read it.)
        window = inventory_ledger._POPULARITY_WINDOW_DAYS
        monthly = round(inc * units * 30.0 / window, 2) if units else None
        if monthly is not None and monthly < MIN_MONTHLY_MARGIN_LOST:
            continue
        d.update({
            "increase_per_plate": round(inc, 2),
            "food_cost_pct_before": round(before_fc * 100, 1),
            "food_cost_pct_now": round(d["plate_cost"] / price * 100, 1),
            "suggested_price": suggested,
            "price_change": round(suggested - price, 2) if suggested else None,
            "monthly_margin_lost": monthly,
            "monthly_basis": (f"units sold over the last {window} days, scaled to 30" if units else
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


# ── one-tap reprice, and what the owner chose (audit #26 / #41) ─────────────
#
# A reprice suggestion used to end at a number on a card: the owner then
# found the dish in Menu Margins and typed a price, and nothing recorded that
# the price they typed came from the suggestion — or what the suggestion had
# been. The outcome tracker started on ANY price set, including a dish's
# first price and a price cleared to nothing. Now:
#   * apply_reprice sets the suggested (or owner-adjusted) price in one tap;
#   * every price that FOLLOWS a live suggestion — tapped or typed — records
#     the suggested price beside the chosen one in reprice_decisions and
#     answers the recommendation ("reprice:<dish>") in rec_ledger;
#   * a price that follows no suggestion records nothing and starts no
#     tracker.

def _conn(db_path=DB_PATH):
    """get_conn resolved at call time (the module-level import is bound)."""
    import models as _m
    return _m.get_conn() if db_path in (None, _m.DB_PATH) else _m.get_conn(db_path)


def init_menu_intelligence(db_path: str = DB_PATH):
    """Boot DDL (models.init_db)."""
    conn = _conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS reprice_decisions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id    INTEGER NOT NULL,
            menu_item_id     INTEGER,
            dish             TEXT,
            old_price        REAL,
            suggested_price  REAL,
            chosen_price     REAL,
            source           TEXT,          -- 'one_tap' | 'manual'
            user_id          INTEGER,
            created_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reprice_decisions_rid "
                     "ON reprice_decisions(restaurant_id, created_at)")
        conn.commit()
    finally:
        conn.close()


def reprice_key(dish) -> str:
    import rec_ledger
    return rec_ledger.rec_key("reprice", (dish or "").strip())


def suggestion_for(restaurant_id, menu_item_id=None, dish=None, db_path=DB_PATH):
    """The live reprice suggestion for one dish, or None."""
    try:
        data = reprice_suggestions(restaurant_id, db_path=db_path)
    except Exception:
        return None
    want = (dish or "").strip().lower()
    for s in (data.get("suggestions") or []) if data.get("available") else []:
        if menu_item_id is not None and s.get("menu_item_id") == menu_item_id:
            return s
        if want and (s.get("dish") or "").strip().lower() == want:
            return s
    return None


def presented_suggestions(restaurant_id, surface="food", user_id=None, db_path=DB_PATH):
    """reprice_suggestions for a screen: each suggestion carries its
    rec_ledger key, is logged as shown, and one the owner has already
    answered (applied, or said no to) is left out."""
    data = reprice_suggestions(restaurant_id, db_path=db_path)
    sug = data.get("suggestions") or []
    if not sug:
        return data
    import insight_store
    items = [{"key": reprice_key(s["dish"]),
              "text": (f"Reprice {s['dish']} to ${s['suggested_price']:.2f}" if s.get("suggested_price")
                       else f"Reprice {s['dish']}"),
              "dollar_value": s.get("monthly_margin_lost"), "model_written": False,
              "cavnar_completes": True, "expected_metric": "food_cost_pct", "_s": s} for s in sug]
    kept = insight_store.present_recs(restaurant_id, "food", surface, items, user_id=user_id, db_path=db_path)
    out = []
    for it in kept:
        s = dict(it["_s"])
        s["rec_key"] = it["key"]
        s["rec_id"] = it.get("rec_id")
        out.append(s)
    return dict(data, suggestions=out)


def record_price_change(restaurant_id, menu_item_id, old_price, new_price, user_id=None,
                        source="manual", suggestion=None, db_path=DB_PATH):
    """A dish's price changed. When the new price FOLLOWS a live suggestion —
    there is one for this dish, the dish already had a price, and the new
    one is higher — record suggested vs chosen and answer the
    recommendation. Returns the suggestion followed, or None (a first price,
    a clear-to-nothing, a cut, or no suggestion: nothing is recorded)."""
    try:
        old = float(old_price) if old_price not in (None, "") else None
        new = float(new_price) if new_price not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if not old or old <= 0 or not new or new <= old:
        return None
    s = suggestion or suggestion_for(restaurant_id, menu_item_id=menu_item_id, db_path=db_path)
    if not s:
        return None
    try:
        conn = _conn(db_path)
        try:
            conn.execute("INSERT INTO reprice_decisions (restaurant_id, menu_item_id, dish, old_price, "
                         "suggested_price, chosen_price, source, user_id) VALUES (?,?,?,?,?,?,?,?)",
                         (restaurant_id, menu_item_id, s.get("dish"), old, s.get("suggested_price"),
                          round(new, 2), source, user_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[menu_intelligence] reprice decision not recorded: {e}")
    try:
        import rec_ledger
        key = reprice_key(s.get("dish"))
        meta = {"suggested_price": s.get("suggested_price"), "chosen_price": round(new, 2),
                "old_price": old, "source": source, "module": "food"}
        rec_ledger.record(restaurant_id, key, "accepted", surface="food", user_id=user_id, meta=meta,
                          db_path=db_path)
        rec_ledger.record(restaurant_id, key, "completed", surface="food", user_id=user_id, meta=meta,
                          db_path=db_path)
    except Exception as e:
        print(f"[menu_intelligence] reprice answer not recorded: {e}")
    return s


def reprice_decisions(restaurant_id, limit=50, db_path=DB_PATH) -> list:
    conn = _conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM reprice_decisions WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
            (restaurant_id, int(limit))).fetchall()]
    finally:
        conn.close()


def apply_reprice(restaurant_id, dish=None, price=None, menu_item_id=None, user_id=None, db_path=DB_PATH):
    """One tap: set a dish to its suggested price (or the price the owner
    adjusted it to). Returns (payload, status). This is the contract Home and
    both clients call: POST /food-cost/reprice/apply {dish, price}."""
    import inventory_ledger
    if not (dish or menu_item_id):
        return {"ok": False, "error": "Which dish?"}, 400
    s = suggestion_for(restaurant_id, menu_item_id=menu_item_id, dish=dish, db_path=db_path)
    if not s:
        return {"ok": False, "error": "There's no price suggestion for that dish right now — "
                                      "set its price from Menu Margins instead."}, 409
    try:
        chosen = float(price) if price not in (None, "") else float(s.get("suggested_price") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "That price isn't a number."}, 400
    if not math.isfinite(chosen) or chosen <= 0:
        return {"ok": False, "error": "Pick a price above $0."}, 400
    old = s.get("sell_price")
    if not inventory_ledger.set_menu_item_price(restaurant_id, int(s["menu_item_id"]), chosen):
        return {"ok": False, "error": "Couldn't set that price — check the dish."}, 400
    followed = record_price_change(restaurant_id, s["menu_item_id"], old, chosen, user_id=user_id,
                                   source="one_tap", suggestion=s, db_path=db_path)
    return {"ok": True, "dish": s["dish"], "menu_item_id": s["menu_item_id"], "old_price": old,
            "suggested_price": s.get("suggested_price"), "price": round(chosen, 2),
            "rec_key": reprice_key(s["dish"]), "tracked": bool(followed)}, 200
