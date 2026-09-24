"""food_cost_intelligence.py — the CFO layer.

The Food Cost module held four financial engines and none of them talked to
each other, or to the AI:

  * inventory.analyse_inventory      — waste, overstock, reorder     -> the AI saw this
  * cogs.build_food_cost_pct         — actual food cost % vs target  -> nothing saw it
  * inventory_ledger.menu_profitability — plate margin, contribution -> nothing saw it
  * inventory_ledger.recipe_coverage / waste_sources — how far to trust the rest

So the AI narrated waste. It was titled "a food cost consultant reviewing
weekly inventory data", which was an accurate description of what it did, and
it could not tell an owner whether their food cost was over target, which dish
was eating the margin, or why anything moved.

This module is the layer that makes a CFO's paragraph possible, and every
piece of it is built so the paragraph cannot be written unless the evidence is
actually there:

  * `weekly_snapshot`      — writes the history the whole module reads, on a
                             SCHEDULE, because it used to be a side effect of
                             someone opening the tab.
  * `cost_drivers`         — every driver of cost movement, each with the
                             dollars it carries, ranked IN PYTHON rather than
                             by asking a model to rank them.
  * `profitability_projection` — COGS + labor against POS sales, projected to
                             month end. The number an owner actually asks for.
  * `weekday_waste`        — which days the waste lands on.
  * `seasonal_baseline`    — this period against the same period last year.
  * `supplier_comparison`  — the same ingredient's price across suppliers.
  * `operational_context`  — what labor, reviews and marketing recorded over
                             the same window.
  * `diagnose`             — the root-cause pass: one Sonnet call over the
                             ranked drivers and the cross-module context,
                             required to cite drivers it was given, to name an
                             alternative explanation and to say what would
                             tell them apart.
  * `executive_brief`      — deterministic answers to the eight questions a
                             morning briefing has to answer, true before any
                             model runs.
  * `record_forecast` / `score_forecasts` — a forecast nobody scores is a
                             claim with no cost to being wrong.

Read-only against the database except `weekly_snapshot`, `diagnose` and the
forecast functions, which say so in their names.
"""
import json
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports):
    a copy bound at import kept pointing wherever models.get_conn pointed
    the first time this module was imported."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()

# A driver has to carry real money before it is worth an owner's attention or
# a line in a paragraph. Mirrors inventory_ledger.MIN_VARIANCE_DOLLARS.
MIN_DRIVER_DOLLARS = 15.0

# How far back the CFO read looks, and how long one stays fresh. Food cost
# moves on the timescale of deliveries and counts, not minutes.
DIAGNOSIS_WINDOW_DAYS = 28
DIAGNOSIS_TTL_HOURS = 24

# A month-end projection needs enough of the month behind it to mean anything.
# Three days into a month, the run rate is noise.
MIN_DAYS_FOR_PROJECTION = 7

# Year-over-year needs a real prior-year window, not one stray snapshot.
MIN_WEEKS_FOR_SEASONAL = 3

CONFIDENCES = ("high", "medium", "low")


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _rows_raw(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def _one_row(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchone()
    except Exception:
        return None


# ── The history writer ──────────────────────────────────────────────────────

def weekly_snapshot(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Write today's inventory snapshot. Scheduled, not render-triggered.

    `inventory_history` had exactly one writer in production: the AI insight
    function, which runs when somebody opens the Food Cost tab. Everything
    downstream reads that table — the weekly waste series and its whole trend
    verdict, the multi-week price trends, the price-spike alert, and the
    opening/closing inventory values behind food cost %. So a week nobody
    looked at is not a zero in the series, it is ABSENT from it; a restaurant
    that never opens the tab gets no price alerts at all, and is told nothing
    about that.

    This runs daily per restaurant. `source='scheduled'` distinguishes these
    rows from the render-time write, which upserts on top of the same
    (restaurant, week_end) key rather than competing with it.

    Also stores `inv_value` — total counted stock value — which is exactly
    what cogs.inventory_value_near needs to bracket a COGS window, and which
    the render-time writer never persisted as its own column.
    """
    from inventory import analysis_for
    items, is_live, analysis = analysis_for(restaurant_id)
    if not is_live:
        return {"ok": False, "reason": "sample data — nothing to snapshot"}

    from time_utils import restaurant_now_by_id
    week_end = restaurant_now_by_id(restaurant_id).strftime("%Y-%m-%d")
    payload = {
        "total_waste_cost": analysis.get("total_waste_cost_week", 0),
        "top_items": [x["item"] for x in (analysis.get("waste_items") or [])[:4]],
    }
    conn = get_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT id, source FROM inventory_history WHERE restaurant_id=? AND week_end=?",
            (restaurant_id, week_end)).fetchone()
        if existing:
            conn.execute(
                "UPDATE inventory_history SET waste_json=?, items_json=?, inv_value=?, "
                "source=COALESCE(source,'scheduled'), saved_at=datetime('now') WHERE id=?",
                (json.dumps(payload), json.dumps(items),
                 analysis.get("total_stock_value"), existing["id"]))
        else:
            conn.execute(
                "INSERT INTO inventory_history "
                "(restaurant_id, waste_json, week_end, items_json, inv_value, source) "
                "VALUES (?,?,?,?,?,'scheduled')",
                (restaurant_id, json.dumps(payload), week_end, json.dumps(items),
                 analysis.get("total_stock_value")))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "week_end": week_end,
            "waste": analysis.get("total_waste_cost_week", 0),
            "inv_value": analysis.get("total_stock_value")}


# ── Where the waste lands ───────────────────────────────────────────────────

_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

# Same idea as every other floor in this codebase: a rate computed on one or
# two events is a number that will be read as a finding.
MIN_EVENTS_PER_BUCKET = 3


def weekday_waste(restaurant_id: int, days: int = 56, db_path: str = DB_PATH) -> dict:
    """Waste dollars by day of week, from the event ledger.

    No waste query in the module grouped by weekday, so "is this a Friday
    problem?" — the most natural operational question an owner has about a
    cost line — had no answer anywhere. A day is only given a share when it
    clears MIN_EVENTS_PER_BUCKET, because a single Friday event at 100% of
    that day's waste is not a Friday pattern.
    """
    conn = get_conn(db_path)
    rows = _rows_raw(conn, """
        SELECT e.event_date, e.source,
               COALESCE(SUM(e.qty * COALESCE(i.unit_cost,0)),0) AS cost,
               COUNT(*) AS n
          FROM ingredient_stock_events e
          JOIN ingredients i ON i.id=e.ingredient_id AND i.restaurant_id=e.restaurant_id
         WHERE e.restaurant_id=? AND e.event_type='waste' AND e.event_date >= ?
         GROUP BY e.event_date, e.source
    """, (restaurant_id, (date.today() - timedelta(days=days - 1)).isoformat()))
    conn.close()

    buckets = {}
    total = 0.0
    for r in rows:
        try:
            wd = _WEEKDAYS[int(datetime.strptime(r["event_date"], "%Y-%m-%d").strftime("%w"))]
        except (ValueError, TypeError):
            continue
        b = buckets.setdefault(wd, {"cost": 0.0, "events": 0, "counted": 0.0, "inferred": 0.0})
        c = _f(r["cost"])
        b["cost"] += c
        b["events"] += int(r["n"] or 0)
        if r["source"] == "inferred":
            b["inferred"] += c
        else:
            b["counted"] += c
        total += c

    out = []
    for wd in _WEEKDAYS:
        b = buckets.get(wd)
        if not b:
            continue
        enough = b["events"] >= MIN_EVENTS_PER_BUCKET
        out.append({
            "weekday": wd, "cost": round(b["cost"], 2), "events": b["events"],
            "counted": round(b["counted"], 2), "inferred": round(b["inferred"], 2),
            "share": round(b["cost"] / total, 3) if (enough and total > 0) else None,
            "below_floor": not enough,
        })
    ranked = sorted([o for o in out if o["share"] is not None],
                    key=lambda o: o["share"], reverse=True)
    # A week has seven days, so an even spread is 14.3% each and that is the
    # baseline — NOT one over the number of days that happen to carry waste.
    #
    # Measuring against days-with-data inverted the test: a restaurant whose
    # waste landed on exactly two days had a baseline of 50%, so Thursday
    # carrying 69% of every waste dollar did not clear 1.6x of that and was
    # reported as "spread fairly evenly across the week" — a statement that
    # was not merely unhelpful but false, and the concentration it hid is
    # precisely the operational signal this function exists to find.
    _EVEN = 1.0 / 7.0
    concentrated = ranked[0] if (ranked and ranked[0]["share"] >= _EVEN * 1.6) else None
    # How many days of the week carry any waste at all. When waste lands on
    # two days out of seven, THAT is the pattern, and the caller needs to be
    # able to say so instead of naming one day as if the others were merely
    # quieter.
    days_with_waste = len([o for o in out if o["cost"] > 0])
    return {"window_days": days, "by_weekday": out, "total": round(total, 2),
            "worst_day": concentrated, "has_data": bool(rows),
            "days_with_waste": days_with_waste,
            "even_share": round(_EVEN, 3),
            "min_events_per_bucket": MIN_EVENTS_PER_BUCKET}


def seasonal_baseline(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """This 4-week window against the same 4 weeks last year.

    Nothing in the module looked back a year, so a restaurant heading into
    its own seasonal peak read as a restaurant with a worsening cost problem.
    Returns available=False with a reason rather than comparing against a
    window that isn't really there.
    """
    from waste_trend import load_waste_history
    weeks, _total = load_waste_history(restaurant_id, None, db_path=db_path)
    if not weeks:
        return {"available": False, "reason": "no waste history on file"}

    today = date.today()
    def _window(end_day, n=28):
        start = end_day - timedelta(days=n - 1)
        vals = []
        for w in weeks:
            try:
                d = date.fromisoformat(w["week_end"])
            except Exception:
                continue
            if start <= d <= end_day:
                vals.append(_f(w.get("waste")))
        return vals

    now_vals = _window(today)
    then_vals = _window(today - timedelta(days=365))
    if len(now_vals) < MIN_WEEKS_FOR_SEASONAL or len(then_vals) < MIN_WEEKS_FOR_SEASONAL:
        return {"available": False,
                "reason": f"needs {MIN_WEEKS_FOR_SEASONAL}+ weeks in both this window and the "
                          f"same window last year (have {len(now_vals)} and {len(then_vals)})"}
    now_avg = sum(now_vals) / len(now_vals)
    then_avg = sum(then_vals) / len(then_vals)
    if then_avg <= 0:
        return {"available": False, "reason": "last year's window recorded no waste to compare against"}
    change = (now_avg - then_avg) / then_avg * 100
    return {"available": True, "this_year_weekly_avg": round(now_avg, 2),
            "last_year_weekly_avg": round(then_avg, 2),
            "change_pct": round(change, 1),
            "weeks_this_year": len(now_vals), "weeks_last_year": len(then_vals),
            "reading": ("running higher than the same weeks last year" if change > 10
                        else "running lower than the same weeks last year" if change < -10
                        else "in line with the same weeks last year")}


def supplier_comparison(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """The same ingredient carried by more than one supplier, at more than one
    price.

    Price Watch compares a restaurant's own cost to its own history, which
    can say a price rose and can never say it is a bad price. This is the
    only genuine sourcing comparison the stored data supports: where the same
    ingredient name appears against two different suppliers, the spread
    between them is real money and the cheaper one is already on file.

    Names are matched case-insensitively and trimmed. It reports nothing when
    an ingredient sits with one supplier, because there is nothing to compare.
    """
    conn = get_conn(db_path)
    rows = _rows_raw(conn, """
        SELECT LOWER(TRIM(name)) AS key, name, supplier_name,
               COALESCE(unit_cost,0) AS unit_cost, unit
          FROM ingredients
         WHERE restaurant_id=? AND is_active=1
           AND supplier_name IS NOT NULL AND TRIM(supplier_name) <> ''
           AND unit_cost > 0
    """, (restaurant_id,))
    conn.close()

    groups = {}
    for r in rows:
        groups.setdefault(r["key"], []).append({
            "name": r["name"], "supplier": r["supplier_name"],
            "unit_cost": round(_f(r["unit_cost"]), 4), "unit": r["unit"] or ""})
    out = []
    for key, entries in groups.items():
        suppliers = {e["supplier"] for e in entries}
        if len(suppliers) < 2:
            continue
        # Units have to agree or the comparison is meaningless — ingredients.unit
        # is free text and nothing converts between a case and a pound.
        units = {(e["unit"] or "").strip().lower() for e in entries}
        if len(units) > 1:
            continue
        entries.sort(key=lambda e: e["unit_cost"])
        low, high = entries[0], entries[-1]
        if low["unit_cost"] <= 0:
            continue
        spread_pct = (high["unit_cost"] - low["unit_cost"]) / low["unit_cost"] * 100
        if spread_pct < 5:
            continue
        out.append({
            "ingredient": high["name"], "unit": high["unit"],
            "cheapest_supplier": low["supplier"], "cheapest_price": low["unit_cost"],
            "dearest_supplier": high["supplier"], "dearest_price": high["unit_cost"],
            "spread_pct": round(spread_pct, 1),
            "spread_per_unit": round(high["unit_cost"] - low["unit_cost"], 4),
        })
    out.sort(key=lambda e: e["spread_pct"], reverse=True)
    return {"available": bool(out), "comparisons": out[:6],
            "reason": None if out else
                      "no ingredient is carried by two suppliers at the same unit with a "
                      "meaningful price gap"}


# ── What actually moved the cost ────────────────────────────────────────────

def _driver_block_failed(restaurant_id, which, exc):
    """A driver source that failed.

    Each of the four blocks in cost_drivers used to end in a bare
    `except Exception: pass`. The ranking is the product here, so a silently
    dropped driver does not merely lose a line — it can change which
    opportunity an owner is told to do first, with nothing anywhere saying a
    source was missing. Reported to the failure digest and named in the
    payload so the caller can say the ranking is incomplete.
    """
    try:
        import ops
        ops.capture(exc, job="food_cost_drivers",
                    context=f"restaurant_id={restaurant_id} source={which}")
    except Exception:
        pass
    return which


def _waste_weeks(restaurant_id, db_path=DB_PATH) -> dict:
    """{item_lower: weeks} — in how many distinct ISO weeks of the last eight
    each item was among the recorded waste offenders, plus "_all": how many
    weeks of waste history exist at all. inventory_history is written per
    render day, so weeks are counted, not rows."""
    out = {"_all": 0}
    conn = get_conn(db_path)
    try:
        rows = _rows_raw(conn, "SELECT week_end, waste_json FROM inventory_history WHERE restaurant_id=? "
                               "AND waste_json IS NOT NULL AND week_end >= date('now', '-56 days')",
                         (restaurant_id,))
    finally:
        conn.close()
    weeks_all, per = set(), {}
    for r in rows:
        try:
            d = datetime.strptime(str(r["week_end"])[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        wk = d.isocalendar()[:2]
        weeks_all.add(wk)
        try:
            top = json.loads(r["waste_json"] or "{}").get("top_items") or []
        except Exception:
            top = []
        for it in top:
            per.setdefault(str(it).strip().lower(), set()).add(wk)
    out["_all"] = len(weeks_all)
    for k, v in per.items():
        out[k] = len(v)
    return out


def _waste_confidence(weeks: int) -> str:
    """A month projected from ONE week of waste (x52/12) is a projection of a
    single observation, not a measured monthly figure (audit #35). Three or
    more weeks of the item in the offender list is a pattern.

    No longer read by cost_drivers (its confidence is measured —
    driver_evidence). Candidate for future cleanup after additional
    verification."""
    if weeks >= 3:
        return "high"
    if weeks == 2:
        return "medium"
    return "low"


def driver_evidence(d: dict) -> dict:
    """A cost driver's Evidence Strength input (confidence_engine.evidence):
    what its own measurement rests on, per kind. Replaces the hand-set
    high/medium bands (audit #35, confidence audit CA1 red flag 8)."""
    kind = d.get("kind")
    if kind == "waste":
        weeks = int(d.get("weeks_of_data") or 1)
        return {"n": weeks, "kind": "waste_weeks",
                "basis": ("one week of waste counts projected to a month" if weeks == 1 else
                          f"an offender in {weeks} of the last 8 weeks of waste counts")}
    if kind == "portion":
        return {"n": 1, "kind": "count", "flags": ("inferred",),
                "basis": "physical counts against recipes — portioning, prep loss or a miscount all fit"}
    if kind == "price":
        weeks = int(d.get("price_weeks") or 1)
        return {"n": weeks, "kind": "price_weeks",
                "basis": f"{weeks} weekly price reading{'s' if weeks != 1 else ''} of this ingredient"}
    if kind == "sourcing":
        return {"n": 1, "kind": "count", "flags": ("list_prices",),
                "basis": "two suppliers' prices on file for the same unit"}
    if kind == "menu":
        lines = int(d.get("recipe_lines") or 0)
        unreviewed = int(d.get("recipe_unreviewed_lines") or 0)
        return {"n": 1, "kind": "count",
                "coverage": ((lines - unreviewed) / float(lines)) if lines else None,
                "basis": ("this dish's plate cost against its own sales"
                          + (f"; {unreviewed} of its {lines} recipe lines are an unreviewed draft" if unreviewed else ""))}
    return {"n": None, "basis": "the ledger"}


def _driver_confidence(restaurant_id, drivers, db_path=DB_PATH):
    """Attach `evidence_input`, `confidence_detail` (K1) and `confidence`
    (its band) to every driver, reading the ledger and sources once."""
    try:
        import rec_trust
        import data_freshness
        import business_intelligence as _bi
        ctx = rec_trust.Context(restaurant_id, db_path=db_path)
        srcs = data_freshness.sources_for(["inventory"])
    except Exception as e:
        print(f"[food_cost_intelligence] driver confidence unavailable: {e}")
        ctx = None
    for d in drivers:
        ev = driver_evidence(d)
        d["evidence_input"] = ev
        if ctx is None:
            import confidence_engine as _ce
            conf = _ce.unknown()
        else:
            conf = rec_trust.assess(restaurant_id, _bi.driver_key(d), evidence=ev, sources=srcs, ctx=ctx)
        d["confidence_detail"] = conf
        d["confidence"] = conf.get("band") or "low"


def _recipe_provenance(restaurant_id, dish_ids, db_path=DB_PATH) -> dict:
    """{menu_item_id: {"lines", "unreviewed", "ingredients"}} — how many of
    each dish's recipe lines came from a Cavnar draft accepted unedited
    (recipe_ingredients.source='draft_accepted'), and the ingredient names."""
    out = {}
    if not dish_ids:
        return out
    marks = ",".join("?" * len(dish_ids))
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(
                f"SELECT ri.menu_item_id AS mid, i.name AS ing, ri.source AS source FROM recipe_ingredients ri "
                f"JOIN ingredients i ON i.id=ri.ingredient_id WHERE ri.menu_item_id IN ({marks}) "
                f"AND i.restaurant_id=?", (*dish_ids, restaurant_id)).fetchall()
        except Exception:
            # A database from before recipe_ingredients.source existed.
            rows = conn.execute(
                f"SELECT ri.menu_item_id AS mid, i.name AS ing, NULL AS source FROM recipe_ingredients ri "
                f"JOIN ingredients i ON i.id=ri.ingredient_id WHERE ri.menu_item_id IN ({marks}) "
                f"AND i.restaurant_id=?", (*dish_ids, restaurant_id)).fetchall()
    finally:
        conn.close()
    for r in rows:
        d = out.setdefault(r["mid"], {"lines": 0, "unreviewed": 0, "ingredients": []})
        d["lines"] += 1
        if r["source"] == "draft_accepted":
            d["unreviewed"] += 1
        d["ingredients"].append(r["ing"])
    return out


def _food_cost_target(restaurant_id) -> float:
    """restaurants.food_cost_target — the same target Home and cogs use.
    The menu driver used a fixed 35% while Home judged against this."""
    try:
        from models import get_restaurant
        r = get_restaurant(restaurant_id)
        t = _f(getattr(r, "food_cost_target", None), 0.0) if r else 0.0
        return t if 5.0 <= t <= 80.0 else 30.0
    except Exception:
        return 30.0


def deduplicated_total(drivers: list) -> dict:
    """The monthly dollars across drivers with no ingredient counted twice
    (audit #36).

    One ingredient can surface as waste, a price rise, a portion gap and
    inside a dish's plate cost at once, and total_monthly summed all four —
    the same dollars counted up to four times. Drivers that share an
    ingredient (a menu driver shares every ingredient in its recipe) are
    grouped, and each group contributes only its LARGEST driver.
    Returns {"total", "groups", "overlapping"}."""
    parent = list(range(len(drivers)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    owner = {}
    for i, d in enumerate(drivers):
        names = set(str(n).strip().lower() for n in (d.get("ingredients") or [d.get("item") or ""]) if n)
        for n in names:
            if n in owner:
                parent[find(i)] = find(owner[n])
            else:
                owner[n] = i
    groups = {}
    for i, d in enumerate(drivers):
        groups.setdefault(find(i), []).append(d)
    total = round(sum(max(x["dollars_monthly"] for x in g) for g in groups.values()), 2)
    return {"total": total, "groups": len(groups),
            "overlapping": sum(1 for g in groups.values() if len(g) > 1)}


def cost_drivers(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Every driver of food cost movement, each with the dollars it carries,
    ranked here rather than by the model.

    The prompt used to say "ranked by dollar impact (highest first)" and hand
    the model an unordered concatenation of three loops, with nothing checking
    the order that came back. Ranking is arithmetic; it belongs in Python, and
    the model's job is to explain the ranking, not to produce it.

    Every driver carries:
      dollars     — monthly, so drivers of different periods are comparable
      confidence  — how much the measurement behind it can bear
      difficulty  — how hard the fix is, which is why 'ease' is in the
                    priority model and could not be expressed before
      evidence    — the specific items or figures it rests on
      if_ignored  — what continues to happen
    """
    from inventory import analysis_for
    import inventory_ledger as il

    drivers = []
    degraded = []
    items, is_live, analysis = analysis_for(restaurant_id)
    if not is_live:
        # Same keys as the full return. An early return with a narrower shape
        # is how `fix_first` ended up meaning two different things depending
        # on which branch produced it; every caller reads one contract.
        return {"available": False, "reason": "sample data — no drivers to rank",
                "drivers": [], "total_monthly": 0.0, "total_monthly_deduplicated": 0.0,
                "min_driver_dollars": MIN_DRIVER_DOLLARS,
                "degraded_sources": [], "complete": True, "basis": None}

    # 1. Recoverable waste, per item, above its own category tolerance band.
    #    Last week's recoverable figure x52/12 is ONE week projected to a
    #    month; it was labelled "high" confidence regardless (audit #35). The
    #    confidence now comes from how many weeks the item has been a waste
    #    offender, and one week says it is one week.
    try:
        _ww = _waste_weeks(restaurant_id, db_path=db_path)
    except Exception:
        _ww = {"_all": 0}
    for x in (analysis.get("waste_items") or [])[:6]:
        rec = _f(x.get("recoverable_cost"))
        if rec <= 0:
            continue
        monthly = round(rec * 52.0 / 12.0, 2)
        if monthly < MIN_DRIVER_DOLLARS:
            continue
        weeks = max(1, int(_ww.get(str(x["item"]).strip().lower(), 0)))
        drivers.append({
            "kind": "waste", "label": f"{x['item']} waste above tolerance",
            "dollars_monthly": monthly, "difficulty": "low",
            "weeks_of_data": weeks,
            "evidence": (f"{x['item']} wasted {x.get('waste_pct')}% of what was ordered "
                         f"against a {x.get('waste_tolerance_pct')}% tolerance band, "
                         f"${_f(x.get('waste_cost')):,.2f} last week"
                         + (" — one week of data projected to a month" if weeks == 1 else
                            f" — an offender in {weeks} of the last 8 weeks")),
            "if_ignored": "the same share keeps going in the bin every week",
            "item": x["item"],
        })

    # 2. Portion / prep variance — the gap between what recipes said should
    #    have been used and what was physically counted. This is the driver
    #    the module measured per ingredient and then aggregated away.
    try:
        var = il.inferred_variance(restaurant_id)
        for v in var.get("material", [])[:4]:
            if v["monthly_cost"] < MIN_DRIVER_DOLLARS:
                continue
            dish = v["dishes"][0]["dish"] if v.get("dishes") else None
            drivers.append({
                "kind": "portion", "label": f"{v['ingredient']} usage over recipe",
                "dollars_monthly": v["monthly_cost"],
                # An inferred gap is a measurement of a discrepancy, not of a
                # cause. It could be portioning, prep loss, shrink or a bad
                # count, and nothing here can separate them — so its evidence
                # is flagged `inferred` and never reads high (driver_evidence).
                "difficulty": "medium",
                "evidence": (f"physical counts came in {v['variance_pct']}% under what recipes "
                             f"predicted for {v['ingredient']} ({v['gap_qty']} {v['unit']} "
                             f"against {v['theoretical_qty']} theoretical)"
                             + (f"; {dish} accounts for "
                                f"{int((v['dishes'][0].get('share') or 0) * 100)}% of its use"
                                if dish else "")),
                "if_ignored": "every plate keeps costing more than the recipe says it does",
                "item": v["ingredient"], "dish": dish,
            })
    except Exception as _e:
        degraded.append(_driver_block_failed(restaurant_id, "portion", _e))

    # 3. Ingredient price movement on the items that carry real spend.
    try:
        from inventory import compute_item_trends, build_price_watch
        # Filter to rises on real spend, price each one, THEN keep the top
        # four by dollars (M-12). The watch list is sorted by the size of the
        # % move, up or down, so cutting it to four first let parsley +80%
        # and lemons -50% crowd out a $900-a-month beef rise.
        _price_drivers = []
        for w in build_price_watch(compute_item_trends(restaurant_id, items)):
            if not w.get("is_big_8") or (w.get("change_pct") or 0) <= 0:
                continue
            # Monthly exposure = the price increase per unit, over the usage
            # this restaurant actually recorded for the item. Never a guess
            # at volume.
            match = next((i for i in items if i["item"] == w["item"]), None)
            usage = _f(match.get("avg_daily_usage")) if match else 0.0
            delta = _f(w.get("new_price")) - _f(w.get("old_price"))
            monthly = round(delta * usage * 30, 2)
            if monthly < MIN_DRIVER_DOLLARS:
                continue
            _price_drivers.append({
                "kind": "price", "label": f"{w['item']} price up {abs(w['change_pct']):.0f}%",
                "dollars_monthly": monthly,
                # Weekly price readings behind the rise: its evidence
                # (driver_evidence); a one-week move is one reading.
                "price_weeks": int(w.get("weeks") or 1),
                "difficulty": "medium",
                "evidence": (f"{w['item']} moved ${_f(w.get('old_price')):.2f} to "
                             f"${_f(w.get('new_price')):.2f}"
                             + (f" over {w['weeks']} weeks" if w.get("weeks") else " this week")
                             + f", against {usage:g} a day of recorded usage"),
                "if_ignored": "the higher unit price flows into every plate using it",
                "item": w["item"],
            })
        _price_drivers.sort(key=lambda d: -d["dollars_monthly"])
        drivers.extend(_price_drivers[:4])
    except Exception as _e:
        degraded.append(_driver_block_failed(restaurant_id, "price", _e))

    # 4. Supplier spread — the same ingredient cheaper on a supplier already
    #    on file.
    try:
        comp = supplier_comparison(restaurant_id, db_path=db_path)
        for c in comp.get("comparisons", [])[:2]:
            match = next((i for i in items if i["item"].lower() == c["ingredient"].lower()), None)
            usage = _f(match.get("avg_daily_usage")) if match else 0.0
            monthly = round(c["spread_per_unit"] * usage * 30, 2)
            if monthly < MIN_DRIVER_DOLLARS:
                continue
            drivers.append({
                "kind": "sourcing", "label": f"{c['ingredient']} cheaper from {c['cheapest_supplier']}",
                "dollars_monthly": monthly, "difficulty": "medium",
                "evidence": (f"{c['cheapest_supplier']} lists it at ${c['cheapest_price']:.2f} vs "
                             f"${c['dearest_price']:.2f} from {c['dearest_supplier']} "
                             f"({c['spread_pct']}% apart, same unit)"),
                "if_ignored": "the spread is paid on every delivery",
                "item": c["ingredient"],
            })
    except Exception as _e:
        degraded.append(_driver_block_failed(restaurant_id, "sourcing", _e))

    # 5. Menu items whose plate cost is over the restaurant's OWN target,
    #    weighted by what they sell. It used a fixed 35% while Home judged
    #    the same restaurant against restaurants.food_cost_target, and it was
    #    "high" confidence even when the plate cost rested on a recipe Cavnar
    #    drafted and the owner accepted without changing a line (audit #35).
    try:
        mp = il.menu_profitability(restaurant_id)
        target_pct = _food_cost_target(restaurant_id)
        # Every dish that sells and runs over target, priced by the dollars
        # its gap costs at its own volume, THEN the top four (M-12). The
        # priced list is sorted by food-cost %, dishes with nothing sold
        # included, so cutting it to four first let four rarely sold dishes
        # at 60% crowd out a best-seller at 38% worth ~$1,000 a month.
        def _gap_monthly(e):
            # Dollars to bring this dish to the target at its current
            # volume. Never a suggestion to raise the price — just the size
            # of the gap.
            target_cost = _f(e.get("sell_price")) * target_pct / 100.0
            return round(max(0.0, _f(e["plate_cost"]) - target_cost)
                         * _f(e["units_sold"]) * (30.0 / il._POPULARITY_WINDOW_DAYS), 2)
        cands = [e for e in (mp.get("priced") or [])
                 if not e.get("unit_warning") and _f(e.get("units_sold")) > 0
                 and _f(e.get("food_cost_pct")) > target_pct
                 and _gap_monthly(e) >= MIN_DRIVER_DOLLARS]
        cands.sort(key=lambda e: -_gap_monthly(e))
        cands = cands[:4]
        prov = _recipe_provenance(restaurant_id, [e["id"] for e in cands], db_path=db_path)
        for e in cands:
            fc = _f(e.get("food_cost_pct"))
            monthly = _gap_monthly(e)
            p = prov.get(e["id"]) or {"lines": 0, "unreviewed": 0, "ingredients": []}
            unreviewed = p["unreviewed"]
            drivers.append({
                "kind": "menu", "label": f"{e['name']} runs at {fc:g}% food cost",
                "dollars_monthly": monthly, "difficulty": "high",
                "target_pct": target_pct,
                "recipe_unreviewed_lines": unreviewed,
                "recipe_lines": p["lines"],
                "evidence": (f"{e['name']} costs ${e['plate_cost']:.2f} on a "
                             f"${_f(e['sell_price']):.2f} price against your {target_pct:g}% target, "
                             f"{e['units_sold']:g} sold in {il._POPULARITY_WINDOW_DAYS} days"
                             + (f"; {unreviewed} of its {p['lines']} recipe lines are a Cavnar draft "
                                f"accepted unedited, so the plate cost is unconfirmed" if unreviewed else "")),
                "if_ignored": "the dish keeps selling at a thin margin",
                "item": e["name"],
                # The ingredients this plate cost is made of, so the
                # de-duplicated total never counts one of them twice.
                "ingredients": p["ingredients"] or [e["name"]],
            })
    except Exception as _e:
        degraded.append(_driver_block_failed(restaurant_id, "menu", _e))

    # Every driver's confidence is measured, not hand-set (confidence audit,
    # E2): its evidence (weeks as an offender, price readings, reviewed
    # recipe lines, the inferred-gap flag) through rec_trust — the K1 object
    # in `confidence_detail`, its band in `confidence` for older clients,
    # which decode that field as a string.
    _driver_confidence(restaurant_id, drivers, db_path=db_path)

    # The priority model, in order: financial impact first, then confidence,
    # then ease. Difficulty breaks a tie between two drivers worth similar
    # money — it never promotes a small easy win over a large hard one,
    # because that is the ranking an owner would not forgive.
    _CONF = {"high": 0, "medium": 1, "low": 2}
    _DIFF = {"low": 0, "medium": 1, "high": 2}
    drivers.sort(key=lambda d: (-d["dollars_monthly"], _CONF.get(d["confidence"], 3),
                                _DIFF.get(d["difficulty"], 3)))
    dedup = deduplicated_total(drivers)
    return {
        "available": bool(drivers),
        "drivers": drivers,
        # The plain sum, kept for existing readers. It counts an ingredient
        # once per driver it appears in; use `total_monthly_deduplicated`
        # wherever one "money at stake" figure is shown (audit #36).
        "total_monthly": round(sum(d["dollars_monthly"] for d in drivers), 2),
        "total_monthly_deduplicated": dedup["total"],
        "total_basis": ("the largest driver per ingredient — an ingredient that shows up as waste, a "
                        "price rise and inside a dish's plate cost is counted once"
                        if dedup["overlapping"] else "no ingredient appears in more than one driver"),
        "min_driver_dollars": MIN_DRIVER_DOLLARS,
        # Sources that failed. A ranking missing a source is not a ranking,
        # and the caller has to be able to say so rather than presenting a
        # partial list as complete.
        "degraded_sources": degraded,
        "complete": not degraded,
        "basis": ("Each driver is priced from this restaurant's own recorded usage and "
                  "prices, expressed monthly so drivers measured over different periods "
                  "are comparable. Ranked by dollars, then confidence, then ease."),
        "reason": None if drivers else
                  f"nothing clears the ${MIN_DRIVER_DOLLARS:g}/month floor",
    }


# ── The number an owner actually asks for ───────────────────────────────────

def profitability_projection(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Month-to-date prime cost, projected to month end.

    "Food cost rose 2.1%" is a ratio. "Profitability is tracking about $3,800
    below last month" is a decision. This computes the second from figures the
    platform already measures — COGS from the ledger, labor from the shift
    analysis, sales from the POS — and returns nothing at all when any of the
    three is missing, because a projection built on a substituted zero looks
    exactly like a real one.

    Prime cost (COGS + labor) is the standard operator measure and the two
    numbers Cavnar actually holds. It is explicitly NOT net profit: rent,
    utilities and everything else below the line are not in this system, and
    the payload says so rather than letting the figure be read as the bottom
    line.
    """
    import cogs as _cogs
    today = date.today()
    month_start = today.replace(day=1)
    days_elapsed = (today - month_start).days + 1
    if days_elapsed < MIN_DAYS_FOR_PROJECTION:
        return {"available": False,
                "reason": f"only {days_elapsed} days into the month — a run rate this early "
                          f"is noise (needs {MIN_DAYS_FOR_PROJECTION})"}

    missing = []
    net_sales, sales_why = _cogs.net_sales_in_window(restaurant_id, month_start, today)
    if net_sales is None:
        missing.append({"component": "net sales", "why": sales_why or "unavailable"})

    fc = _cogs.build_food_cost_pct(restaurant_id, days=days_elapsed, db_path=db_path, today=today)
    if not fc.get("ok"):
        for m in (fc.get("missing") or []):
            missing.append(m)

    labor_cost, labor_pct, labor_why, labor_period = None, None, None, None
    try:
        from labor import analyse_shifts_for_restaurant
        la = analyse_shifts_for_restaurant(restaurant_id)
        if la and la.get("is_live") and _f(la.get("total_sales")) > 0:
            labor_pct = _f(la.get("overall_labor_pct"))
            rng = la.get("date_range") or {}
            labor_period = f"{rng.get('start')} to {rng.get('end')}" if rng.get("end") else None
        else:
            labor_why = "no shift data synced — labor cannot be measured"
    except Exception as e:
        labor_why = f"labor analysis unavailable: {e}"
    if labor_pct is None:
        missing.append({"component": "labor", "why": labor_why or "unavailable"})

    # The labor share comes from the shift analysis's OWN period, which is not
    # this calendar month — applying it to month-to-date sales assumes the
    # scheduling shape held. That assumption is the weakest link in the whole
    # projection and it is stated rather than buried, because an owner reading
    # "prime cost 39%" is entitled to know which half of it was measured over
    # the days being projected and which was carried across from elsewhere.
    basis = ("Prime cost = COGS + labor, month to date, projected to month end at the "
             "current daily run rate. This is prime cost, not net profit — rent, "
             "utilities and overheads are not in this system."
             + (f" The labor share is this restaurant's own {labor_period} figure applied to "
                f"month-to-date sales, not a labor measurement of these specific days."
                if labor_period else
                " The labor share comes from the shift analysis's own period, not these "
                "specific days."))
    if missing:
        return {"available": False, "missing": missing, "basis": basis,
                "reason": "; ".join(m["component"] for m in missing) + " missing"}

    days_in_month = ((month_start + timedelta(days=32)).replace(day=1) - month_start).days
    cogs_mtd = _f(fc["cogs"])
    labor_cost = net_sales * labor_pct / 100.0
    prime_mtd = cogs_mtd + labor_cost
    prime_pct = round(prime_mtd / net_sales * 100, 1)
    scale = days_in_month / days_elapsed
    projected_sales = round(net_sales * scale)
    projected_prime = round(prime_mtd * scale)

    # Against last month, measured the same way, when last month can be
    # measured at all.
    prev_end = month_start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    prev_pct, prev_why = None, None
    prev_sales, prev_sales_why = _cogs.net_sales_in_window(restaurant_id, prev_start, prev_end)
    prev_fc = _cogs.build_food_cost_pct(
        restaurant_id, days=(prev_end - prev_start).days + 1, db_path=db_path, today=prev_end)
    if prev_sales and prev_fc.get("ok") and labor_pct is not None:
        # The same labor share is used on both sides on purpose: there is only
        # one labor measurement available, so it cancels out of the comparison
        # and the month-over-month movement this reports is entirely the FOOD
        # cost component. Saying that is the difference between a comparison
        # and a number that looks like it covers both halves of prime cost.
        prev_prime = _f(prev_fc["cogs"]) + prev_sales * labor_pct / 100.0
        prev_pct = round(prev_prime / prev_sales * 100, 1)
    else:
        prev_why = prev_sales_why or "last month cannot be measured the same way"

    delta_dollars = None
    if prev_pct is not None:
        # The dollar consequence of the prime-cost RATIO moving, at this
        # month's projected sales. Stated this way rather than as a raw
        # difference of two months' dollars, which would mostly measure the
        # difference in how busy the two months were.
        delta_dollars = round((prime_pct - prev_pct) / 100.0 * projected_sales)

    return {
        "available": True,
        "claim_kind": "forecast",
        "days_elapsed": days_elapsed, "days_in_month": days_in_month,
        "net_sales_mtd": round(net_sales, 2),
        "cogs_mtd": round(cogs_mtd, 2),
        "labor_cost_mtd": round(labor_cost, 2),
        "food_cost_pct": fc.get("pct"), "food_cost_target": fc.get("target"),
        "labor_pct": labor_pct,
        "prime_cost_pct": prime_pct,
        "projected_sales": projected_sales,
        "projected_prime_cost": projected_prime,
        "forecast_kind": "profitability_month",
        "prev_month_prime_pct": prev_pct,
        "prev_month_why": prev_why,
        "prime_pct_delta": round(prime_pct - prev_pct, 1) if prev_pct is not None else None,
        "dollars_vs_last_month": delta_dollars,
        "direction": (None if delta_dollars is None else
                      "worse" if delta_dollars > 0 else "better"),
        "comparison_basis": ("Both months use the same labor share, so this movement is the "
                             "food cost component only." if prev_pct is not None else None),
        "labor_period": labor_period,
        "basis": basis,
    }


# ── What the rest of the platform saw over the same window ──────────────────

def operational_context(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Labor, reviews and marketing for the same period.

    Food Cost read none of it. Every function below is in the same process —
    ask_cavnar.build_context already composes all of them — so "waste is up"
    and "you added 40 covers a night off a promotion" sat one tab apart and
    were never joined. Sample data is refused rather than reported, following
    ask_cavnar's own precedent.
    """
    ctx = {"labor": None, "reviews": None, "marketing": None, "notes": []}
    try:
        from labor import analyse_shifts_for_restaurant
        a = analyse_shifts_for_restaurant(restaurant_id)
        if a and a.get("is_live"):
            rng = a.get("date_range") or {}
            ctx["labor"] = {
                "labor_pct": round(_f(a.get("overall_labor_pct")), 1),
                "target_pct": round(_f(a.get("labor_target"), 30.0), 1),
                "total_sales": round(_f(a.get("total_sales"))),
                "period_days": a.get("period_days"),
                "overstaffed_days": len(a.get("overstaffed_days") or []),
                "understaffed_days": len(a.get("understaffed_days") or []),
                "covers_to": rng.get("end"),
            }
        elif a:
            ctx["notes"].append("Labor: sample data only — no shifts uploaded, so labor "
                                "cannot be used as evidence.")
    except Exception:
        pass

    # Guests complaining about portion size or value is the one review signal
    # that bears directly on a food cost decision — it is the constraint on
    # "just use less".
    try:
        import review_intelligence as _ri
        clusters = _ri.complaint_clusters(restaurant_id, db_path=db_path)
        relevant = [c for c in clusters if c["category"] in ("value", "food_quality")]
        if relevant:
            c = relevant[0]
            ctx["reviews"] = {
                "category": c["category"], "mentions": c["mentions"],
                "window_days": c["window_days"],
                "dish": (c["dish"]["value"] if c.get("dish") else None),
            }
    except Exception:
        pass

    try:
        conn = get_conn(db_path)
        row = _one_row(conn, """
            SELECT COUNT(*) AS n, COALESCE(SUM(COALESCE(reach,0)+COALESCE(impressions,0)),0) AS reach
              FROM marketing_content_log
             WHERE restaurant_id=? AND post_id IS NOT NULL
               AND created_at >= datetime('now','-30 days')
        """, (restaurant_id,))
        conn.close()
        if row and (row["n"] or 0) > 0:
            ctx["marketing"] = {"posts_30d": int(row["n"]), "reach_30d": int(row["reach"] or 0)}
    except Exception:
        pass
    return ctx


def _operational_block(ctx) -> str:
    """The cross-module evidence as prose, or an explicit statement that there
    is none. An empty block reads to a model as "nothing notable happened",
    which is a different claim from "we have no data" — and it is the second
    that has to pull the confidence down."""
    lines = []
    lab = ctx.get("labor")
    if lab:
        lines.append(f"- Labor: {lab['labor_pct']}% of sales against a {lab['target_pct']}% "
                     f"target, {lab['understaffed_days']} understaffed and "
                     f"{lab['overstaffed_days']} overstaffed days over {lab['period_days']} days"
                     + (f", data through {lab['covers_to']}" if lab.get("covers_to") else ""))
    rev = ctx.get("reviews")
    if rev:
        lines.append(f"- Reviews: {rev['mentions']} negative reviews mention "
                     f"{rev['category'].replace('_',' ')} in the last {rev['window_days']} days"
                     + (f", concentrated on {rev['dish']}" if rev.get("dish") else "")
                     + " — relevant because it bounds how far portions can be cut")
    mk = ctx.get("marketing")
    if mk:
        lines.append(f"- Marketing: {mk['posts_30d']} published posts in 30 days reaching "
                     f"{mk['reach_30d']:,} — a demand change would show up in usage")
    for note in ctx.get("notes") or []:
        lines.append(f"- {note}")
    if not lines:
        return ("(No data from any other module. You have NO operational evidence beyond the "
                "food cost figures themselves — reason from those alone and keep confidence "
                "at most \"medium\".)")
    return "\n".join(lines)


# ── Forecasts that get scored ───────────────────────────────────────────────

def record_forecast(restaurant_id: int, kind: str, horizon_end: str, predicted: float,
                    basis: str = None, db_path: str = DB_PATH) -> None:
    """Store a forecast so it can be checked later.

    The weekly waste forecast was emitted to the owner and never compared to
    what happened. A forecast nobody scores costs nothing to get wrong, which
    is the opposite of what a projection is for.
    """
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, basis) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, kind, horizon_end) DO UPDATE SET "
            "predicted=excluded.predicted, basis=excluded.basis, created_at=datetime('now')",
            (restaurant_id, kind, horizon_end, round(_f(predicted), 2), basis))
        conn.commit()
    finally:
        conn.close()


PROFITABILITY_FORECAST_DAY = 15   # the day of the month the projection is frozen for scoring


def record_profitability_forecast(restaurant_id: int, db_path: str = DB_PATH, today=None) -> dict:
    """Freeze this month's prime-cost projection once, mid-month, so it can
    be scored against the month that closes (moat audit #16).

    Deliberately NOT on the page render: a projection re-recorded on every
    open converges on the actual as the month ends and scores itself
    perfect. One fixed prediction, taken by the nightly job on or after the
    15th, is a forecast; the last render before month end is a reading."""
    from datetime import date as _date
    import calendar as _cal
    today = today or _date.today()
    if today.day < PROFITABILITY_FORECAST_DAY:
        return {"recorded": False, "reason": "before the 15th"}
    horizon = today.replace(day=_cal.monthrange(today.year, today.month)[1]).isoformat()
    conn = get_conn(db_path)
    try:
        have = conn.execute("SELECT 1 FROM forecast_log WHERE restaurant_id=? AND kind='profitability_month' "
                            "AND horizon_end=?", (restaurant_id, horizon)).fetchone()
    finally:
        conn.close()
    if have:
        return {"recorded": False, "reason": "already frozen this month"}
    proj = profitability_projection(restaurant_id, db_path=db_path)
    if not proj.get("available") or proj.get("prime_cost_pct") is None:
        return {"recorded": False, "reason": proj.get("reason") or "no projection"}
    record_forecast(restaurant_id, "profitability_month", horizon, proj["prime_cost_pct"],
                    basis=f"{proj.get('days_elapsed')} days of the month, prime cost run rate", db_path=db_path)
    return {"recorded": True, "horizon_end": horizon, "predicted": proj["prime_cost_pct"]}


def score_forecasts(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Fill in the actual for every forecast whose period has closed."""
    from waste_trend import load_waste_history
    conn = get_conn(db_path)
    try:
        due = _rows_raw(conn,
            "SELECT id, kind, horizon_end, predicted FROM forecast_log "
            "WHERE restaurant_id=? AND actual IS NULL AND horizon_end <= date('now')",
            (restaurant_id,))
    finally:
        conn.close()
    if not due:
        return {"scored": 0}

    weeks, _t = load_waste_history(restaurant_id, None, db_path=db_path)
    by_week = {}
    for w in weeks:
        try:
            iso = date.fromisoformat(w["week_end"]).isocalendar()
            by_week[(iso[0], iso[1])] = _f(w.get("waste"))
        except Exception:
            continue

    scored = 0
    conn = get_conn(db_path)
    try:
        for f in due:
            actual = None
            if f["kind"] == "waste_week":
                try:
                    iso = date.fromisoformat(f["horizon_end"]).isocalendar()
                    actual = by_week.get((iso[0], iso[1]))
                except Exception:
                    actual = None
            elif f["kind"] == "profitability_month":
                # Prime cost = food cost % + labor % over the closed month,
                # from the same measurements the monthly review reads.
                try:
                    import metrics as _m
                    from monthly_review import month_bounds as _mb
                    ms, me = _mb(date.fromisoformat(f["horizon_end"]))
                    fc, _ = _m.measure(restaurant_id, "food_cost_pct", ms.isoformat(), me.isoformat(), db_path)
                    lb, _ = _m.measure(restaurant_id, "labor_pct", ms.isoformat(), me.isoformat(), db_path)
                    actual = (float(fc) + float(lb)) if fc is not None and lb is not None else None
                except Exception:
                    actual = None
            if actual is None:
                continue
            pred = _f(f["predicted"])
            err = round(abs(actual - pred) / pred * 100, 1) if pred > 0 else None
            # Signed as well: (predicted − actual)/actual, so a run of
            # forecasts that keep landing high can be read as a bias and
            # corrected, not just reported as "often wide".
            signed = round((pred - actual) / actual * 100, 1) if actual > 0 else None
            conn.execute(
                "UPDATE forecast_log SET actual=?, error_pct=?, signed_error_pct=?, scored_at=datetime('now') WHERE id=?",
                (round(actual, 2), err, signed, f["id"]))
            scored += 1
        conn.commit()
    finally:
        conn.close()
    return {"scored": scored}


def forecast_accuracy(restaurant_id: int, kind: str = "waste_week",
                      db_path: str = DB_PATH) -> dict:
    """How well this module's forecasts have actually held up.

    Shown alongside a new forecast so an owner can weigh it. A module that
    has been 40% out four times running should say so next to the fifth.
    """
    conn = get_conn(db_path)
    rows = _rows_raw(conn,
        "SELECT predicted, actual, error_pct FROM forecast_log "
        "WHERE restaurant_id=? AND kind=? AND actual IS NOT NULL "
        "ORDER BY horizon_end DESC LIMIT 8", (restaurant_id, kind))
    conn.close()
    errs = [_f(r["error_pct"]) for r in rows if r["error_pct"] is not None]
    if len(errs) < 2:
        return {"available": False, "scored": len(errs),
                "reason": "not enough scored forecasts yet to say how accurate these are"}
    mean_err = sum(errs) / len(errs)
    return {"available": True, "scored": len(errs),
            "mean_error_pct": round(mean_err, 1),
            "reading": ("close" if mean_err <= 15 else
                        "roughly right" if mean_err <= 30 else "often wide")}


CALIBRATION_MIN_SCORED = 3
CALIBRATION_MIN_BIAS_PCT = 10.0


def forecast_calibration(restaurant_id: int, kind: str = "waste_week", db_path: str = DB_PATH) -> dict:
    """The forecast error loop. Reads the signed error of the last eight
    scored forecasts; with three or more and a mean bias past
    CALIBRATION_MIN_BIAS_PCT, returns the factor that would have made them
    land, so the next projection is corrected and SAYS so. Absent — never
    a silent tweak — until the record exists."""
    conn = get_conn(db_path)
    rows = _rows_raw(conn,
        "SELECT signed_error_pct FROM forecast_log WHERE restaurant_id=? AND kind=? "
        "AND signed_error_pct IS NOT NULL ORDER BY horizon_end DESC LIMIT 8", (restaurant_id, kind))
    conn.close()
    errs = [_f(r["signed_error_pct"]) for r in rows]
    if len(errs) < CALIBRATION_MIN_SCORED:
        return {"available": False, "scored": len(errs)}
    bias = sum(errs) / len(errs)
    if abs(bias) < CALIBRATION_MIN_BIAS_PCT:
        return {"available": True, "scored": len(errs), "bias_pct": round(bias, 1), "factor": 1.0,
                "reading": "no consistent lean"}
    factor = round(1.0 / (1.0 + bias / 100.0), 4)
    return {"available": True, "scored": len(errs), "bias_pct": round(bias, 1), "factor": factor,
            "reading": f"ran {abs(bias):.0f}% {'high' if bias > 0 else 'low'} across the last {len(errs)} scored weeks"}


def calibrated(restaurant_id: int, kind: str, predicted, db_path: str = DB_PATH):
    """(corrected_value, calibration) — the value unchanged when there is no
    record to correct it with."""
    cal = forecast_calibration(restaurant_id, kind, db_path=db_path)
    if not cal.get("available") or cal.get("factor", 1.0) == 1.0 or predicted is None:
        return predicted, cal
    return round(_f(predicted) * cal["factor"], 2), cal


# ── The root-cause pass ─────────────────────────────────────────────────────

CAUSE_VOCABULARY = """\
- Vendor or commodity price movement on an ingredient that carries real spend
- Over-portioning or inconsistent plating against the written recipe
- Prep loss, trim yield or spoilage between delivery and service
- Ordering above what the covers justify (par set too high, or not moved down)
- Ordering frequency against shelf life (too much held too long on perishables)
- A recount that disagrees with the ledger — a counting or receiving process gap
- Menu mix shifting toward dishes with a thinner margin
- A menu price that has not moved while its plate cost has
- Demand change (a promotion, an event, a season) outrunning what was ordered for
- Staffing or scheduling affecting prep quality and consistency
- Recipe coverage gaps making usage look lower than it is"""

DIAGNOSE_PROMPT = """You are an experienced restaurant CFO reviewing one restaurant's food cost. You are not writing a summary: the owner can already see their waste total and their inventory value. Your value is the step after the number — what is driving it, what it is costing, and what to do first.

Return ONLY valid JSON — no markdown, no commentary.

{untrusted_note}

RESTAURANT: {restaurant_name}
TODAY: {today}
WINDOW: the last {window_days} days

WHERE THE MONEY IS (measured — each figure is computed from this restaurant's own recorded usage and prices, already ranked by dollars, then confidence, then ease):
{drivers_block}

FOOD COST POSITION:
{position_block}

PROFITABILITY:
{profit_block}

HOW FAR THESE FIGURES CAN BE TRUSTED:
{trust_block}

WHERE AND WHEN THE WASTE LANDS:
{pattern_block}

WHAT THE OTHER MODULES RECORDED OVER THE SAME PERIOD:
{operational_block}

CAUSE VOCABULARY — pick from these kinds of cause:
{cause_vocabulary}

EVIDENCE RULES — these bound what you may claim:
- State no figure — a dollar amount, a percentage, a quantity — that does not appear above. Not one, not even rounded.
- Your `cause` must name at least one driver from "WHERE THE MONEY IS" by its label. You may not introduce a driver that is not listed.
- Name an ingredient, a dish, a supplier or a weekday ONLY if it appears above.
- The drivers are ALREADY RANKED. Do not re-rank them. Your job is to explain why the top ones are the top ones and what connects them.
- You may connect food cost to a figure under "WHAT THE OTHER MODULES RECORDED" only by naming that figure in `operational_evidence`. If that section says there is no data, you have NO operational evidence — say so, and let it pull your confidence down.
- Two things moving together in one window is not proof one caused the other. Say they moved together.
- Read "HOW FAR THESE FIGURES CAN BE TRUSTED" before you commit. Low recipe coverage or a high inferred-waste share means the underlying usage figures are soft, and your confidence must reflect that regardless of how large the dollar figures look.
- `confidence` is "high" only when the drivers are specific AND the trust block is clean AND another module points the same way. It is "low" when you are reasoning mostly from totals.
- If the evidence does not identify a driving cause, say that in `cause` and set confidence "low". An owner acting on a confident guess about their food cost loses real money.

Return this exact shape:
{{
  "headline": "one sentence an owner reads first — what is happening to their food cost and what it is worth, max 25 words",
  "cause": "the most likely driver or combination of drivers, naming them, 1-2 sentences",
  "alternative_cause": "the next most likely explanation the same evidence also fits, 1 sentence",
  "what_would_confirm": "one concrete thing the owner could check or count this week that would tell the two apart, 1 sentence",
  "operational_evidence": [{{"module": "labor|reviews|marketing", "metric": "what it is", "value": "the figure exactly as given above"}}],
  "confidence": "high" | "medium" | "low",
  "recommended_action": "the single highest-value thing to do first, startable this week with the staff and suppliers they already have, 1 sentence",
  "expected_outcome": "what should change if the cause is right, and roughly when, 1 sentence"
}}"""


def _drivers_block(drv) -> str:
    if not drv.get("drivers"):
        return f"(Nothing clears the ${MIN_DRIVER_DOLLARS:g}/month floor. There is no driver to explain.)"
    out = []
    if drv.get("degraded_sources"):
        out.append(f"NOTE: {', '.join(drv['degraded_sources'])} could not be computed this run, "
                   f"so this ranking is incomplete. Say so rather than presenting it as the "
                   f"whole picture.")
    for i, d in enumerate(drv["drivers"][:6], 1):
        out.append(f"{i}. {d['label']} — ${d['dollars_monthly']:,.0f}/month "
                   f"({d['confidence']} confidence, {d['difficulty']} to fix)\n"
                   f"   evidence: {d['evidence']}\n"
                   f"   if ignored: {d['if_ignored']}")
    # The total, stated. Without it the model adds the drivers up itself to
    # open with a combined figure — which is the natural thing for a CFO to
    # write and is exactly what verify_figures then rejects, costing the
    # passage its confidence for arithmetic that was correct. Giving it the
    # sum removes the incentive to compute one.
    # The de-duplicated total (M-10): the plain sum counts an ingredient
    # once per driver it appears in — up to four times — and the model was
    # told to quote it, while the web card showed the de-duplicated figure.
    _combined = drv.get("total_monthly_deduplicated", drv["total_monthly"])
    out.append(f"\nCombined: ${_combined:,.0f}/month across "
               f"{len(drv['drivers'])} drivers, with no ingredient counted twice. Quote this "
               f"figure if you want a total — never add the drivers up yourself.")
    return "\n".join(out)


def _position_block(fc) -> str:
    if not fc.get("ok"):
        why = "; ".join(f"{m['component']} ({m['why']})" for m in (fc.get("missing") or []))
        return (f"(Food cost % cannot be computed: {why or 'inputs missing'}. "
                f"Do not state or estimate a food cost percentage.)")
    line = f"Food cost is {fc['pct']}% of net sales over {fc['window_days']} days ({fc['label']})."
    if fc.get("target"):
        line += (f" Their own target is {fc['target']}%, so they are "
                 f"{abs(fc['variance_pts'])} points "
                 f"{'over' if fc['variance_pts'] > 0 else 'under'} it.")
    line += (f" COGS ${fc['cogs']:,.0f} on ${fc['net_sales']:,.0f} net sales "
             f"(opening ${fc['opening']:,.0f} + purchases ${fc['purchases']:,.0f} "
             f"- closing ${fc['closing']:,.0f}).")
    return line


def _profit_block(pp) -> str:
    if not pp.get("available"):
        return (f"(Not computable: {pp.get('reason', 'inputs missing')}. "
                f"Do not state or estimate a profitability figure.)")
    # The food cost % here is the MONTH-TO-DATE one and the FOOD COST POSITION
    # block above it is the trailing 28-day one, so the two legitimately
    # differ. Adjacent and unlabelled they read as a contradiction, which is
    # worse than either number alone — name the window on both.
    line = (f"Prime cost is {pp['prime_cost_pct']}% of sales month to date "
            f"(month-to-date food {pp['food_cost_pct']}% + labor {pp['labor_pct']}%), on "
            f"${pp['net_sales_mtd']:,.0f} of sales over {pp['days_elapsed']} days. "
            f"Note this month-to-date food cost figure covers a different window from the "
            f"trailing-28-day one above, so the two will not match. "
            f"At this run rate the month lands near ${pp['projected_sales']:,.0f} of sales "
            f"and ${pp['projected_prime_cost']:,.0f} of prime cost.")
    if pp.get("dollars_vs_last_month") is not None:
        line += (f" Against last month's {pp['prev_month_prime_pct']}%, that is "
                 f"${abs(pp['dollars_vs_last_month']):,.0f} "
                 f"{'worse' if pp['dollars_vs_last_month'] > 0 else 'better'} across the month. "
                 f"This is a projection, not a measurement. {pp.get('comparison_basis') or ''}")
    line += f" {pp.get('basis') or ''}"
    return line.strip()


def _trust_block(cov, wsrc) -> str:
    lines = []
    if cov.get("has_data") and cov.get("coverage_pct") is not None:
        lines.append(f"- Recipe coverage is {cov['coverage_pct']}% of units sold over "
                     f"{cov['window_days']} days. Anything a recipe does not cover depletes "
                     f"nothing, so usage on those ingredients reads lower than it is."
                     + (f" {cov['uncovered_count']} selling items have no recipe."
                        if cov.get("uncovered_count") else ""))
    else:
        lines.append("- Recipe coverage is unknown (no sales mapped), so usage and days-remaining "
                     "figures cannot be relied on.")
    if wsrc.get("has_data") and wsrc.get("inferred_pct") is not None:
        lines.append(f"- {wsrc['inferred_pct']}% of recorded waste (${wsrc['inferred']:,.0f}) is "
                     f"INFERRED from counts coming in under expectation, not counted waste. "
                     f"An inferred gap is a discrepancy, not observed spoilage.")
    else:
        lines.append("- No waste events recorded in the window, so nothing separates counted "
                     "waste from a counting gap.")
    return "\n".join(lines)


def _pattern_block(wd, seasonal) -> str:
    lines = []
    if wd.get("worst_day"):
        w = wd["worst_day"]
        lines.append(f"- {w['weekday']} carries {int(w['share']*100)}% of waste dollars across "
                     f"the last {wd['window_days']} days (${w['cost']:,.0f} over {w['events']} "
                     f"events), against an even spread of {int(wd['even_share']*100)}% a day.")
        if (wd.get("days_with_waste") or 7) <= 3:
            lines.append(f"- Waste lands on only {wd['days_with_waste']} days of the week at all, "
                         f"which is itself the pattern.")
    elif wd.get("has_data") and (wd.get("days_with_waste") or 7) <= 3:
        # Never call this an even spread. It is the opposite of one.
        named = ", ".join(o["weekday"] for o in wd["by_weekday"] if o["cost"] > 0)
        lines.append(f"- Waste lands on only {wd['days_with_waste']} days of the week ({named}), "
                     f"with no single one of them dominant.")
    elif wd.get("has_data"):
        lines.append("- Waste is spread fairly evenly across the week — no single day stands out.")
    if seasonal.get("available"):
        lines.append(f"- Against the same weeks last year, waste is {seasonal['reading']} "
                     f"(${seasonal['this_year_weekly_avg']:,.0f}/week vs "
                     f"${seasonal['last_year_weekly_avg']:,.0f}/week, "
                     f"{seasonal['change_pct']:+g}%).")
    else:
        lines.append(f"- No year-over-year comparison available ({seasonal.get('reason')}), so "
                     f"nothing here separates a seasonal swing from a real change.")
    return "\n".join(lines) if lines else "- Nothing above the evidence floor."


def _validate_diagnosis(raw, driver_labels, prompt, restaurant_id):
    """Reject a diagnosis that names a driver it was not given.

    The same discipline ai_guard applies to figures, applied to drivers. A
    CFO's paragraph is only worth more than a summary because every claim in
    it traces to a measured line; a cause naming a driver nobody computed
    breaks that in the one place it matters most.
    """
    if not isinstance(raw, dict):
        raise ValueError("diagnosis was not a JSON object")

    def _line(key, limit=500):
        return " ".join(str(raw.get(key) or "").split())[:limit] or None

    headline = _line("headline", 240)
    cause = _line("cause", 600)
    if not headline or not cause:
        raise ValueError("diagnosis had no headline or cause")

    # The cause has to name at least one driver it was handed. Matching on the
    # driver's ITEM (the ingredient or dish) rather than the whole label,
    # because the model writes prose, not labels.
    low = (headline + " " + cause).lower()
    cited = [lbl for lbl in driver_labels if lbl and lbl.lower() in low]
    if driver_labels and not cited:
        raise ValueError("diagnosis named no driver it was given")

    conf = str(raw.get("confidence") or "").strip().lower()
    conf = conf if conf in CONFIDENCES else "low"

    op = []
    for e in (raw.get("operational_evidence") or [])[:4]:
        if isinstance(e, dict) and e.get("module") in ("labor", "reviews", "marketing"):
            op.append({"module": e["module"], "metric": str(e.get("metric") or "")[:80],
                       "value": str(e.get("value") or "")[:80]})

    out = {
        "headline": headline, "cause": cause,
        "alternative_cause": _line("alternative_cause"),
        "what_would_confirm": _line("what_would_confirm"),
        "operational_evidence": op, "confidence": conf,
        "recommended_action": _line("recommended_action"),
        "expected_outcome": _line("expected_outcome"),
        "cited_drivers": cited,
    }

    # Every figure it states has to be one it was handed — the check the
    # insight and the weekly digest already run. A fabricated percentage
    # inside a CFO's paragraph is the hardest kind to catch by eye.
    from ai_guard import verify_figures
    joined = " ".join(v for v in (out["headline"], out["cause"], out["alternative_cause"],
                                  out["what_would_confirm"], out["recommended_action"],
                                  out["expected_outcome"]) if v)
    bad = verify_figures(joined, prompt, "food_cost_diagnosis", restaurant_id)
    if bad:
        out["unsupported_figures"] = bad
        out["confidence"] = "low"
    return out


def build_evidence(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Everything the CFO layer knows, assembled once.

    Shared by `diagnose`, the insight prompt and `executive_brief`, so the
    paragraph, the narrative and the morning brief can never be computed from
    three different readings of the same restaurant.
    """
    import cogs as _cogs
    import inventory_ledger as il
    return {
        "drivers": cost_drivers(restaurant_id, db_path=db_path),
        "food_cost": _cogs.build_food_cost_pct(restaurant_id, db_path=db_path),
        "profitability": profitability_projection(restaurant_id, db_path=db_path),
        "coverage": il.recipe_coverage(restaurant_id),
        "waste_sources": il.waste_sources(restaurant_id),
        "weekday": weekday_waste(restaurant_id, db_path=db_path),
        "seasonal": seasonal_baseline(restaurant_id, db_path=db_path),
        "suppliers": supplier_comparison(restaurant_id, db_path=db_path),
        "operational": operational_context(restaurant_id, db_path=db_path),
        "forecast_accuracy": forecast_accuracy(restaurant_id, db_path=db_path),
    }


def diagnose(restaurant_id: int, db_path: str = DB_PATH, force: bool = False) -> dict:
    """Produce and store the root-cause read for one restaurant's food cost.

    One Sonnet call. Skips when a stored diagnosis is still inside
    DIAGNOSIS_TTL_HOURS and the top driver has not moved — a cause re-derived
    hourly is the same cause in different words, which reads as instability
    rather than insight.
    """
    import os
    import anthropic
    from ai_utils import create_with_retry, extract_text, get_client, model_for
    from ai_guard import UNTRUSTED_NOTE
    from models import get_restaurant
    from time_utils import restaurant_now_by_id

    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return {}
    ev = build_evidence(restaurant_id, db_path=db_path)
    drv = ev["drivers"]
    if not drv.get("drivers"):
        return {"ok": False, "reason": drv.get("reason") or "no drivers above the floor"}

    prior = get_diagnosis(restaurant_id, db_path=db_path, include_stale=True)
    if not force and prior and not prior.get("stale"):
        prior_top = (prior.get("drivers") or [{}])[0].get("label")
        if prior_top == drv["drivers"][0]["label"]:
            return prior

    prompt = DIAGNOSE_PROMPT.format(
        untrusted_note=UNTRUSTED_NOTE,
        restaurant_name=restaurant.name,
        today=restaurant_now_by_id(restaurant_id).strftime("%B %d, %Y"),
        window_days=DIAGNOSIS_WINDOW_DAYS,
        drivers_block=_drivers_block(drv),
        position_block=_position_block(ev["food_cost"]),
        profit_block=_profit_block(ev["profitability"]),
        trust_block=_trust_block(ev["coverage"], ev["waste_sources"]),
        pattern_block=_pattern_block(ev["weekday"], ev["seasonal"]),
        operational_block=_operational_block(ev["operational"]),
        cause_vocabulary=CAUSE_VOCABULARY,
    )
    client = get_client()
    msg = create_with_retry(
        client, model=model_for("food_cost_diagnosis"),
        max_tokens=900, messages=[{"role": "user", "content": prompt}],
        restaurant_id=restaurant_id, action="food_cost_diagnosis")
    if getattr(msg, "stop_reason", None) == "max_tokens":
        raise ValueError("food cost diagnosis was truncated")
    # A leading sentence before the JSON failed json.loads (AI-26).
    from ai_utils import parse_json_reply
    labels = [d.get("item") or d["label"] for d in drv["drivers"]]
    result = _validate_diagnosis(parse_json_reply(extract_text(msg), expect=dict),
                                 labels, prompt, restaurant_id)

    # One "at stake" figure, the same one the web card and iOS header show:
    # what the drivers carry, with no ingredient counted twice (M-10). It
    # used to be abs(dollars_vs_last_month) — a prime-cost RATIO move
    # projected to the month — so a month $2,000 BETTER than the last read
    # "$2,000/mo at stake" on Home (M-11); and without it, the plain sum
    # that counts one ingredient up to four times. The month-over-month move
    # is its own line in the profitability read, labelled as such.
    at_stake = drv.get("total_monthly_deduplicated", drv["total_monthly"])
    _save_diagnosis(restaurant_id, drv, result, at_stake, db_path)
    result.update({"drivers": drv["drivers"][:6], "dollars_at_stake": round(at_stake, 2),
                   "window_days": DIAGNOSIS_WINDOW_DAYS, "stale": False, "ok": True})
    return result


def _save_diagnosis(restaurant_id, drv, result, at_stake, db_path):
    conn = get_conn(db_path)
    try:
        conn.execute("""
            INSERT INTO food_cost_diagnoses
                (restaurant_id, window_days, headline, cause, alternative_cause,
                 what_would_confirm, drivers_json, operational_evidence, confidence,
                 recommended_action, expected_outcome, dollars_at_stake, unsupported_figures,
                 generated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(restaurant_id, window_days) DO UPDATE SET
                headline=excluded.headline, cause=excluded.cause,
                alternative_cause=excluded.alternative_cause,
                what_would_confirm=excluded.what_would_confirm,
                drivers_json=excluded.drivers_json,
                operational_evidence=excluded.operational_evidence,
                confidence=excluded.confidence,
                recommended_action=excluded.recommended_action,
                expected_outcome=excluded.expected_outcome,
                dollars_at_stake=excluded.dollars_at_stake,
                unsupported_figures=excluded.unsupported_figures,
                generated_at=excluded.generated_at
        """, (restaurant_id, DIAGNOSIS_WINDOW_DAYS, result["headline"], result["cause"],
              result["alternative_cause"], result["what_would_confirm"],
              json.dumps(drv["drivers"][:6]), json.dumps(result["operational_evidence"]),
              result["confidence"], result["recommended_action"], result["expected_outcome"],
              round(_f(at_stake), 2),
              # Kept with the read so every surface shows the caveat (M-17).
              json.dumps(result.get("unsupported_figures")) if result.get("unsupported_figures") else None))
        conn.commit()
    finally:
        conn.close()


def _mdy_safe(stamp):
    try:
        from time_utils import mdy
        return mdy(stamp)
    except Exception:
        return None


def get_diagnosis(restaurant_id: int, db_path: str = DB_PATH,
                  include_stale: bool = False):
    """The stored CFO read, with its own age. `stale` is computed rather than
    enforced: a stale cause is still the best answer available, and hiding it
    leaves the owner with the bare waste total the module used to give them."""
    conn = get_conn(db_path)
    row = _one_row(conn, "SELECT * FROM food_cost_diagnoses WHERE restaurant_id=? "
                     "ORDER BY generated_at DESC LIMIT 1", (restaurant_id,))
    conn.close()
    if not row:
        return None
    age_h = None
    try:
        when = datetime.strptime(str(row["generated_at"]).replace("T", " ")[:19],
                                 "%Y-%m-%d %H:%M:%S")
        age_h = (datetime.utcnow() - when).total_seconds() / 3600.0
    except (ValueError, TypeError):
        age_h = None
    stale = bool(age_h is None or age_h > DIAGNOSIS_TTL_HOURS)
    if stale and not include_stale:
        return None

    def _j(v, fallback):
        try:
            return json.loads(v) if v else fallback
        except Exception:
            return fallback

    out = {
        "ok": True, "headline": row["headline"], "cause": row["cause"],
        "alternative_cause": row["alternative_cause"],
        "what_would_confirm": row["what_would_confirm"],
        "drivers": _j(row["drivers_json"], []),
        "operational_evidence": _j(row["operational_evidence"], []),
        "confidence": row["confidence"],
        "recommended_action": row["recommended_action"],
        "expected_outcome": row["expected_outcome"],
        "dollars_at_stake": row["dollars_at_stake"],
        "unsupported_figures": _j(row["unsupported_figures"] if "unsupported_figures" in row.keys() else None, []),
        "window_days": row["window_days"], "generated_at": row["generated_at"],
        "age_hours": round(age_h, 1) if age_h is not None else None, "stale": stale,
        # Owner-facing date of the read (M/D/YY) and, when it is past its
        # TTL, the sentence that says so — a stale cause read as current
        # wherever a surface dropped the `stale` flag.
        "as_of": _mdy_safe(row["generated_at"]),
        "stale_note": (f"From a read on {_mdy_safe(row['generated_at'])} — it has not been refreshed since."
                       if stale else None),
    }
    # The measured confidence (K6, confidence audit): evidence from the
    # verified figures it cites, capped by the model's own band and by any
    # unsupported figure; `confidence` stays the band string older clients
    # decode.
    try:
        import rec_trust
        import data_freshness
        n_ev = rec_trust.verified_evidence_count(out)
        out["confidence_detail"] = rec_trust.diagnosis_confidence(
            restaurant_id, "diag_food", out, n_ev, "evidence_items",
            f"{n_ev} verified figure{'s' if n_ev != 1 else ''} from the ledger behind it",
            sources=data_freshness.sources_for(["inventory"]), db_path=db_path)
    except Exception as e:
        print(f"[food_cost_intelligence] diagnosis confidence unavailable: {e}")
    return out


# ── The eight questions a morning briefing has to answer ────────────────────

def _as_action(d: dict) -> dict:
    """One driver in the shape every consumer of the brief reads."""
    return {"what": d["label"], "dollars_monthly": d["dollars_monthly"],
            "confidence": d["confidence"], "difficulty": d["difficulty"],
            "evidence": d["evidence"], "if_ignored": d["if_ignored"],
            # The measured confidence (K1) and what it rests on — additive.
            "confidence_detail": d.get("confidence_detail"), "evidence_input": d.get("evidence_input"),
            "kind": d.get("kind"), "item": d.get("item")}


def executive_brief(restaurant_id: int, db_path: str = DB_PATH) -> dict:
    """Deterministic answers to the eight questions, computed in Python and
    true before any model runs.

    The morning brief's food-cost headline was "$X recoverable/month" — a
    waste-recovery estimate, when the question an operator opens with is
    where their margin is. What changed, why, what to fix first, how much is
    involved, what improved, what got worse, what needs attention now, and
    what can wait: each is answered from a measured figure or explicitly
    reported as unanswerable.
    """
    from waste_trend import build_waste_trend
    ev = build_evidence(restaurant_id, db_path=db_path)
    drv, fc, pp = ev["drivers"], ev["food_cost"], ev["profitability"]
    diag = get_diagnosis(restaurant_id, db_path=db_path, include_stale=True)

    try:
        wt = (build_waste_trend(restaurant_id, db_path=db_path) or {}).get("stats") or {}
    except Exception:
        wt = {}

    changed, improved, worsened = [], [], []
    if pp.get("available") and pp.get("dollars_vs_last_month") is not None:
        entry = {"what": f"Prime cost {pp['prime_cost_pct']}% vs {pp['prev_month_prime_pct']}% "
                         f"last month",
                 "dollars": pp["dollars_vs_last_month"], "claim_kind": "forecast"}
        changed.append(entry)
        (worsened if pp["dollars_vs_last_month"] > 0 else improved).append(entry)
    if fc.get("ok") and fc.get("variance_pts") is not None:
        entry = {"what": f"Food cost {fc['pct']}% against a {fc['target']}% target",
                 "variance_pts": fc["variance_pts"], "claim_kind": "measured"}
        changed.append(entry)
        (worsened if fc["variance_pts"] > 0 else improved).append(entry)
    if wt.get("direction") and wt.get("confidence"):
        entry = {"what": f"Waste {wt['direction']} {abs(_f(wt.get('change_pct'))):g}% over "
                         f"{wt.get('weeks')} weeks",
                 "confidence": wt["confidence"], "claim_kind": "computed"}
        changed.append(entry)
        if wt["direction"] == "worsening":
            worsened.append(entry)
        elif wt["direction"] == "improving":
            improved.append(entry)
    if ev["seasonal"].get("available"):
        changed.append({"what": f"Waste is {ev['seasonal']['reading']}",
                        "change_pct": ev["seasonal"]["change_pct"], "claim_kind": "measured"})

    now, later = [], []
    for d in drv.get("drivers", []):
        # What needs attention now versus what can wait: a driver that is
        # both expensive and easy is this week's work; everything else is
        # planned. This is the only place "ease" changes the ORDER an owner
        # sees, and it does so within the top band rather than across it.
        (now if (d["difficulty"] == "low" or d["dollars_monthly"] >= 200) else later).append(
            _as_action(d))

    # One shape, whichever branch produced it. `now[0]` carried "what" and the
    # raw-driver fallback carried "label", so a consumer reading fix_first
    # ["what"] — the Ask Cavnar tool and the morning brief both do — got None
    # on exactly the path where every driver was expensive-but-hard, which is
    # the case an owner most needs answered.
    fix_first = None
    if now:
        fix_first = now[0]
    elif later:
        fix_first = later[0]
    elif drv.get("drivers"):
        fix_first = _as_action(drv["drivers"][0])

    return {
        "what_changed": changed or None,
        "why": ({"cause": diag["cause"], "confidence": ((diag.get("confidence_detail") or {}).get("band") or diag["confidence"]), "confidence_detail": diag.get("confidence_detail"),
                 "alternative": diag["alternative_cause"], "stale": diag["stale"],
                 "as_of": diag.get("as_of"), "stale_note": diag.get("stale_note")}
                if diag else {"cause": None,
                              "reason": "no root-cause read has been produced yet"}),
        "fix_first": fix_first,
        "money_involved": ({"monthly_at_stake": drv.get("total_monthly_deduplicated", drv["total_monthly"]),
                            "monthly_at_stake_basis": drv.get("total_basis"),
                            "projected_month_delta": pp.get("dollars_vs_last_month"),
                            "claim_kind": "forecast"}
                           if drv.get("available") else
                           {"monthly_at_stake": None, "reason": drv.get("reason")}),
        "improved": improved or None,
        "worsened": worsened or None,
        "needs_attention_now": now[:3] or None,
        "can_wait": later[:3] or None,
        "trust": {"recipe_coverage_pct": ev["coverage"].get("coverage_pct"),
                  "inferred_waste_pct": ev["waste_sources"].get("inferred_pct"),
                  "forecast_accuracy": ev["forecast_accuracy"]},
        "food_cost": {"pct": fc.get("pct"), "target": fc.get("target"),
                      "label": fc.get("label"), "missing": fc.get("missing")},
        "profitability": pp,
    }
