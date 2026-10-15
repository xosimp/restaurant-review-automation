"""
bar/comps.py — comps and voids at the bar, by bartender shift (bar fix round
10/15/26, F12 / CMP §5 item 5; Decision 9). OWNER-ONLY: the account holder
alone sees it (for_viewer fills it at the route; every other login gets
`{"owner_only": true}` and nothing else).

It follows loss_detection's rules and wording exactly — its thresholds
(SPIKE_MULTIPLE, MIN_WEEK_DOLLARS, MIN_EVENTS), its "worth reviewing, not a
finding of wrongdoing" note, and an innocent explanation beside every flag.
It is never an accusation:

  * the measure is the bar's comps and voids as a share of the bar's sales
    on the SHIFTS a bartender was scheduled — everyone on those shifts
    shares them; a comp is a manager's decision, and the approving manager
    is named as loss_detection names them;
  * a bartender's rate is said only past MIN_SHIFTS shifts, against the
    bar's own baseline over at least MIN_SHIFTS other shifts;
  * game nights (event_intel's followed games) are shown apart and never
    in a bartender's rate or the baseline;
  * without a published schedule over the span the by-shift read is ABSENT
    (`shifts.available: false` with why), never zero.

The bar's lines: lines on bar tickets (an is_bar room or a "Bar" profit
center); a store that marks no ticket as the bar's falls back to the lines
of its beer, liquor and wine categories (the bar mapping), and says so.
"""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from bar.common import POUR_CATEGORIES, _mdy, get_conn

SPAN_DAYS = 56
MIN_SHIFTS = 6
_BARTENDER_RE = re.compile(r"bartend|\bbar\b|mixolog", re.I)
_NOT_BARTENDER_RE = re.compile(r"bar\s*back", re.I)
_BAR_CENTER_RE = re.compile(r"\bbar\b", re.I)


def _rules():
    import loss_detection as ld
    return ld.SPIKE_MULTIPLE, ld.MIN_WEEK_DOLLARS, ld.MIN_EVENTS


def _note():
    return ("Amounts are as reported by the POS. A pattern worth reviewing, not a finding of wrongdoing — check "
            "the tickets before drawing a conclusion. Every comp and void on a shift is shared by everyone on it.")


ALTERNATIVE = ("busier or event shifts, guest recovery a manager asked for, a new menu's re-rings, or simply who "
               "else was working — everyone on those shifts shares these")


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _minute(business_date, stamp):
    """Minutes on the business date's timeline (past 1440 after midnight)."""
    try:
        dt = datetime.fromisoformat(str(stamp)[:19])
        base = date.fromisoformat(str(business_date)[:10])
    except (TypeError, ValueError):
        return None
    return (dt.date() - base).days * 1440 + dt.hour * 60 + dt.minute


def published_bartender_shifts(restaurant_id, start, end, db_path=None) -> list:
    """[{date, employee, role, start_m, end_m}] — the bartenders on the
    PUBLISHED schedule (each week's live copy, models.live_where) between
    start and end. [] when nothing was published."""
    import models as _m
    import schedule_shifts
    from schedule_time import span as shift_span
    lo = (date.fromisoformat(start) - timedelta(days=6)).isoformat()
    conn = get_conn(db_path)
    try:
        hist = conn.execute(f"SELECT h.id FROM schedule_history h WHERE h.restaurant_id=? AND {_m.live_where('h')} "
                            "AND h.week_start>=? AND h.week_start<=?", (int(restaurant_id), lo, end)).fetchall()
        by_week = schedule_shifts.rows_by_week([h["id"] for h in hist], "live", conn=conn)
    finally:
        conn.close()
    out = []
    for rows in by_week.values():
        for r in rows or []:
            d = str(r.get("date") or "")[:10]
            role = str(r.get("role") or "")
            if not (start <= d <= end) or not (r.get("employee") or "").strip():
                continue
            if not _BARTENDER_RE.search(role) or _NOT_BARTENDER_RE.search(role):
                continue
            sp = shift_span(r)
            if sp is None:
                continue
            out.append({"date": d, "employee": " ".join(str(r["employee"]).split()), "role": role,
                        "start_m": sp[0], "end_m": sp[1]})
    return out


def _game_nights(restaurant_id, start, end, db_path=None) -> set:
    try:
        from event_intel import store
        followed = store.follows(restaurant_id, db_path=db_path) if db_path else store.follows(restaurant_id)
        ids = [f["series_id"] for f in followed]
        if not ids:
            return set()
        kw = {"db_path": db_path} if db_path else {}
        gone = store.dismissed(restaurant_id, **kw)
        return {str(e["event_date"])[:10] for e in store.events_for(ids, start, end, **kw)
                if e.get("status") not in ("cancelled", "postponed") and e["id"] not in gone}
    except Exception:
        return set()


def comps_block(restaurant_id, today=None, db_path=None) -> dict:
    """{available, span, basis, totals {sales_usd, comp_usd, void_usd,
    rate_pct, events}, game_nights, shifts {available, why | baseline_rate_pct,
    by_bartender [...], outside}, flagged [...], note}."""
    today = today or date.today()
    spike, min_dollars, min_events = _rules()
    conn = get_conn(db_path)
    try:
        last = conn.execute("SELECT MAX(business_date) FROM pos_archive_days WHERE restaurant_id=? AND business_date<?",
                            (int(restaurant_id), today.isoformat())).fetchone()[0]
        if not last:
            return {"available": False, "why": "No POS tickets archived yet.", "note": _note()}
        end = str(last)[:10]
        start = (date.fromisoformat(end) - timedelta(days=SPAN_DAYS - 1)).isoformat()
        tickets = {r["ticket_id"]: dict(r) for r in conn.execute(
            "SELECT ticket_id, is_bar, profit_center, server_name FROM pos_tickets WHERE restaurant_id=? "
            "AND business_date>=? AND business_date<=?", (int(restaurant_id), start, end)).fetchall()}
        rows = [dict(r) for r in conn.execute(
            "SELECT ticket_id, business_date, item_id, kind, sales, loss_amount, reason, approver_id, item_at, "
            "department, sales_category, category_id FROM pos_ticket_lines WHERE restaurant_id=? AND business_date>=? "
            "AND business_date<=? AND kind IN ('sale','comp','void')", (int(restaurant_id), start, end)).fetchall()]
        names = {str(r[0]): r[1] for r in conn.execute(
            "SELECT employee_id, MAX(employee_name) FROM pos_punches WHERE restaurant_id=? AND employee_id IS NOT NULL "
            "GROUP BY employee_id", (int(restaurant_id),)).fetchall()}
        from bar.sales import _filer
        filed = _filer(conn, int(restaurant_id))
    finally:
        conn.close()
    bar_tickets = {t for t, r in tickets.items()
                   if r.get("is_bar") or _BAR_CENTER_RE.search(str(r.get("profit_center") or ""))}
    if bar_tickets:
        basis = "bar_tickets"
        mine = [r for r in rows if r["ticket_id"] in bar_tickets]
    else:
        from bar.mapping import Mapping, key_for
        mp = Mapping(restaurant_id, db_path)
        basis = "drink_categories"
        mine = [r for r in rows if mp.resolve(*key_for(*filed(r)))["kind"] in POUR_CATEGORIES]
    games = _game_nights(restaurant_id, start, end, db_path)

    def tally(lines):
        t = {"sales_usd": 0.0, "comp_usd": 0.0, "void_usd": 0.0, "events": 0, "approvers": defaultdict(float),
             "reasons": defaultdict(float)}
        for r in lines:
            if r["kind"] == "sale":
                t["sales_usd"] += _f(r["sales"])
                continue
            amt = abs(_f(r["loss_amount"])) or abs(_f(r["sales"]))
            t["comp_usd" if r["kind"] == "comp" else "void_usd"] += amt
            t["events"] += 1
            who = r.get("approver_id")
            t["approvers"][names.get(str(who), f"POS id {who}") if who else "not recorded"] += amt
            t["reasons"][r.get("reason") or "no reason recorded"] += amt
        return t

    def public(t):
        loss = t["comp_usd"] + t["void_usd"]
        return {"sales_usd": round(t["sales_usd"], 2), "comp_usd": round(t["comp_usd"], 2),
                "void_usd": round(t["void_usd"], 2), "events": t["events"],
                "rate_pct": round(loss / t["sales_usd"] * 100, 2) if t["sales_usd"] > 0 else None}

    regular = [r for r in mine if r["business_date"] not in games]
    on_games = [r for r in mine if r["business_date"] in games]
    tot = tally(regular)
    out = {"available": bool(mine), "span": {"from": start, "to": end, "from_mdy": _mdy(start), "to_mdy": _mdy(end)},
           "basis": basis,
           "basis_text": ("the bar's tickets (its room or profit center)" if basis == "bar_tickets" else
                          "beer, liquor and wine lines (no ticket is marked as the bar's)"),
           "totals": public(tot),
           "reasons": [{"reason": k, "amount": round(v, 2)} for k, v in
                       sorted(tot["reasons"].items(), key=lambda kv: -kv[1])][:8],
           "game_nights": dict(public(tally(on_games)), nights=len({r["business_date"] for r in on_games})),
           "flagged": [], "note": _note()}
    if not mine:
        out["why"] = "No bar sales on file over the last eight weeks."
        out["shifts"] = {"available": False, "why": out["why"]}
        return out
    shifts = published_bartender_shifts(restaurant_id, start, end, db_path)
    if not shifts:
        out["shifts"] = {"available": False,
                         "why": "No published schedule with bartenders over these weeks, so comps can't be set "
                                "against shifts."}
        return out
    by_day = defaultdict(list)
    for i, s in enumerate(shifts):
        by_day[s["date"]].append(i)
    on_shift = defaultdict(list)
    outside = []
    for r in regular:
        m = _minute(r["business_date"], r.get("item_at"))
        hits = [i for i in by_day.get(r["business_date"], [])
                if m is not None and shifts[i]["start_m"] <= m <= shifts[i]["end_m"]]
        for i in hits:
            on_shift[i].append(r)
        if not hits:
            outside.append(r)
    per = defaultdict(list)
    for i, s in enumerate(shifts):
        if s["date"] in games:
            continue
        per[s["employee"]].append(i)
    rows_out = []
    for who, idx in per.items():
        mine_lines = [r for i in idx for r in on_shift.get(i, [])]
        t = tally(mine_lines)
        others = [r for name, j in per.items() if name != who for i in j for r in on_shift.get(i, [])]
        o_shifts = sum(len(j) for name, j in per.items() if name != who)
        b = tally(others)
        row = dict(public(t), bartender=who, shifts=len(idx))
        row["approvers"] = [{"approver": k, "amount": round(v, 2)} for k, v in
                            sorted(t["approvers"].items(), key=lambda kv: -kv[1])][:4]
        base = ((b["comp_usd"] + b["void_usd"]) / b["sales_usd"] * 100) if b["sales_usd"] > 0 else None
        if len(idx) < MIN_SHIFTS:
            row["rate_pct"] = None
            row["too_few"] = f"{len(idx)} shift{'s' if len(idx) != 1 else ''}; a rate needs {MIN_SHIFTS}"
        row["baseline_rate_pct"] = round(base, 2) if base is not None and o_shifts >= MIN_SHIFTS else None
        loss = t["comp_usd"] + t["void_usd"]
        if (row["rate_pct"] is not None and row["baseline_rate_pct"] is not None and loss >= min_dollars
                and t["events"] >= min_events and row["rate_pct"] >= spike * max(row["baseline_rate_pct"], 0.0)
                and row["baseline_rate_pct"] > 0):
            mult = row["rate_pct"] / row["baseline_rate_pct"]
            out["flagged"].append({
                "type": "shift_rate", "bartender": who, "key": f"barloss:{start}:{who.lower()}",
                "headline": (f"Bar comps and voids ran {mult:.1f}× the bar's usual rate on the {len(idx)} shifts "
                             f"{who} was scheduled ({row['rate_pct']:.1f}% of bar sales vs about "
                             f"{row['baseline_rate_pct']:.1f}%)"),
                "alternative": ALTERNATIVE})
        rows_out.append(row)
    rows_out.sort(key=lambda r: (r["rate_pct"] is None, -(r["rate_pct"] or 0)))
    all_base = tally([r for i, s in enumerate(shifts) if s["date"] not in games for r in on_shift.get(i, [])])
    out["shifts"] = {"available": True, "by_bartender": rows_out, "min_shifts": MIN_SHIFTS,
                     "baseline_rate_pct": public(all_base)["rate_pct"],
                     "outside": dict(public(tally(outside)), why="rung outside every scheduled bartender's shift")}
    return out


def for_viewer(payload, restaurant_id, user, today=None, db_path=None) -> dict:
    """The Bar payload for one login: the account holder's `bar_ops.comps`
    filled in; everyone else's left as `{"owner_only": true}`. Mutates and
    returns `payload`."""
    from permissions import is_principal
    ops = payload.get("bar_ops")
    if not isinstance(ops, dict):
        return payload
    if not is_principal(user):
        ops["comps"] = {"owner_only": True}
        return payload
    try:
        ops["comps"] = dict(comps_block(restaurant_id, today=today, db_path=db_path), owner_only=True)
    except Exception:
        import logging
        logging.getLogger("bar.comps").exception("[bar.comps] unread for %s", restaurant_id)
        ops["comps"] = {"owner_only": True, "available": False, "why": "Couldn't be read just now."}
    return payload
