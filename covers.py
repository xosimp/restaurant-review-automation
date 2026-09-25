"""Cover counts.

Labor has always had one blind spot it names in its own prompt: a day that
ran lean on strong sales could be a good day or a short-staffed one, and
"this system has no cover-count data, so you cannot tell which it was".
Covers are the figure that tells them apart — sales per cover on a lean
day that matches the period's average is a good day; a lean day whose
sales per cover collapsed is a floor that could not serve what walked in.

Entered by hand or pasted as CSV (date,covers). The POS integrations do
not carry a guest count reliably enough to write here, so this table is
only ever what someone typed — and the analysis says when it is absent.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH

MAX_ROWS = 400


def _d(s):
    return date.fromisoformat(str(s)[:10]).isoformat()


def save(restaurant_id, rows, source="manual", db_path=DB_PATH):
    """rows: [{date, covers}]. Returns {written, skipped, errors}."""
    written = skipped = 0
    errors = []
    conn = get_conn(db_path)
    try:
        for r in list(rows or [])[:MAX_ROWS]:
            try:
                day = _d(r.get("date"))
                n = int(round(float(r.get("covers"))))
            except (TypeError, ValueError, AttributeError):
                skipped += 1
                errors.append(f"{(r or {}).get('date', '?')}: not a date and a whole number")
                continue
            if n < 0 or n > 100000:
                skipped += 1
                errors.append(f"{day}: {n} is not a cover count")
                continue
            conn.execute(
                "INSERT INTO covers_daily (restaurant_id, date, covers, source) VALUES (?,?,?,?) "
                "ON CONFLICT(restaurant_id, date) DO UPDATE SET covers=excluded.covers, source=excluded.source, "
                "saved_at=datetime('now')", (restaurant_id, day, n, source))
            written += 1
        conn.commit()
    finally:
        conn.close()
    return {"written": written, "skipped": skipped, "errors": errors[:10]}


def parse_csv(text):
    """'date,covers' lines → rows. A header line is skipped; blank lines ignored."""
    rows = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.replace("\t", ",").split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        if parts[0].lower() in ("date", "day"):
            continue
        rows.append({"date": parts[0], "covers": parts[1]})
    return rows


def by_date(restaurant_id, start=None, end=None, db_path=DB_PATH):
    """{iso date: covers} inside [start, end] (default: last 35 days)."""
    end = date.fromisoformat(end) if end else date.today()
    start = date.fromisoformat(start) if start else end - timedelta(days=34)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT date, covers FROM covers_daily WHERE restaurant_id=? AND date BETWEEN ? AND ?",
                            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()
    finally:
        conn.close()
    return {r["date"]: int(r["covers"]) for r in rows}


def pos_offers(restaurant_id, days=14, db_path=DB_PATH):
    """Nights the POS counted guests (the DSR's sales.guests) that have no
    cover count yet, newest first: [{"date", "guests"}] (friction audit
    U2-18). An OFFER, not a write - the module docstring's caveat stands:
    a POS guest count is not reliable enough to become a cover count on its
    own, so it is shown for the owner to accept or correct."""
    end = date.today()
    start = end - timedelta(days=days - 1)
    have = by_date(restaurant_id, start.isoformat(), end.isoformat(), db_path=db_path)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? "
                            "AND metric='sales.guests' AND value IS NOT NULL AND value > 0 "
                            "AND business_date BETWEEN ? AND ? ORDER BY business_date DESC",
                            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return [{"date": r["business_date"], "guests": int(round(float(r["value"])))}
            for r in rows if r["business_date"] not in have]


def recent(restaurant_id, days=28, db_path=DB_PATH):
    end = date.today()
    m = by_date(restaurant_id, (end - timedelta(days=days - 1)).isoformat(), end.isoformat(), db_path=db_path)
    return [{"date": k, "covers": v} for k, v in sorted(m.items(), reverse=True)]
