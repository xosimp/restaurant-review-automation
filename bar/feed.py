"""
bar/feed.py — how current the Bar page's two feeds are: the distributor
invoices (Fintech, by email — email_intake) and the POS's drink sales (the
nightly report's `sales.cat:*`). A feed that stopped used to look like a
quiet week: invoices stopped 7/27 and the page still showed 6/30–7/27 as
"the last four weeks" (bar fix round 10/15/26, V22).

`feed_status` is the payload's `feed`; `invoices_through` is what the
`invoices` source in source_health is dated by (email_intake stamps it on
every intake and the daily feed watch).
"""
from datetime import date, timedelta

from bar.common import _mdy, get_conn

# Invoices more than this many days behind today: the feed has stopped
# (the judgement floor's "invoices fresh", PLAN decision 10, reads the same).
FEED_STALE_DAYS = 7


def _today(today):
    if today is None:
        return date.today()
    return today if isinstance(today, date) else date.fromisoformat(str(today)[:10])


def invoices_through(restaurant_id, today=None, db_path=None, conn=None):
    """The newest invoice date on file (ISO), never one past tomorrow (a
    future date is a typo, not data); None with no dated invoice."""
    latest_ok = (_today(today) + timedelta(days=1)).isoformat()
    own = conn is None
    conn = conn or get_conn(db_path)
    try:
        row = conn.execute("SELECT MAX(invoice_date) AS d FROM fintech_invoices WHERE restaurant_id=? "
                           "AND invoice_date IS NOT NULL AND invoice_date <= ?",
                           (int(restaurant_id), latest_ok)).fetchone()
    finally:
        if own:
            conn.close()
    return str(row["d"])[:10] if row and row["d"] else None


def feed_status(restaurant_id, today=None, db_path=None) -> dict:
    """{invoices_through, invoices_through_mdy, sales_through,
    sales_through_mdy, last_file, last_file_mdy, stale_days}: the newest
    invoice date, the newest night with drink sales, the restaurant-local
    day the newest Fintech file arrived, and — when the invoices are more
    than FEED_STALE_DAYS behind today — how many days behind (else None)."""
    from time_utils import local_iso
    day = _today(today)
    conn = get_conn(db_path)
    try:
        inv = invoices_through(restaurant_id, day, conn=conn)
        # A range on the metric (not LIKE) so the (restaurant_id, metric,
        # business_date) index is used on SQLite and Postgres alike.
        s = conn.execute("SELECT MAX(business_date) AS d FROM dsr_metrics WHERE restaurant_id=? "
                         "AND metric >= 'sales.cat:' AND metric < 'sales.cat;' AND value IS NOT NULL "
                         "AND business_date <= ?", (int(restaurant_id), day.isoformat())).fetchone()
        f = conn.execute("SELECT MAX(created_at) AS at FROM fintech_files WHERE restaurant_id=? "
                         "AND status<>'purged'",
                         (int(restaurant_id),)).fetchone()
        tz = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (int(restaurant_id),)).fetchone()
    finally:
        conn.close()
    sales = str(s["d"])[:10] if s and s["d"] else None
    last_file = local_iso(f["at"], (tz["timezone"] if tz else None) or None) if f and f["at"] else None
    behind = (day - date.fromisoformat(inv)).days if inv else None
    return {"invoices_through": inv, "invoices_through_mdy": _mdy(inv) if inv else None,
            "sales_through": sales, "sales_through_mdy": _mdy(sales) if sales else None,
            "last_file": last_file, "last_file_mdy": _mdy(last_file) if last_file else None,
            "stale_days": behind if behind is not None and behind > FEED_STALE_DAYS else None}
