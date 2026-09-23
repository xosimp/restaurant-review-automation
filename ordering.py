"""Trusted-supplier ordering and supplier memory for invoices.

Two of the automation audit's "the owner signs what the AI already
wrote" items, both graduated on the owner's own record:

  supplier_trust   — a supplier the owner has sent at least
                     ORDER_TRUST_MIN orders to, with the draft's total
                     inside the band of what they usually spend there.
                     Such an order is queued (delayed.py) with an undo
                     window and the owner is told; the send itself is the
                     same code the Send button runs.

  invoice_trust    — a supplier whose scanned invoices the owner has
                     applied INVOICE_TRUST_MIN times accepting every
                     proposed line. Lines the proposal preselects (they
                     add up, match one ingredient, and are not a large
                     change) are then applied on scan; anything flagged
                     still waits for a human.

Nothing here writes a price or sends an order to a supplier with no
history. A new supplier is a decision, and decisions stay with the owner.
"""
import json
import statistics
from datetime import datetime, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports):
    a copy bound at import kept pointing wherever models.get_conn pointed
    the first time this module was imported."""
    return _models_mod.get_conn(db_path) if db_path is not None else _models_mod.get_conn()

ORDER_TRUST_MIN = 3          # prior orders to this supplier before one can go on its own
ORDER_BAND = (0.65, 1.35)    # draft total must sit inside this multiple of the usual order
ORDER_MIN_GAP_DAYS = 5       # never a second automatic order inside the usual cadence
ORDER_UNDO_MINUTES = 60
INVOICE_TRUST_MIN = 3
# An automatic order is only as good as the count it was built from. Past
# this, the quantities are a guess about stock nobody has looked at.
COUNT_FRESH_DAYS = 7


def supplier_trust(restaurant_id, supplier_email, db_path=DB_PATH):
    """{orders, median_total, last_sent_at, trusted} for one supplier.

    Trust is the OWNER's record (audit): only orders a person sent, exactly
    as drafted, count. An automatic order counted as history, so the rule
    that sends orders on its own was earning its own trust; and an order the
    owner changed before sending is evidence the draft was wrong, not that
    it can go unread. Rows from before `source` existed count as the owner's.
    `last_sent_at` still reads every order — the cadence guard is about what
    the supplier received, whoever sent it."""
    email = (supplier_email or "").strip().lower()
    if not email:
        return {"orders": 0, "median_total": None, "last_sent_at": None, "trusted": False}
    conn = get_conn(db_path)
    try:
        try:
            rows = conn.execute(
                "SELECT total_cost, sent_at FROM purchase_orders WHERE restaurant_id=? "
                "AND LOWER(supplier_email)=? AND COALESCE(status,'') NOT IN ('void','voided','cancelled') "
                "AND COALESCE(source,'owner')='owner' AND COALESCE(edited,0)=0 "
                "ORDER BY id DESC LIMIT 20", (restaurant_id, email)).fetchall()
        except Exception:
            # A database from before source/edited existed.
            rows = conn.execute(
                "SELECT total_cost, sent_at FROM purchase_orders WHERE restaurant_id=? "
                "AND LOWER(supplier_email)=? AND COALESCE(status,'') NOT IN ('void','voided','cancelled') "
                "ORDER BY id DESC LIMIT 20", (restaurant_id, email)).fetchall()
        last_row = conn.execute(
            "SELECT sent_at FROM purchase_orders WHERE restaurant_id=? AND LOWER(supplier_email)=? "
            "AND COALESCE(status,'') NOT IN ('void','voided','cancelled') ORDER BY id DESC LIMIT 1",
            (restaurant_id, email)).fetchone()
    finally:
        conn.close()
    totals = [float(r["total_cost"]) for r in rows if r["total_cost"] is not None]
    last = last_row["sent_at"] if last_row else None
    return {"orders": len(rows),
            "median_total": (round(statistics.median(totals), 2) if totals else None),
            "last_sent_at": last,
            "trusted": len(rows) >= ORDER_TRUST_MIN and bool(totals)}


def count_freshness(restaurant_id, ingredient_ids=None, db_path=DB_PATH, today=None) -> dict:
    """{"last_count_at", "age_days", "stale", "stalest_item"} for the counts
    an order rests on: the OLDEST count among these ingredients (every line's
    quantity depends on its own count), or the restaurant's newest count when
    no ids are given. Never counted is stale."""
    from datetime import date as _date
    today = today or _date.today()
    conn = get_conn(db_path)
    try:
        if ingredient_ids:
            ids = [int(i) for i in ingredient_ids if i]
            marks = ",".join("?" * len(ids))
            rows = conn.execute(f"SELECT name, last_recount_at FROM ingredients WHERE restaurant_id=? "
                                f"AND id IN ({marks})", (restaurant_id, *ids)).fetchall() if ids else []
        else:
            rows = conn.execute("SELECT name, MAX(last_recount_at) AS last_recount_at FROM ingredients "
                                "WHERE restaurant_id=? AND is_active=1", (restaurant_id,)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"last_count_at": None, "age_days": None, "stale": True, "stalest_item": None}
    worst, worst_age = None, -1
    for r in rows:
        stamp = r["last_recount_at"]
        if not stamp:
            return {"last_count_at": None, "age_days": None, "stale": True, "stalest_item": r["name"]}
        try:
            d = _date.fromisoformat(str(stamp)[:10])
        except ValueError:
            return {"last_count_at": stamp, "age_days": None, "stale": True, "stalest_item": r["name"]}
        age = (today - d).days
        if age > worst_age:
            worst, worst_age = (r["name"], str(stamp)[:10]), age
    return {"last_count_at": worst[1], "age_days": worst_age, "stale": worst_age > COUNT_FRESH_DAYS,
            "stalest_item": worst[0]}


def order_can_go(restaurant_id, group, db_path=DB_PATH):
    """(ok, reason) for one draft group. ok only when the supplier is
    trusted, the total is inside the band, the last order is older than the
    cadence guard, and every line rests on a count from the last
    COUNT_FRESH_DAYS days — an order built from a two-week-old count is a
    guess, and it held with nothing saying why."""
    t = supplier_trust(restaurant_id, group.get("supplier_email"), db_path=db_path)
    if not t["trusted"]:
        return False, f"only {t['orders']} prior orders"
    ids = [it.get("ingredient_id") for it in (group.get("items") or [])
           if isinstance(it, dict) and it.get("ingredient_id")]
    fresh = count_freshness(restaurant_id, ids or None, db_path=db_path)
    if fresh["stale"]:
        from time_utils import mdy
        if fresh["last_count_at"]:
            return False, (f"held: the last count of {fresh['stalest_item']} was {mdy(fresh['last_count_at'])}, "
                           f"more than {COUNT_FRESH_DAYS} days ago — count before this order goes on its own")
        return False, ((f"held: {fresh['stalest_item']} has never been counted" if fresh["stalest_item"]
                        else "held: nothing has been counted") + " — count before this order goes on its own")
    total = float(group.get("total_cost") or 0)
    if not total:
        return False, "no total"
    lo, hi = ORDER_BAND
    if not (t["median_total"] * lo <= total <= t["median_total"] * hi):
        return False, f"${total:,.0f} is outside the usual ${t['median_total']:,.0f}"
    if t["last_sent_at"]:
        try:
            last = datetime.fromisoformat(str(t["last_sent_at"]).replace("Z", "")[:19])
            if datetime.utcnow() - last < timedelta(days=ORDER_MIN_GAP_DAYS):
                return False, "an order went out this week"
        except ValueError:
            pass
    return True, "trusted"


def queue_trusted_orders(restaurant_id, restaurant=None, db_path=DB_PATH, held=None):
    """Queue every draft group that can go, with the undo window, and say
    so. Returns the queued rows. `held`, when a list, receives
    {supplier_email, supplier_name, reason} for every trusted supplier whose
    order was held on a stale count — and each is written to the account
    activity so the owner can see why no order went out."""
    import delayed
    from inventory import build_supplier_orders
    draft = build_supplier_orders(restaurant_id)
    queued = []
    for g in (draft.get("groups") or []):
        ok, why = order_can_go(restaurant_id, g, db_path=db_path)
        if not ok:
            if why.startswith("held:"):
                entry = {"supplier_email": g.get("supplier_email"), "supplier_name": g.get("supplier_name"),
                         "reason": why[len("held:"):].strip()}
                if isinstance(held, list):
                    held.append(entry)
                try:
                    from client_api import log_account_event
                    log_account_event(restaurant_id, "supplier_order_held", {"username": "Cavnar AI"},
                                      detail=f"{g.get('supplier_name') or g.get('supplier_email')}: {entry['reason']}")
                except Exception:
                    pass
            continue
        # One pending send per supplier at a time.
        if any(a["kind"] == "order_send" and (a["payload"].get("supplier_email") or "").lower()
               == (g.get("supplier_email") or "").lower() for a in delayed.pending(restaurant_id, db_path=db_path)):
            continue
        total = float(g.get("total_cost") or 0)
        row = delayed.schedule(
            restaurant_id, "order_send",
            # This supplier's own hash, so a count that moves another
            # supplier's lines in the hour does not void it (MOD-FC-10).
            {"supplier_email": g.get("supplier_email"), "draft_hash": g.get("draft_hash") or draft.get("draft_hash"),
             # Recorded on the PO as source='automatic', so it never counts
             # toward the trust that let it go (supplier_trust).
             "automatic": True},
            ORDER_UNDO_MINUTES,
            label=f"Sending the {g.get('supplier_name') or g.get('supplier_email')} order "
                  f"(${total:,.0f}, {len(g.get('items') or [])} items)",
            db_path=db_path)
        queued.append(row)
    return queued


# ── invoices ────────────────────────────────────────────────────────────────

def invoice_trust(restaurant_id, supplier, db_path=DB_PATH):
    """{applied, full_accepts, trusted}: how many of this supplier's past
    scans the owner applied accepting every preselected line."""
    name = (supplier or "").strip().lower()
    if not name:
        return {"applied": 0, "full_accepts": 0, "trusted": False}
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT lines_json, applied_json FROM invoice_imports WHERE restaurant_id=? "
            "AND LOWER(COALESCE(supplier,''))=? AND applied_at IS NOT NULL ORDER BY id DESC LIMIT 10",
            (restaurant_id, name)).fetchall()
    finally:
        conn.close()
    applied = full = 0
    for r in rows:
        applied += 1
        try:
            lines = (json.loads(r["lines_json"] or "{}").get("lines") or [])
            done = json.loads(r["applied_json"] or "[]") or []
        except Exception:
            continue
        proposed = {ln["index"] for ln in lines if ln.get("selected") and ln.get("proposed_cost")}
        got = {a.get("index") for a in done}
        if proposed and proposed <= got:
            full += 1
    return {"applied": applied, "full_accepts": full,
            "trusted": full >= INVOICE_TRUST_MIN and full == applied}


def auto_apply_if_trusted(restaurant_id, proposal, user_id=None, db_path=DB_PATH):
    """After a scan: apply the preselected lines when the supplier has
    earned it. Returns the (possibly updated) proposal with `auto_applied`
    and `trust` set. Flagged lines are never applied here."""
    import invoices
    trust = invoice_trust(restaurant_id, proposal.get("supplier"), db_path=db_path)
    proposal["trust"] = trust
    proposal["auto_applied"] = []
    if not trust["trusted"] or proposal.get("duplicate") or proposal.get("applied_at"):
        return proposal
    # Only lines whose reading was actually checked (invoices.propose's
    # `verified`): arithmetic that ran and agreed, and a current cost to judge
    # the move against. Everything else waits for the owner (AI-19).
    picks = [{"index": ln["index"], "ingredient_id": ln["ingredient_id"], "unit_cost": ln["proposed_cost"]}
             for ln in (proposal.get("lines") or [])
             if ln.get("selected") and ln.get("verified") and ln.get("ingredient_id")
             and ln.get("proposed_cost") and not ln.get("note")]
    if not picks:
        return proposal
    out = invoices.apply(restaurant_id, proposal["id"], picks, user_id=user_id, db_path=db_path)
    if out.get("ok"):
        proposal["auto_applied"] = out.get("updated") or []
        proposal["applied_at"] = "now"
        try:
            from client_api import log_account_event
            log_account_event(restaurant_id, "invoice_auto_applied", {"username": "Cavnar AI"},
                              detail=f"{proposal.get('supplier')}: {len(proposal['auto_applied'])} lines")
        except Exception:
            pass
    return proposal
