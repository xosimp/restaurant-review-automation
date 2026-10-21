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
  * a check is pos_checks' — a ticket the POS did not cancel with an item
    sold on it — so the average check and covers here are the nightly
    report's (Audit #23 A2-04); net still counts every ticket;
  * a tip rate reads the PAYMENT records' tips (pos_checks, A2-06) over the
    net of the checks that carried one (cash tips never reach the POS), and
    says how many did;
  * the window ends on the restaurant's last complete night
    (pos.local_complete_through), never the server's date (A2-19);
  * kitchen minutes need the POS's fire and bump times; a POS that records
    none (Simple EJ's, 9/29/26: no kitchen screens) says so and shows none.
"""
import re
import statistics
from datetime import date, timedelta, datetime

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports): the
    import-time binding kept whichever test's redirect was in force when
    this module was first imported, so a later caller read another
    database (found by the staffing estimates, scheduler audit 10/10/26)."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()

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


def _window(days, today=None, restaurant_id=None):
    """(start, end): the `days` complete nights to the restaurant's last
    complete one (pos.local_complete_through — Audit #23 A2-19: on a UTC host
    after 7pm Central, date.today() ended the window on tonight, still
    trading and not archived, so "28 days" read 27). `today` (tests, a
    caller that knows it) ends it on the day before."""
    days = max(1, min(int(days or 28), MAX_DAYS))
    if today is not None:
        end = today - timedelta(days=1)
    elif restaurant_id is not None:
        import pos
        end = pos.local_complete_through(restaurant_id)
    else:
        end = date.today() - timedelta(days=1)
    return end - timedelta(days=days - 1), end


def _tickets(restaurant_id, start, end, db_path):
    """(tickets, comps by ticket, days archived). Each ticket carries
    `is_check` (pos_checks) and `pay_tip`, the tips its payments recorded —
    the one tip source (A2-06)."""
    import pos_checks
    conn = get_conn(db_path)
    try:
        tickets = [dict(r) for r in conn.execute(
            "SELECT t.ticket_id, t.business_date, t.opened_at, t.closed_at, t.fired_at, t.bumped_at, t.server_id, "
            "t.server_name, t.room_name, t.is_bar, t.guest_count, t.entree_count, t.bev_count, t.net_sales, t.tip, "
            f"t.mealtime, {pos_checks.IS_CHECK_SQL} AS is_check FROM pos_tickets t "
            "WHERE t.restaurant_id=? AND t.business_date>=? AND t.business_date<=? AND t.cancelled=0",
            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()]
        tips = {}
        for r in conn.execute(
                "SELECT business_date, ticket_id, SUM(tip) FROM pos_payments WHERE restaurant_id=? AND "
                "business_date>=? AND business_date<=? AND tip > 0 GROUP BY business_date, ticket_id",
                (restaurant_id, start.isoformat(), end.isoformat())).fetchall():
            tips[(str(r[0])[:10], str(r[1]))] = float(r[2] or 0)
        for t in tickets:
            t["pay_tip"] = tips.get((str(t["business_date"])[:10], str(t["ticket_id"])), 0.0)
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
    """One group's record: net over every ticket; checks, covers, average
    check and drinks per entree over the CHECKS (pos_checks); tip % from the
    payments' tips over the net of the checks that carried one."""
    import pos_checks
    checks = [t for t in ts if pos_checks.is_check(t)]
    net = sum(t["net_sales"] for t in ts)
    covers = sum(t["guest_count"] for t in checks)
    entrees = sum(t["entree_count"] for t in checks)
    drinks = sum(t["bev_count"] for t in checks)
    tip_pct, tipped = pos_checks.tip_rate(ts, {str(t["ticket_id"]): t.get("pay_tip") or 0.0 for t in ts})
    turns = [m for m in (_turn(t) for t in ts) if m is not None]
    return {
        "tickets": len(ts), "checks": len(checks), "covers": covers, "net_sales": round(net, 2),
        "sales_per_cover": round(net / covers, 2) if covers else None,
        "check_average": round(net / len(checks), 2) if checks else None,
        "drinks_per_entree": round(drinks / entrees, 2) if entrees else None,
        "tip_rate_pct": tip_pct,
        "tickets_with_tip": tipped,
        "median_turn_minutes": round(statistics.median(turns), 0) if turns else None,
        "turns_measured": len(turns),
        "comps": round(sum(comps.get(t["ticket_id"], 0.0) for t in ts), 2),
    }


def servers(restaurant_id, days=28, db_path=DB_PATH, today=None) -> dict:
    """Each server's measured record over the window, busiest first."""
    start, end = _window(days, today, restaurant_id)
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
    start, end = _window(days, today, restaurant_id)
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
    start, end = _window(days, today, restaurant_id)
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


MIN_SHIFTS = 8          # a person's pay-and-tips figures appear past this many shifts


def pay_and_tips(restaurant_id, days=28, db_path=DB_PATH, today=None) -> dict:
    """What the POS's payroll engine paid, and the tips it recorded, by role
    and by person (RPower endpoint audit, 9/29/26, High ROI #6): hours, pay,
    overtime pay, tips, and pay and tips per hour worked. Breaks and punch
    edits are reported only where the POS records them — a POS that records
    none says so rather than showing a clean zero."""
    start, end = _window(days, today, restaurant_id)
    conn = get_conn(db_path)
    try:
        punches = [dict(r) for r in conn.execute(
            "SELECT employee_id, employee_name, role, reg_hours, ot_hours, dt_hours, pay, ot_pay, tips, "
            "meal_minutes, rest_minutes, edited_at FROM pos_punches WHERE restaurant_id=? AND business_date>=? "
            "AND business_date<=? AND is_station=0 AND clock_out IS NOT NULL",
            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()]
    finally:
        conn.close()
    if not punches:
        return {"available": False, "reason": "no time punches archived for these days yet"}

    def _agg(ps):
        hours = sum(p["reg_hours"] + p["ot_hours"] + p["dt_hours"] for p in ps)
        pay, tips = sum(p["pay"] for p in ps), sum(p["tips"] for p in ps)
        return {"shifts": len(ps), "hours": round(hours, 1), "pay": round(pay, 2),
                "overtime_pay": round(sum(p["ot_pay"] for p in ps), 2), "tips": round(tips, 2),
                "pay_per_hour": round(pay / hours, 2) if hours else None,
                "tips_per_hour": round(tips / hours, 2) if hours else None,
                "earned_per_hour": round((pay + tips) / hours, 2) if hours else None}
    by_role, by_person = {}, {}
    for p in punches:
        by_role.setdefault(p["role"] or "not recorded", []).append(p)
        if p["employee_id"]:
            by_person.setdefault(p["employee_id"], []).append(p)
    people = []
    for eid, ps in by_person.items():
        name = next((x["employee_name"] for x in ps if x["employee_name"]), None) or f"POS id {eid}"
        row = {"name": name, "shifts": len(ps), "enough": len(ps) >= MIN_SHIFTS}
        if row["enough"]:
            row.update(_agg(ps))
        people.append(row)
    people.sort(key=lambda r: -(r.get("hours") or 0))
    breaks = sum(1 for p in punches if (p["meal_minutes"] or 0) > 0 or (p["rest_minutes"] or 0) > 0)
    edits = sum(1 for p in punches if p["edited_at"])
    return {"available": True, "window": [start.isoformat(), end.isoformat()], "house": _agg(punches),
            "roles": sorted(({"role": k, **_agg(v)} for k, v in by_role.items()), key=lambda r: -r["hours"]),
            "people": people, "min_shifts": MIN_SHIFTS,
            "tips_note": ("Tips are those the POS recorded on each punch; tips shared another way (a pool, "
                          "cash handed out) are not included, so a role can read low."),
            "breaks": ({"recorded": breaks} if breaks else
                       {"recorded": 0, "note": "the POS recorded no meal or rest breaks on these punches"}),
            "edited_punches": ({"count": edits} if edits else
                               {"count": 0, "note": "the POS recorded no punch edits in these days"})}


def payments(restaurant_id, days=28, db_path=DB_PATH, today=None) -> dict:
    """How guests paid, and the cash paid out of the drawer (High ROI #9,
    #10): tender mix by method, cash vs card, card tips and their rate; each
    payout and pay-in category with who approved it. No card-fee figure: the
    processor's rate is not on file, and a guessed rate would be a guess."""
    start, end = _window(days, today, restaurant_id)
    conn = get_conn(db_path)
    try:
        # A payment on a cancelled ticket is not money taken (A2-11); each
        # carries its check's net, the tip rate's one denominator (A2-06).
        import pos_checks
        pays = [dict(r) for r in conn.execute(
            "SELECT p.method, p.is_cash, p.is_card, p.amount, p.tip, p.business_date, p.ticket_id, "
            "(SELECT t.net_sales FROM pos_tickets t WHERE t.restaurant_id=p.restaurant_id AND t.provider=p.provider "
            "AND t.business_date=p.business_date AND t.ticket_id=p.ticket_id) AS check_net "
            "FROM pos_payments p WHERE p.restaurant_id=? AND p.business_date>=? AND p.business_date<=? "
            f"AND {pos_checks.PAYMENT_LIVE_SQL}",
            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()]
        outs = [dict(r) for r in conn.execute(
            "SELECT category, is_payin, amount, manager_name, manager_id FROM pos_payouts WHERE restaurant_id=? AND "
            "business_date>=? AND business_date<=?", (restaurant_id, start.isoformat(), end.isoformat())).fetchall()]
    finally:
        conn.close()
    if not pays:
        return {"available": False, "reason": "no payments archived for these days yet"}
    total = sum(p["amount"] for p in pays)
    card = [p for p in pays if p["is_card"]]
    card_amt = sum(p["amount"] for p in card)
    by = {}
    for p in pays:
        b = by.setdefault(p["method"] or "not recorded", {"payments": 0, "amount": 0.0})
        b["payments"] += 1
        b["amount"] += p["amount"]
    out = {"available": True, "window": [start.isoformat(), end.isoformat()],
           "total_paid": round(total, 2), "card_paid": round(card_amt, 2),
           "cash_paid": round(sum(p["amount"] for p in pays if p["is_cash"]), 2),
           "card_share_pct": round(100.0 * card_amt / total, 1) if total else None,
           "card_tips": round(sum(p["tip"] for p in card), 2),
           "card_tip_rate_pct": _card_tip_rate(card),
           "card_tip_basis": "card tips over the net sales of the checks they were left on (before tax)",
           "by_method": sorted(({"method": k, "payments": v["payments"], "amount": round(v["amount"], 2),
                                 "share_pct": round(100.0 * v["amount"] / total, 1) if total else None}
                                for k, v in by.items()), key=lambda r: -r["amount"])}
    if not outs:
        out["payouts"] = {"recorded": 0, "note": "no cash payouts or pay-ins were recorded in the POS in these days"}
    else:
        cats = {}
        for p in outs:
            key = (p["category"] or "not recorded", "pay-in" if p["is_payin"] else "payout")
            c = cats.setdefault(key, {"count": 0, "amount": 0.0, "by": {}})
            c["count"] += 1
            c["amount"] += p["amount"]
            who = p["manager_name"] or (f"POS id {p['manager_id']}" if p["manager_id"] else "not recorded")
            c["by"][who] = round(c["by"].get(who, 0.0) + p["amount"], 2)
        out["payouts"] = {"recorded": len(outs), "categories": [
            {"category": k[0], "type": k[1], "count": v["count"], "amount": round(v["amount"], 2), "by": v["by"]}
            for k, v in sorted(cats.items(), key=lambda kv: -abs(kv[1]["amount"]))]}
    return out


def _card_tip_rate(card):
    """Card tips over the net of the checks they were left on, before tax
    (A2-06: the house tip rate's denominator; it was the paid amount, tax
    included, beside a tip rate on net). None without a tipped check."""
    tips, nets = 0.0, {}
    for p in card:
        if (p.get("tip") or 0) > 0 and p.get("check_net") and float(p["check_net"]) > 0:
            tips += float(p["tip"])
            nets[(str(p.get("business_date"))[:10], str(p.get("ticket_id")))] = float(p["check_net"])
    net = sum(nets.values())
    return round(100.0 * tips / net, 1) if net > 0 else None


def summary(restaurant_id, days=28, db_path=DB_PATH, today=None) -> dict:
    """Servers, rooms and dayparts, and the kitchen, in one read."""
    s = servers(restaurant_id, days, db_path, today)
    if not s.get("available"):
        return s
    r = rooms(restaurant_id, days, db_path, today)
    return {**s, "rooms": r.get("rooms"), "dayparts": r.get("dayparts"),
            "kitchen": kitchen(restaurant_id, days, db_path, today),
            "pay_and_tips": pay_and_tips(restaurant_id, days, db_path, today),
            "payments": payments(restaurant_id, days, db_path, today),
            "note": ("Measured from the POS's own tickets. A server's figures appear past "
                     f"{MIN_TICKETS} tickets; checks and covers count tickets with an item sold on them; tip rate "
                     "is the card tips on the payments over the net of the checks that had one; turn time counts "
                     "dine-in tables only.")}
