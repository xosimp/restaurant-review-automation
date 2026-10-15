"""
bar/spend.py — where the money goes: spend, fees, deposits and rebates by
distributor; every product with its spend (the ones to check first); the
keg-deposit balance each distributor holds (F7); and the rebates and free
goods that never show as a price (F7, V33).
"""
from collections import defaultdict

from bar.common import LABELS, _UOM_WORD, _mdy, display_name

_POUR_CATS = ("beer", "liquor", "wine", "na")


def _distributors(lines) -> list:
    by = defaultdict(lambda: {"spend": 0.0, "fees": 0.0, "invoices": set(), "deposits": 0.0, "rebates": 0.0})
    for l in lines:
        b = by[l["distributor"]]
        b["invoices"].add(l["invoice_number"])
        if l["category"] == "fee":
            b["fees"] += float(l["cost"] or 0)
        elif l["category"] == "deposit":
            b["deposits"] += float(l["cost"] or 0)
        else:
            # A product's own discount (V33) lowers spend in its category;
            # it is also said on its own.
            b["spend"] += l["net"]
            if l.get("rebate"):
                b["rebates"] += float(l["cost"] or 0)
    total = sum(v["spend"] for v in by.values()) or 0
    out = [{"distributor": k, "spend": round(v["spend"], 2), "fees": round(v["fees"], 2),
            "deposits": round(v["deposits"], 2), "rebates": round(v["rebates"], 2), "invoices": len(v["invoices"]),
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
                                             "owner_set": l["owner_set"], "spend": 0.0, "last": "",
                                             "mixer": bool(l.get("mixer"))})
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


def _keg_deposit(l) -> float:
    """The deposit charged inside one keg line's cost: the reader's own
    `deposit` (both formats since 10/15/26), else the export's Item
    Adjustment, else what the cost carries over quantity × price (lines
    stored before the reader set it)."""
    if l.get("deposit") is not None:
        return float(l["deposit"])
    if str(l.get("uom") or "").upper() not in ("KE", "KG"):
        return 0.0
    if l.get("adjustment") not in (None, ""):
        return float(l["adjustment"])
    q, p, c = float(l.get("qty") or 0), float(l.get("unit_price") or 0), float(l.get("cost") or 0)
    extra = c - q * p
    return extra if abs(extra) >= 1 else 0.0


def deposits(lines) -> list:
    """The keg deposits each distributor holds: [{distributor, charged,
    credited, balance, kegs_in, kegs_back}] — charged with each keg (and any
    deposit line), credited with each empty back. A balance is money out on
    the street, never a cost."""
    by = defaultdict(lambda: {"charged": 0.0, "credited": 0.0, "kegs_in": 0.0, "kegs_back": 0.0})
    for l in lines:
        b = None
        if l.get("kind") == "item" and l.get("category") not in ("deposit", "fee"):
            dep = _keg_deposit(l)
            if dep:
                b = by[l["distributor"]]
                b["charged"] += dep
                b["kegs_in"] += float(l.get("qty") or 0)
        elif l.get("category") == "deposit":
            c = float(l.get("cost") or 0)
            b = by[l["distributor"]]
            if c < 0:
                b["credited"] += -c
                b["kegs_back"] += abs(float(l.get("qty") or 0))
            else:
                b["charged"] += c
    out = [{"distributor": k, "charged": round(v["charged"], 2), "credited": round(v["credited"], 2),
            "balance": round(v["charged"] - v["credited"], 2), "kegs_in": round(v["kegs_in"], 2),
            "kegs_back": round(v["kegs_back"], 2)}
           for k, v in by.items() if v["charged"] or v["credited"]]
    return sorted(out, key=lambda r: -r["balance"])


def rebates_and_free_goods(lines, canon=None) -> list:
    """Credit-ledger rows that are not price pairs: a product's own discount
    or rebate line (kind 'rebate', V33) and a $0 case beside a deal (kind
    'free_goods': its value at the product's known price, and the deal's
    cost a unit once the free units are counted, when the same product was
    paid for on that invoice)."""
    canon = canon or {}
    known = {}
    for l in lines:
        if l.get("kind") == "item" and float(l.get("unit_price") or 0) >= 1 and float(l.get("qty") or 0) > 0:
            known[(canon.get(l["product_key"], l["product_key"]), str(l.get("uom") or ""))] = float(l["unit_price"])
    paid = defaultdict(lambda: [0.0, 0.0])
    for l in lines:
        if l.get("kind") == "item" and float(l.get("unit_price") or 0) >= 1 and float(l.get("qty") or 0) > 0:
            k = (l["invoice_number"], canon.get(l["product_key"], l["product_key"]), str(l.get("uom") or ""))
            paid[k][0] += float(l.get("cost") or 0) - float(l.get("deposit") or 0)
            paid[k][1] += float(l["qty"])
    out = []
    for l in lines:
        unit = _UOM_WORD.get(str(l.get("uom") or "").upper(), "unit")
        base = {"distributor": l["distributor"], "product_key": l["product_key"],
                "name": display_name(l["description"]), "uom": l.get("uom"), "unit": unit,
                "date": str(l["invoice_date"])[:10], "date_mdy": _mdy(l["invoice_date"]),
                "invoice_number": l["invoice_number"], "ref_invoice_number": l.get("ref_invoice_number"),
                "category": l.get("category")}
        if l.get("rebate"):
            out.append(dict(base, kind="rebate", units=abs(float(l.get("qty") or 0)),
                            original_price=None, rebilled_price=None,
                            credit_usd=round(-float(l.get("cost") or 0), 2)))
            continue
        if (l.get("kind") in ("item", "charge") and float(l.get("qty") or 0) > 0
                and abs(float(l.get("unit_price") or 0)) < 0.005 and abs(float(l.get("cost") or 0)) < 0.005
                and l.get("category") in _POUR_CATS):
            ck = canon.get(l["product_key"], l["product_key"])
            q = float(l["qty"])
            price = known.get((ck, str(l.get("uom") or "")))
            pk = paid.get((l["invoice_number"], ck, str(l.get("uom") or "")))
            deal = None
            if pk and pk[1]:
                deal = {"paid_units": round(pk[1], 2), "free_units": round(q, 2),
                        "cost_per_unit": round(pk[0] / (pk[1] + q), 2),
                        "billed_per_unit": round(pk[0] / pk[1], 2)}
            out.append(dict(base, kind="free_goods", units=round(q, 2), original_price=price,
                            rebilled_price=0.0, credit_usd=round(price * q, 2) if price else None,
                            deal=deal))
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


def mixers(lines, start=None, end=None, counted=False) -> dict:
    """Cocktail mixers bought (Decision 3): {usd, counted, items [{name,
    usd}]} between start and end (ISO, inclusive) — shown beside liquor;
    counted is the owner's switch to count them as liquor."""
    by = defaultdict(float)
    for l in lines:
        if not l.get("mixer") or l.get("kind") not in ("item", "charge"):
            continue
        d = str(l["invoice_date"])[:10]
        if (start and d < start) or (end and d > end):
            continue
        by[display_name(l["description"])] += float(l.get("net") or 0)
    items = sorted(({"name": k, "usd": round(v, 2)} for k, v in by.items()), key=lambda r: -r["usd"])
    return {"usd": round(sum(by.values()), 2), "counted": bool(counted), "items": items[:8],
            "from_mdy": _mdy(start) if start else None, "to_mdy": _mdy(end) if end else None}
