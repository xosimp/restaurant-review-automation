"""
bar/purchases.py — the restaurant's Fintech invoice lines, each sorted into a
category (the owner's correction first), with its net cost (a keg's deposit
taken out).
"""
import re
from collections import defaultdict

from bar.classify import classify, is_mixer, is_rebate, product_key
from bar.common import get_conn
from bar.ledger import Lines

# A negative invoice-level line that is a discount on what the invoice
# bought (an INVOICE DISCOUNT, an early-pay or cash discount, a negative
# summary-level adjustment): it lowers what the invoice's products cost,
# pro rata (BAR-CALC-18, 10/16/26) — it was "Fees & taxes", outside pour
# cost, so liquor read high by every discount.
_INVOICE_DISCOUNT_RE = re.compile(r"\b(DISCOUNT|DISC|EARLY\s*PAY\w*|PROMPT\s*PAY\w*|CASH|ALLOWANCE|REBATE|"
                                  r"SUMMARY\s*LEVEL|ADJUST\w*)\b", re.I)
_NOT_PRODUCT = ("fee", "deposit")


def _allocate_invoice_discounts(rows) -> None:
    """Spread each invoice's negative invoice-level discount over its
    product lines by their cost (module note at _INVOICE_DISCOUNT_RE):
    each product line's `net` falls by its share (`invoice_discount`), and
    the discount line keeps its `cost` but no `net` of its own
    (`allocated`), so it is counted once. A discount on an invoice with no
    product line stays a fee. Mutates `rows`."""
    by_inv = defaultdict(list)
    for r in rows:
        by_inv[r.get("invoice_id")].append(r)
    for rs in by_inv.values():
        disc = [r for r in rs if r["category"] == "fee" and float(r.get("cost") or 0) < 0
                and (r.get("kind") == "adjustment" or _INVOICE_DISCOUNT_RE.search(str(r.get("description") or "")))]
        if not disc:
            continue
        items = [r for r in rs if r.get("kind") in ("item", "charge") and r["category"] not in _NOT_PRODUCT
                 and not r.get("rebate") and r["net"] > 0]
        base = sum(r["net"] for r in items)
        if base <= 0:
            continue
        amount = sum(float(r["cost"] or 0) for r in disc)
        for r in items:
            share = amount * r["net"] / base
            r["invoice_discount"] = round(share, 4)
            r["net"] += share
        for r in disc:
            r["allocated"] = True
            r["net"] = 0.0


def _lines(restaurant_id, db_path=None, backoffice=True) -> list:
    """Every dated invoice line of the restaurant, in date order. Each row:
    distributor, distributor_key (canonical: one key whatever the file's
    spelling), invoice_id, invoice_number, invoice_date, ref_invoice_number
    (the invoice a credit memo refers to, when the file names one),
    po_number, balanced (True / False / None — False when the invoice's lines
    do not add up to its total or a line could not be read: its categories
    are incomplete), item_code, description, upc, uom, uom_inferred (True
    when a printout line took its unit from the export history), qty,
    unit_price, cost, deposit, adjustment, discount, kind, received_at (when
    the file carrying the invoice arrived, or None), product_key,
    category, sure, owner_set, net (cost less a keg's deposit), rebate (a
    product's own discount line, V33), mixer (a cocktail input). A line the
    reader could not parse at all (kind 'unparsed') is left out; its invoice
    reads unbalanced."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT i.id AS invoice_id, i.distributor, i.distributor_key, i.invoice_number, i.invoice_date, "
            "i.ref_invoice_number, i.po_number, i.balanced, l.item_code, l.description, l.upc, l.uom, "
            "l.uom_inferred, l.qty, l.unit_price, l.cost, l.deposit, l.adjustment, l.discount, l.kind, "
            "f.created_at AS received_at "
            "FROM fintech_invoice_lines l JOIN fintech_invoices i ON i.id=l.invoice_id "
            "LEFT JOIN fintech_files f ON f.id=i.file_id "
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
    # A per-unit charge line ("charge") belongs to the item it charges on: it
    # takes that item's category, never one of its own — wherever the item
    # line sits (a charge is now recognised against the code's price on any
    # invoice, so it can come first).
    # Rows read by position against the first row's names: dict(row) asks
    # the row for each name, a third of this read at 45k lines (V17).
    names = list(rows[0].keys()) if rows else []
    rows = [dict(zip(names, r)) for r in rows]
    # The sorting is a pure function of a line's words: read once per
    # distinct product line (one lookup a line), not once per line (V17 —
    # 45k lines ran the regexes 90k times).
    sorted_as, item_cat, info = {}, {}, []
    for r in rows:
        sig = (r["distributor"], r["item_code"], r["description"], r["uom"], r["kind"])
        v = sorted_as.get(sig)
        if v is None:
            v = sorted_as[sig] = (product_key(r["distributor"], r["item_code"], r["description"]),
                                  classify(r["distributor"], r["description"], r["uom"], r["kind"]),
                                  is_rebate(r["description"], r["kind"]))
        info.append(v)
        if r["kind"] == "item":
            item_cat[v[0]] = v[1]
    mixers = {}
    for r, (key, cls, rebate) in zip(rows, info):
        cat, sure = cls
        if r["kind"] == "charge" and key in item_cat:
            cat, sure = item_cat[key]
        if key in overrides and r["kind"] not in ("fee", "adjustment", "keg_credit") and r["uom"] != "DS":
            cat, sure = overrides[key], True
        # A product's own discount or rebate lowers its category's cost (V33);
        # a cocktail mixer is non-alcoholic unless the owner counts it as
        # liquor (Decision 3: bar.products.settings count_mixers).
        mixer = cat == "na" and r["kind"] in ("item", "charge")
        if mixer:
            d = r["description"]
            if d not in mixers:
                mixers[d] = is_mixer(d)
            mixer = mixers[d]
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
    _allocate_invoice_discounts(out)
    # Alcohol paid through Back Office that no Fintech invoice carries (a
    # wine house paid by check, a liquor-store run; backoffice_ap, 10/15/26):
    # purchase lines of kind 'backoffice' - counted in bought vs sold, never
    # in the price watch, the product list or the gap detector.
    try:
        import backoffice_ap
        extra = backoffice_ap.outside_fintech(restaurant_id, db_path)["lines"] if backoffice else []
    except Exception as e:                  # Back Office never costs the Bar its page
        import logging
        logging.getLogger(__name__).warning("bar: Back Office lines unread rid=%s: %s", restaurant_id, e)
        extra = []
    if extra:
        for x in extra:
            if x["mixer"] and count_mixers:
                x["category"] = "liquor"
        merged = sorted(list(out) + extra, key=lambda l: str(l["invoice_date"])[:10])
        out = Lines()
        out.extend(merged)
    return out


def product_catalog(restaurant_id, db_path=None, kinds=("item",)) -> dict:
    """{product_key: {name, category, description}} — every product the
    restaurant's invoices carry, sorted exactly as _lines sorts them (the
    owner's correction first, a cocktail mixer as liquor only when the owner
    counts mixers), read from its DISTINCT product lines rather than every
    invoice line (SCALE-09, 10/16/26: the waste log and drink-spec routes
    read ~45k lines per request). The newest description names a product."""
    from bar.common import display_name
    from bar.products import settings
    conn = get_conn(db_path)
    try:
        marks = ",".join("?" * len(kinds))
        rows = conn.execute(
            "SELECT i.distributor, l.item_code, l.description, l.uom, l.kind, MAX(i.invoice_date) AS last "
            "FROM fintech_invoice_lines l JOIN fintech_invoices i ON i.id=l.invoice_id "
            f"WHERE i.restaurant_id=? AND i.invoice_date IS NOT NULL AND l.kind IN ({marks}) "
            "GROUP BY i.distributor, l.item_code, l.description, l.uom, l.kind",
            (int(restaurant_id),) + tuple(kinds)).fetchall()
        overrides = {r["product_key"]: r["category"] for r in conn.execute(
            "SELECT product_key, category FROM bar_product_categories WHERE restaurant_id=?", (int(restaurant_id),))}
    finally:
        conn.close()
    count_mixers = settings(restaurant_id, db_path)["count_mixers"]
    out, newest = {}, {}
    for r in sorted((dict(x) for x in rows), key=lambda x: str(x["last"] or "")):
        key = product_key(r["distributor"], r["item_code"], r["description"])
        cat, _sure = classify(r["distributor"], r["description"], r["uom"], r["kind"])
        if key in overrides and r["uom"] != "DS":
            cat = overrides[key]
        elif cat == "na" and count_mixers and is_mixer(r["description"]):
            cat = "liquor"
        if str(r["last"] or "") >= newest.get(key, ""):
            newest[key] = str(r["last"] or "")
            out[key] = {"name": display_name(r["description"]), "category": cat, "description": r["description"]}
    return out
