"""
bar/waste.py — the bar's waste, spill and breakage log (bar fix round
10/15/26, F14 / AIX-18 step 3).

What was poured out, dropped, comped off the books or lost cleaning the
draft lines is a count the bar keeps: one row per event (`bar_waste`, created
at boot by init_bar_waste) — a restaurant, a date, a bought product, a
quantity in a unit (a bottle or can, ounces, or a whole keg), a reason, and
who entered it. Entered by the account holder and by FOOD_COST_ENTER logins
(whoever counts the bar, as the bar counts are); removed by the account
holder.

It is shown BESIDE the variance (bar.variance) and never netted into it: a
gap of 8 cans with 3 logged as breakage reads "8 short · 3 logged as
breakage", never "5 short". An owner who logs waste learns what it explains;
a log that silently lowered the gap would hide exactly what variance is for.

A financial record: kept, never pruned (DATABASE_SCHEMA.md).
"""
from datetime import date, timedelta

from bar.common import _mdy, display_name, get_conn
from bar.products import _now

REASONS = ("spill", "breakage", "comp", "line_clean", "other")
REASON_LABELS = {"spill": "Spill", "breakage": "Breakage", "comp": "Comped off the POS",
                 "line_clean": "Line cleaning", "other": "Other"}
# Each: a bottle or can as bought (a keg's worth is "keg"), ounces, a keg.
UNITS = ("each", "oz", "keg")
UNIT_LABELS = {"each": "bottle or can", "oz": "oz", "keg": "keg"}
MAX_QTY = {"each": 500.0, "oz": 4000.0, "keg": 20.0}
# An entry is for a night already over, at most this far back.
MAX_BACK_DAYS = 120
MAX_NOTE_CHARS = 300
RECENT_ROWS = 20


def init_bar_waste(db_path=None):
    """The bar's waste log. Boot only (models.init_db). Kept, never pruned."""
    conn = get_conn(db_path)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bar_waste (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            restaurant_id INTEGER NOT NULL,
            waste_date TEXT NOT NULL,
            product_key TEXT NOT NULL,
            qty REAL NOT NULL,
            unit TEXT NOT NULL,
            reason TEXT NOT NULL,
            note TEXT,
            entered_by INTEGER,
            entered_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bar_waste_rid_date ON bar_waste(restaurant_id, waste_date)")
        conn.commit()
    finally:
        conn.close()


def _day(value, today):
    try:
        d = date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        raise ValueError("Pick the date from the calendar.")
    if d > today:
        raise ValueError("Log waste for a day that's already happened.")
    if d < today - timedelta(days=MAX_BACK_DAYS):
        raise ValueError(f"Waste can be logged for the last {MAX_BACK_DAYS} days.")
    return d


def bought_products(restaurant_id, db_path=None, lines=None) -> dict:
    """{product_key: {name, category}} — the beer, liquor and wine the bar
    has bought: the products a waste entry can name (V40's rule: a key the
    invoices never carried is refused)."""
    if lines is None:
        from bar.purchases import _lines
        lines = _lines(restaurant_id, db_path)
    out = {}
    for l in lines:
        if l.get("kind") == "item" and l.get("category") in ("beer", "liquor", "wine"):
            out[l["product_key"]] = {"name": display_name(l["description"]), "category": l["category"]}
    return out


def log_waste(restaurant_id, waste_date, product_key, qty, unit, reason, note=None, user_id=None,
              today=None, db_path=None, products=None) -> dict:
    """One waste event. Raises ValueError with an owner-facing sentence."""
    today = today or date.today()
    d = _day(waste_date, today)
    key = str(product_key or "").strip()
    products = products if products is not None else bought_products(restaurant_id, db_path)
    if key not in products:
        raise ValueError("Pick a product the bar has bought.")
    u = str(unit or "").strip()
    if u not in UNITS:
        raise ValueError("Say it in bottles or cans, ounces, or kegs.")
    try:
        n = float(qty)
    except (TypeError, ValueError):
        raise ValueError("Enter how much, as a number.")
    if not 0 < n <= MAX_QTY[u]:
        raise ValueError(f"Enter between 0 and {MAX_QTY[u]:,.0f} {UNIT_LABELS[u]}{'' if u == 'oz' else 's'}.")
    r = str(reason or "").strip()
    if r not in REASONS:
        raise ValueError("Pick why: spill, breakage, comped off the POS, line cleaning or other.")
    text = (str(note).strip()[:MAX_NOTE_CHARS] or None) if note else None
    conn = get_conn(db_path)
    try:
        cur = conn.execute("INSERT INTO bar_waste (restaurant_id, waste_date, product_key, qty, unit, reason, note, "
                           "entered_by, entered_at) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id",
                           (int(restaurant_id), d.isoformat(), key, n, u, r, text, user_id, _now()))
        new_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "id": new_id, "waste_date": d.isoformat(), "waste_date_mdy": _mdy(d.isoformat()),
            "product_key": key, "product": products[key]["name"], "qty": n, "unit": u, "reason": r}


def delete_waste(restaurant_id, row_id, db_path=None) -> dict:
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT waste_date, product_key FROM bar_waste WHERE restaurant_id=? AND id=?",
                           (int(restaurant_id), int(row_id))).fetchone()
        if row:
            conn.execute("DELETE FROM bar_waste WHERE restaurant_id=? AND id=?", (int(restaurant_id), int(row_id)))
            conn.commit()
    finally:
        conn.close()
    return {"ok": bool(row), "deleted": 1 if row else 0, "id": int(row_id),
            "waste_date_mdy": _mdy(row["waste_date"]) if row else None}


def waste_rows(restaurant_id, start=None, end=None, db_path=None, limit=None) -> list:
    """[{id, waste_date, waste_date_mdy, product_key, qty, unit, reason,
    reason_label, note, entered_by}] newest first, within [start, end]."""
    sql = ("SELECT id, waste_date, product_key, qty, unit, reason, note, entered_by FROM bar_waste "
           "WHERE restaurant_id=?")
    args = [int(restaurant_id)]
    if start:
        sql += " AND waste_date>=?"
        args.append(str(start)[:10])
    if end:
        sql += " AND waste_date<=?"
        args.append(str(end)[:10])
    sql += " ORDER BY waste_date DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(sql, args).fetchall()
        except Exception:
            rows = []
    finally:
        conn.close()
    return [dict(dict(r), waste_date_mdy=_mdy(r["waste_date"]), reason_label=REASON_LABELS.get(r["reason"], r["reason"]))
            for r in rows]


def waste_block(restaurant_id, start, end, products=None, db_path=None) -> dict:
    """The payload's `bar_ops.waste`: what was logged over the variance's
    span, as counts (no dollars — a waste entry is a count): {span, entries,
    by_reason [{reason, label, entries}], recent [...], note}."""
    rows = waste_rows(restaurant_id, start, end, db_path=db_path)
    products = products or {}
    by = {}
    for r in rows:
        by[r["reason"]] = by.get(r["reason"], 0) + 1
        r["product"] = (products.get(r["product_key"]) or {}).get("name") or r["product_key"]
        r["unit_label"] = UNIT_LABELS.get(r["unit"], r["unit"])
    return {"span": {"from": start, "to": end, "from_mdy": _mdy(start), "to_mdy": _mdy(end)} if start else None,
            "entries": len(rows),
            "by_reason": [{"reason": k, "label": REASON_LABELS[k], "entries": by[k]} for k in REASONS if by.get(k)],
            "recent": rows[:RECENT_ROWS],
            "note": "Logged waste is shown beside the gap, never taken out of it."}
