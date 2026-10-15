"""
bar/purchases.py — the restaurant's Fintech invoice lines, each sorted into a
category (the owner's correction first), with its net cost (a keg's deposit
taken out).
"""
from bar.classify import classify, product_key
from bar.common import get_conn


def _lines(restaurant_id, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT i.distributor, i.invoice_number, i.invoice_date, l.item_code, l.description, l.uom, l.qty, "
            "l.unit_price, l.cost, l.deposit, l.kind FROM fintech_invoice_lines l JOIN fintech_invoices i ON i.id=l.invoice_id "
            "WHERE i.restaurant_id=? AND i.invoice_date IS NOT NULL ORDER BY i.invoice_date, i.id, l.line_no",
            (int(restaurant_id),)).fetchall()
        overrides = {r["product_key"]: r["category"] for r in conn.execute(
            "SELECT product_key, category FROM bar_product_categories WHERE restaurant_id=?", (int(restaurant_id),))}
    finally:
        conn.close()
    out = []
    # A per-unit charge line ("charge") belongs to the item beside it: it
    # takes that item's category, never one of its own.
    seen_cat = {}
    for r in rows:
        r = dict(r)
        key = product_key(r["distributor"], r["item_code"], r["description"])
        cat, sure = classify(r["distributor"], r["description"], r["uom"], r["kind"])
        if r["kind"] == "charge" and key in seen_cat:
            cat, sure = seen_cat[key]
        if key in overrides and r["kind"] not in ("fee", "adjustment", "keg_credit") and r["uom"] != "DS":
            cat, sure = overrides[key], True
        if r["kind"] == "item":
            seen_cat[key] = (cat, sure)
        # A keg's $30 deposit inside its cost comes back with the empty: it is
        # never counted as what the beer cost.
        net = float(r["cost"] or 0) - (float(r["deposit"] or 0) if r["kind"] == "item" else 0.0)
        r.update(product_key=key, category=cat, sure=sure, owner_set=key in overrides, net=net)
        out.append(r)
    return out
