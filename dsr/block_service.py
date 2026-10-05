"""
dsr.block_service — the night at check level: `collect(ctx) -> block`.

Reads the ticket-level archive (pos_tickets, pos_ticket_lines, pos_punches)
for the business date, after dsr.common.night_archive has read the night
from the POS when the report runs — so these figures are in the report's
first version, not the 4am archive pass. No model call.

  dayparts        net, guests and checks by the POS's own meal period
                  (RPOWER tags each check Lunch or Dinner — the split a
                  Back Office report uses too)
  rooms           net, guests and checks by the POS's profit centre (Bar,
                  Dining, Pick-Up)
  drinks/guest    the checks' drink count over their guests
  servers         each person who carried checks: checks, guests, net, spend
                  per guest, drinks per guest, card tip % and net per hour on
                  the clock — only past NIGHT_MIN_CHECKS checks (a server who
                  carried three checks has no rate worth reading), and never a
                  station login (a shared "To Go" or bar terminal is not a
                  person). Personnel data: shown to owners and managers (Will,
                  9/30/26), never in the model's prompt (dsr.narrative
                  PRIVATE_DETAIL)
  loss            comps, discounts and refunds by reason and by the manager
                  the POS says approved them (rpower.fetch_loss_lines: the
                  approver is the attribution), and voided lines apart — a
                  void never reached the guest's bill, so it is a control
                  signal, not money given away. Named loss*: a manager sees
                  it only with the comps-and-voids permission (dsr.access)
  register        cash and cards: the night's tenders by method (payments,
                  card tips and the tip fees the POS recorded) and every
                  payout and pay-in rung on the POS — petty cash and check
                  requests are payouts once rung there (Erik, Simple EJ's,
                  10/5/26: "pulled from RPower"); each with who approved it
  timeclock_edits punches a manager edited, with who, when and RPOWER's own
                  edit code (its meaning is RPOWER's; it is shown, never
                  interpreted). Owner only (dsr.access OWNER_ONLY_PREFIXES)

Statuses:
  not_connected  the POS can't send check-level detail
  awaiting       the night's sales aren't in yet (nothing to read)
  unavailable    the read failed, or the archive holds no checks for the night
  ready          the figures
"""
import dsr
from dsr import common

NIGHT_MIN_CHECKS = 8          # a server's rates are shown past this many checks on the night
LOSS_KINDS = ("comp", "discount", "refund")
REASON_NOT_CONNECTED = "Your POS doesn't share check-level detail with Cavnar AI yet"
REASON_FAILED = "The night's checks couldn't be read from the POS"
REASON_EMPTY = "No checks were archived for this night"
NO_REASON = "No reason given"


def _rows(ctx, sql, args):
    from dsr.store import get_conn
    conn = get_conn(ctx.db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _employee_names(ctx, provider):
    """{employee id: name} from the POS's people list (rpower.employee_names),
    else {} — an approver then reads "Not recorded", never a guess."""
    import pos
    try:
        _p, mod = pos.connected_provider(ctx.restaurant_id)
        fn = getattr(mod, "employee_names", None) if mod else None
        return dict(fn(ctx.restaurant_id) or {}) if fn else {}
    except Exception:
        return {}


# A check opened before this local hour on the business date's next morning
# is the same night run late (a 12:40am close-out), never breakfast.
LATE_NIGHT_ENDS = 5
LATE_NIGHT = "Late night"


def late_night_daypart(late_hour):
    """The daypart of one check: "Late night" when it was opened at or after
    the restaurant's late-night hour, or after midnight (before
    LATE_NIGHT_ENDS); else the meal period the POS tagged. None for the hour
    keeps the POS's tags (Erik, 10/2/26: "after 10pm is late night and
    should be tracked separately")."""
    def of(t):
        tag = t.get("mealtime") or "Other"
        if late_hour is None:
            return tag
        stamp = str(t.get("opened_at") or "")
        try:
            hour = int(stamp[11:13])
        except ValueError:
            return tag
        return LATE_NIGHT if (hour >= int(late_hour) or hour < LATE_NIGHT_ENDS) else tag
    return of


def _split(tickets, key, total):
    out = {}
    for t in tickets:
        name = (key(t) if callable(key) else t.get(key)) or "Other"
        g = out.setdefault(name, {"name": name, "net": 0.0, "guests": 0, "checks": 0})
        g["net"] += float(t.get("net_sales") or 0)
        g["guests"] += int(t.get("guest_count") or 0)
        g["checks"] += 1
    rows = []
    for g in sorted(out.values(), key=lambda x: -x["net"]):
        g["net"] = round(g["net"], 2)
        g["per_guest"] = round(g["net"] / g["guests"], 2) if g["guests"] else None
        g["share_pct"] = round(g["net"] / total * 100.0, 1) if total > 0 else None
        rows.append(g)
    return rows


def _hours(punch):
    return float(punch.get("reg_hours") or 0) + float(punch.get("ot_hours") or 0) + float(punch.get("dt_hours") or 0)


def _servers(tickets, punches, is_station_name):
    stations = {p["employee_id"] for p in punches if p.get("is_station") and p.get("employee_id")}
    hours = {}
    for p in punches:
        if p.get("employee_id") and not p.get("is_station"):
            hours[p["employee_id"]] = hours.get(p["employee_id"], 0.0) + _hours(p)
    by = {}
    for t in tickets:
        sid = t.get("server_id")
        if not sid or sid in stations:
            continue
        name = t.get("server_name")
        if not name or (is_station_name and is_station_name(name)):
            continue
        s = by.setdefault(sid, {"name": name, "checks": 0, "guests": 0, "net": 0.0, "drinks": 0,
                                "tipped_net": 0.0, "tips": 0.0})
        s["checks"] += 1
        s["guests"] += int(t.get("guest_count") or 0)
        s["net"] += float(t.get("net_sales") or 0)
        s["drinks"] += int(t.get("bev_count") or 0)
        if float(t.get("tip") or 0) > 0:
            s["tips"] += float(t["tip"])
            s["tipped_net"] += float(t.get("net_sales") or 0)
    shown, below = [], 0
    for sid, s in by.items():
        if s["checks"] < NIGHT_MIN_CHECKS:
            below += 1
            continue
        h = hours.get(sid)
        shown.append({
            "name": s["name"], "checks": s["checks"], "guests": s["guests"], "net": round(s["net"], 2),
            "per_guest": round(s["net"] / s["guests"], 2) if s["guests"] else None,
            "drinks_per_guest": round(s["drinks"] / s["guests"], 2) if s["guests"] else None,
            "tip_pct": round(s["tips"] / s["tipped_net"] * 100.0, 1) if s["tipped_net"] > 0 else None,
            "hours": round(h, 2) if h else None,
            "net_per_hour": round(s["net"] / h, 2) if h else None})
    shown.sort(key=lambda x: -x["net"])
    guests = sum(x["guests"] for x in shown)
    floor = round(sum(x["net"] for x in shown) / guests, 2) if guests else None
    for x in shown:
        x["vs_floor"] = (round(x["per_guest"] - floor, 2)
                         if x["per_guest"] is not None and floor is not None else None)
    return shown, below, floor


def _loss(lines, names, sales_metrics):
    """{"given": {...}, "voids": {...}} — money given away (comps,
    discounts, refunds) by reason and approver, and voided lines apart."""
    def group(rows):
        by_reason, by_approver, total = {}, {}, 0.0
        for r in rows:
            amt = abs(float(r.get("loss_amount") if r.get("loss_amount") is not None else r.get("sales") or 0))
            total += amt
            reason = r.get("reason") or NO_REASON
            kind = r["kind"]
            k = (kind, reason)
            g = by_reason.setdefault(k, {"kind": kind, "reason": reason, "lines": 0, "amount": 0.0})
            g["lines"] += 1
            g["amount"] += amt
            who = names.get(r.get("approver_id")) if r.get("approver_id") else None
            a = by_approver.setdefault(who or "Not recorded", {"approver": who or "Not recorded", "lines": 0,
                                                               "amount": 0.0})
            a["lines"] += 1
            a["amount"] += amt
        fix = lambda d: sorted(({**v, "amount": round(v["amount"], 2)} for v in d.values()),
                               key=lambda x: -x["amount"])
        return {"total": round(total, 2), "lines": len(rows), "by_reason": fix(by_reason)[:8],
                "by_approver": fix(by_approver)[:6]}
    given = group([r for r in lines if r.get("kind") in LOSS_KINDS])
    voids = group([r for r in lines if r.get("kind") == "void"])
    gross = sales_metrics.get("gross_items")
    given["pct_of_gross"] = (round(given["total"] / float(gross) * 100.0, 1)
                             if isinstance(gross, (int, float)) and gross > 0 else None)
    return {"given": given, "voids": voids,
            "basis": ("comps, discounts and refunds from the night's check lines, by the reason and the "
                      "approving manager the POS recorded; voided lines are listed apart — a void never "
                      "reached the guest's bill")}


def _edits(punches, names):
    out = []
    for p in punches:
        if not p.get("edited_by"):
            continue
        out.append({"employee": p.get("employee_name") or "Unknown", "role": p.get("role"),
                    "clock_in": p.get("clock_in"), "clock_out": p.get("clock_out"),
                    "hours": round(_hours(p), 2),
                    "edited_by": names.get(p["edited_by"]) or "Not recorded",
                    "edited_at": p.get("edited_at"), "code": p.get("edit_what")})
    out.sort(key=lambda x: str(x.get("edited_at") or ""))
    return out


# What the register says when nothing was paid out of, or into, the drawer.
REGISTER_NO_PAYOUTS = ("No payouts or pay-ins were rung on the POS tonight. Petty cash and check requests show "
                       "here once they're rung as payouts in the POS.")


def _register(payments, payouts):
    """{tenders, totals, payouts, payouts_note} — the night's cash and cards.
    Tenders by method (card brands apart, as the bank deposits them); the
    tip fee is what the POS recorded on the payment, never a guessed
    processor rate. Payouts and pay-ins one line each, with the approver
    the POS names (a station login like "Office Drawer" as it is)."""
    by = {}
    for p in payments:
        m = (p.get("method") or "").strip() or "Not recorded"
        b = by.setdefault(m, {"method": m, "kind": "card" if p.get("is_card") else "cash" if p.get("is_cash")
                              else "other", "payments": 0, "amount": 0.0, "tips": 0.0, "tip_fees": 0.0})
        b["payments"] += 1
        b["amount"] += float(p.get("amount") or 0)
        b["tips"] += float(p.get("tip") or 0)
        b["tip_fees"] += float(p.get("tip_fee") or 0)
    tenders = []
    for b in sorted(by.values(), key=lambda x: (("card", "cash", "other").index(x["kind"]), -x["amount"])):
        tenders.append(dict(b, amount=round(b["amount"], 2), tips=round(b["tips"], 2), tip_fees=round(b["tip_fees"], 2)))

    def _sum(kind, key="amount"):
        return round(sum(t[key] for t in tenders if kind is None or t["kind"] == kind), 2)
    totals = {"card": _sum("card"), "cash": _sum("cash"), "other": _sum("other"), "all": _sum(None),
              "card_tips": _sum("card", "tips"), "card_tip_fees": _sum("card", "tip_fees")}
    lines = [{"category": (o.get("category") or "").strip() or "Not recorded",
              "type": "pay-in" if o.get("is_payin") else "payout",
              "amount": round(float(o.get("amount") or 0), 2),
              "approved_by": (o.get("manager_name") or "").strip() or "Not recorded",
              "at": o.get("paid_at"), "reference": (o.get("reference") or "").strip() or None}
             for o in sorted(payouts, key=lambda x: str(x.get("paid_at") or ""))]
    totals["payouts"] = round(sum(x["amount"] for x in lines if x["type"] == "payout"), 2)
    totals["payins"] = round(sum(x["amount"] for x in lines if x["type"] == "pay-in"), 2)
    return {"tenders": tenders, "totals": totals, "payouts": lines,
            "payouts_note": None if lines else REGISTER_NO_PAYOUTS}


def collect(ctx):
    arc = common.night_archive(ctx)
    if arc["reason"] == "no_provider":
        return dsr.block(dsr.NOT_CONNECTED, reason=REASON_NOT_CONNECTED, block_name="service")
    provider = arc["provider"]
    if arc["reason"] == "sales_pending":
        return dsr.block(dsr.AWAITING, source=provider, block_name="service",
                         reason="Waiting for the night's sales", detail={"waiting_for": "sales"})
    if not arc["ok"]:
        return dsr.block(dsr.UNAVAILABLE, source=provider, reason=REASON_FAILED, block_name="service",
                         detail={"why": arc["reason"]})
    args = (ctx.restaurant_id, provider, ctx.day)
    tickets = _rows(ctx, "SELECT * FROM pos_tickets WHERE restaurant_id=? AND provider=? AND business_date=? "
                         "AND cancelled=0", args)
    if not tickets:
        return dsr.block(dsr.UNAVAILABLE, source=provider, reason=REASON_EMPTY, block_name="service")
    lines = _rows(ctx, "SELECT kind, sales, loss_amount, reason, approver_id FROM pos_ticket_lines "
                       "WHERE restaurant_id=? AND provider=? AND business_date=? AND kind IN "
                       "('comp','discount','refund','void')", args)
    punches = _rows(ctx, "SELECT * FROM pos_punches WHERE restaurant_id=? AND provider=? AND business_date=?", args)
    payments = _rows(ctx, "SELECT method, is_cash, is_card, amount, tip, tip_fee FROM pos_payments "
                          "WHERE restaurant_id=? AND provider=? AND business_date=?", args)
    payouts = _rows(ctx, "SELECT category, is_payin, amount, manager_name, paid_at, reference FROM pos_payouts "
                         "WHERE restaurant_id=? AND provider=? AND business_date=?", args)

    import pos
    try:
        _p, mod = pos.connected_provider(ctx.restaurant_id)
    except Exception:
        mod = None
    names = _employee_names(ctx, provider)
    station_fn = getattr(mod, "is_station_name", None) if mod else None

    total = round(sum(float(t.get("net_sales") or 0) for t in tickets), 2)
    guests = sum(int(t.get("guest_count") or 0) for t in tickets)
    drinks = sum(int(t.get("bev_count") or 0) for t in tickets)
    late_hour = getattr(ctx.restaurant, "dsr_late_night_hour", None) if getattr(ctx, "restaurant", None) else None
    dayparts = _split(tickets, late_night_daypart(late_hour), total)
    rooms = _split(tickets, "profit_center", total)
    servers, below, floor = _servers(tickets, punches, station_fn)
    sales_m = ((ctx.blocks.get("sales") or {}).get("metrics") or {})
    loss = _loss(lines, names, sales_m)
    edits = _edits(punches, names)
    register = _register(payments, payouts) if payments or payouts else None

    metrics = {"checks": len(tickets), "check_guests": guests, "check_net": total,
               "drinks_per_guest": round(drinks / guests, 2) if guests else None,
               "servers_measured": len(servers), "server_floor_per_guest": floor,
               "loss_given": loss["given"]["total"], "loss_given_pct": loss["given"]["pct_of_gross"],
               "loss_void_lines": loss["voids"]["lines"], "loss_void_amount": loss["voids"]["total"],
               "timeclock_edits": len(edits)}
    if register:
        rt = register["totals"]
        metrics.update({"register_card": rt["card"], "register_cash": rt["cash"],
                        "register_card_tips": rt["card_tips"], "register_payouts": rt["payouts"],
                        "register_payins": rt["payins"]})
    for d in dayparts:
        metrics[f"daypart:{d['name']}"] = d["net"]
    for r in rooms:
        metrics[f"room:{r['name']}"] = r["net"]
    detail = {
        "dayparts": dayparts, "rooms": rooms,
        "dayparts_basis": ("the meal period the POS tagged on each check" if late_hour is None else
                           f"the meal period the POS tagged on each check; checks opened from "
                           f"{int(late_hour) - 12}pm (or after midnight) are late night"),
        "rooms_basis": "the profit centre the POS tagged on each check",
        "servers": servers, "servers_below_floor": below, "servers_min_checks": NIGHT_MIN_CHECKS,
        "servers_basis": (f"people who carried at least {NIGHT_MIN_CHECKS} checks; spend per guest is net over "
                          "the guests on their checks; tip % reads only checks that recorded a card tip; "
                          "station logins aren't people and are left out"),
        "loss": loss,
        "register": register,
        "register_basis": "payments and drawer payouts as the POS recorded them for the night",
        "timeclock_edits": edits,
        "timeclock_basis": "punches a manager edited in the POS; the code is RPOWER's own",
        "read_at_report_time": True,
        "archive": arc.get("counts"),
    }
    return dsr.block(dsr.READY, source=provider, metrics=metrics, detail=detail, block_name="service")
