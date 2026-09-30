"""service_performance — how each server, room and the kitchen performed,
measured from the POS archive (pos_archive; RPower endpoint audit, 9/29/26,
Critical #4).

Staff scoring rested on manager ratings, which stay empty until someone
rates. The POS already records, on every ticket, who served it, where, how
many guests, what they ordered and tipped, and when it opened and closed —
a measured record of each person's work. This reads it; it calls no POS.

Personnel data: every figure here is for the account holders only (Ask's
read_service_performance is a principal tool; permissions.is_principal).

Rules that keep a figure honest:
  * a server is shown with figures only past MIN_TICKETS tickets in the
    window; below it, the count alone (a slow Tuesday is not a trend);
  * a turn time counts only for a dine-in room and only between TURN_MIN and
    TURN_MAX minutes (a ticket left open overnight, or a pick-up rung and
    closed in one go, is not a table turn);
  * a tip rate reads only tickets that recorded a tip (cash tips never reach
    the POS), and says how many did;
  * kitchen minutes need the POS's fire and bump times; a POS that records
    none (Simple EJ's, 9/29/26: no kitchen screens) says so and shows none.
"""
import re
import statistics
from datetime import date, timedelta, datetime

from models import get_conn, DB_PATH

MIN_TICKETS = 20
TURN_MIN, TURN_MAX = 5.0, 300.0
MAX_DAYS = 90
# Rooms that are not tables: a pick-up counter, open bar tabs, online orders.
_NOT_DINE_IN = re.compile(r"pick\s*-?\s*up|to\s*-?\s*go|take\s*-?\s*out|\btab\b|online|delivery|curbside|drive", re.I)


def _minutes(a, b):
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


def _turn(t):
    if _NOT_DINE_IN.search(t["room_name"] or "") or t["is_bar"]:
        return None
    m = _minutes(t["opened_at"], t["closed_at"])
    return m if m is not None and TURN_MIN <= m <= TURN_MAX else None


def _window(days, today=None):
    days = max(1, min(int(days or 28), MAX_DAYS))
    end = (today or date.today()) - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def _tickets(restaurant_id, start, end, db_path):
    conn = get_conn(db_path)
    try:
        tickets = [dict(r) for r in conn.execute(
            "SELECT ticket_id, business_date, opened_at, closed_at, fired_at, bumped_at, server_id, server_name, "
            "room_name, is_bar, guest_count, entree_count, bev_count, net_sales, tip, mealtime FROM pos_tickets "
            "WHERE restaurant_id=? AND business_date>=? AND business_date<=? AND cancelled=0",
            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()]
        comps = {r[0]: float(r[1] or 0) for r in conn.execute(
            "SELECT ticket_id, SUM(COALESCE(loss_amount, 0)) FROM pos_ticket_lines WHERE restaurant_id=? AND "
            "business_date>=? AND business_date<=? AND kind='comp' GROUP BY ticket_id",
            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()}
        days = conn.execute("SELECT COUNT(*) FROM pos_archive_days WHERE restaurant_id=? AND business_date>=? "
                            "AND business_date<=?", (restaurant_id, start.isoformat(), end.isoformat())).fetchone()[0]
    finally:
        conn.close()
    return tickets, comps, days


def _figures(ts, comps):
    net = sum(t["net_sales"] for t in ts)
    covers = sum(t["guest_count"] for t in ts)
    entrees = sum(t["entree_count"] for t in ts)
    drinks = sum(t["bev_count"] for t in ts)
    tipped = [t for t in ts if (t["tip"] or 0) > 0 and (t["net_sales"] or 0) > 0]
    turns = [m for m in (_turn(t) for t in ts) if m is not None]
    return {
        "tickets": len(ts), "covers": covers, "net_sales": round(net, 2),
        "sales_per_cover": round(net / covers, 2) if covers else None,
        "check_average": round(net / len(ts), 2) if ts else None,
        "drinks_per_entree": round(drinks / entrees, 2) if entrees else None,
        "tip_rate_pct": (round(100.0 * sum(t["tip"] for t in tipped) / sum(t["net_sales"] for t in tipped), 1)
                         if tipped else None),
        "tickets_with_tip": len(tipped),
        "median_turn_minutes": round(statistics.median(turns), 0) if turns else None,
        "turns_measured": len(turns),
        "comps": round(sum(comps.get(t["ticket_id"], 0.0) for t in ts), 2),
    }


def servers(restaurant_id, days=28, db_path=DB_PATH, today=None) -> dict:
    """Each server's measured record over the window, busiest first."""
    start, end = _window(days, today)
    tickets, comps, archived = _tickets(restaurant_id, start, end, db_path)
    if not tickets:
        return {"available": False, "window": [start.isoformat(), end.isoformat()],
                "reason": ("no tickets archived for these days yet" if not archived
                           else "the POS recorded no tickets in these days")}
    by = {}
    for t in tickets:
        if t["server_id"]:
            by.setdefault(t["server_id"], []).append(t)
    rows = []
    for sid, ts in by.items():
        name = next((t["server_name"] for t in sorted(ts, key=lambda x: x["business_date"], reverse=True)
                     if t["server_name"]), None) or f"POS id {sid}"
        row = {"server": name, "tickets": len(ts), "enough": len(ts) >= MIN_TICKETS}
        if row["enough"]:
            row.update(_figures(ts, comps))
        rows.append(row)
    rows.sort(key=lambda r: (-(r.get("net_sales") or 0), -r["tickets"]))
    return {"available": True, "window": [start.isoformat(), end.isoformat()], "days_archived": archived,
            "house": _figures(tickets, comps), "servers": rows, "min_tickets": MIN_TICKETS}


def rooms(restaurant_id, days=28, db_path=DB_PATH, today=None) -> dict:
    """Tickets, covers, sales and turn time by room and by daypart."""
    start, end = _window(days, today)
    tickets, comps, archived = _tickets(restaurant_id, start, end, db_path)
    if not tickets:
        return {"available": False, "window": [start.isoformat(), end.isoformat()]}
    out = {"available": True, "window": [start.isoformat(), end.isoformat()], "rooms": [], "dayparts": []}
    for key, label in (("room_name", "rooms"), ("mealtime", "dayparts")):
        groups = {}
        for t in tickets:
            groups.setdefault(t[key] or "not recorded", []).append(t)
        for name, ts in sorted(groups.items(), key=lambda kv: -sum(t["net_sales"] for t in kv[1])):
            f = _figures(ts, comps)
            out[label].append({"name": name, "tickets": f["tickets"], "covers": f["covers"],
                               "net_sales": f["net_sales"], "check_average": f["check_average"],
                               "median_turn_minutes": f["median_turn_minutes"]})
    return out


def kitchen(restaurant_id, days=28, db_path=DB_PATH, today=None) -> dict:
    """Median minutes from fire to bump, by hour — only where the POS
    records both times."""
    start, end = _window(days, today)
    tickets, _c, _a = _tickets(restaurant_id, start, end, db_path)
    timed = [(t, _minutes(t["fired_at"], t["bumped_at"])) for t in tickets if t["fired_at"] and t["bumped_at"]]
    timed = [(t, m) for t, m in timed if m is not None and 0 < m <= 120]
    if not timed:
        return {"available": False,
                "reason": "this POS records no kitchen fire and bump times (no kitchen screens), so kitchen speed "
                          "cannot be measured"}
    by_hour = {}
    for t, m in timed:
        by_hour.setdefault(t["fired_at"][11:13], []).append(m)
    return {"available": True, "median_minutes": round(statistics.median([m for _t, m in timed]), 1),
            "tickets_timed": len(timed),
            "by_hour": [{"hour": h, "median_minutes": round(statistics.median(v), 1), "tickets": len(v)}
                        for h, v in sorted(by_hour.items())]}


def summary(restaurant_id, days=28, db_path=DB_PATH, today=None) -> dict:
    """Servers, rooms and dayparts, and the kitchen, in one read."""
    s = servers(restaurant_id, days, db_path, today)
    if not s.get("available"):
        return s
    r = rooms(restaurant_id, days, db_path, today)
    return {**s, "rooms": r.get("rooms"), "dayparts": r.get("dayparts"),
            "kitchen": kitchen(restaurant_id, days, db_path, today),
            "note": ("Measured from the POS's own tickets. A server's figures appear past "
                     f"{MIN_TICKETS} tickets; tip rate counts only tickets with a recorded (card) tip; turn "
                     "time counts dine-in tables only.")}
