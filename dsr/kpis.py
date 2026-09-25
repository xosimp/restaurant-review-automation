"""
dsr.kpis — the night's key numbers WITH direction: every figure beside how
it moved, whether it is the best (or worst) in weeks, a short trend line,
its target and — where a fair one exists — restaurants like yours.

    Labor           27.5%    ↓ 1.4 pts vs last Saturday · Best Saturday in 5 weeks
                             Target 28% · Restaurants like yours 29.8% (28-day)

Read at render time from the night's blocks and dsr_metrics history; no
model call. Each view gets its own set (OWNER_SET / MANAGER_SET — the
Manager DSR is operations, not finance) and every KPI passes
dsr.access.line_allowed and the block's view permission, so a manager never
sees the budget, food cost or (without LOSS_VIEW) comps, voids and refunds.

Direction is read against the SAME WEEKDAY: a Saturday against last
Saturday and the Saturdays before it, never against a Tuesday.
  change   vs last <weekday>: points for a percentage, percent for money
           and counts; ↑/↓ with good/bad tone from the metric's better side
  streak   "Best <weekday> in N weeks" / "Worst …" when tonight beats (or
           trails) every one of the last N ≥ STREAK_MIN same weekdays
  spark    the last SPARK_NIGHTS measured nights, oldest first
  target   labor % and food cost % (thresholds.target_for — "Cavnar's
           starting target" said so)
  peers    the Benchmark Engine's peer band median for the matching 28-day
           metric, only when the engine has a fair comparison; otherwise
           its reason (intelligence.engine — no all-types averages for
           economics, organisation floors, etc.)

Derived KPIs (sales per labor hour, prime cost %, beverage mix %) are
computed from the night's own measured figures and say so.
"""
from datetime import date, timedelta

import dsr

STREAK_MIN = 3
STREAK_WEEKS = 12
SPARK_NIGHTS = 14
# The DSR categories that are beverage (alcohol) sales — matched whole, by
# the category the owner mapped a POS department to, never by a substring
# ("bar" matched "Barbecue", and "NA Beverage" counted as beverage, D1-18).
BEVERAGE_CATEGORIES = ("liquor", "beer", "wine", "spirits", "cocktails")

# key: (label, fact key or derived, unit, better)  unit: money | pct | count | hours | stars | ratio
KPIS = {
    "net": ("Net sales", "sales.net", "money", "higher"),
    "guests": ("Guests", "sales.guests", "count", "higher"),
    "avg_ticket": ("Average ticket", "sales.avg_ticket", "money", "higher"),
    "labor_pct": ("Labor", "labor.pct", "pct", "lower"),
    "labor_cost": ("Labor dollars", "labor.cost", "money", None),
    "splh": ("Sales per labor hour", "derived:splh", "money", "higher"),
    "overtime": ("Overtime hours", "labor.overtime_hours", "hours", "lower"),
    "food_pct": ("Food cost", "food.est_food_cost_pct", "pct", "lower"),
    "prime_pct": ("Prime cost", "derived:prime", "pct", "lower"),
    "bev_mix": ("Beverage mix", "derived:bev_mix", "pct", None),
    "voids": ("Voids", "sales.voids", "money", "lower"),
    "discounts": ("Discounts", "sales.discounts", "money", "lower"),
    "comps": ("Comps", "sales.comps", "money", "lower"),
    "refunds": ("Refunds", "sales.refunds", "money", "lower"),
    "rating": ("Guest rating", "reviews.avg_rating", "stars", "higher"),
}
OWNER_SET = ("net", "labor_pct", "food_pct", "prime_pct", "avg_ticket", "guests", "splh", "labor_cost",
             "overtime", "bev_mix", "rating")
# The Manager DSR's Top KPIs and its Operations are two sets that never share
# a key (D3-5): guests, average ticket and sales per labor hour are
# Operations; overtime is a row of Today's shift. build() also skips any key
# Operations already placed, so the same tile is never drawn twice.
MANAGER_SET = ("labor_pct", "rating")
OPERATIONS_SET = ("avg_ticket", "guests", "splh", "voids", "discounts", "comps")
# the Benchmark Engine's comparable metric for a nightly KPI (28/30-day)
ENGINE_METRIC = {"labor_pct": "labor_pct_28d", "food_pct": "food_cost_pct_28d", "rating": "avg_rating_30d"}
TARGET_KIND = {"labor_pct": "labor", "food_pct": "food"}


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fmt(v, unit):
    if v is None:
        return "—"
    if unit == "money":
        return f"${v:,.2f}" if abs(v) < 100 else f"${v:,.0f}"
    if unit == "pct":
        return f"{v:.1f}%"
    if unit == "hours":
        return f"{v:g}h"
    if unit == "stars":
        return f"{v:.1f}★"
    if unit == "count":
        return f"{v:,.0f}"
    return f"{v:g}"


def _series(rid, fact, start, end, db_path):
    from dsr import store
    try:
        return {d: float(v) for d, v in store.metric_series(rid, fact, start, end, db_path=db_path) if _num(v)}
    except Exception:
        return {}


def _bev_share(metrics):
    net = metrics.get("net")
    if not _num(net) or net <= 0:
        return None
    cats = {k[4:]: v for k, v in metrics.items() if k.startswith("cat:") and _num(v)}
    bev = [v for k, v in cats.items() if " ".join(k.lower().split()) in BEVERAGE_CATEGORIES]
    return round(sum(bev) / net * 100, 1) if bev else None


def _prime(labor_cost, food_cost, net, coverage):
    """Prime cost % from DOLLARS over one denominator (D1-11): labor $ plus
    the estimated recipe cost of what sold, over the night's net — never two
    percentages with different bases added. Withheld (None) unless every
    part is measured, net is positive and the recipe estimate covers at
    least block_food.ESTIMATE_MIN_COVERAGE_PCT of the units sold."""
    from dsr.block_food import ESTIMATE_MIN_COVERAGE_PCT
    if not (_num(labor_cost) and _num(food_cost) and _num(net) and net > 0 and _num(coverage)):
        return None
    if coverage < ESTIMATE_MIN_COVERAGE_PCT:
        return None
    return round((labor_cost + food_cost) / net * 100, 1)


def _value(key, blocks):
    """Tonight's value for one KPI, or None (not measured)."""
    fact = KPIS[key][1]
    def m(block):
        b = blocks.get(block) or {}
        return (b.get("metrics") or {}) if b.get("status") == dsr.READY else {}
    if fact == "derived:splh":
        net, hours = m("sales").get("net"), m("labor").get("hours")
        return round(net / hours, 2) if _num(net) and _num(hours) and hours > 0 else None
    if fact == "derived:prime":
        return _prime(m("labor").get("cost"), m("food").get("est_food_cost"), m("sales").get("net"),
                      m("food").get("recipe_coverage_pct"))
    if fact == "derived:bev_mix":
        return _bev_share(m("sales"))
    block, _, k = fact.partition(".")
    v = m(block).get(k)
    return float(v) if _num(v) else None


def _history(key, rid, day, db_path):
    """{date: value} for the KPI over the streak window (same-weekday reads
    pick from it) — derived KPIs rebuilt night by night."""
    start = day - timedelta(days=7 * STREAK_WEEKS)
    end = day - timedelta(days=1)
    fact = KPIS[key][1]
    if fact == "derived:splh":
        net, hrs = _series(rid, "sales.net", start, end, db_path), _series(rid, "labor.hours", start, end, db_path)
        return {d: round(net[d] / hrs[d], 2) for d in net if d in hrs and hrs[d] > 0}
    if fact == "derived:prime":
        lab, food, net, cov = (_series(rid, "labor.cost", start, end, db_path),
                               _series(rid, "food.est_food_cost", start, end, db_path),
                               _series(rid, "sales.net", start, end, db_path),
                               _series(rid, "food.recipe_coverage_pct", start, end, db_path))
        out = {d: _prime(lab.get(d), food.get(d), net.get(d), cov.get(d)) for d in lab}
        return {d: v for d, v in out.items() if v is not None}
    if fact == "derived:bev_mix":
        return {}
    return _series(rid, fact, start, end, db_path)


def _change(value, prior, unit, better, wd):
    if value is None or prior is None:
        return None
    if unit in ("pct", "stars"):
        d = round(value - prior, 1)
        text = f"{'↑' if d > 0 else '↓' if d < 0 else '→'} {abs(d):.1f}{' pts' if unit == 'pct' else '★'} vs last {wd}"
    else:
        if prior == 0:
            return None
        d = round((value - prior) / abs(prior) * 100, 1)
        text = f"{'↑' if d > 0 else '↓' if d < 0 else '→'} {abs(d):.1f}% vs last {wd}"
    tone = None
    if better and d != 0:
        tone = "good" if (d > 0) == (better == "higher") else "bad"
    return {"delta": d, "text": text, "tone": tone}


def _streak(value, same_days, better, wd):
    """Best/worst of the last N same weekdays, N ≥ STREAK_MIN."""
    if value is None or not better or len(same_days) < STREAK_MIN:
        return None
    hi = better == "higher"
    best = worst = 0
    for v in same_days:                         # newest first
        if (value > v) if hi else (value < v):
            best += 1
        else:
            break
    for v in same_days:
        if (value < v) if hi else (value > v):
            worst += 1
        else:
            break
    if best >= STREAK_MIN:
        return {"text": f"Best {wd} in {best + 1} weeks", "tone": "good"}
    if worst >= STREAK_MIN:
        return {"text": f"Worst {wd} in {worst + 1} weeks", "tone": "bad"}
    return None


def _peers(key, restaurant, db_path):
    metric = ENGINE_METRIC.get(key)
    if not metric or restaurant is None:
        return None
    try:
        from intelligence import engine
        c = engine.compare(restaurant.id, metric, kinds=("peers",), restaurant=restaurant, db_path=db_path)
        p = next((x for x in c.get("comparisons") or [] if x.get("kind") == "peers"), None) or {}
    except Exception:
        return None
    if p.get("available") and _num(p.get("p50")):
        unit = KPIS[key][2]
        v = p["p50"]
        return {"available": True, "value_text": fmt(v, unit), "label": "Restaurants like yours",
                "detail": f"{p.get('n')} {p.get('cohort_label') or 'similar restaurants'} · 28-day median",
                "strength_pct": (p.get("strength") or {}).get("pct")}
    return {"available": False, "label": "Restaurants like yours",
            "why_not": (p.get("why_not") or "No fair comparison yet")}


def _target(key, restaurant):
    kind = TARGET_KIND.get(key)
    if not kind or restaurant is None:
        return None
    try:
        import thresholds
        t = thresholds.target_for(restaurant, kind)
    except Exception:
        return None
    if not _num(t.get("pct")):
        return None
    label = "Target" if t.get("source") == "set" else (t.get("label") or "Target").capitalize()
    return {"value": float(t["pct"]), "value_text": f"{float(t['pct']):g}%", "label": label,
            "source": t.get("source")}


def _allowed(key, user, view):
    from dsr import access
    fact = KPIS[key][1]
    if fact.startswith("derived:"):
        if fact == "derived:prime":
            return view == access.OWNER
        return True
    block, _, k = fact.partition(".")
    need = access._block_permissions().get(block)
    if need and not access._sees(user, need):
        return False
    return access.line_allowed(user, view, k)


def kpi(key, blocks, restaurant, day, db_path=None, with_peers=True) -> dict | None:
    label, fact, unit, better = KPIS[key]
    value = _value(key, blocks)
    if value is None:
        return None
    wd = day.strftime("%A")
    rid = getattr(restaurant, "id", None)
    hist = _history(key, rid, day, db_path) if rid else {}
    last_week = hist.get((day - timedelta(days=7)).isoformat())
    nights = sorted(d for d in hist if d >= (day - timedelta(days=SPARK_NIGHTS - 1)).isoformat())
    spark = [hist[d] for d in nights] + [value]
    out = {"key": key, "label": label, "value": value, "value_text": fmt(value, unit), "unit": unit,
           "better": better, "change": _change(value, last_week, unit, better, wd),
           "streak": None, "spark": spark if len(spark) >= 3 else [],
           "target": _target(key, restaurant),
           "peers": _peers(key, restaurant, db_path) if with_peers else None,
           "derived": fact.startswith("derived:"),
           "estimate": key in ("food_pct", "prime_pct")}
    if key == "prime_pct":
        food = blocks.get("food") or {}
        cov = (food.get("metrics") or {}).get("recipe_coverage_pct")
        out["basis"] = (f"Labor dollars plus the estimated recipe cost of what sold ({cov:g}% of units sold have a "
                        "costed recipe; anything without one, drinks included, isn't in it), over net sales")
    # a streak needs an unbroken run of measured same weekdays
    run = []
    for w in range(1, STREAK_WEEKS + 1):
        d = (day - timedelta(days=7 * w)).isoformat()
        if d not in hist:
            break
        run.append(hist[d])
    out["streak"] = _streak(value, run, better, wd)
    return out


def build(facts, restaurant, user, view, db_path=None) -> dict:
    """{"top": [kpi], "operations": [kpi] (manager), "shift": {...} (manager)}
    for this login's view."""
    from dsr import access
    facts = facts or {}
    blocks = facts.get("blocks") or {}
    try:
        day = date.fromisoformat(str(facts.get("business_date"))[:10])
    except Exception:
        return {"top": [], "operations": [], "shift": None}
    keys = OWNER_SET if view == access.OWNER else [k for k in MANAGER_SET if k not in OPERATIONS_SET]
    top = []
    for k in keys:
        if not _allowed(k, user, view):
            continue
        x = kpi(k, blocks, restaurant, day, db_path)
        if x:
            top.append(x)
    ops, shift = [], None
    if view != access.OWNER:
        for k in OPERATIONS_SET:
            if not _allowed(k, user, view):
                continue
            x = kpi(k, blocks, restaurant, day, db_path, with_peers=False)
            if x:
                ops.append(x)
        comp = _complaints(blocks)
        if comp is not None:
            ops.insert(2, comp)
        shift = shift_recap(blocks, user)
    return {"top": top, "operations": ops, "shift": shift}


def _complaints(blocks):
    b = blocks.get("reviews") or {}
    if b.get("status") != dsr.READY:
        return None
    n = (b.get("metrics") or {}).get("negative")
    if not _num(n):
        return None
    return {"key": "complaints", "label": "Guest complaints", "value": n, "value_text": f"{int(n)}",
            "unit": "count", "better": "lower", "change": None, "streak": None, "spark": [], "target": None,
            "peers": None, "derived": False, "estimate": False,
            "detail": "negative reviews that arrived today"}


def shift_recap(blocks, user=None) -> dict | None:
    """The Manager DSR's "Today's shift": who was scheduled and how the shift
    ran, from the Labor block. Rows Cavnar has no source for (break
    compliance) are left off, not shown empty."""
    from dsr import access
    from permissions import LABOR_VIEW
    b = blocks.get("labor") or {}
    if b.get("status") != dsr.READY or (user is not None and not access._sees(user, LABOR_VIEW)):
        return None
    m = b.get("metrics") or {}
    rows = []
    # "No-shows", as the Labor block names them (D1-17): scheduled people
    # who never clocked in. A call-off is someone who told you — Cavnar has
    # no record of that.
    for key, label, fmt_ in (("scheduled", "Employees scheduled", "{:.0f}"), ("no_shows", "No-shows", "{:.0f}"),
                             ("late_arrivals", "Late arrivals", "{:.0f}"), ("overtime_hours", "Overtime hours", "{:g}"),
                             ("shift_quality", "Shift quality", "{:.0f}")):
        v = m.get(key)
        if _num(v):
            tone = None
            if key in ("no_shows", "late_arrivals", "overtime_hours"):
                tone = "good" if v == 0 else "warn"
            elif key == "shift_quality":
                tone = "good" if v >= 80 else ("warn" if v >= 60 else "bad")
            rows.append({"key": key, "label": label, "value": v, "value_text": fmt_.format(v), "tone": tone})
    cov = ((b.get("detail") or {}).get("coverage") or {})
    note = None if cov.get("measured", True) else cov.get("reason")
    return {"rows": rows, "note": note} if rows or note else None
