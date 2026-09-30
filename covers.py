"""Cover counts.

Labor has always had one blind spot it names in its own prompt: a day that
ran lean on strong sales could be a good day or a short-staffed one, and
"this system has no cover-count data, so you cannot tell which it was".
Covers are the figure that tells them apart — sales per cover on a lean
day that matches the period's average is a good day; a lean day whose
sales per cover collapsed is a floor that could not serve what walked in.

Entered by hand or pasted as CSV (date,covers), and — since 9/30/26 —
filled from the POS's own guest count (source 'pos', sync_from_pos) for
each night the nightly report measured one (the DSR's sales.guests). RPOWER
carries a guest count on every ticket and the report only totals a night
when every sale ticket has one; at Simple EJ's it ran $28-47 a guest night
to night, a real count (owner, 9/30/26: "can't we just automate this from
the rpower api?"). A count someone typed or confirmed ('manual',
'pos_confirmed') is never overwritten; a POS row is refreshed when a later
version of the night's report moves the count.
"""
from datetime import date, timedelta

import models as _models
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports): the
    default path follows models.DB_PATH as it is now, not as it was when
    this module was first imported."""
    if db_path is None or db_path == DB_PATH:
        return _models.get_conn()
    return _models.get_conn(db_path)

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


# Sources a person chose; the POS never writes over them.
_PERSON_SOURCES = ("manual", "pos_confirmed")
POS_SYNC_DAYS = 35


def sync_from_pos(restaurant_id, dates=None, days=POS_SYNC_DAYS, db_path=DB_PATH) -> int:
    """Write the POS guest count (dsr_metrics sales.guests) as the night's
    covers (source 'pos') for each night in `dates`, or the last `days`,
    that has no person-entered count. Returns rows written. Never raises."""
    try:
        conn = get_conn(db_path)
        try:
            if dates:
                qs = ",".join("?" * len(dates))
                rows = conn.execute(f"SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? "
                                    f"AND metric='sales.guests' AND value > 0 AND business_date IN ({qs})",
                                    (restaurant_id, *[_d(x) for x in dates])).fetchall()
            else:
                start = (date.today() - timedelta(days=days - 1)).isoformat()
                rows = conn.execute("SELECT business_date, value FROM dsr_metrics WHERE restaurant_id=? "
                                    "AND metric='sales.guests' AND value > 0 AND business_date >= ?",
                                    (restaurant_id, start)).fetchall()
            n = 0
            for r in rows:
                guests = int(round(float(r["value"])))
                if guests <= 0 or guests > 100000:
                    continue
                cur = conn.execute(
                    "INSERT INTO covers_daily (restaurant_id, date, covers, source) VALUES (?,?,?,'pos') "
                    "ON CONFLICT(restaurant_id, date) DO UPDATE SET covers=excluded.covers, saved_at=datetime('now') "
                    f"WHERE covers_daily.source NOT IN ({','.join('?' * len(_PERSON_SOURCES))}) "
                    "AND covers_daily.covers != excluded.covers",
                    (restaurant_id, r["business_date"], guests, *_PERSON_SOURCES))
                n += cur.rowcount or 0
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception:
        return 0


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
    start = (end - timedelta(days=days - 1)).isoformat()
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT date, covers, source FROM covers_daily WHERE restaurant_id=? AND date BETWEEN ? AND ? "
                            "ORDER BY date DESC", (restaurant_id, start, end.isoformat())).fetchall()
    finally:
        conn.close()
    return [{"date": r["date"], "covers": int(r["covers"]), "source": r["source"]} for r in rows]
