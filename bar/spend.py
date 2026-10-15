"""
bar/spend.py — where the money goes: spend, fees and deposits by
distributor, and every product with its spend (the ones to check first).
"""
from collections import defaultdict

from bar.common import LABELS


def _distributors(lines) -> list:
    by = defaultdict(lambda: {"spend": 0.0, "fees": 0.0, "invoices": set(), "deposits": 0.0})
    for l in lines:
        b = by[l["distributor"]]
        b["invoices"].add(l["invoice_number"])
        if l["category"] == "fee":
            b["fees"] += float(l["cost"] or 0)
        elif l["category"] == "deposit":
            b["deposits"] += float(l["cost"] or 0)
        else:
            b["spend"] += l["net"]
    total = sum(v["spend"] for v in by.values()) or 0
    out = [{"distributor": k, "spend": round(v["spend"], 2), "fees": round(v["fees"], 2),
            "deposits": round(v["deposits"], 2), "invoices": len(v["invoices"]),
            "share": round(v["spend"] / total * 100, 1) if total else None} for k, v in by.items()]
    return sorted(out, key=lambda r: -r["spend"])


def _products(lines) -> list:
    by = {}
    for l in lines:
        if l["kind"] not in ("item", "charge"):
            continue
        p = by.setdefault(l["product_key"], {"product_key": l["product_key"], "product": l["description"],
                                             "distributor": l["distributor"], "uom": l["uom"],
                                             "category": l["category"], "sure": l["sure"],
                                             "owner_set": l["owner_set"], "spend": 0.0, "last": ""})
        p["spend"] += l["net"]
        p["last"] = max(p["last"], str(l["invoice_date"])[:10])
        if l["kind"] == "item":
            p["category"], p["sure"], p["owner_set"] = l["category"], l["sure"], l["owner_set"]
    out = list(by.values())
    for p in out:
        p["spend"] = round(p["spend"], 2)
        p["category_label"] = LABELS.get(p["category"], "")
    # The ones a person should look at first: not sorted, then guessed, by spend.
    out.sort(key=lambda p: (p["category"] != "other", p["sure"] or p["owner_set"], -p["spend"]))
    return out
