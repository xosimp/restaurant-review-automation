"""pos_checks — what a CHECK is, and its guests and tips, for every surface
that counts them (Audit #23, 10/21/26: A2-04, A2-06, A2-11).

Average check and covers had three definitions: the nightly report counted
tickets with a sale line (a cancelled one included) and blanked the night's
guests when one was cancelled; its service block counted every non-cancelled
ticket, a fully comped table and a $0 ticket included; Ask's service record
divided the sum of ticket nets by every non-cancelled ticket. So the same
night's "avg check" read three ways. Tips were read from the ticket in one
block and from the payment in the next, over two denominators.

The one rule, stated where a figure is shown (CHECK_BASIS, TIP_BASIS):

  check   a ticket the POS did not cancel that carries at least one sale
          line (something a guest paid for). A fully comped table or a
          ticket of discount lines only is not a check: it adds nothing to
          net and would pull the average down.
  guests  the guest count on those checks; None when a check is missing
          from the ticket feed (the count would be partial) or the store
          records no covers (all zero).
  tips    the PAYMENT record's tip (ticketpayment.paid_tip) — the card tip
          the processor settled, one per payment — over the net of the
          checks those payments paid, before tax.
  paid    a payment on a cancelled ticket is not money taken for the night.

Pure functions plus the SQL fragments the archive readers share. No POS
call, no model call.
"""

CHECK_BASIS = ("checks are tickets the POS did not cancel with at least one item sold on them; a fully "
               "comped or discount-only ticket is not a check")
GUEST_BASIS = "guests are the guest counts on those checks"
TIP_BASIS = "tips are the card tips on the payments, over the net sales of the checks they paid (before tax)"

# An archived ticket (pos_tickets t) is a check: not cancelled, with a sale
# line on the same business date. For `... FROM pos_tickets t WHERE ...`.
CHECK_SQL = ("t.cancelled=0 AND EXISTS (SELECT 1 FROM pos_ticket_lines l WHERE l.restaurant_id=t.restaurant_id "
             "AND l.provider=t.provider AND l.business_date=t.business_date AND l.ticket_id=t.ticket_id "
             "AND l.kind='sale')")
# The same test as a column: 1 for a check, 0 otherwise.
IS_CHECK_SQL = f"(CASE WHEN {CHECK_SQL} THEN 1 ELSE 0 END)"
# A payment (pos_payments p) that does not belong to a cancelled ticket — a
# payment with no ticket row still counts (A2-11).
PAYMENT_LIVE_SQL = ("NOT EXISTS (SELECT 1 FROM pos_tickets t WHERE t.restaurant_id=p.restaurant_id "
                    "AND t.provider=p.provider AND t.business_date=p.business_date AND t.ticket_id=p.ticket_id "
                    "AND t.cancelled=1)")


def count(tickets, sale_ticket_ids):
    """{checks, guests, ids} for a night's tickets (non-cancelled, {id:
    ticket}) and the ids of tickets with a sale line. `guests` is None when a
    sale ticket is missing from `tickets` or no check records a guest."""
    ids = {str(t) for t in sale_ticket_ids or ()}
    counted = [tickets[t] for t in ids if t in tickets]
    guests = sum(int((t or {}).get("guest_count") or 0) for t in counted)
    return {"checks": len(ids), "ids": ids,
            "guests": guests if guests and len(counted) == len(ids) else None}


def is_check(ticket) -> bool:
    """For an archived ticket row that carries `is_check` (IS_CHECK_SQL)."""
    return bool(int((ticket or {}).get("is_check") or 0))


def tips_by_ticket(payments) -> dict:
    """{ticket id: tip} from the night's payment records — the one tip
    source (A2-06)."""
    out = {}
    for p in payments or ():
        tid = p.get("ticket_id")
        tip = float(p.get("tip") or 0)
        if tid and tip:
            out[str(tid)] = round(out.get(str(tid), 0.0) + tip, 2)
    return out


def tip_rate(tickets, tips) -> tuple:
    """(tip %, tipped checks) over `tickets` with `tips` ({ticket id: tip}):
    tips over the net of the checks that carried one. (None, 0) without."""
    tipped = [t for t in tickets if tips.get(str(t.get("ticket_id"))) and float(t.get("net_sales") or 0) > 0]
    net = sum(float(t.get("net_sales") or 0) for t in tipped)
    if not tipped or net <= 0:
        return None, 0
    return round(100.0 * sum(tips[str(t["ticket_id"])] for t in tipped) / net, 1), len(tipped)


def applied_amount(paid, cash_change=0.0, target=0.0) -> float:
    """What a payment put on the check (A2-11). RPOWER's `paid_ticket` is
    read as the amount APPLIED (RPOWER question 3 is open). A payment that
    gave change (`cash_change` > 0) is checked against the check's due
    amount (`target_amt`): paid already equal to it is applied; otherwise
    paid less the change is what stayed in the drawer — never below 0."""
    paid = float(paid or 0)
    change = float(cash_change or 0)
    target = float(target or 0)
    if change <= 0:
        return round(paid, 2)
    if target > 0 and abs(paid - target) < 0.01:
        return round(paid, 2)
    return round(max(paid - change, 0.0), 2)
