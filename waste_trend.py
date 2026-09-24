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

# Kept in step with models.init_db's declaration, including the columns the
# SELECT below names — this only ever runs on a database that predates that
# declaration, and a create missing a selected column turns the safety net
# into the failure it was meant to prevent.
_SCHEMA = """CREATE TABLE IF NOT EXISTS inventory_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id INTEGER NOT NULL,
    waste_json TEXT,
    week_end    TEXT,
    items_json  TEXT,
    source      TEXT,
    inv_value   REAL,
    saved_at    TEXT DEFAULT (datetime('now'))
)"""

# Set once the table is known to exist — see load_waste_history.
_SCHEMA_ENSURED = False


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _label(d):
    return f"{d.month}/{d.day}"


def _week_from_row(week_end, waste_json, items_json, inv_value_col=None):
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
    # week_end is the day the snapshot was taken, which is whenever the owner
    # last opened the page — not a business week boundary. The label is
    # therefore "the seven days ending when this was counted", and the payload
    # says so rather than implying a Mon-Sun week the data never described.
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
    # The scheduled snapshot writes the counted stock value into its own
    # column. cogs.inventory_value_near needs exactly this to bracket a COGS
    # window, and before the column existed it could only be recovered by
    # re-summing items_json — so a snapshot row without per-item detail
    # contributed no opening or closing value and food cost % silently
    # refused to compute.
    if inv_value_col not in (None, 0):
        week["inv_value"] = round(_f(inv_value_col), 2)

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


def load_waste_history(restaurant_id, limit=None, db_path=None, since=None, until=None):
    """The restaurant's weekly waste series, oldest first, one entry per
    ISO week. When a week holds several snapshots (the insight ran more
    than once), the most recent one is that week's figure. `limit` keeps
    the newest N weeks after bucketing, so a range of 8 is eight distinct
    weeks, not eight rows.

    `since` / `until` (dates, inclusive) bound which snapshot rows are read
    and parsed at all. A caller that needs only the counts near two dates —
    cogs.build_food_cost_pct — used to JSON-parse every snapshot the
    restaurant had ever written on each request (MOD-FC-18)."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        # models.init_db declares this table now; the lazy create stays only
        # as a safety net for a database that predates that, and is skipped
        # once the table is known to exist rather than run per request.
        global _SCHEMA_ENSURED
        if not _SCHEMA_ENSURED:
            conn.execute(_SCHEMA)
            _SCHEMA_ENSURED = True
        # Two queries on purpose. Snapshots are written per day the page is
        # viewed, not per week, so a single unbounded SELECT used to read and
        # JSON-parse every row this restaurant had ever written — each
        # items_json carrying a full item list — and then discard almost all
        # of it in Python. The first query reads one small indexed column to
        # find the true week count and the cutoff; only the rows inside the
        # requested range carry their JSON across.
        all_days = [r["week_end"] for r in conn.execute(
            "SELECT week_end FROM inventory_history "
            "WHERE restaurant_id=? AND week_end IS NOT NULL ORDER BY week_end ASC",
            (restaurant_id,),
        ).fetchall()]
        seen, ordered_keys, first_day_of = set(), [], {}
        for d in all_days:
            try:
                key = date.fromisoformat(d).isocalendar()[:2]
            except Exception:
                continue
            if key not in seen:
                seen.add(key)
                ordered_keys.append(key)
                first_day_of[key] = d
        total_weeks = len(ordered_keys)
        cutoff = None
        if limit and total_weeks > limit:
            cutoff = first_day_of[ordered_keys[-limit]]
        lo = max([str(v)[:10] for v in (cutoff, since) if v] or [""]) or None
        sql = ("SELECT week_end, waste_json, items_json, inv_value FROM inventory_history "
               "WHERE restaurant_id=? AND week_end IS NOT NULL")
        params = [restaurant_id]
        if lo:
            sql += " AND week_end >= ?"
            params.append(lo)
        if until:
            sql += " AND week_end <= ?"
            params.append(str(until)[:10])
        rows = conn.execute(sql + " ORDER BY week_end ASC", params).fetchall()
    finally:
        conn.close()

    buckets = {}
    order = []
    for row in rows:
        week = _week_from_row(row["week_end"], row["waste_json"],
                              row["items_json"] if "items_json" in row.keys() else None,
                              row["inv_value"] if "inv_value" in row.keys() else None)
        if not week:
            continue
        iso = date.fromisoformat(week["week_end"]).isocalendar()
        key = (iso[0], iso[1])
        if key not in buckets:
            order.append(key)
        buckets[key] = week  # rows arrive oldest→newest, so the last one wins
    weeks = [buckets[k] for k in order]
    # total is every week on file, not just the ones fetched — build_waste_trend
    # decides which range buttons to offer from it.
    total = total_weeks
    if limit and len(weeks) > limit:
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


# How strong a trend is, measured (confidence re-audit B1 H8, B4 L2). The
# old band gave "medium" to ANY five weeks whatever they did: the zig-zag
# [4.5, 3.9, 4.6, 3.8, 4.3] read "declining, medium" and cleared the
# negative-trend alert. trend_strength_pct is
#     R² × sample adequacy
# where R² is the share of the weeks' spread the fitted line explains (a
# zig-zag explains almost none; eight weeks of pure noise explain about
# 1/7 on average) and sample adequacy = min(1, n / TREND_FULL_WEEKS). The
# band words are read off it with two more tests: the least-squares slope's
# two-sided p-value (Student's t, n − 2 degrees of freedom) and the share of
# week-over-week moves agreeing with the slope (`consistency`). "high" needs
# TREND_HIGH_WEEKS weeks, p < TREND_P_HIGH, TREND_STRENGTH_HIGH and
# TREND_CONSISTENCY_HIGH; "medium" p < TREND_P_MEDIUM, TREND_STRENGTH_MEDIUM
# and TREND_CONSISTENCY_MEDIUM; anything else is "low".
TREND_P_HIGH = 0.05
TREND_P_MEDIUM = 0.10
TREND_STRENGTH_HIGH = 50
TREND_STRENGTH_MEDIUM = 25
TREND_CONSISTENCY_HIGH = 0.6
TREND_CONSISTENCY_MEDIUM = 0.5
TREND_HIGH_WEEKS = 8
TREND_FULL_WEEKS = 8


def trend_strength(values, slope=None) -> dict:
    """{trend_strength_pct, r_squared, consistency, p_value, significant, n}
    for a series (oldest first) — trend_strength_pct None below
    WEEKS_FOR_TREND weeks (not measurable, never 0). See the note above.
    Pure."""
    n = len(values)
    out = {"trend_strength_pct": None, "r_squared": None, "consistency": None, "p_value": None,
           "significant": False, "n": n}
    if n < WEEKS_FOR_TREND:
        return out
    xs = list(range(n))
    xm, ym = _mean(xs), _mean(values)
    sxx = sum((x - xm) ** 2 for x in xs) or 1.0
    b = sum((x - xm) * (y - ym) for x, y in zip(xs, values)) / sxx
    if slope is None:
        slope = b
    resid = [y - (ym + b * (x - xm)) for x, y in zip(xs, values)]
    sse = sum(r * r for r in resid)
    sst = sum((y - ym) ** 2 for y in values)
    r2 = max(0.0, min(1.0, 1.0 - sse / sst)) if sst > 0 else 0.0
    if b == 0:
        p = 1.0
    elif sse <= 1e-12 * max(1.0, sum(v * v for v in values)):
        p = 0.0                      # an exact line: no noise to mistake it for
    else:
        import metrics
        se = math.sqrt(sse / (n - 2) / sxx)
        p = 2.0 * (1.0 - metrics.t_cdf(abs(b) / se, n - 2))
    deltas = [values[i] - values[i - 1] for i in range(1, n)]
    agree = sum(1 for d in deltas if (d > 0) == (slope > 0) or d == 0)
    c = agree / len(deltas) if deltas else 0.0
    adequacy = min(1.0, n / float(TREND_FULL_WEEKS))
    out.update(trend_strength_pct=int(round(100.0 * r2 * adequacy)), r_squared=round(r2, 3),
               consistency=round(c, 3), p_value=round(p, 4), significant=p < TREND_P_MEDIUM)
    return out


def _confidence(values, slope):
    """How much to trust the direction call, as a band read off
    trend_strength (the measured figure clients show): None below
    WEEKS_FOR_TREND weeks, else high / medium / low."""
    ts = trend_strength(values, slope)
    pct, p, n, c = ts["trend_strength_pct"], ts["p_value"], ts["n"], ts["consistency"]
    if pct is None:
        return None
    if (n >= TREND_HIGH_WEEKS and p < TREND_P_HIGH and pct >= TREND_STRENGTH_HIGH
            and c >= TREND_CONSISTENCY_HIGH):
        return "high"
    if p < TREND_P_MEDIUM and pct >= TREND_STRENGTH_MEDIUM and c >= TREND_CONSISTENCY_MEDIUM:
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
        "trend_strength_pct": None, "trend_p_value": None,
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

    # WEEKS_FOR_TREND, not WEEKS_FOR_TREND - 1. The constant's own comment
    # says a trend needs "more than a coincidence of three", and the code
    # then declared one on exactly three.
    if n >= WEEKS_FOR_TREND:
        d = _direction(values)
        stats["direction"] = d["direction"]
        stats["change_pct"] = d["change_pct"]
        stats["slope"] = d["slope"]
        stats["confidence"] = _confidence(values, d["slope"])
        ts = trend_strength(values, d["slope"])
        stats["trend_strength_pct"] = ts["trend_strength_pct"]
        stats["trend_p_value"] = ts["p_value"]

    flags = _anomalies(values)
    stats["anomalies"] = [{"index": i, "kind": k, "label": weeks[i]["label"], "week_end": weeks[i]["week_end"],
                           "waste": values[i]} for i, k in enumerate(flags) if k]

    if target_weekly:
        stats["above_target"] = values[-1] > target_weekly
        stats["gap_weekly"] = round(values[-1] - target_weekly, 2)
        stats["weeks_over_target"] = sum(1 for v in values if v > target_weekly)
        # One baseline per statement. gap_weekly came from the LATEST week
        # while savings_if_at_target came from rolling4 x 52, and the card put
        # them in one sentence — "you're $G a week over, about $S a year" —
        # where G x 52 did not equal S. An owner who multiplied found the
        # sentence contradicting itself.
        stats["gap_baseline_weekly"] = round(stats["rolling4"] - target_weekly, 2)
        # Annualising needs enough weeks to mean anything. rolling4 is a mean
        # of whatever is there, so with two weeks on file this produced a
        # confident "$X a year" from a fortnight.
        if n >= WEEKS_FOR_TREND:
            stats["annualized_current"] = round(stats["rolling4"] * 52)
            stats["annualized_if_target"] = round(target_weekly * 52)
            stats["savings_if_at_target"] = round(max(0.0, stats["rolling4"] - target_weekly) * 52)
            stats["annualized_basis"] = "%d-week average x 52" % min(n, 4)
            # What each annual figure is (NS3 R1): the gap to target is an
            # opportunity (never "good"/green), the run rate a projection.
            stats["money_kinds"] = {"savings_if_at_target": "opportunity", "annualized_current": "projection",
                                    "annualized_if_target": "plan"}
        else:
            stats["annualized_basis"] = None
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


def waste_trend_observations(stats, weeks, target_pct=WASTE_TARGET_PCT):
    """Short, data-sourced statements for the card — every number in them
    is one the stats dict already holds, so nothing is asserted that the
    series doesn't show. Ordered by what an owner should read first."""
    out = []
    n = stats.get("weeks") or 0
    if not n:
        return out
    # The measured trend strength, not a band word (re-audit B4 L2).
    strength = stats.get("trend_strength_pct")
    conf_note = f"{strength}% trend strength · {n} weeks" if strength is not None else None

    if stats.get("direction") and n >= WEEKS_FOR_TREND:
        pct = abs(stats["change_pct"])
        if stats["direction"] == "worsening":
            out.append({"text": f"Waste is trending up about {pct:g}% across the last {n} weeks.",
                        "tone": "bad", "confidence": conf_note, "trend_strength_pct": strength})
        elif stats["direction"] == "improving":
            out.append({"text": f"Waste is trending down about {pct:g}% across the last {n} weeks — the current direction is the right one.",
                        "tone": "good", "confidence": conf_note, "trend_strength_pct": strength})
        else:
            out.append({"text": f"Waste is holding steady across the last {n} weeks — no clear trend either way.",
                        "tone": "neutral", "confidence": conf_note, "trend_strength_pct": strength})

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
            txt = f"You're {_money(gap)} a week over the {target_pct:g}% target"
            if save:
                # Quoted against the same baseline the annual figure uses, so
                # the two halves of this sentence agree with each other.
                run_rate = stats.get("gap_baseline_weekly") or 0
                txt += (f" — averaging {_money(run_rate)} a week over across the last "
                        f"{min(n, 4)} weeks, about {_money(save)} a year if brought to target")
            out.append({"text": txt + ".", "tone": "bad" if stats.get("intervention") else "warn"})
        elif stats.get("annualized_if_target") is not None and stats.get("annualized_current") is not None:
            # The annual figure rests on the 4-week average, so the weekly
            # figure beside it does too: "$50 a week under ... roughly $260 a
            # year" paired the latest week with the average (x52 = $2,600) —
            # the mistake already fixed for the over-target branch (NS3 M7).
            bl = float(stats.get("gap_baseline_weekly") or 0)
            if bl < 0:
                base = abs(bl)
                out.append({"text": f"You're {_money(abs(gap))} a week under the {target_pct:g}% target — averaging "
                                    f"{_money(base)} a week under across the last {min(n, 4)} weeks, about "
                                    f"{_money(round(base * 52))} a year below target if it holds (a projection).",
                            "tone": "good"})
            else:
                # Under target this week, but the average still runs over.
                out.append({"text": f"You're {_money(abs(gap))} under the {target_pct:g}% target this week — the last "
                                    f"{min(n, 4)} weeks still averaged {_money(bl)} a week over it.",
                            "tone": "neutral"})
        else:
            out.append({"text": f"You're {_money(gap)} a week under the {target_pct:g}% target.",
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


def implied_target_weekly(analysis=None, weeks=None, target_pct=WASTE_TARGET_PCT):
    """The target line in dollars: the target % band applied to what the
    restaurant actually buys in a week. The live analysis is the best
    source (this week's real purchases); failing that, the newest week
    whose snapshot carried purchase data. Returns (dollars, basis)."""
    if analysis:
        rate = _f(analysis.get("waste_rate_pct"))
        waste = _f(analysis.get("total_waste_cost_week"))
        if rate > 0 and waste > 0:
            purchased = waste / (rate / 100.0)
            return round(purchased * target_pct / 100.0, 2), "live"
    for w in reversed(weeks or []):
        if w.get("purchased"):
            return round(w["purchased"] * target_pct / 100.0, 2), "history"
    return None, None


def get_waste_target_pct(restaurant_id, db_path=None):
    """This restaurant's own waste target %, or the industry default (see
    WASTE_TARGET_PCT) when they haven't set one."""
    from models import get_conn
    conn = get_conn(db_path) if db_path else get_conn()
    try:
        row = conn.execute("SELECT waste_target_pct FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    pct = row["waste_target_pct"] if row else None
    if pct is None:
        return WASTE_TARGET_PCT
    # Validated here, not just in the settings form's min/max attributes —
    # those are client-side only, and admin_routes stores a bare float(). A
    # zero made implied_target_weekly return 0.0, which is falsy, so the
    # entire target section silently disappeared with no explanation.
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return WASTE_TARGET_PCT
    if not (0 < pct <= 100):
        return WASTE_TARGET_PCT
    return pct


def build_waste_trend(restaurant_id, range_key="8w", analysis=None, is_live=True, db_path=None, target_pct=None):
    """The whole card payload for one restaurant and one range."""
    range_key = range_key if range_key in RANGE_WEEKS else "8w"
    if target_pct is None:
        target_pct = get_waste_target_pct(restaurant_id, db_path=db_path)
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
    target, basis = implied_target_weekly(analysis, weeks, target_pct)
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
        "target": {"pct": target_pct, "weekly": target, "basis": basis},
        "weeks": weeks,
        "stats": stats,
        "observations": waste_trend_observations(stats, weeks, target_pct),
        "categories": categories,
        "empty": _empty_state(len(weeks), is_live) if len(weeks) < WEEKS_FOR_COMPARISON else None,
    }
