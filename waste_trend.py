"""
waste_trend.py — the Food Cost waste-trend engine.

One place computes everything the Waste Trend card shows: the weekly
series, the numbers derived from it (direction, week-over-week, rolling
averages, best/worst weeks, anomalies, target gap, financial impact) and
the plain-language observations built from those numbers. Pure functions
over the series so every rule is unit-testable; only load_waste_history
touches the database.

Why a rebuild rather than a restyle: inventory_history rows are written
by get_claude_insights() on every insight view and keyed by the calendar
day, so two views in one week produced two "weeks" a day apart on the
chart. The series here is bucketed by ISO week — the latest snapshot in a
week stands for that week — and every figure downstream is computed from
that corrected series.
"""
import json
import math
from datetime import date, timedelta

# Midpoint of the 4–5% industry band analyse_inventory() benchmarks
# against; the same figure iOS's FoodCostTrendChart uses for its target
# rule, so both surfaces draw the line in the same place.
WASTE_TARGET_PCT = 4.5

RANGE_WEEKS = {"8w": 8, "13w": 13, "26w": 26, "all": None}

# Weeks of history needed before each kind of statement is made. A single
# comparison needs two points; calling something a trend needs more than a
# coincidence of three.
WEEKS_FOR_COMPARISON = 2
WEEKS_FOR_TREND = 4

_SCHEMA = """CREATE TABLE IF NOT EXISTS inventory_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    waste_json TEXT,
    week_end    TEXT,
    items_json  TEXT,
    saved_at    TEXT DEFAULT (datetime('now'))
)"""


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _label(d):
    return f"{d.month}/{d.day}"


def _week_from_row(week_end, waste_json, items_json):
    """One chart week from one stored snapshot. Per-item detail (costs by
    item and by category, the purchase total behind the waste rate) only
    exists when the snapshot carried items_json — older rows and seeded
    rows carry the total alone."""
    try:
        data = json.loads(waste_json or "{}")
    except Exception:
        data = {}
    try:
        we = date.fromisoformat(week_end)
    except Exception:
        return None
    ws = we - timedelta(days=6)
    week = {
        "week_end": we.isoformat(),
        "start": ws.isoformat(),
        "label": _label(we),
        "start_label": _label(ws),
        "waste": round(_f(data.get("total_waste_cost")), 2),
        "rate": None,
        "inv_value": None,
        "purchased": None,
        "top_items": [{"item": n, "cost": None} for n in (data.get("top_items") or []) if n],
        "categories": None,
        "has_items": False,
    }
    if data.get("waste_rate_pct"):
        week["rate"] = round(_f(data.get("waste_rate_pct")), 1)
    if data.get("inventory_value"):
        week["inv_value"] = round(_f(data.get("inventory_value")), 2)

    items = None
    if items_json:
        try:
            items = json.loads(items_json)
        except Exception:
            items = None
    if items:
        per_item, cats = [], {}
        purchased = inv_value = 0.0
        for it in items:
            unit_cost = _f(it.get("unit_cost"))
            waste_cost = _f(it.get("waste_last_week")) * unit_cost
            purchased += _f(it.get("last_order_qty")) * unit_cost
            inv_value += _f(it.get("current_stock")) * unit_cost
            cat = (it.get("category") or "other").strip().lower() or "other"
            cats[cat] = cats.get(cat, 0.0) + waste_cost
            if waste_cost > 0 and it.get("item"):
                per_item.append({"item": it["item"], "cost": round(waste_cost, 2)})
        per_item.sort(key=lambda x: x["cost"], reverse=True)
        week["top_items"] = per_item[:5]
        week["categories"] = {k: round(v, 2) for k, v in cats.items() if v > 0}
        week["purchased"] = round(purchased, 2)
        week["inv_value"] = round(inv_value, 2)
        week["has_items"] = True
        if purchased > 0 and week["waste"] > 0:
            week["rate"] = round(week["waste"] / purchased * 100, 1)
    return week


def load_waste_history(restaurant_id, limit=None, db_path=None):
    """The restaurant's weekly waste series, oldest first, one entry per
    ISO week. When a week holds several snapshots (the insight ran more
    than once), the most recent one is that week's figure. `limit` keeps
    the newest N weeks after bucketing, so a range of 8 is eight distinct
    weeks, not eight rows."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        conn.execute(_SCHEMA)
        rows = conn.execute(
            "SELECT week_end, waste_json, items_json FROM inventory_history "
            "WHERE restaurant_id=? AND week_end IS NOT NULL ORDER BY week_end ASC",
            (restaurant_id,),
        ).fetchall()
    finally:
        conn.close()

    buckets = {}
    order = []
    for row in rows:
        week = _week_from_row(row["week_end"], row["waste_json"],
                              row["items_json"] if "items_json" in row.keys() else None)
        if not week:
            continue
        iso = date.fromisoformat(week["week_end"]).isocalendar()
        key = (iso[0], iso[1])
        if key not in buckets:
            order.append(key)
        buckets[key] = week  # rows arrive oldest→newest, so the last one wins
    weeks = [buckets[k] for k in order]
    total = len(weeks)
    if limit and total > limit:
        weeks = weeks[-limit:]
    return weeks, total


def _direction(values):
    """Least-squares slope over the series, read as improving / worsening /
    flat. `change_pct` is the fitted line's total move across the window
    as a share of the average week, so a $10 drift on $60 weeks and a $100
    drift on $600 weeks read the same."""
    n = len(values)
    if n < 2:
        return {"direction": None, "slope": 0.0, "change_pct": 0.0}
    xs = list(range(n))
    xm, ym = _mean(xs), _mean(values)
    den = sum((x - xm) ** 2 for x in xs) or 1.0
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, values)) / den
    avg = ym or 1.0
    change_pct = round(slope * (n - 1) / avg * 100, 1)
    if abs(change_pct) < 8:
        direction = "flat"
    else:
        direction = "worsening" if slope > 0 else "improving"
    return {"direction": direction, "slope": round(slope, 2), "change_pct": change_pct}


def _confidence(values, slope):
    """How much to trust the direction call: more weeks, and consecutive
    moves that mostly agree with the fitted slope, earn more confidence."""
    n = len(values)
    if n < WEEKS_FOR_TREND - 1:
        return None
    deltas = [values[i] - values[i - 1] for i in range(1, n)]
    agree = sum(1 for d in deltas if (d > 0) == (slope > 0) or d == 0)
    consistency = agree / len(deltas) if deltas else 0
    if n >= 8 and consistency >= 0.6:
        return "high"
    if n >= 5 or (n >= 4 and consistency >= 0.66):
        return "medium"
    return "low"


def _anomalies(values):
    """Weeks that sit well outside the series' own spread — a z-score past
    1.5 against every other week. Needs four weeks before a spread means
    anything; with fewer, nothing is called unusual."""
    n = len(values)
    flags = [None] * n
    if n < WEEKS_FOR_TREND:
        return flags
    mean = _mean(values)
    var = _mean([(v - mean) ** 2 for v in values])
    std = math.sqrt(var)
    if std <= 0 or mean <= 0:
        return flags
    for i, v in enumerate(values):
        z = (v - mean) / std
        # A very tight series makes tiny moves statistically "extreme" —
        # $5 on $150 weeks is not a spike an owner should act on, so the
        # week also has to sit a real distance (15% of the average) away.
        if abs(v - mean) < 0.15 * mean:
            continue
        if z >= 1.5:
            flags[i] = "spike"
        elif z <= -1.5:
            flags[i] = "dip"
    return flags


def waste_trend_stats(weeks, target_weekly=None):
    """Every derived number the card shows, computed once from the series.
    Pure: weeks in, dict out. All money is dollars per week unless the key
    says otherwise."""
    n = len(weeks)
    values = [w["waste"] for w in weeks]
    stats = {
        "weeks": n,
        "latest": values[-1] if n else None,
        "prev": values[-2] if n >= 2 else None,
        "wow_delta": None, "wow_pct": None,
        "avg": round(_mean(values), 2) if n else None,
        "rolling4": None, "prior4": None,
        "mom_delta": None, "mom_pct": None,
        "best": None, "worst": None,
        "largest_increase": None, "largest_decrease": None,
        "direction": None, "change_pct": 0.0, "slope": 0.0, "confidence": None,
        "anomalies": [],
        "target_weekly": round(target_weekly, 2) if target_weekly else None,
        "above_target": None, "gap_weekly": None, "weeks_over_target": None,
        "annualized_current": None, "annualized_if_target": None, "savings_if_at_target": None,
        "intervention": False,
    }
    if not n:
        return stats

    if n >= 2:
        d = values[-1] - values[-2]
        stats["wow_delta"] = round(d, 2)
        stats["wow_pct"] = round(d / values[-2] * 100, 1) if values[-2] else None

    last4 = values[-4:]
    stats["rolling4"] = round(_mean(last4), 2)
    if n >= 5:
        prior = values[-8:-4] if n >= 8 else values[:-4]
        stats["prior4"] = round(_mean(prior), 2)
        md = stats["rolling4"] - stats["prior4"]
        stats["mom_delta"] = round(md, 2)
        stats["mom_pct"] = round(md / stats["prior4"] * 100, 1) if stats["prior4"] else None

    bi = min(range(n), key=lambda i: values[i])
    wi = max(range(n), key=lambda i: values[i])
    stats["best"] = {"index": bi, "week_end": weeks[bi]["week_end"], "label": weeks[bi]["label"], "waste": values[bi]}
    stats["worst"] = {"index": wi, "week_end": weeks[wi]["week_end"], "label": weeks[wi]["label"], "waste": values[wi]}

    if n >= 2:
        moves = []
        for i in range(1, n):
            d = values[i] - values[i - 1]
            moves.append({"index": i, "label": weeks[i]["label"], "week_end": weeks[i]["week_end"],
                          "delta": round(d, 2),
                          "pct": round(d / values[i - 1] * 100, 1) if values[i - 1] else None})
        inc = max(moves, key=lambda m: m["delta"])
        dec = min(moves, key=lambda m: m["delta"])
        stats["largest_increase"] = inc if inc["delta"] > 0 else None
        stats["largest_decrease"] = dec if dec["delta"] < 0 else None

    if n >= WEEKS_FOR_TREND - 1:
        d = _direction(values)
        stats["direction"] = d["direction"]
        stats["change_pct"] = d["change_pct"]
        stats["slope"] = d["slope"]
        stats["confidence"] = _confidence(values, d["slope"])

    flags = _anomalies(values)
    stats["anomalies"] = [{"index": i, "kind": k, "label": weeks[i]["label"], "week_end": weeks[i]["week_end"],
                           "waste": values[i]} for i, k in enumerate(flags) if k]

    if target_weekly:
        stats["above_target"] = values[-1] > target_weekly
        stats["gap_weekly"] = round(values[-1] - target_weekly, 2)
        stats["weeks_over_target"] = sum(1 for v in values if v > target_weekly)
        stats["annualized_current"] = round(stats["rolling4"] * 52)
        stats["annualized_if_target"] = round(target_weekly * 52)
        stats["savings_if_at_target"] = round(max(0.0, stats["rolling4"] - target_weekly) * 52)
        recent = values[-4:]
        over_recent = sum(1 for v in recent if v > target_weekly)
        # Over target and either getting worse, or stuck there for most of
        # the last month. A series already coming down fast is not asked
        # to intervene — it already has.
        stats["intervention"] = bool(
            stats["above_target"] and stats["direction"] != "improving"
            and (stats["direction"] == "worsening" or over_recent >= 3)
        )
    return stats


def _money(v):
    return f"${int(round(abs(v))):,}"


def waste_trend_observations(stats, weeks):
    """Short, data-sourced statements for the card — every number in them
    is one the stats dict already holds, so nothing is asserted that the
    series doesn't show. Ordered by what an owner should read first."""
    out = []
    n = stats.get("weeks") or 0
    if not n:
        return out
    conf = stats.get("confidence")
    conf_note = {"high": f"high confidence · {n} weeks", "medium": f"moderate confidence · {n} weeks",
                 "low": f"early read · {n} weeks"}.get(conf)

    if stats.get("direction") and n >= WEEKS_FOR_TREND - 1:
        pct = abs(stats["change_pct"])
        if stats["direction"] == "worsening":
            out.append({"text": f"Waste is trending up about {pct:g}% across the last {n} weeks.",
                        "tone": "bad", "confidence": conf_note})
        elif stats["direction"] == "improving":
            out.append({"text": f"Waste is trending down about {pct:g}% across the last {n} weeks — the current direction is the right one.",
                        "tone": "good", "confidence": conf_note})
        else:
            out.append({"text": f"Waste is holding steady across the last {n} weeks — no clear trend either way.",
                        "tone": "neutral", "confidence": conf_note})

    if stats.get("wow_delta") is not None:
        d, p = stats["wow_delta"], stats["wow_pct"]
        pct_txt = f" ({abs(p):g}%)" if p is not None else ""
        if abs(d) < 1:
            out.append({"text": "This week's waste is level with last week.", "tone": "neutral"})
        elif d > 0:
            out.append({"text": f"Waste rose {_money(d)}{pct_txt} this week compared to last week.", "tone": "bad"})
        else:
            out.append({"text": f"Waste fell {_money(d)}{pct_txt} this week compared to last week.", "tone": "good"})

    worst = stats.get("worst")
    if worst and n >= WEEKS_FOR_COMPARISON and stats.get("avg"):
        over = (worst["waste"] - stats["avg"]) / stats["avg"] * 100 if stats["avg"] else 0
        wk = weeks[worst["index"]]
        driver = ""
        named = [t["item"] for t in (wk.get("top_items") or [])[:2]]
        if named:
            driver = " — driven by " + " and ".join(named)
        spike = any(a["index"] == worst["index"] and a["kind"] == "spike" for a in stats.get("anomalies") or [])
        lead = "Unusual spike: the" if spike else "The"
        out.append({"text": f"{lead} week of {wk['start_label']}–{wk['label']} was the worst at {_money(worst['waste'])}, {over:.0f}% above your average{driver}.",
                    "tone": "bad" if spike or over >= 15 else "neutral"})

    if stats.get("target_weekly"):
        gap = stats.get("gap_weekly") or 0
        if stats.get("above_target"):
            save = stats.get("savings_if_at_target") or 0
            txt = f"You're {_money(gap)} a week over the {WASTE_TARGET_PCT:g}% target"
            if save:
                txt += f" — about {_money(save)} a year if waste is brought to target"
            out.append({"text": txt + ".", "tone": "bad" if stats.get("intervention") else "warn"})
        else:
            out.append({"text": f"You're {_money(gap)} a week under the {WASTE_TARGET_PCT:g}% target — holding here keeps roughly {_money(stats['annualized_if_target'] - stats['annualized_current'])} a year off the waste bill versus target.",
                        "tone": "good"})

    if stats.get("mom_delta") is not None and len(out) < 4:
        md, mp = stats["mom_delta"], stats["mom_pct"]
        pct_txt = f" ({abs(mp):g}%)" if mp is not None else ""
        if abs(md) >= 1:
            out.append({"text": f"The last 4 weeks averaged {_money(md)}{pct_txt} {'more' if md > 0 else 'less'} per week than the 4 before them.",
                        "tone": "bad" if md > 0 else "good"})
    return out[:4]


def _empty_state(weeks_have, is_live):
    """Why there is no trend yet, what fills it, and when it appears."""
    if not is_live:
        return {
            "title": "Your waste trend starts with your first count",
            "reason": "The numbers on this page are a sample pantry, so there is no history of yours to chart yet.",
            "needed": "Upload an inventory CSV or connect your POS. Cavnar records a weekly waste snapshot every time your analysis runs.",
            "when": f"A first comparison appears after {WEEKS_FOR_COMPARISON} weekly counts; a trend line after {WEEKS_FOR_TREND}.",
            "weeks_have": 0, "weeks_needed": WEEKS_FOR_COMPARISON,
        }
    if weeks_have == 0:
        return {
            "title": "No waste history recorded yet",
            "reason": "Your inventory is connected, but no weekly snapshot has been saved for it yet.",
            "needed": "Open this page once a week after updating counts — each analysis run records that week's waste.",
            "when": f"A first comparison appears after {WEEKS_FOR_COMPARISON} weeks; a trend line after {WEEKS_FOR_TREND}.",
            "weeks_have": 0, "weeks_needed": WEEKS_FOR_COMPARISON,
        }
    return {
        "title": "First week recorded",
        "reason": "One week of waste is on file — a trend needs something to compare it against.",
        "needed": "Keep counts updated; next week's snapshot starts the comparison.",
        "when": f"Week-over-week next week · a trend line after {WEEKS_FOR_TREND} weeks.",
        "weeks_have": weeks_have, "weeks_needed": WEEKS_FOR_COMPARISON,
    }


def implied_target_weekly(analysis=None, weeks=None):
    """The target line in dollars: the 4.5% band applied to what the
    restaurant actually buys in a week. The live analysis is the best
    source (this week's real purchases); failing that, the newest week
    whose snapshot carried purchase data. Returns (dollars, basis)."""
    if analysis:
        rate = _f(analysis.get("waste_rate_pct"))
        waste = _f(analysis.get("total_waste_cost_week"))
        if rate > 0 and waste > 0:
            purchased = waste / (rate / 100.0)
            return round(purchased * WASTE_TARGET_PCT / 100.0, 2), "live"
    for w in reversed(weeks or []):
        if w.get("purchased"):
            return round(w["purchased"] * WASTE_TARGET_PCT / 100.0, 2), "history"
    return None, None


def build_waste_trend(restaurant_id, range_key="8w", analysis=None, is_live=True, db_path=None):
    """The whole card payload for one restaurant and one range."""
    range_key = range_key if range_key in RANGE_WEEKS else "8w"
    weeks, total = load_waste_history(restaurant_id, RANGE_WEEKS[range_key], db_path=db_path)
    # Only offer a wider range once there is history beyond the narrower
    # one — a "26 weeks" button over 6 weeks of data is a button that does
    # nothing — and a request for a range the history can't fill resolves
    # to the widest one it can.
    ranges = ["8w"]
    if total > 8:
        ranges.append("13w")
    if total > 13:
        ranges.append("26w")
    if total > 26:
        ranges.append("all")
    if range_key not in ranges:
        range_key = ranges[-1]
        if RANGE_WEEKS[range_key]:
            weeks = weeks[-RANGE_WEEKS[range_key]:]
    target, basis = implied_target_weekly(analysis, weeks)
    stats = waste_trend_stats(weeks, target)

    flag_by_index = {a["index"]: a["kind"] for a in stats["anomalies"]}
    for i, w in enumerate(weeks):
        w["flags"] = {
            "anomaly": flag_by_index.get(i),
            "best": bool(stats["best"] and stats["best"]["index"] == i and len(weeks) >= WEEKS_FOR_COMPARISON),
            "worst": bool(stats["worst"] and stats["worst"]["index"] == i and len(weeks) >= WEEKS_FOR_COMPARISON),
            "over_target": bool(target and w["waste"] > target),
            "this_week": i == len(weeks) - 1,
        }

    categories = sorted({c for w in weeks if w.get("categories") for c in w["categories"]})
    return {
        "ok": True,
        "range": range_key,
        "ranges": ranges,
        "weeks_total": total,
        "target": {"pct": WASTE_TARGET_PCT, "weekly": target, "basis": basis},
        "weeks": weeks,
        "stats": stats,
        "observations": waste_trend_observations(stats, weeks),
        "categories": categories,
        "empty": _empty_state(len(weeks), is_live) if len(weeks) < WEEKS_FOR_COMPARISON else None,
    }
