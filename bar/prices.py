"""
bar/prices.py — the price watch: every product bought more than once, its
latest price change per unit of measure and what it does at the quantities
the bar actually buys.
"""
from collections import defaultdict
from datetime import timedelta

from bar.common import LABELS, PRICE_LOOKBACK_DAYS, _UOM_WORD, _mdy, display_name


def _price_watch(lines, today) -> dict:
    series = defaultdict(list)
    meta = {}
    for l in lines:
        if l["kind"] != "item" or not l["unit_price"] or float(l["unit_price"]) < 1 or float(l["qty"] or 0) <= 0:
            continue
        # A tax, fee or deposit printed as an item line is not a product with
        # a price ("Sales TAX" read as a 70% price drop).
        if l["category"] in ("fee", "deposit"):
            continue
        k = (l["product_key"], str(l["uom"] or ""))
        series[k].append((str(l["invoice_date"])[:10], float(l["unit_price"]), float(l["qty"] or 0)))
        meta[k] = l
    cutoff = (today - timedelta(days=PRICE_LOOKBACK_DAYS)).isoformat()
    since90 = (today - timedelta(days=90)).isoformat()
    ups, downs, repeat = [], [], 0
    for k, pts in series.items():
        if len(pts) < 2:
            continue
        repeat += 1
        # The latest price against the last different one before it.
        last_d, last_p, _q = pts[-1]
        prev = next(((d, p) for d, p, _ in reversed(pts[:-1]) if abs(p - last_p) >= 0.01), None)
        if not prev:
            continue
        # The day the current price started: the first purchase at it after the previous price.
        since = last_d
        for d, p, _q in reversed(pts):
            if abs(p - last_p) >= 0.01:
                break
            since = d
        if since < cutoff:
            continue
        l = meta[k]
        change = round(last_p - prev[1], 2)
        qty90 = sum(q for d, p, q in pts if d >= since90)
        recent = [(d, p) for d, p, _q in pts if d >= cutoff]
        row = {"product_key": l["product_key"], "product": l["description"], "name": display_name(l["description"]),
               "distributor": l["distributor"],
               # Every purchase in the lookback, oldest first, for the row's
               # own small chart; how often it is bought.
               "history": [{"date": d, "date_mdy": _mdy(d), "price": round(p, 2)} for d, p in recent[-12:]],
               "purchases": len(recent), "last_bought_mdy": _mdy(last_d),
               "uom": l["uom"], "unit": _UOM_WORD.get(str(l["uom"] or "").upper(), "unit"),
               "category": l["category"], "category_label": LABELS.get(l["category"], ""),
               "from": round(prev[1], 2), "to": round(last_p, 2), "change": change,
               "pct": round(change / prev[1] * 100, 1) if prev[1] else None,
               "since": since, "since_mdy": _mdy(since), "was_since_mdy": _mdy(prev[0]),
               "bought_90d": round(qty90, 2), "impact_90d": round(change * qty90, 2)}
        (ups if change > 0 else downs).append(row)
    ups.sort(key=lambda r: (-(r["impact_90d"] or 0), -(r["pct"] or 0)))
    downs.sort(key=lambda r: (r["impact_90d"] or 0, r["pct"] or 0))
    up_usd = round(sum(r["impact_90d"] for r in ups if r["impact_90d"]), 2)
    down_usd = round(sum(r["impact_90d"] for r in downs if r["impact_90d"]), 2)
    by_pct_up = max(ups, key=lambda r: r["pct"] or 0) if ups else None
    by_pct_down = min(downs, key=lambda r: r["pct"] or 0) if downs else None
    # The changes at the amounts bought in the last 90 days: an estimate of
    # what each change does at that volume, never money already paid or saved.
    summary = {"up_n": len(ups), "down_n": len(downs),
               "up_impact_90d": up_usd, "down_impact_90d": down_usd, "net_impact_90d": round(up_usd + down_usd, 2),
               "largest_up": ({"product_key": by_pct_up["product_key"], "name": by_pct_up["name"],
                               "pct": by_pct_up["pct"], "change": by_pct_up["change"], "unit": by_pct_up["unit"]}
                              if by_pct_up else None),
               "largest_down": ({"product_key": by_pct_down["product_key"], "name": by_pct_down["name"],
                                 "pct": by_pct_down["pct"], "change": by_pct_down["change"],
                                 "unit": by_pct_down["unit"]} if by_pct_down else None)}
    return {"increases": ups, "decreases": downs, "products_repeat": repeat,
            "lookback_days": PRICE_LOOKBACK_DAYS, "summary": summary}
