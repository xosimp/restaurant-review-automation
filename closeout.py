"""
closeout.py — the 60 seconds at the end of the night that the data can't see.

Every number this product reads about a service arrives the next morning:
the POS syncs at 3am, reviews land days later, labor is settled after the
pay period. So the one account of what actually happened tonight is the
person who was standing in it, and nothing asked them.

Four questions, none of them typing-heavy, all optional:

  * what went well            (the thing worth repeating)
  * what went wrong           (the thing worth fixing)
  * what you ran out of       (86s — tomorrow's order, in tonight's words)
  * who didn't make it        (callouts — tomorrow's schedule problem)

It is a HANDOFF, not a report: it is written by whoever closed and read by
whoever opens, and it appears in the next morning's brief. Nothing here is
scored, ranked or fed to a model — an owner reading their GM's own sentence
is the point.
"""
from datetime import date, timedelta

from models import get_conn, DB_PATH

FIELDS = ("went_well", "went_wrong", "eighty_sixed", "callouts")
MAX_LEN = 1000


def business_date_for(restaurant, now_local=None):
    """The service a close-out belongs to. A close-out filed at 1am is about
    yesterday's service — restaurants close after midnight, and filing it
    under the new date would put it in the wrong morning's brief."""
    from time_utils import restaurant_now
    local = now_local or restaurant_now(restaurant, naive=True)
    return (local.date() - timedelta(days=1)) if local.hour < 5 else local.date()


def save(restaurant_id, fields, user_id=None, submitted_by=None, business_date=None,
         db_path=DB_PATH, restaurant=None):
    """Write tonight's close-out. Re-filing the same night replaces it —
    a manager correcting what they typed is not a second close-out."""
    from models import get_restaurant
    restaurant = restaurant or get_restaurant(restaurant_id)
    day = (business_date or business_date_for(restaurant)).isoformat() \
        if not isinstance(business_date, str) else business_date
    values = {k: (str(fields.get(k) or "").strip()[:MAX_LEN] or None) for k in FIELDS}
    if not any(values.values()):
        raise ValueError("Write at least one line — otherwise there is nothing to hand over.")
    conn = get_conn(db_path)
    try:
        conn.execute(
            "INSERT INTO close_outs (restaurant_id, business_date, submitted_by, user_id, "
            "went_well, went_wrong, eighty_sixed, callouts) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(restaurant_id, business_date) DO UPDATE SET "
            "submitted_by=excluded.submitted_by, user_id=excluded.user_id, "
            "went_well=excluded.went_well, went_wrong=excluded.went_wrong, "
            "eighty_sixed=excluded.eighty_sixed, callouts=excluded.callouts, "
            "created_at=datetime('now')",
            (restaurant_id, day, submitted_by, user_id, values["went_well"],
             values["went_wrong"], values["eighty_sixed"], values["callouts"]))
        conn.commit()
    finally:
        conn.close()
    return get(restaurant_id, day, db_path=db_path)


def get(restaurant_id, business_date, db_path=DB_PATH):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM close_outs WHERE restaurant_id=? AND business_date=?",
                           (restaurant_id, str(business_date))).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def latest(restaurant_id, before=None, db_path=DB_PATH):
    """The most recent close-out on or before `before` (default today)."""
    before = str(before or date.today())
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM close_outs WHERE restaurant_id=? AND business_date<=? "
                           "ORDER BY business_date DESC LIMIT 1",
                           (restaurant_id, before)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def summarise(entry) -> str:
    """One line for the morning brief, in the closer's own words."""
    if not entry:
        return ""
    who = entry.get("submitted_by") or "Last night"
    bits = []
    if entry.get("went_wrong"):
        bits.append(entry["went_wrong"])
    if entry.get("eighty_sixed"):
        bits.append(f"86'd: {entry['eighty_sixed']}")
    if entry.get("callouts"):
        bits.append(f"callouts: {entry['callouts']}")
    if not bits and entry.get("went_well"):
        bits.append(entry["went_well"])
    return f"{who} at close — " + " · ".join(bits) if bits else ""
