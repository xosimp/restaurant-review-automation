"""
bar/pour_cost.py — purchase-based pour cost by category: a window's
purchases against its sales, the distributors whose invoices stop inside a
window (a gap is said, never judged), the judgement against the owner's
target or the rule-of-thumb range, the four-week reading with its months,
and the week-by-week trend.
"""
from collections import defaultdict
from datetime import date, timedelta

from bar.common import LABELS, POUR_CATEGORIES, TREND_POINTS, WINDOW_DAYS, _mdy

# The industry rule-of-thumb range each pour category is commonly held to
# (beer blends draft ~18-22% with package ~24-28%).
RULE_OF_THUMB = {"beer": (20.0, 26.0), "liquor": (16.0, 22.0), "wine": (30.0, 40.0)}


def _window(lines, sales, start, end) -> dict:
    """{cat: {purchases, sales, pct, days_with_sales}} for [start, end]."""
    out = {}
    for cat in POUR_CATEGORIES:
        bought = sum(l["net"] for l in lines
                     if l["category"] == cat and start <= str(l["invoice_date"])[:10] <= end)
        sold_days = {d: v for d, v in (sales.get(cat) or {}).items() if start <= d <= end}
        sold = sum(sold_days.values())
        pct = round(bought / sold * 100, 1) if sold > 0 and bought > 0 else None
        lo, hi = RULE_OF_THUMB[cat]
        tone = None if pct is None else ("good" if lo <= pct <= hi else ("warn" if pct > hi else "low"))
        out[cat] = {"key": cat, "label": LABELS[cat], "purchases": round(bought, 2), "sales": round(sold, 2),
                    "pct": pct, "tone": tone, "range": [lo, hi], "days_with_sales": len(sold_days)}
    return out


def _gaps(lines, start, end) -> list:
    """Distributors whose invoices stop inside [start, end] for much longer
    than their own rhythm: [{distributor, from, to, from_mdy, to_mdy,
    categories}]. Simple EJ's history came from Fintech's payment-activity
    export (10/14/26) — only invoices already PAID — so the spirits and wine
    houses, paid about 30 days out, were missing their last weeks and liquor
    read 7%. A window with a gap is said, never judged."""
    by = defaultdict(set)
    cats = defaultdict(lambda: defaultdict(float))
    for l in lines:
        by[l["distributor"]].add(str(l["invoice_date"])[:10])
        if l["category"] in POUR_CATEGORIES:
            cats[l["distributor"]][l["category"]] += abs(l["net"])
    out = []
    for dist, days in by.items():
        ds = sorted(days)
        if len(ds) < 4:
            continue
        steps = sorted((date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(ds, ds[1:]))
        typical = steps[len(steps) // 2] or 1
        limit = max(10, int(typical * 2))
        inside = [d for d in ds if start <= d <= end]
        before = [d for d in ds if d < start]
        marks = ([before[-1]] if before else [start]) + inside + [end]
        for a, b in zip(marks, marks[1:]):
            gap = (date.fromisoformat(b) - date.fromisoformat(a)).days
            if gap > limit:
                lo = max(start, (date.fromisoformat(a) + timedelta(days=1)).isoformat())
                hi = (date.fromisoformat(b) - timedelta(days=1)).isoformat() if b in days else end
                mine = sorted(cats[dist], key=lambda c: -cats[dist][c])
                out.append({"distributor": dist, "from": lo, "to": hi, "from_mdy": _mdy(lo), "to_mdy": _mdy(hi),
                            "categories": [c for c in mine if cats[dist][c] > 0], "typical_days": typical})
    return out


def _judge(x, target):
    """The owner's target, when they set one, is what a reading is judged
    against: at or under it is good, over it is a warning. Without one the
    rule-of-thumb range judges (good inside, warn above, low below)."""
    x["target"] = target
    x["target_source"] = "owner" if target is not None else "industry"
    if target is None or x.get("pct") is None or x.get("incomplete"):
        return
    x["tone"] = "good" if x["pct"] <= target else "warn"


def _pour_cost(lines, sales, today, targets=None) -> dict:
    inv_dates = sorted({str(l["invoice_date"])[:10] for l in lines})
    sale_dates = sorted({d for c in sales.values() for d in c})
    if not inv_dates or not sale_dates:
        return {"available": False,
                "why_not": ("No distributor invoices on file yet." if not inv_dates
                            else "No sales by category on file yet — they come from the nightly report.")}
    lo = max(inv_dates[0], sale_dates[0])
    hi = min(inv_dates[-1], sale_dates[-1])
    if lo > hi:
        return {"available": False, "why_not": "The invoices and the sales on file don't overlap yet."}
    end = hi
    start = max(lo, (date.fromisoformat(end) - timedelta(days=WINDOW_DAYS - 1)).isoformat())
    cur = _window(lines, sales, start, end)
    gaps = _gaps(lines, start, end)
    for g in gaps:
        for c in g["categories"]:
            if c in cur:
                cur[c]["incomplete"] = True
                cur[c]["tone"] = None
    bought = sum(c["purchases"] for c in cur.values())
    sold = sum(c["sales"] for c in cur.values())
    months = []
    m = date.fromisoformat(lo).replace(day=1)
    while m.isoformat() <= hi:
        nxt = (m.replace(day=28) + timedelta(days=4)).replace(day=1)
        a, b = max(lo, m.isoformat()), min(hi, (nxt - timedelta(days=1)).isoformat())
        w = _window(lines, sales, a, b)
        for g in _gaps(lines, a, b):
            for c in g["categories"]:
                if c in w:
                    w[c]["incomplete"] = True
                    w[c]["tone"] = None
        full = a == m.isoformat() and b == (nxt - timedelta(days=1)).isoformat()
        months.append({"month": m.strftime("%Y-%m"), "label": m.strftime("%b %Y"), "from": a, "to": b,
                       "partial": not full, "from_mdy": _mdy(a), "to_mdy": _mdy(b),
                       "categories": [w[c] for c in POUR_CATEGORIES]})
        m = nxt
    trend = _trend(lines, sales, lo, hi)
    # Four weeks before the latest weekly point, when the trend reaches back that far.
    earlier = trend[-5] if len(trend) >= 5 else None
    targets = targets or {}
    for c in POUR_CATEGORIES:
        x = cur[c]
        lo_r, hi_r = x["range"]
        x["spark"] = [p[c] for p in trend]
        x["prev_pct"] = earlier[c] if earlier else None
        x["prev_end_mdy"] = earlier["end_mdy"] if earlier else None
        judged = x["pct"] is not None and not x.get("incomplete")
        # What the reading is above the rule of thumb's top, in dollars at
        # this window's sales: the range's arithmetic, never money "lost".
        x["over_range_usd"] = round((x["pct"] - hi_r) / 100 * x["sales"], 2) if judged and x["pct"] > hi_r else None
        _judge(x, targets.get(c))
        # Above the owner's own target, in dollars at this window's sales.
        x["over_target_usd"] = (round((x["pct"] - x["target"]) / 100 * x["sales"], 2)
                                if judged and x["target"] is not None and x["pct"] > x["target"] else None)
    weights = {c: cur[c]["sales"] for c in POUR_CATEGORIES if cur[c]["sales"] > 0}
    wsum = sum(weights.values())
    mix_range = ([round(sum(RULE_OF_THUMB[c][0] * v for c, v in weights.items()) / wsum, 1),
                  round(sum(RULE_OF_THUMB[c][1] * v for c, v in weights.items()) / wsum, 1)] if wsum else None)
    total_pct = round(bought / sold * 100, 1) if sold > 0 and bought > 0 else None
    total_target = targets.get(None)
    total_over = (round((total_pct - total_target) / 100 * sold, 2)
                  if total_pct is not None and total_target is not None and total_pct > total_target and not gaps else None)
    return {"available": True, "window": {"from": start, "to": end, "from_mdy": _mdy(start), "to_mdy": _mdy(end),
                                          "days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1},
            "categories": [cur[c] for c in POUR_CATEGORIES],
            "total": {"purchases": round(bought, 2), "sales": round(sold, 2), "pct": total_pct,
                      # The rule-of-thumb range for this bar's own mix: each
                      # category's range weighted by its share of the sales.
                      "range": mix_range,
                      "spark": [p["total"] for p in trend],
                      # The owner's own overall alcohol target, when set.
                      "target": total_target, "target_source": "owner" if total_target is not None else "industry",
                      "over_target_usd": total_over,
                      "prev_pct": earlier["total"] if earlier else None,
                      "prev_end_mdy": earlier["end_mdy"] if earlier else None},
            "trend": trend,
            "months": months, "overlap": {"from_mdy": _mdy(lo), "to_mdy": _mdy(hi)},
            "gaps": gaps, "complete": not gaps}


def _trend(lines, sales, lo, hi) -> list:
    """The four-week pour cost read week by week: one point for the four weeks
    ending on the last day both are on file, then every seven days back,
    while a whole four weeks of invoices and sales fits ([oldest .. newest]).
    A category whose distributor's invoices are missing in a window is a gap
    (None) there, never a low reading; the total is a gap with it."""
    first = date.fromisoformat(lo)
    end = date.fromisoformat(hi)
    pts = []
    while len(pts) < TREND_POINTS:
        start = end - timedelta(days=WINDOW_DAYS - 1)
        if start < first:
            break
        a, b = start.isoformat(), end.isoformat()
        w = _window(lines, sales, a, b)
        missing = {c for g in _gaps(lines, a, b) for c in g["categories"]}
        point = {"end": b, "end_mdy": _mdy(b), "from_mdy": _mdy(a)}
        bought = sum(w[c]["purchases"] for c in POUR_CATEGORIES)
        sold = sum(w[c]["sales"] for c in POUR_CATEGORIES)
        for c in POUR_CATEGORIES:
            point[c] = None if c in missing else w[c]["pct"]
        point["total"] = (round(bought / sold * 100, 1)
                          if not missing and sold > 0 and bought > 0 else None)
        pts.append(point)
        end -= timedelta(days=7)
    return list(reversed(pts))
