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
ORDER_TRUST_EDIT_RATE = 0.20  # ...and at most this share of the owner's orders changed before sending
ORDER_BAND = (0.65, 1.35)    # draft total must sit inside this multiple of the usual order
ORDER_MIN_GAP_DAYS = 5       # never a second automatic order inside the usual cadence
ORDER_UNDO_MINUTES = 60
INVOICE_TRUST_MIN = 3
# An automatic order is only as good as the count it was built from. Past
# this, the quantities are a guess about stock nobody has looked at.
COUNT_FRESH_DAYS = 7


def supplier_trust(restaurant_id, supplier_email, db_path=DB_PATH):
    """{orders, edited, edit_rate, median_total, last_sent_at, trusted} for
    one supplier.

    Trust is the OWNER's record (audit): only orders a person sent count.
    An automatic order counted as history, so the rule that sends orders on
    its own was earning its own trust. An order the owner changed before
    sending is evidence the draft was wrong: it is IN the count and counts
    against the supplier (M-5). It used to be left out, so 12 owner orders
    with 9 of them corrected read as "3 orders, trusted" and automatic
    sends began for a supplier whose drafts the owner fixes 75% of the
    time. `orders` is every owner-sent order; trusted needs
    ORDER_TRUST_MIN of them unedited and an edit rate at or under
    ORDER_TRUST_EDIT_RATE — the shape auto_approve_trust uses for replies.
    Rows from before `source` existed count as the owner's; an admin's send
    through view-as (authority 'admin') is not (memory audit 9/29/26,
    "view_as"). `last_sent_at`
    still reads every order — the cadence guard is about what the supplier
    received, whoever sent it."""
    email = (supplier_email or "").strip().lower()
    if not email:
        return {"orders": 0, "edited": 0, "edit_rate": None, "median_total": None,
                "last_sent_at": None, "trusted": False}
    conn = get_conn(db_path)
    try:
        rows = []
        # Newest schema first: a database from before `authority`, or from
        # before source/edited existed, is read with what it has.
        for sql in ("SELECT total_cost, sent_at, COALESCE(edited,0) AS edited FROM purchase_orders "
                    "WHERE restaurant_id=? "
                    "AND LOWER(supplier_email)=? AND COALESCE(status,'') NOT IN ('void','voided','cancelled') "
                    "AND COALESCE(source,'owner')='owner' AND COALESCE(authority,'') != 'admin' "
                    "ORDER BY id DESC LIMIT 20",
                    "SELECT total_cost, sent_at, COALESCE(edited,0) AS edited FROM purchase_orders "
                    "WHERE restaurant_id=? "
                    "AND LOWER(supplier_email)=? AND COALESCE(status,'') NOT IN ('void','voided','cancelled') "
                    "AND COALESCE(source,'owner')='owner' "
                    "ORDER BY id DESC LIMIT 20",
                    "SELECT total_cost, sent_at, 0 AS edited FROM purchase_orders WHERE restaurant_id=? "
                    "AND LOWER(supplier_email)=? AND COALESCE(status,'') NOT IN ('void','voided','cancelled') "
                    "ORDER BY id DESC LIMIT 20"):
            try:
                rows = conn.execute(sql, (restaurant_id, email)).fetchall()
                break
            except Exception:
                continue
        last_row = conn.execute(
            "SELECT sent_at FROM purchase_orders WHERE restaurant_id=? AND LOWER(supplier_email)=? "
            "AND COALESCE(status,'') NOT IN ('void','voided','cancelled') ORDER BY id DESC LIMIT 1",
            (restaurant_id, email)).fetchone()
    finally:
        conn.close()
    # The usual order size is what the owner actually sends, edits included.
    totals = [float(r["total_cost"]) for r in rows if r["total_cost"] is not None]
    last = last_row["sent_at"] if last_row else None
    n = len(rows)
    edited = sum(1 for r in rows if int(r["edited"] or 0))
    rate = (edited / n) if n else None
    # An owner's undo of an automatic send to this supplier counts against
    # it (memory audit 9/29/26, "undo"): ORDER_TRUST_MIN clean orders must be
    # sent after the latest undo before it sends on its own again.
    try:
        import delayed as _dl
        undone_at = _dl.last_undo(restaurant_id, "order_send", supplier_email=email, db_path=db_path)
    except Exception:
        undone_at = None
    clean_since_undo = None
    if undone_at:
        clean_since_undo = sum(1 for r in rows if not int(r["edited"] or 0)
                               and str(r["sent_at"] or "").replace("T", " ")[:19] > undone_at)
    trusted = bool((n - edited) >= ORDER_TRUST_MIN and rate is not None and rate <= ORDER_TRUST_EDIT_RATE and totals)
    if clean_since_undo is not None and clean_since_undo < ORDER_TRUST_MIN:
        trusted = False
    return {"orders": n, "edited": edited, "edit_rate": rate,
            "median_total": (round(statistics.median(totals), 2) if totals else None),
            "last_sent_at": last, "undone_at": undone_at, "clean_since_undo": clean_since_undo,
            "trusted": trusted}


def count_freshness(restaurant_id, ingredient_ids=None, db_path=DB_PATH, today=None) -> dict:
    """{"last_count_at", "age_days", "stale", "stalest_item"} for the counts
    an order rests on: the OLDEST count among these ingredients (every line's
    quantity depends on its own count), or the restaurant's newest count when
    no ids are given. Never counted is stale."""
    from datetime import date as _date
    if today is None:
        # The restaurant's own date, the rule inventory.analysis_for uses
        # for the same counts (DH1-14).
        try:
            from time_utils import restaurant_now_by_id
            today = restaurant_now_by_id(restaurant_id).date()
        except Exception:
            today = _date.today()
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
        if t.get("edit_rate") is not None and t["edit_rate"] > ORDER_TRUST_EDIT_RATE:
            return False, f"{t['edited']} of your last {t['orders']} orders were changed before sending"
        return False, f"only {t['orders'] - t.get('edited', 0)} prior orders sent as drafted"
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
    scans the owner applied accepting every preselected line.

    Only the OWNER's applies are evidence (M-4). A scan the rule applied on
    its own (auto_applied=1) was counted as the owner accepting every line,
    so after ten auto-applies no human decision was left in the window and
    the rule kept itself trusted."""
    name = (supplier or "").strip().lower()
    if not name:
        return {"applied": 0, "full_accepts": 0, "trusted": False}
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT lines_json, applied_json FROM invoice_imports WHERE restaurant_id=? "
            "AND LOWER(COALESCE(supplier,''))=? AND applied_at IS NOT NULL "
            "AND COALESCE(auto_applied, 0) = 0 ORDER BY id DESC LIMIT 10",
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
    and `trust` set. Flagged lines are never applied here; they stay open
    for the owner (invoices.apply takes the lines not yet in, M-4)."""
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
    out = invoices.apply(restaurant_id, proposal["id"], picks, user_id=user_id, db_path=db_path, auto=True)
    if out.get("ok"):
        proposal["auto_applied"] = out.get("updated") or []
        proposal["applied_at"] = "now"
        # The lines the rule left (flagged, unverified) stay open for the
        # owner: the card keeps its apply button for them (M-4).
        invoices.mark_applied(proposal, proposal["auto_applied"], True)
        try:
            from client_api import log_account_event
            log_account_event(restaurant_id, "invoice_auto_applied", {"username": "Cavnar AI"},
                              detail=f"{proposal.get('supplier')}: {len(proposal['auto_applied'])} lines")
        except Exception:
            pass
    return proposal


# ── what the owner's own orders teach (memory audit 9/29/26, food_corrections)
#
# The draft and the sent lines of every owner-sent order are kept
# (purchase_orders.draft_items_json / items_json) and were reduced to a trust
# rate; the quantity formula never read them, so an owner who cuts salmon 30%
# on every order got the same salmon draft forever. Now each ingredient the
# owner keeps correcting carries a factor: the median of sent ÷ drafted over
# their recent orders, shrunk toward 1 by ORDER_CORRECTION_SHRINK pseudo-orders,
# bounded to ORDER_CORRECTION_BOUNDS, used only past ORDER_CORRECTION_MIN_ORDERS
# orders and a ORDER_CORRECTION_MIN_MOVE change — and shown on the line. The
# ratio is always against the formula's own quantity (a line's `base_qty`), so
# a corrected draft the owner then sends unchanged confirms the factor instead
# of pulling it back to 1.
ORDER_CORRECTION_MIN_ORDERS = 3
ORDER_CORRECTION_WINDOW = 8          # the owner's latest orders an ingredient appears in
ORDER_CORRECTION_SHRINK = 3
ORDER_CORRECTION_BOUNDS = (0.5, 1.5)
ORDER_CORRECTION_MIN_MOVE = 0.10


def _line_qty(line, *keys):
    for k in keys:
        try:
            v = float(line.get(k))
        except (TypeError, ValueError, AttributeError):
            continue
        return v
    return None


def order_corrections(restaurant_id, db_path=DB_PATH) -> dict:
    """{ingredient_id: {"factor", "orders", "median", "basis"}} for the
    ingredients whose draft the owner keeps changing — {} for a restaurant
    that does not learn for itself (models.learns_for_itself). Only orders a
    person at the restaurant sent (source 'owner', never an admin's through
    view-as) with their draft kept; a line the owner took off the order is a
    0. Never raises."""
    try:
        if not _models_mod.learns_for_itself(restaurant_id):
            return {}
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT items_json, draft_items_json FROM purchase_orders WHERE restaurant_id=? "
                "AND COALESCE(source, 'owner')='owner' AND COALESCE(authority, '') != 'admin' "
                "AND draft_items_json IS NOT NULL "
                "AND COALESCE(status,'') NOT IN ('void','voided','cancelled') ORDER BY id DESC LIMIT 60",
                (restaurant_id,)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f"[ordering] order corrections unreadable for {restaurant_id}: {e}")
        return {}
    ratios = {}
    for r in rows:
        try:
            drafted = [i for i in (json.loads(r["draft_items_json"] or "[]") or []) if isinstance(i, dict)]
            sent = {i.get("ingredient_id"): i for i in (json.loads(r["items_json"] or "[]") or [])
                    if isinstance(i, dict) and i.get("ingredient_id")}
        except Exception:
            continue
        for d in drafted:
            ing = d.get("ingredient_id")
            base = _line_qty(d, "base_qty", "qty")
            if not ing or not base or base <= 0:
                continue
            got = ratios.setdefault(ing, [])
            if len(got) >= ORDER_CORRECTION_WINDOW:
                continue
            s = sent.get(ing)
            got.append(((_line_qty(s, "qty") or 0.0) if s else 0.0) / base)
    out = {}
    for ing, rs in ratios.items():
        n = len(rs)
        if n < ORDER_CORRECTION_MIN_ORDERS:
            continue
        med = statistics.median(rs)
        factor = 1.0 + (med - 1.0) * n / (n + ORDER_CORRECTION_SHRINK)
        lo, hi = ORDER_CORRECTION_BOUNDS
        factor = round(min(hi, max(lo, factor)), 2)
        if abs(factor - 1.0) < ORDER_CORRECTION_MIN_MOVE:
            continue
        out[ing] = {"factor": factor, "orders": n, "median": round(med, 2),
                    "basis": (f"your last {n} orders sent about {round(med * 100)}% of what the draft suggested "
                              f"for this item, so this draft is adjusted to {round(factor * 100)}% of the formula")}
    return out


def apply_order_correction(line, correction):
    """One order line with the owner's correction applied: `base_qty` keeps
    the formula's quantity (what the next correction is measured against),
    `qty` the corrected one, `owner_adjusted` says why. Case-size rounding
    is kept (the case the formula line was rounded to). Returns the line."""
    if not correction:
        return line
    try:
        base = int(line.get("qty") or 0)
    except (TypeError, ValueError):
        return line
    if base <= 0:
        return line
    qty = max(1, int(round(base * float(correction["factor"]))))
    unit_cost = float(line.get("unit_cost") or 0)
    return dict(line, base_qty=base, qty=qty, line_cost=round(qty * unit_cost, 2),
                owner_adjusted={"factor": correction["factor"], "orders": correction["orders"],
                                "basis": correction["basis"]})


# A suggested par increase after repeated 86s (food_corrections): an item
# the close-out says ran out on PAR_SUGGEST_86S or more nights in the last
# PAR_SUGGEST_WINDOW_DAYS gets a higher par offered — never written until the
# owner accepts it.
PAR_SUGGEST_86S = 2
PAR_SUGGEST_WINDOW_DAYS = 28
PAR_SUGGEST_STEP = 1.25              # a quarter more, or a day's usage, whichever is more


def par_suggestions(restaurant_id, db_path=DB_PATH, today=None) -> list:
    """[{"ingredient_id", "name", "unit", "par", "suggested_par", "times",
    "last", "key", "basis"}] — items 86'd at close on PAR_SUGGEST_86S or more
    nights in the last PAR_SUGGEST_WINDOW_DAYS (inventory_ledger recounts to
    0 with source 'closeout'), most often first. Never raises."""
    import math
    try:
        if today is None:
            from time_utils import restaurant_now_by_id
            today = restaurant_now_by_id(restaurant_id).date()
        since = (today - timedelta(days=PAR_SUGGEST_WINDOW_DAYS)).isoformat()
        conn = get_conn(db_path)
        try:
            rows = conn.execute(
                "SELECT i.id, i.name, i.unit, COALESCE(i.par_level, 0) AS par, COALESCE(i.avg_daily_usage, 0) AS usage, "
                "COUNT(DISTINCT e.event_date) AS times, MAX(e.event_date) AS last "
                "FROM ingredient_stock_events e JOIN ingredients i ON i.id=e.ingredient_id AND i.restaurant_id=e.restaurant_id "
                "WHERE e.restaurant_id=? AND e.event_type='recount' AND e.source='closeout' AND e.event_date >= ? "
                "AND i.is_active=1 GROUP BY i.id HAVING COUNT(DISTINCT e.event_date) >= ? "
                "ORDER BY times DESC, last DESC",
                (restaurant_id, since, PAR_SUGGEST_86S)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f"[ordering] par suggestions unreadable for {restaurant_id}: {e}")
        return []
    from time_utils import mdy, parse_stamp, restaurant_now_by_id
    import rec_ledger
    # 86s count only after the par was last SET. Nights before it were
    # measured against another par, whoever changed it: a hand edit, an
    # admin's accept through view-as (which M1 records with no
    # implemented_at), a sync, or an implemented raise_par answer. The
    # par's own stamp (ingredients.par_changed_at, local day) is the cutoff,
    # and the answer's date backs it for a par set before the stamp existed.
    tz = restaurant_now_by_id(restaurant_id).tzinfo
    def _local_day(stamp):
        at = parse_stamp(stamp) if stamp else None
        return at.astimezone(tz).date().isoformat() if at and tz else (str(stamp)[:10] if stamp else None)
    recent = {}
    try:
        conn = get_conn(db_path)
        try:
            raised = {r["key"]: str(r["at"])[:10] for r in conn.execute(
                "SELECT key, MAX(implemented_at) AS at FROM rec_instances WHERE restaurant_id=? "
                "AND key LIKE 'raise_par:%' AND implemented_at IS NOT NULL GROUP BY key", (restaurant_id,)).fetchall()}
            try:
                changed = {r["id"]: _local_day(r["par_changed_at"]) for r in conn.execute(
                    "SELECT id, par_changed_at FROM ingredients WHERE restaurant_id=? AND par_changed_at IS NOT NULL",
                    (restaurant_id,)).fetchall()}
            except Exception:
                changed = {}
            for r in rows:
                cutoff = max([d for d in (raised.get(rec_ledger.rec_key("raise_par", r["name"])),
                                          changed.get(r["id"])) if d] or [""])
                if cutoff:
                    after = conn.execute(
                        "SELECT COUNT(DISTINCT event_date) AS n, MAX(event_date) AS last FROM ingredient_stock_events "
                        "WHERE restaurant_id=? AND ingredient_id=? AND event_type='recount' AND source='closeout' "
                        "AND event_date > ? AND event_date >= ?", (restaurant_id, r["id"], cutoff, since)).fetchone()
                    recent[r["id"]] = (int(after["n"] or 0), after["last"])
        finally:
            conn.close()
    except Exception as e:
        print(f"[ordering] par-change cutoffs unreadable for {restaurant_id}: {e}")
    out = []
    for r in rows:
        times, last = int(r["times"]), r["last"]
        if r["id"] in recent:
            times, last = recent[r["id"]]
            if times < PAR_SUGGEST_86S:
                continue
        par, usage = float(r["par"] or 0), float(r["usage"] or 0)
        target = max(par * PAR_SUGGEST_STEP, par + usage)
        suggested = math.ceil(target) if target > 0 else None
        if not suggested or suggested <= par:
            continue
        out.append({"ingredient_id": r["id"], "name": r["name"], "unit": r["unit"] or "", "par": par,
                    "suggested_par": suggested, "times": times, "last": last,
                    "key": rec_ledger.rec_key("raise_par", r["name"]),
                    "basis": (f"86'd at close on {times} nights in the last {PAR_SUGGEST_WINDOW_DAYS} days "
                              + ("since the par was last set " if r["id"] in recent else "")
                              + f"(last {mdy(last)}); par {par:g} → {suggested:g}")})
    return out
