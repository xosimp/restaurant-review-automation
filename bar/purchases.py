"""
bar/purchases.py — the restaurant's Fintech invoice lines, each sorted into a
category (the owner's correction first), with its net cost (a keg's deposit
taken out).
"""
from bar.classify import classify, is_mixer, is_rebate, product_key
from bar.common import get_conn
from bar.ledger import Lines


def _lines(restaurant_id, db_path=None) -> list:
    """Every dated invoice line of the restaurant, in date order. Each row:
    distributor, distributor_key (canonical: one key whatever the file's
    spelling), invoice_id, invoice_number, invoice_date, ref_invoice_number
    (the invoice a credit memo refers to, when the file names one),
    po_number, balanced (True / False / None — False when the invoice's lines
    do not add up to its total or a line could not be read: its categories
    are incomplete), item_code, description, upc, uom, uom_inferred (True
    when a printout line took its unit from the export history), qty,
    unit_price, cost, deposit, adjustment, discount, kind, product_key,
    category, sure, owner_set, net (cost less a keg's deposit), rebate (a
    product's own discount line, V33), mixer (a cocktail input). A line the
    reader could not parse at all (kind 'unparsed') is left out; its invoice
    reads unbalanced."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT i.id AS invoice_id, i.distributor, i.distributor_key, i.invoice_number, i.invoice_date, "
            "i.ref_invoice_number, i.po_number, i.balanced, l.item_code, l.description, l.upc, l.uom, "
            "l.uom_inferred, l.qty, l.unit_price, l.cost, l.deposit, l.adjustment, l.discount, l.kind "
            "FROM fintech_invoice_lines l JOIN fintech_invoices i ON i.id=l.invoice_id "
            "WHERE i.restaurant_id=? AND i.invoice_date IS NOT NULL AND l.kind<>'unparsed' "
            "ORDER BY i.invoice_date, i.id, l.line_no",
            (int(restaurant_id),)).fetchall()
        overrides = {r["product_key"]: r["category"] for r in conn.execute(
            "SELECT product_key, category FROM bar_product_categories WHERE restaurant_id=?", (int(restaurant_id),))}
    finally:
        conn.close()
    # Decision 3: cocktail mixers stay non-alcoholic unless the owner counts them.
    from bar.products import settings
    count_mixers = settings(restaurant_id, db_path)["count_mixers"]
    out = Lines()
    # The sorting is a pure function of a line's words: read once per
    # distinct product line, not once per line (V17 — 45k lines ran the
    # regexes 90k times).
    memo = {}

    def once(fn, *args):
        k = (fn, args)
        v = memo.get(k, memo)
        if v is memo:
            v = memo[k] = fn(*args)
        return v
    # A per-unit charge line ("charge") belongs to the item it charges on: it
    # takes that item's category, never one of its own — wherever the item
    # line sits (a charge is now recognised against the code's price on any
    # invoice, so it can come first).
    rows = [dict(r) for r in rows]
    item_cat = {}
    for r in rows:
        r["product_key"] = once(product_key, r["distributor"], r["item_code"], r["description"])
        if r["kind"] == "item":
            item_cat[r["product_key"]] = once(classify, r["distributor"], r["description"], r["uom"], r["kind"])
    for r in rows:
        key = r["product_key"]
        cat, sure = once(classify, r["distributor"], r["description"], r["uom"], r["kind"])
        if r["kind"] == "charge" and key in item_cat:
            cat, sure = item_cat[key]
        if key in overrides and r["kind"] not in ("fee", "adjustment", "keg_credit") and r["uom"] != "DS":
            cat, sure = overrides[key], True
        # A product's own discount or rebate lowers its category's cost (V33);
        # a cocktail mixer is non-alcoholic unless the owner counts it as
        # liquor (Decision 3: bar.products.settings count_mixers).
        rebate = once(is_rebate, r["description"], r["kind"])
        mixer = cat == "na" and r["kind"] in ("item", "charge") and once(is_mixer, r["description"])
        if mixer and count_mixers and key not in overrides:
            cat = "liquor"
        # A keg's $30 deposit inside its cost comes back with the empty: it is
        # never counted as what the beer cost.
        net = float(r["cost"] or 0) - (float(r["deposit"] or 0) if r["kind"] == "item" else 0.0)
        r.update(product_key=key, category=cat, sure=sure, owner_set=key in overrides, net=net,
                 rebate=rebate, mixer=mixer,
                 balanced=None if r["balanced"] is None else bool(r["balanced"]),
                 uom_inferred=bool(r["uom_inferred"]))
        out.append(r)
    return out
