"""
dsr.block_food — food cost for one night.

Every figure comes from the module that already computes it; nothing here
calls a model.

  estimated food cost  THEORETICAL: each dish's plate cost (recipes ×
                       ingredient unit costs, inventory_ledger.menu_profitability)
                       × what that dish sold on the night (menu_item_sales).
                       Labelled an estimate in detail, with its coverage —
                       the share of units sold that had a costed recipe — and
                       the theoretical food cost % on the dishes that have
                       both a recipe and a price.
  waste                the ledger's waste events dated that night
                       (inventory_ledger.waste_sources for one day), split
                       into counted and inferred-from-a-count. Measured only
                       on a night something was counted or logged.
  low / critical stock inventory.analysis_for — the same lists Food Cost and
                       the supplier order read; stock as of the report.
  recoverable          food_cost_intelligence.cost_drivers's DEDUPLICATED
                       monthly total (one ingredient counted once), never the
                       plain sum.
  variance             inventory_ledger.inferred_variance over the 7 days to
                       the night, and only when a count was taken in them —
                       without a count there is nothing to compare usage to.

Statuses:
  not_connected  Food Cost is off, or no inventory is on file (sample data)
  unavailable    the inventory read itself failed
  awaiting       recipes are costed, no item sales are synced for the night
                 and tonight's Sales block itself is still coming — its POS
                 pull lists every item sold, which the estimate costs when
                 the 5am item sync (menu_item_sales) hasn't run yet (D1-8).
                 Nothing here waits on the item sync: no later version is
                 made for it.
  ready          otherwise. A figure that can't be measured is None, with the
                 sentence saying why in detail.
"""
from datetime import timedelta

import dsr
from dsr import common

VARIANCE_WINDOW_DAYS = 7
# The estimated food cost % is stated only when at least this share of the
# units sold that night had a fully costed recipe — most of what was sold.
# Below it the percentage describes a minority of the night and is withheld
# with the coverage stated (CA1 D5). The dollars of what was costed stay.
ESTIMATE_MIN_COVERAGE_PCT = 50
# A night with no item sales is "not on file" only for a restaurant that
# has had item sales before — otherwise it is "this POS doesn't send them".
ITEM_HISTORY_DAYS = 28


def _rows(ctx, sql, args):
    from dsr.store import get_conn
    conn = get_conn(ctx.db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── the estimate ────────────────────────────────────────────────────────────

def _tonight_items(ctx):
    """([{"id", "name", "qty"}], waiting) — tonight's items from the Sales
    block's own POS pull, matched to the menu by the POS's item id
    (menu_items.toast_guid, as the nightly item sync matches them). An item
    the menu list doesn't have is left out, as the item sync leaves it out.
    `waiting` is True while the Sales block itself is still coming."""
    sales = (ctx.blocks or {}).get("sales") or {}
    if sales.get("status") == dsr.AWAITING:
        return [], True
    if sales.get("status") != dsr.READY:
        return [], False
    by_guid = {}
    for it in (sales.get("detail") or {}).get("items_sold") or []:
        guid = it.get("guid")
        qty = float(it.get("qty") or 0)
        if guid and qty > 0:
            by_guid[str(guid)] = by_guid.get(str(guid), 0.0) + qty
    if not by_guid:
        return [], False
    marks = ",".join("?" for _ in by_guid)
    menu = _rows(ctx, f"SELECT id, name, toast_guid FROM menu_items WHERE restaurant_id=? AND is_active=1 "
                      f"AND toast_guid IN ({marks})", (ctx.restaurant_id, *by_guid))
    return [{"id": m["id"], "name": m["name"], "qty": by_guid[str(m["toast_guid"])]} for m in menu
            if str(m["toast_guid"]) in by_guid], False


def _estimate(ctx):
    """(metrics, detail, awaiting_reason)."""
    import inventory_ledger as il
    from time_utils import mdy
    mp = il.menu_profitability(ctx.restaurant_id)
    plate, price, implausible = {}, {}, set()
    for e in (mp.get("priced") or []) + (mp.get("unpriced") or []):
        if e.get("unit_warning"):
            implausible.add(e["id"])
            continue
        plate[e["id"]] = float(e["plate_cost"])
        if e.get("sell_price"):
            price[e["id"]] = float(e["sell_price"])
    uncosted = {e["id"] for e in (mp.get("uncosted") or [])}
    empty = {"est_food_cost": None, "est_food_cost_pct": None, "recipe_coverage_pct": None}

    if not plate:
        why = ("No dish has a fully costed recipe yet, so food cost can't be estimated."
               if (uncosted or implausible) else
               "No recipes are set up, so food cost can't be estimated from what sold.")
        return empty, {"estimate": None, "note": why}, None

    sold = _rows(ctx, "SELECT s.menu_item_id AS id, m.name, s.qty_sold AS qty FROM menu_item_sales s "
                      "JOIN menu_items m ON m.id=s.menu_item_id AND m.restaurant_id=s.restaurant_id "
                      "WHERE s.restaurant_id=? AND s.business_date=?", (ctx.restaurant_id, ctx.day))
    sold = [s for s in sold if float(s["qty"] or 0) > 0]
    units_from = "the item sales synced for the night"
    if not sold:
        # The nightly item sync (menu_item_sales) runs at 5am Central, after
        # most report deadlines, and nothing makes a later version for it —
        # so "the estimate follows" never did (D1-8). Tonight's own POS pull
        # (the Sales block) already lists every item sold by the POS's item
        # id, the same id the item sync matches menu_items on: cost those.
        tonight, waiting = _tonight_items(ctx)
        if tonight:
            sold, units_from = tonight, "tonight's POS item sales"
        elif waiting:
            return empty, {"estimate": None, "note": "Waiting for tonight's sales from the POS."}, \
                "Waiting for tonight's sales from the POS"
    if not sold:
        import pos
        since = (ctx.business_date - timedelta(days=ITEM_HISTORY_DAYS)).isoformat()
        had_items = _rows(ctx, "SELECT 1 FROM menu_item_sales WHERE restaurant_id=? AND business_date>=? "
                               "AND business_date<? LIMIT 1", (ctx.restaurant_id, since, ctx.day))
        reports_items = bool(had_items) or pos.supports(ctx.restaurant_id, "fetch_order_selections")
        if not reports_items:
            return empty, {"estimate": None, "note": "Your POS doesn't send item-level sales to Cavnar, "
                                                      "so food cost can't be estimated from recipes."}, None
        # Said as it stands, never as a promise: no later version of this
        # report is made for item sales.
        return empty, {"estimate": None,
                       "note": f"No item sales for {mdy(ctx.business_date)} were on file when this report was "
                               "built, so food cost wasn't estimated."}, None

    units = sum(float(s["qty"]) for s in sold)
    costed = [s for s in sold if s["id"] in plate]
    units_costed = sum(float(s["qty"]) for s in costed)
    cost = sum(float(s["qty"]) * plate[s["id"]] for s in costed)
    priced = [s for s in costed if s["id"] in price]
    revenue = sum(float(s["qty"]) * price[s["id"]] for s in priced)
    priced_cost = sum(float(s["qty"]) * plate[s["id"]] for s in priced)
    missing = sorted((s for s in sold if s["id"] not in plate), key=lambda s: -float(s["qty"]))
    coverage = round(units_costed / units * 100, 1) if units else 0.0
    # The estimate is a percentage of the night only when enough of what was
    # sold is costed: a percentage built on 30% of the units sold says
    # nothing about the night (CA1 D5). Under the floor the dollars of what
    # WAS costed stay, the percentage is withheld and says why.
    covered = coverage >= ESTIMATE_MIN_COVERAGE_PCT
    metrics = {
        "est_food_cost": round(cost, 2) if costed else None,
        "est_food_cost_pct": round(priced_cost / revenue * 100, 1) if (revenue > 0 and covered) else None,
        "recipe_coverage_pct": coverage,
    }
    detail = {"estimate": {
        "estimated": True,
        # "units", not "dishes": the coverage and the estimate are counted in
        # units sold, and a dish sold twenty times is twenty of them.
        "label": (f"Estimated — what the units sold on {mdy(ctx.business_date)} should have cost by their "
                  "recipes, at current ingredient prices. Not a count of what was actually used."),
        "units_from": units_from,
        "units_sold": round(units, 2),
        "units_with_recipe": round(units_costed, 2),
        "coverage_basis": "share of units sold that had a fully costed recipe",
        "coverage_floor_pct": ESTIMATE_MIN_COVERAGE_PCT,
        "coverage_note": (None if covered else
                          f"Only {coverage:g}% of units sold have a costed recipe — the food cost % is "
                          f"withheld until it reaches {ESTIMATE_MIN_COVERAGE_PCT}%."),
        "pct_basis": ("theoretical cost over menu price, on the dishes sold that have both a recipe and a price"
                      if revenue > 0 else None),
        "without_recipe": [{"dish": s["name"], "units": round(float(s["qty"]), 2),
                            "why": ("recipe has an ingredient with no cost" if s["id"] in uncosted else
                                    "plate cost looks like a unit mismatch" if s["id"] in implausible else
                                    "no recipe")} for s in missing[:5]],
        "without_recipe_count": len(missing),
        "scope": "dishes in your menu list; items the POS sold that aren't in it yet aren't counted",
    }}
    return metrics, detail, None


# ── waste, stock, recoverable, variance ─────────────────────────────────────

def _waste(ctx, ledger):
    import inventory_ledger as il
    from time_utils import mdy
    if not ledger:
        return None, "Waste is tracked per week on your inventory sheet, not per night."
    counts = _rows(ctx, "SELECT COUNT(*) AS n FROM ingredient_stock_events WHERE restaurant_id=? AND event_date=? "
                        "AND event_type='recount' AND COALESCE(source,'') != 'migration'",
                   (ctx.restaurant_id, ctx.day))[0]["n"]
    ws = il.waste_sources(ctx.restaurant_id, days=1, as_of=ctx.business_date)
    if not counts and not ws.get("has_data"):
        return None, (f"Nothing was counted or logged as waste on {mdy(ctx.business_date)}, "
                      "so the night's waste isn't known.")
    return {
        "total": ws["total"], "counted": ws["counted"], "inferred": ws["inferred"],
        "counts_taken": counts,
        "inferred_basis": ("inferred = what the ledger expected minus what was counted: waste, portioning "
                           "or a miscount — a count can't tell them apart"),
        "top_inferred": [{"ingredient": x["ingredient"], "cost": x["cost"], "gap_qty": x["gap_qty"],
                          "unit": x["unit"]} for x in (ws.get("top_inferred") or [])[:3]],
    }, None


def _stock(analysis):
    def line(x):
        return {"item": x.get("item"), "days_remaining": x.get("days_remaining"),
                "current_stock": x.get("current_stock"), "unit": x.get("unit") or "",
                "count_stale": bool(x.get("count_stale"))}
    crit = analysis.get("critical_low") or []
    low = analysis.get("reorder_soon") or []
    fresh = analysis.get("count_freshness") or {}
    return {"critical": [line(x) for x in crit[:8]], "critical_count": len(crit),
            "low": [line(x) for x in low[:8]], "low_count": len(low),
            "as_of": "stock at the time of this report",
            "basis": analysis.get("stock_basis"),
            "count_stale": bool(fresh.get("stale"))}


def _drivers_at_stake(ctx):
    """The cost drivers' monthly dollars, each ingredient once — what is at
    stake across price rises, waste, menu and supplier gaps. It used to be
    published as `recoverable_monthly`, the name every other surface gives
    inventory's waste above tolerance ($782 here against $182 there for the
    same restaurant, NS3 H3); it is `drivers_at_stake_monthly` now, with its
    split by money kind, and `recoverable_monthly` is inventory's figure."""
    import food_cost_intelligence as fci
    drv = fci.cost_drivers(ctx.restaurant_id)
    total = drv.get("total_monthly_deduplicated")
    complete = bool(drv.get("complete", True))
    if total is None or (not complete and not drv.get("drivers")):
        return None, {"note": "The cost drivers couldn't be priced tonight.",
                      "degraded_sources": drv.get("degraded_sources") or []}
    return float(total), {
        "total_monthly": float(total),
        "basis": drv.get("total_basis") or drv.get("basis"),
        "per": "month",
        # Opportunities and estimates of money already being spent, never
        # money saved; by kind, never one blended claim (NS3 H3).
        "kind": "opportunity",
        "totals_by_kind": drv.get("totals_by_kind") or {},
        "complete": complete,
        "degraded_sources": drv.get("degraded_sources") or [],
        "note": drv.get("reason"),
        "top": [{"label": d["label"], "dollars_monthly": d["dollars_monthly"], "confidence": d["confidence"],
                 "value_kind": d.get("value_kind")}
                for d in (drv.get("drivers") or [])[:3]],
    }


def _recoverable(analysis):
    """inventory's recoverable figure — waste above each category's
    tolerance, one week projected to a month — the one number "recoverable"
    means on every surface (NS3 H3, R11)."""
    v = (analysis or {}).get("recoverable_monthly")
    if v is None:
        return None, {"note": "Recoverable waste couldn't be read tonight."}
    return float(v), {
        "total_monthly": float(v),
        "per": "month",
        "kind": "opportunity",
        "basis": (analysis or {}).get("recoverable_basis"),
        "projection_basis": (analysis or {}).get("projection_basis"),
        "note": "An opportunity projected from one week's waste — not money saved.",
    }


def _variance(ctx, ledger):
    import inventory_ledger as il
    from time_utils import mdy, mdy_range
    start = ctx.business_date - timedelta(days=VARIANCE_WINDOW_DAYS - 1)
    window = mdy_range(start, ctx.business_date)
    if not ledger:
        return None, {"note": "Variance needs the ingredient ledger and a count; neither is on file."}
    counts = _rows(ctx, "SELECT COUNT(*) AS n, MAX(event_date) AS last FROM ingredient_stock_events "
                        "WHERE restaurant_id=? AND event_type='recount' AND COALESCE(source,'') != 'migration' "
                        "AND event_date BETWEEN ? AND ?", (ctx.restaurant_id, start.isoformat(), ctx.day))[0]
    if not counts["n"]:
        return None, {"window": window,
                      "note": f"No count was taken {window}, so there's no actual usage to compare recipes to."}
    v = il.inferred_variance(ctx.restaurant_id, days=VARIANCE_WINDOW_DAYS, as_of=ctx.business_date)
    material = v.get("material") or []
    return {"cost": round(sum(e["cost"] for e in material), 2), "items": len(material)}, {
        "window": window, "last_count": mdy(counts["last"]), "basis": v.get("basis"),
        "items": [{"ingredient": e["ingredient"], "cost": e["cost"], "variance_pct": e["variance_pct"],
                   "gap_qty": e["gap_qty"], "unit": e["unit"],
                   "dish": (e.get("dishes") or [{}])[0].get("dish")} for e in material[:5]],
    }


# ── the block ───────────────────────────────────────────────────────────────

def collect(ctx):
    r = ctx.restaurant
    if not getattr(r, "module_inventory", 1):
        return dsr.block(dsr.NOT_CONNECTED, source="cavnar", block_name="food",
                         reason="Food Cost isn't switched on for this location")
    import inventory
    gaps = []
    got = common.guard(ctx, "food", "inventory", lambda: inventory.analysis_for(ctx.restaurant_id), gaps)
    if got is None:
        return dsr.block(dsr.UNAVAILABLE, source="cavnar", block_name="food")
    _items, is_live, analysis = got
    if not is_live:
        return dsr.block(dsr.NOT_CONNECTED, source="cavnar", block_name="food",
                         reason="Inventory isn't set up — no ingredients or counts on file")

    ledger = bool(common.guard(ctx, "food", "ledger", lambda: _rows(
        ctx, "SELECT 1 FROM ingredients WHERE restaurant_id=? AND is_active=1 LIMIT 1", (ctx.restaurant_id,)), gaps))

    metrics, detail = {}, {}
    est = common.guard(ctx, "food", "estimate", lambda: _estimate(ctx), gaps)
    awaiting = None
    if est is None:
        metrics.update({"est_food_cost": None, "est_food_cost_pct": None, "recipe_coverage_pct": None})
        detail["estimate"] = None
    else:
        m, d, awaiting = est
        metrics.update(m)
        detail.update(d)

    waste = common.guard(ctx, "food", "waste", lambda: _waste(ctx, ledger), gaps, default=(None, None))
    w, w_note = waste
    metrics["waste_logged"] = w["total"] if w else None
    metrics["waste_counted"] = w["counted"] if w else None
    metrics["waste_inferred"] = w["inferred"] if w else None
    detail["waste"] = w or {"note": w_note or "Waste couldn't be read tonight."}

    stock = _stock(analysis)
    metrics["critical_low"] = stock["critical_count"]
    metrics["low_stock"] = stock["low_count"]
    detail["stock"] = stock

    rec = _recoverable(analysis)
    metrics["recoverable_monthly"] = rec[0]
    detail["recoverable"] = rec[1]
    drv = common.guard(ctx, "food", "drivers", lambda: _drivers_at_stake(ctx), gaps, default=(None, None))
    metrics["drivers_at_stake_monthly"] = drv[0]
    detail["drivers_at_stake"] = drv[1] or {"note": "The cost drivers couldn't be priced tonight."}

    var = common.guard(ctx, "food", "variance", lambda: _variance(ctx, ledger), gaps, default=(None, None))
    metrics["variance_cost"] = var[0]["cost"] if var[0] else None
    metrics["variance_items"] = var[0]["items"] if var[0] else None
    detail["variance"] = var[1] or {"note": "Variance couldn't be read tonight."}

    detail["unavailable_parts"] = gaps
    if awaiting:
        return dsr.block(dsr.AWAITING, source="cavnar", block_name="food", reason=awaiting,
                         metrics=metrics, detail=detail)
    return dsr.block(dsr.READY, source="cavnar", metrics=metrics, detail=detail)
