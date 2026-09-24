"""
loss_detection.py — comps, voids and refunds, and when they stop looking normal.

Over-comping, void abuse and refund fraud are among the ways a restaurant
leaks money without any single transaction looking wrong. The POS records
every one of them and nothing in this product read them: RPOWER's sales types
already mark comps and refunds (rpower._counts_as_revenue filters them OUT of
net sales), so the evidence was being discarded on the way in.

Two signals, both deliberately phrased as "worth reviewing", never as an
accusation:

  * a kind (comps, voids, refunds) running well above its own baseline;
  * one approving manager accounting for most of a kind's dollars.

Attribution is to the APPROVER (RPOWER's mgr_mid / voidmgr_mid), not the
server. That is the loss-prevention convention and the fair one — a comp is a
manager's decision — and it is what the data actually says.

Stored daily in pos_loss_daily, per RPOWER's request that integrators archive
rather than re-query.
"""
import json
from datetime import date, timedelta

from models import get_conn, DB_PATH

# A kind is "running high" only past BOTH of these: a multiple of its own
# baseline AND an absolute dollar floor, so a quiet week going from $12 to
# $30 of comps does not raise an alarm.
SPIKE_MULTIPLE = 2.0
MIN_WEEK_DOLLARS = 75.0
MIN_EVENTS = 4
# One approver is "concentrated" past this share of a kind's dollars, over
# enough events that the share means something.
APPROVER_SHARE = 0.60
MIN_APPROVER_EVENTS = 5
BASELINE_WEEKS = 8

KINDS = ("comp", "void", "refund")

ALTERNATIVES = {
    "comp": "a service-recovery push, a VIP or friends-and-family night, or a promotion run as comps",
    "void": "a new server still learning the POS, a menu change causing re-rings, or a busy night's corrections",
    "refund": "a delivery-platform dispute, a batch of genuine complaints, or a card-reader error",
}


def sync(restaurant_id, days=14, db_path=DB_PATH):
    """Pull comp/void/refund lines from the POS and store daily totals.

    Returns {"ok", "days", "provider"} or {"ok": False, "reason"}. A POS that
    cannot report this is a normal state, not an error.
    """
    import pos
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    try:
        lines, provider = pos.fetch_loss_lines(restaurant_id, start, end)
    except pos.POSCapabilityError as e:
        return {"ok": False, "reason": str(e)}
    daily = {}
    for ln in lines:
        if not ln.get("business_date") or ln.get("kind") not in KINDS:
            continue
        key = (ln["business_date"], ln["kind"])
        d = daily.setdefault(key, {"amount": 0.0, "events": 0, "by_approver": {}})
        d["amount"] += float(ln.get("amount") or 0)
        d["events"] += 1
        who = ln.get("approver") or "unrecorded"
        a = d["by_approver"].setdefault(who, {"amount": 0.0, "events": 0})
        a["amount"] += float(ln.get("amount") or 0)
        a["events"] += 1
    conn = get_conn(db_path)
    try:
        # Replace the whole window: a day RPOWER re-posts must not keep a
        # stale row, and a day with no losses must read as zero only when the
        # POS was actually asked about it — which is exactly this window.
        conn.execute("DELETE FROM pos_loss_daily WHERE restaurant_id=? AND business_date>=? "
                     "AND business_date<=?", (restaurant_id, start.isoformat(), end.isoformat()))
        # Every day in the window gets a row for every kind, zero included.
        # Absence has to mean "never asked", and a zero row "asked, none" —
        # otherwise the baseline below counts only days that HAD a comp, a
        # quiet restaurant's weekly baseline is inflated by every day it
        # did nothing, and a genuine spike is measured against it and missed.
        d0 = start
        while d0 <= end:
            for kind in KINDS:
                daily.setdefault((d0.isoformat(), kind), {"amount": 0.0, "events": 0, "by_approver": {}})
            d0 += timedelta(days=1)
        for (day, kind), d in daily.items():
            conn.execute(
                "INSERT INTO pos_loss_daily (restaurant_id, business_date, kind, amount, events, "
                "by_approver, provider) VALUES (?,?,?,?,?,?,?)",
                (restaurant_id, day, kind, round(d["amount"], 2), d["events"],
                 json.dumps({k: {"amount": round(v["amount"], 2), "events": v["events"]}
                             for k, v in d["by_approver"].items()}), provider))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "days": days, "provider": provider, "lines": len(lines)}


def _window(restaurant_id, start, end, db_path):
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT business_date, kind, amount, events, by_approver FROM pos_loss_daily "
            "WHERE restaurant_id=? AND business_date>=? AND business_date<=?",
            (restaurant_id, start.isoformat(), end.isoformat())).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def signals(restaurant_id, today=None, db_path=DB_PATH):
    """The last 7 days of comps/voids/refunds against their own baseline."""
    today = today or date.today()
    week_end = today - timedelta(days=1)
    week_start = week_end - timedelta(days=6)
    base_end = week_start - timedelta(days=1)
    base_start = base_end - timedelta(weeks=BASELINE_WEEKS) + timedelta(days=1)

    recent = _window(restaurant_id, week_start, week_end, db_path)
    history = _window(restaurant_id, base_start, base_end, db_path)
    if not recent and not history:
        return {"available": False,
                "reason": "no comp/void data synced — only RPOWER reports it today"}

    out = []
    for kind in KINDS:
        wk = [r for r in recent if r["kind"] == kind]
        amount = round(sum(r["amount"] for r in wk), 2)
        events = sum(r["events"] for r in wk)
        hist = [r for r in history if r["kind"] == kind]
        # Weeks the POS was actually asked about (zero rows included — see
        # sync), so a restaurant three weeks into syncing is not compared
        # against an eight-week baseline that is five-eighths empty.
        weeks_seen = len({r["business_date"] for r in hist}) / 7.0
        baseline = (round(sum(r["amount"] for r in hist) / weeks_seen, 2)
                    if hist and weeks_seen >= 1.0 else None)
        entry = {"kind": kind, "week_amount": amount, "week_events": events,
                 "weekly_baseline": baseline, "flags": []}
        # `baseline is not None`, not truthiness: a measured zero baseline is
        # the strongest possible contrast (weeks of no comps, then a real
        # week of them), and a truthiness test silently skipped exactly it.
        if baseline is not None and amount >= MIN_WEEK_DOLLARS and events >= MIN_EVENTS \
                and amount >= baseline * SPIKE_MULTIPLE:
            vs = (f"{amount / baseline:.1f}× their usual week (${amount:,.0f} vs about ${baseline:,.0f})"
                  if baseline > 0 else f"${amount:,.0f} after {weeks_seen:.0f} weeks with none")
            entry["flags"].append({
                "type": "spike",
                # Its rec_ledger key: the week and the kind, the same shape
                # the concentration issue files under, so it is owner-only
                # everywhere decisions.py reads it (the "loss:" prefix).
                "key": f"loss:{week_start.isoformat()}:{kind}:spike",
                "headline": f"{kind.capitalize()}s ran {vs}",
                "alternative": ALTERNATIVES[kind]})
        # Who approved them.
        who = {}
        for r in wk:
            try:
                for k, v in json.loads(r["by_approver"] or "{}").items():
                    w = who.setdefault(k, {"amount": 0.0, "events": 0})
                    w["amount"] += v["amount"]
                    w["events"] += v["events"]
            except Exception:
                continue
        if amount >= MIN_WEEK_DOLLARS and who:
            top, stats = max(who.items(), key=lambda kv: kv[1]["amount"])
            share = stats["amount"] / amount if amount else 0
            if top != "unrecorded" and share >= APPROVER_SHARE and stats["events"] >= MIN_APPROVER_EVENTS \
                    and len(who) > 1:
                entry["flags"].append({
                    "type": "concentration", "approver": top,
                    # The key strategy_jobs._loss_flags_to_issues files the
                    # issue under: resolving the issue answers this flag.
                    "key": f"loss:{week_start.isoformat()}:{kind}:{top}",
                    "headline": f"One manager (POS id {top}) approved {share:.0%} of this week's "
                                f"{kind} dollars ({stats['events']} of {events})",
                    "alternative": "they may simply have worked the busiest shifts, or be the "
                                   "manager assigned to handle guest recovery"})
        out.append(entry)
    return {"available": True, "week": [week_start.isoformat(), week_end.isoformat()],
            "kinds": out, "flagged": [f for e in out for f in e["flags"]],
            "note": ("Amounts are as reported by the POS. A pattern worth reviewing, not a "
                     "finding of wrongdoing — check the tickets before drawing a conclusion.")}
