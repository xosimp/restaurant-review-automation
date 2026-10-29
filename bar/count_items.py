"""
bar/count_items.py — the bar count, product by product (owner, 10/29/26:
"I need to be able to put a count for ALL drinks, not just beer and
seltzer").

The count was three dollar figures a night — beer & seltzer, liquor, wine —
typed as each category's shelf value at cost, which nobody counts a bar by.
Now the count is a sheet of every beer, liquor and wine product the bar's
invoices carry (bar.purchases.product_catalog), each counted in the unit it
is poured from: bottles for liquor and wine (a part bottle in tenths — 2.5),
cans or bottles for packaged beer, kegs (0.5) for draft. A bottle is counted
as whole bottles (Full) and each opened bottle's level in tenths (Open: .3,
.9 nearly full, .1 nearly out) — the count is their sum, the split kept.

Each product's count is saved as it is typed (`save_item`, one product at a
time — a count is never overwritten as a list) into `bar_count_items`, with
the unit cost it was valued at: the product's newest invoice line through
its pack (bar.products.unit_costs, the owner's pack corrections laid over),
kept with the count so a later price never moves an old count. A keg is
valued without its deposit.

The categories follow: after every save the date's beer, liquor and wine
values are the sums of their counted products, written to `bar_counts`
(method "items", bar.counts.save_count) — so counted pour cost
(bar.counts.true_pour_cost) reads a product count exactly as it read a typed
category value. A product with no price on its invoices is counted and named
(`unpriced`), never valued at 0.
"""
from datetime import date, timedelta

from bar.common import LABELS, _mdy, get_conn

COUNT_CATEGORIES = ("liquor", "wine", "beer")          # the sheet's order
# Products bought within this many days are on the sheet (with any product
# already counted that date): what could be on the shelf, not every one-off
# bottle since the first invoice.
SHEET_DAYS = 180
MAX_QTY = 10_000
UNIT_WORDS = {"bottle": ("bottle", "bottles"), "can": ("can", "cans"), "keg": ("keg", "kegs"),
              "unit": ("unit", "units")}


def init_bar_count_items(db_path=None):
    """One product's count on one date. Boot only (bar.counts.init_bar_counts).
    A financial record: kept, never pruned."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_count_items (
            restaurant_id INTEGER NOT NULL,
            count_date TEXT NOT NULL,
            product_key TEXT NOT NULL,
            category TEXT NOT NULL,
            qty REAL NOT NULL,
            unit TEXT NOT NULL,
            unit_cost REAL,
            entered_by INTEGER,
            entered_at TEXT NOT NULL,
            PRIMARY KEY (restaurant_id, count_date, product_key))""")
        # A bottle count's split (10/29/26): whole bottles, and each opened
        # bottle's level in tenths (.3; .9 nearly full) — qty is their sum.
        have = {r[1] for r in conn.execute("PRAGMA table_info(bar_count_items)")}
        for col, decl in (("full_qty", "REAL"), ("open_json", "TEXT")):
            if col not in have:
                conn.execute(f"ALTER TABLE bar_count_items ADD COLUMN {col} {decl}")
        conn.commit()
    finally:
        conn.close()


def _newest_lines(restaurant_id, db_path=None) -> dict:
    """{product_key: {description, uom, unit_price, ppc, priced, last}} —
    each product's newest priced invoice line (`last` its newest date, priced
    or not), read from the distinct price points, never every line
    (SCALE-09)."""
    from bar.classify import product_key
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT i.distributor, l.item_code, l.description, l.uom, l.unit_price, l.ppc, MAX(i.invoice_date) AS last "
            "FROM fintech_invoice_lines l JOIN fintech_invoices i ON i.id=l.invoice_id "
            "WHERE i.restaurant_id=? AND i.invoice_date IS NOT NULL AND l.kind='item' "
            "GROUP BY i.distributor, l.item_code, l.description, l.uom, l.unit_price, l.ppc",
            (int(restaurant_id),)).fetchall()
    finally:
        conn.close()
    out = {}
    for r in sorted((dict(x) for x in rows), key=lambda x: str(x["last"] or "")):
        k = product_key(r["distributor"], r["item_code"], r["description"])
        d = str(r["last"] or "")[:10]
        priced = float(r["unit_price"] or 0) > 0
        cur = out.setdefault(k, {"priced": False, "last": d})
        cur["last"] = max(cur["last"], d)
        if priced or not cur["priced"]:
            cur.update(description=r["description"], uom=r["uom"], unit_price=float(r["unit_price"] or 0),
                       ppc=r["ppc"], priced=priced or cur["priced"])
    return out


def _unit(line, category, override=None) -> tuple:
    """(unit, unit_cost or None) — what one counted unit is (bottle, can,
    keg) and what it cost, from the product's newest invoice line."""
    from bar.products import parse_pack, unit_costs
    pk = parse_pack(line["description"], line.get("uom"), category, ppc=line.get("ppc"))
    unit = pk.get("each") if pk.get("each") in UNIT_WORDS else "unit"
    if not line.get("priced"):
        return unit, None
    uc = unit_costs(line["unit_price"], line.get("uom"), line["description"], category, override=override,
                    ppc=line.get("ppc"))
    if not uc or not uc.get("per_each"):
        return unit, None
    return (uc.get("each") if uc.get("each") in UNIT_WORDS else unit), round(float(uc["per_each"]), 4)


def _saved(restaurant_id, count_date, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT product_key, category, qty, unit, unit_cost, full_qty, open_json FROM bar_count_items "
                            "WHERE restaurant_id=? AND count_date=?", (int(restaurant_id), count_date)).fetchall()
    finally:
        conn.close()
    return {r["product_key"]: dict(r) for r in rows}


def _products(restaurant_id, db_path=None, today=None) -> dict:
    """{product_key: {name, category, unit, unit_cost, last}} — every beer,
    liquor and wine product on the invoices, its counting unit and cost."""
    from bar.products import pack_overrides
    from bar.purchases import product_catalog
    cat = product_catalog(restaurant_id, db_path)
    lines = _newest_lines(restaurant_id, db_path)
    packs = pack_overrides(restaurant_id, db_path)
    out = {}
    for k, v in cat.items():
        if v["category"] not in COUNT_CATEGORIES or k not in lines:
            continue
        unit, cost = _unit(lines[k], v["category"], packs.get(k))
        out[k] = {"name": v["name"], "category": v["category"], "unit": unit, "unit_cost": cost,
                  "last": lines[k]["last"]}
    # The owner's bottles bought outside the invoices (bar.hand): always on
    # the sheet, valued at the price they gave.
    from bar.hand import hand_products
    for h in hand_products(restaurant_id, db_path):
        if h["category"] in COUNT_CATEGORIES:
            unit = "can" if (h["category"] == "beer" and h["size_label"].endswith("OZ")) else "bottle"
            out[h["product_key"]] = {"name": h["product"], "category": h["category"], "unit": unit,
                                     "unit_cost": round(float(h["price"]), 4),
                                     "last": (today or date.today()).isoformat()}
    return out


def _parse_day(value, today):
    from bar.counts import _parse_day as parse
    d = parse(value)
    if d > today:
        raise ValueError("A count can't be dated in the future.")
    if d < today - timedelta(days=3 * 366):
        raise ValueError("That count is more than three years old.")
    return d


def sheet(restaurant_id, count_date, dollars=True, db_path=None, today=None, products=None) -> dict:
    """{ok, count_date, count_date_mdy, groups: [{category, label, products:
    [{product_key, name, unit, unit_label, qty, unit_cost?, value?}]}],
    counted, of, totals? {category: value}, unpriced} for one count date.
    Without `dollars` (a counts-only login) no cost, value or total."""
    from bar.counts import _restaurant_today
    today = today or _restaurant_today(restaurant_id, db_path)
    iso = _parse_day(count_date, today).isoformat()
    since = (today - timedelta(days=SHEET_DAYS)).isoformat()
    prods = products if products is not None else _products(restaurant_id, db_path, today)
    saved = _saved(restaurant_id, iso, db_path)
    groups = {c: [] for c in COUNT_CATEGORIES}
    totals = {c: 0.0 for c in COUNT_CATEGORIES}
    unpriced = []
    for k, p in prods.items():
        s = saved.get(k)
        if s is None and p["last"] < since:
            continue
        row = {"product_key": k, "name": p["name"], "unit": s["unit"] if s else p["unit"],
               "qty": float(s["qty"]) if s else None}
        row["unit_label"] = UNIT_WORDS[row["unit"]][1]
        if s and row["unit"] == "bottle":
            row["full"], row["open"] = _split(s)
        cost = (s["unit_cost"] if s else p["unit_cost"])
        if dollars:
            row["unit_cost"] = round(cost, 2) if cost is not None else None
            row["value"] = round(row["qty"] * cost, 2) if (s and cost is not None) else None
        if s:
            if cost is None:
                unpriced.append(p["name"])
            else:
                totals[p["category"]] += float(s["qty"]) * cost
        groups[p["category"]].append(row)
    out_groups = []
    for c in COUNT_CATEGORIES:
        rows = sorted(groups[c], key=lambda r: r["name"].lower())
        if rows:
            out_groups.append({"category": c, "label": LABELS.get(c, c), "products": rows,
                               "counted": sum(1 for r in rows if r["qty"] is not None)})
    out = {"ok": True, "count_date": iso, "count_date_mdy": _mdy(iso), "groups": out_groups,
           "counted": sum(g["counted"] for g in out_groups), "of": sum(len(g["products"]) for g in out_groups),
           "unpriced": sorted(unpriced)}
    if dollars:
        out["totals"] = {c: round(v, 2) for c, v in totals.items()}
    return out


MAX_OPEN = 30           # opened bottles of one product on one count


def _split(saved) -> tuple:
    """(full, [open levels]) of a saved bottle count: as typed, else read
    from its total (a count saved as one number: 2.5 → 2 full, one at .5)."""
    import json
    if saved.get("full_qty") is not None or saved.get("open_json"):
        try:
            opens = [float(x) for x in json.loads(saved.get("open_json") or "[]")]
        except (TypeError, ValueError):
            opens = []
        return float(saved.get("full_qty") or 0), opens
    q = float(saved["qty"])
    whole = int(q)
    return float(whole), ([round(q - whole, 2)] if q - whole > 0.004 else [])


def _parse_split(full, opens) -> tuple:
    """(full, [levels]) from the Full box and the Open boxes — None, None
    when every box is blank (clear the count). Full is whole bottles; an
    opened bottle is its level, more than 0 and less than 1 (.3; .9 nearly
    full). ValueError with an owner sentence."""
    if isinstance(opens, (str, int, float)) and not isinstance(opens, bool):
        opens = [opens]
    levels = []
    for o in (opens or []):
        if o is None or (isinstance(o, str) and not o.strip()):
            continue
        try:
            v = float(str(o).replace(",", "").strip())
        except (TypeError, ValueError):
            raise ValueError("Type an open bottle's level as a part of one — .3, or .9 nearly full.")
        if isinstance(o, bool) or v != v or not 0 < v < 1:
            raise ValueError("An open bottle is between .1 and .9 — a full one goes in Full.")
        levels.append(round(v, 2))
    if len(levels) > MAX_OPEN:
        raise ValueError(f"At most {MAX_OPEN} open bottles of one product.")
    f = _parse_qty(full)
    if f is None and not levels:
        return None, None
    f = f or 0.0
    if f != int(f):
        raise ValueError("Full is whole bottles — an opened one's level goes in Open.")
    return f, levels


def _parse_qty(value):
    """None for blank (clear the product's count), else a number of units
    from 0 to MAX_QTY, to two places."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("Type how many, as a number — 2.5 for two and a half bottles.")
    try:
        v = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise ValueError("Type how many, as a number — 2.5 for two and a half bottles.")
    if v != v or v < 0 or v > MAX_QTY:
        raise ValueError(f"A count is between 0 and {MAX_QTY:,}.")
    return round(v, 2)


def save_item(restaurant_id, count_date, product_key, qty=None, user_id=None, db_path=None, today=None,
              full=None, opens=None) -> dict:
    """Save ONE product's count for a date (blank clears it), then the
    date's category values from its counted products (bar.counts.save_count,
    method "items"; a category with no product counted is taken back off).
    → {ok, count_date, count_date_mdy, product_key, name, category, qty,
    unit, unit_cost, value, category_total, counted, of}. ValueError (an
    owner sentence) for a bad date, product or number."""
    from bar.counts import _restaurant_today, save_count, _now
    today = today or _restaurant_today(restaurant_id, db_path)
    iso = _parse_day(count_date, today).isoformat()
    key = str(product_key or "").strip()
    prods = _products(restaurant_id, db_path, today)
    if key not in prods:
        raise ValueError("Pick a beer, liquor or wine the bar has bought.")
    p = prods[key]
    import json
    full_q = open_j = None
    if full is not None or opens is not None:
        # A bottle's Full and Open boxes (10/29/26); the count is their sum.
        f, levels = _parse_split(full, opens)
        n = None if f is None else round(f + sum(levels), 2)
        if f is not None:
            full_q, open_j = f, json.dumps(levels)
        if n is not None and n > MAX_QTY:
            raise ValueError(f"A count is between 0 and {MAX_QTY:,}.")
    else:
        n = _parse_qty(qty)
    conn = get_conn(db_path)
    try:
        if n is None:
            conn.execute("DELETE FROM bar_count_items WHERE restaurant_id=? AND count_date=? AND product_key=?",
                         (int(restaurant_id), iso, key))
        else:
            conn.execute(
                "INSERT INTO bar_count_items (restaurant_id, count_date, product_key, category, qty, unit, unit_cost, "
                "full_qty, open_json, entered_by, entered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(restaurant_id, count_date, product_key) "
                "DO UPDATE SET category=excluded.category, qty=excluded.qty, unit=excluded.unit, "
                "unit_cost=excluded.unit_cost, full_qty=excluded.full_qty, open_json=excluded.open_json, "
                "entered_by=excluded.entered_by, entered_at=excluded.entered_at",
                (int(restaurant_id), iso, key, p["category"], n, p["unit"], p["unit_cost"], full_q, open_j, user_id,
                 _now()))
        conn.commit()
        rows = conn.execute("SELECT category, qty, unit_cost FROM bar_count_items WHERE restaurant_id=? "
                            "AND count_date=?", (int(restaurant_id), iso)).fetchall()
        sums, items = {}, {}
        for r in rows:
            items[r["category"]] = items.get(r["category"], 0) + 1
            if r["unit_cost"] is not None:
                sums[r["category"]] = sums.get(r["category"], 0.0) + float(r["qty"]) * float(r["unit_cost"])
        # A category none of whose products is counted any more comes off,
        # when its value came from product counts.
        for c in COUNT_CATEGORIES:
            if c not in items:
                conn.execute("DELETE FROM bar_counts WHERE restaurant_id=? AND count_date=? AND category=? "
                             "AND method='items'", (int(restaurant_id), iso, c))
        conn.commit()
    finally:
        conn.close()
    values = {c: round(sums.get(c, 0.0), 2) for c in items if c in COUNT_CATEGORIES}
    if values:
        save_count(restaurant_id, iso, values, note=None, user_id=user_id, source="owner", method="items",
                   db_path=db_path, today=today)
    sh = sheet(restaurant_id, iso, dollars=False, db_path=db_path, today=today, products=prods)
    return {"ok": True, "count_date": iso, "count_date_mdy": _mdy(iso), "product_key": key, "name": p["name"],
            "category": p["category"], "qty": n, "unit": p["unit"], "unit_cost": p["unit_cost"],
            "full": full_q, "open": json.loads(open_j) if open_j else ([] if full_q is not None else None),
            "value": round(n * p["unit_cost"], 2) if (n is not None and p["unit_cost"] is not None) else None,
            "category_total": values.get(p["category"]), "counted": sh["counted"], "of": sh["of"]}
