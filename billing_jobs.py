"""
billing_jobs.py — the billing lifecycle's durable state and its scheduled work.

Fix round H. Before this, the billing lifecycle lived entirely inside the
Stripe and DocuSign webhook handlers, and whatever those handlers did was
the whole record:

  * a payment link or welcome email that failed after signing was printed,
    logged 'sent', and never retried (#12);
  * a failed card got one email, on the first attempt only, and nothing
    stopped anything when the invoice was later paid (#25);
  * signed-but-unpaid clients were never chased, and nothing measured them
    (#26);
  * the only copy of a subscription's amount, interval, discount or trial
    end was Stripe's, read live per page load (#15, #51), and nothing ever
    compared local billing state with Stripe's (#115).

What lives here:

  owed_sends            the outbox. A billing email is a row first, written in
                        the same step as the event that owes it; the drain sends
                        it, marks it from the real delivery result, retries with
                        backoff, and gives up loudly (ops.capture + status
                        'failed', which the console raises) — never silently.
  stripe_invoices       one row per Stripe invoice: amounts, setup vs recurring,
                        attempt count, hosted link, charge id (so a dispute can
                        find its restaurant without a Stripe call).
  stripe_subscriptions  (created by models.init_db) is the subscription mirror;
                        upsert_subscription keeps it current from webhook events
                        and reconcile_stripe re-reads Stripe nightly.
  billing_status_history  written by models.update_restaurant (DDL here).
  billing_reconcile     what the nightly reconcile found wrong, per restaurant.

Scheduled entry points (registered by the integration wave, not here):
  run_owed_sends     every scheduler tick — drain whatever is due.
  run_dunning        hourly — the dunning safety net, then drain dunning mail.
  run_contract_chase daily — pay-link reminders on days 2, 5 and 9 after signing.
  reconcile_stripe   nightly — mirror refresh and local-vs-Stripe mismatches.
Each returns {attempted, ok, failed, skipped, hit_bound} (ops.run_outcome).

Nothing here sends where scheduler.scheduling_allowed() is False: a local
backend holds production's Resend key and must never mail a real client.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import models as _models

log = logging.getLogger("billing_jobs")


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md: bound imports)."""
    return _models.get_conn(db_path) if db_path is not None else _models.get_conn()


# ── schema ───────────────────────────────────────────────────────────────────

_DDL = (
    """CREATE TABLE IF NOT EXISTS owed_sends (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id    INTEGER,
        kind             TEXT NOT NULL,
        dedupe_key       TEXT NOT NULL UNIQUE,
        to_email         TEXT,
        payload_json     TEXT,
        status           TEXT NOT NULL DEFAULT 'pending',
        attempts         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at  TEXT NOT NULL DEFAULT (datetime('now')),
        locked_until     TEXT,
        last_error       TEXT,
        last_status_code INTEGER,
        message_id       TEXT,
        source           TEXT,
        actor            TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        sent_at          TEXT,
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_owed_sends_due ON owed_sends(status, next_attempt_at)",
    "CREATE INDEX IF NOT EXISTS idx_owed_sends_rid ON owed_sends(restaurant_id, created_at)",
    """CREATE TABLE IF NOT EXISTS stripe_invoices (
        invoice_id             TEXT PRIMARY KEY,
        restaurant_id          INTEGER,
        customer_id            TEXT,
        subscription_id        TEXT,
        status                 TEXT,
        billing_reason         TEXT,
        kind                   TEXT,
        currency               TEXT,
        subtotal_cents         INTEGER,
        discount_cents         INTEGER,
        tax_cents              INTEGER,
        total_cents            INTEGER,
        amount_due_cents       INTEGER,
        amount_paid_cents      INTEGER,
        amount_remaining_cents INTEGER,
        recurring_cents        INTEGER,
        setup_cents            INTEGER,
        attempt_count          INTEGER,
        next_payment_attempt   TEXT,
        hosted_invoice_url     TEXT,
        invoice_pdf            TEXT,
        charge_id              TEXT,
        payment_intent_id      TEXT,
        period_start           TEXT,
        period_end             TEXT,
        created                TEXT,
        paid_at                TEXT,
        last_failed_at         TEXT,
        last_event_id          TEXT,
        updated_at             TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_stripe_invoices_rid ON stripe_invoices(restaurant_id, created)",
    "CREATE INDEX IF NOT EXISTS idx_stripe_invoices_charge ON stripe_invoices(charge_id)",
    "CREATE INDEX IF NOT EXISTS idx_stripe_invoices_sub ON stripe_invoices(subscription_id)",
    """CREATE TABLE IF NOT EXISTS billing_status_history (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id   INTEGER NOT NULL,
        field           TEXT NOT NULL,
        old_value       TEXT,
        new_value       TEXT,
        source          TEXT,
        actor           TEXT,
        stripe_event_id TEXT,
        reason          TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_billing_history_rid ON billing_status_history(restaurant_id, id)",
    # A second checkout's subscription, cancelled against the first
    # (MOD-BIL-5). Its refund, cancellation and invoices are then recognised
    # for what they are instead of pausing, churning or re-keying the paying
    # account (COMMS-19).
    """CREATE TABLE IF NOT EXISTS stripe_duplicate_subscriptions (
        subscription_id TEXT PRIMARY KEY,
        restaurant_id   INTEGER,
        customer_id     TEXT,
        session_id      TEXT,
        cancelled       INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS billing_reconcile (
        restaurant_id   INTEGER PRIMARY KEY,
        checked_at      TEXT NOT NULL,
        local_status    TEXT,
        stripe_status   TEXT,
        subscription_id TEXT,
        mismatches      TEXT,
        first_seen_at   TEXT
    )""",
)


def init_billing(db_path=None):
    """Boot-time DDL (models.init_db calls it after ensure_columns)."""
    conn = get_conn(db_path)
    try:
        for sql in _DDL:
            conn.execute(sql)
        # One ledger row per Stripe event however often Stripe redelivers it
        # (#50). Partial, so every non-Stripe row (external_id NULL) is free.
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_admin_events_external "
                         "ON admin_events(source, external_id) WHERE external_id IS NOT NULL")
        except Exception as e:
            # Only a database without admin_events (a partial fixture) lands
            # here; init_db always creates it first.
            if "no such" not in str(e).lower():
                raise
        _backfill_lifecycle_stamps(conn)
        conn.commit()
    finally:
        conn.close()
    # The backfill writes restaurants outside update_restaurant (CLAUDE.md):
    # at boot there is no request memo, but the rule costs nothing to keep.
    _models._invalidate_request_cache()


def _backfill_lifecycle_stamps(conn):
    """contract_signed_at and converted_at for clients who signed or paid
    before the columns existed, read from the event ledger (the DocuSign
    'contract.signed' row; the first checkout, or the first paid invoice
    with money in it). Only NULLs are filled, so every later boot is a
    no-op; at boot there is no request cache to invalidate. Best effort:
    a ledger older than the columns is the only record there is."""
    try:
        conn.execute(
            "UPDATE restaurants SET contract_signed_at = (SELECT MIN(e.created_at) FROM admin_events e "
            "  WHERE e.source='docusign' AND e.event_type='contract.signed' AND e.restaurant_id=restaurants.id) "
            "WHERE contract_status='signed' AND contract_signed_at IS NULL AND EXISTS (SELECT 1 FROM admin_events e "
            "  WHERE e.source='docusign' AND e.event_type='contract.signed' AND e.restaurant_id=restaurants.id)")
        conn.execute(
            "UPDATE restaurants SET converted_at = (SELECT MIN(e.created_at) FROM admin_events e "
            "  WHERE e.source='stripe' AND e.restaurant_id=restaurants.id AND (e.event_type='checkout.session.completed' "
            "  OR (e.event_type='invoice.paid' AND COALESCE(e.amount,0) > 0))) "
            "WHERE converted_at IS NULL AND EXISTS (SELECT 1 FROM admin_events e WHERE e.source='stripe' "
            "  AND e.restaurant_id=restaurants.id AND (e.event_type='checkout.session.completed' "
            "  OR (e.event_type='invoice.paid' AND COALESCE(e.amount,0) > 0)))")
    except Exception as e:
        # A partial fixture without these columns; init_db adds them first.
        if "no such" not in str(e).lower():
            raise


# ── small helpers ────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _stamp(dt=None) -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS', the form datetime('now') writes."""
    return (dt or _now()).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _iso(ts):
    """A Stripe unix timestamp as a UTC stamp, or None."""
    if ts in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _parse(stamp):
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "")[:19])
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _g(obj, key, default=None):
    """obj[key] for a dict or a Stripe object, attribute access otherwise.

    StripeObject subclasses dict, and its .items/.keys are the dict methods —
    getattr(sub, "items") is a bound method, not the subscription's items —
    so the mapping read comes first and a callable attribute never counts."""
    if obj is None:
        return default
    if hasattr(obj, "get") and hasattr(obj, "keys"):
        try:
            v = obj.get(key)
            return default if v is None else v
        except Exception:
            pass
    try:
        v = getattr(obj, key)
    except Exception:
        return default
    return default if (v is None or callable(v)) else v


def _list(obj):
    """The .data of a Stripe list object, or the list itself."""
    if obj is None:
        return []
    if isinstance(obj, (list, tuple)):
        return list(obj)
    data = _g(obj, "data")
    return list(data) if data else []


def _cents(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _mask(email):
    email = (email or "").strip()
    if "@" not in email:
        return "(none)"
    name, dom = email.split("@", 1)
    return (name[:2] + "…@" + dom) if name else "@" + dom


def _sending_allowed() -> bool:
    """Only where the scheduler may run: Railway, or ALLOW_LOCAL_SCHEDULER=1.
    A laptop has production's Resend key and a copy of real clients."""
    try:
        import scheduler
        return bool(scheduler.scheduling_allowed())
    except Exception as e:
        log.warning(f"scheduling_allowed unavailable ({e}); not sending")
        return False


# ── the subscription mirror (stripe_subscriptions) ──────────────────────────

# A subscription in one of these states is still the restaurant's live
# subscription. canceled / incomplete_expired / unpaid are over: Stripe's
# "unpaid" is the end of dunning and the account churns on it, so a later
# checkout is a new subscription, never a "second checkout" (COMMS-8).
LIVE_SUB_STATES = ("active", "trialing", "past_due", "paused", "incomplete")
ENDED_SUB_STATES = ("canceled", "incomplete_expired", "unpaid")

_MONTHS_PER = {"month": 1, "year": 12, "week": 12 / 52.0, "day": 12 / 365.0}


def is_live_status(status) -> bool:
    """None (a row written before the mirror had columns) is not known to be
    over, so it counts as live; callers that must know ask Stripe."""
    s = (status or "").strip().lower()
    return not s or s in LIVE_SUB_STATES


def _price(item):
    return _g(item, "price") or _g(item, "plan") or {}


def _recurring(price):
    rec = _g(price, "recurring")
    if rec:
        return rec
    # The legacy Plan object carries interval on itself.
    if _g(price, "interval"):
        return {"interval": _g(price, "interval"), "interval_count": _g(price, "interval_count") or 1}
    return None


def subscription_facts(sub) -> dict:
    """The mirror's columns, read out of a Stripe subscription object."""
    items = _list(_g(sub, "items"))
    recurring = [it for it in items if _recurring(_price(it))]
    first = recurring[0] if recurring else (items[0] if items else None)
    price = _price(first) if first is not None else {}
    rec = _recurring(price) or {}
    amount = 0
    qty = 0
    for it in recurring:
        q = _g(it, "quantity") or 1
        qty += int(q)
        amount += int(_g(_price(it), "unit_amount") or 0) * int(q)
    discount_pct = None
    disc = _g(sub, "discount")
    discounts = [d for d in _list(_g(sub, "discounts")) if not isinstance(d, str)]
    for d in ([disc] if disc else []) + discounts:
        coupon = _g(d, "coupon") or {}
        pct = _g(coupon, "percent_off")
        off = _g(coupon, "amount_off")
        if pct:
            discount_pct = float(pct)
            break
        if off and amount:
            discount_pct = round(100.0 * float(off) / float(amount), 4)
            break
    period_end = _g(sub, "current_period_end") or (_g(first, "current_period_end") if first is not None else None)
    details = _g(sub, "cancellation_details") or {}
    reason = _g(details, "reason")
    feedback = _g(details, "feedback")
    comment = _g(details, "comment")
    reason_txt = " · ".join(str(x) for x in (reason, feedback, comment) if x) or None
    meta = _g(sub, "metadata") or {}
    customer = _g(sub, "customer")
    if customer is not None and not isinstance(customer, str):
        customer = _g(customer, "id")
    return {
        "subscription_id": _g(sub, "id"),
        "customer_id": customer,
        "status": (_g(sub, "status") or None),
        "interval": _g(rec, "interval"),
        "interval_count": int(_g(rec, "interval_count") or 1) if rec else None,
        "amount_cents": amount if recurring else None,
        "currency": _g(price, "currency") or _g(sub, "currency"),
        "quantity": qty or None,
        "discount_pct": discount_pct,
        "trial_end": _iso(_g(sub, "trial_end")),
        "current_period_end": _iso(period_end),
        "cancel_at_period_end": 1 if _g(sub, "cancel_at_period_end") else 0,
        "canceled_at": _iso(_g(sub, "canceled_at")),
        "ended_at": _iso(_g(sub, "ended_at")),
        "cancellation_reason": reason_txt,
        "module_keys": _g(meta, "module_keys"),
        "price_id": _g(price, "id"),
    }


_MIRROR_COLS = ("customer_id", "status", "interval", "interval_count", "amount_cents", "currency",
                "quantity", "discount_pct", "trial_end", "current_period_end", "cancel_at_period_end",
                "canceled_at", "ended_at", "cancellation_reason", "module_keys", "price_id")


def upsert_subscription(restaurant_id, sub=None, subscription_id=None, session_id=None,
                        facts=None, event_id=None, db_path=None) -> dict:
    """Write (or replace) this restaurant's mirror row.

    `sub` is a Stripe subscription object; without one (a checkout session
    carries only the id) the row keeps whatever facts it already had for the
    same subscription and learns the rest from the next event or the nightly
    reconcile. A different subscription id replaces the row: the mirror holds
    the restaurant's CURRENT subscription, and the ledger keeps the old one's
    history (billing_status_history, admin_events, stripe_invoices)."""
    f = dict(facts or (subscription_facts(sub) if sub is not None else {}))
    sid = subscription_id or f.get("subscription_id")
    if not sid:
        return {}
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM stripe_subscriptions WHERE restaurant_id=?",
                           (restaurant_id,)).fetchone()
        same = bool(row) and row["subscription_id"] == sid
        cols = {c: f.get(c) for c in _MIRROR_COLS if c in f}
        if row and not same:
            conn.execute("DELETE FROM stripe_subscriptions WHERE restaurant_id=?", (restaurant_id,))
        if not row or not same:
            conn.execute("INSERT INTO stripe_subscriptions (restaurant_id, subscription_id, session_id) "
                         "VALUES (?,?,?)", (restaurant_id, sid, session_id))
        elif session_id and not row["session_id"]:
            conn.execute("UPDATE stripe_subscriptions SET session_id=? WHERE restaurant_id=?",
                         (session_id, restaurant_id))
        sets = dict(cols)
        sets["updated_at"] = _stamp()
        if event_id:
            sets["last_event_id"] = event_id
        conn.execute(f"UPDATE stripe_subscriptions SET {', '.join(k + '=?' for k in sets)} "
                     "WHERE restaurant_id=?", list(sets.values()) + [restaurant_id])
        conn.commit()
        out = conn.execute("SELECT * FROM stripe_subscriptions WHERE restaurant_id=?",
                           (restaurant_id,)).fetchone()
        return dict(out) if out else {}
    finally:
        conn.close()


def mirror_row(restaurant_id, db_path=None):
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM stripe_subscriptions WHERE restaurant_id=?",
                           (restaurant_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def live_subscription(restaurant_id, db_path=None):
    """The restaurant's own mirror row while its subscription is live."""
    row = mirror_row(restaurant_id, db_path)
    return row if row and is_live_status(row.get("status")) else None


def restaurant_for_subscription(subscription_id, db_path=None):
    if not subscription_id:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT restaurant_id FROM stripe_subscriptions WHERE subscription_id=?",
                           (subscription_id,)).fetchone()
        return row["restaurant_id"] if row else None
    finally:
        conn.close()


def record_duplicate_subscription(subscription_id, restaurant_id, customer_id=None, session_id=None,
                                  cancelled=False, db_path=None):
    if not subscription_id:
        return
    conn = get_conn(db_path)
    try:
        conn.execute("INSERT INTO stripe_duplicate_subscriptions (subscription_id, restaurant_id, customer_id, "
                     "session_id, cancelled) VALUES (?,?,?,?,?) ON CONFLICT(subscription_id) DO UPDATE SET "
                     "cancelled=MAX(stripe_duplicate_subscriptions.cancelled, excluded.cancelled)",
                     (subscription_id, restaurant_id, customer_id, session_id, 1 if cancelled else 0))
        conn.commit()
    finally:
        conn.close()


def duplicate_subscription(subscription_id=None, customer_id=None, db_path=None):
    """The duplicate-checkout row this subscription (or customer) belongs to."""
    if not subscription_id and not customer_id:
        return None
    conn = get_conn(db_path)
    try:
        if subscription_id:
            row = conn.execute("SELECT * FROM stripe_duplicate_subscriptions WHERE subscription_id=?",
                               (subscription_id,)).fetchone()
            if row:
                return dict(row)
        if customer_id:
            # Only a customer no restaurant is billed to: a duplicate checkout
            # normally made its own Stripe customer.
            row = conn.execute(
                "SELECT d.* FROM stripe_duplicate_subscriptions d WHERE d.customer_id=? "
                "AND NOT EXISTS (SELECT 1 FROM restaurants r WHERE r.stripe_customer_id=d.customer_id) "
                "LIMIT 1", (customer_id,)).fetchone()
            if row:
                return dict(row)
        return None
    finally:
        conn.close()


def module_mismatch(restaurant_id, stripe_keys, db_path=None):
    """{'stripe': [...], 'local': [...]} when what Stripe bills (module_keys)
    and the local module flags disagree, else None. Nothing is compared when
    Stripe carries no module_keys — an empty list is not "no modules"."""
    keys = sorted({k.strip().lower() for k in (stripe_keys or "").split(",") if k.strip()}
                  & {"reviews", "labor", "inventory", "marketing"})
    if not keys:
        return None
    r = _models.get_restaurant(restaurant_id) if db_path is None else _models.get_restaurant(restaurant_id, db_path)
    if not r:
        return None
    local = sorted(k for k in ("reviews", "labor", "inventory", "marketing") if getattr(r, f"module_{k}", 0))
    return None if local == keys else {"stripe": keys, "local": local}


def record_module_mismatch(restaurant_id, stripe_keys=None, db_path=None):
    """Recompute and store stripe_subscriptions.module_mismatch (#106)."""
    row = mirror_row(restaurant_id, db_path)
    if not row:
        return None
    mm = module_mismatch(restaurant_id, stripe_keys if stripe_keys is not None else row.get("module_keys"),
                         db_path)
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE stripe_subscriptions SET module_mismatch=? WHERE restaurant_id=?",
                     (json.dumps(dict(mm, at=_stamp())) if mm else None, restaurant_id))
        conn.commit()
    finally:
        conn.close()
    return mm


def billed_mrr(row) -> float:
    """Monthly revenue one mirror row bills: amount ÷ interval months, after
    its discount. 0 for anything not live or not yet priced."""
    if not row or not is_live_status(row.get("status")) or not row.get("amount_cents"):
        return 0.0
    if (row.get("status") or "") == "trialing":
        return 0.0
    months = _MONTHS_PER.get((row.get("interval") or "month"), 1) * int(row.get("interval_count") or 1)
    amt = row["amount_cents"] / 100.0 * (1 - float(row.get("discount_pct") or 0) / 100.0)
    return round(amt / months, 2) if months else 0.0


# ── coverage: which location pays for which (owner decision 1) ──────────────

def billing_group_ids(restaurant_id):
    """Every location billed together with this one (webhook_routes'
    rule: the location group scoped to its owner, else the Stripe customer)."""
    from webhook_routes import _sibling_restaurant_ids
    return _sibling_restaurant_ids(restaurant_id)


def subscription_scope(payer_id, subscription_id=None, db_path=None):
    """The locations one subscription covers: the paying location, and every
    location of its group that has no live subscription of its own. One
    subscription per group (owner decision 1) — state follows the
    subscription, so a location billed separately is never moved by
    another location's card, cancellation or dispute."""
    out = [payer_id]
    for rid in billing_group_ids(payer_id):
        if rid == payer_id:
            continue
        own = live_subscription(rid, db_path)
        if own and own.get("subscription_id") != subscription_id:
            continue
        out.append(rid)
    return out


def billed_by(restaurant_id, db_path=None):
    """The location whose subscription pays for this one: itself when it has a
    live subscription, the group member that has one when it is covered, or
    None when nothing live covers it."""
    if live_subscription(restaurant_id, db_path):
        return restaurant_id
    payers = [rid for rid in billing_group_ids(restaurant_id)
              if rid != restaurant_id and live_subscription(rid, db_path)]
    return min(payers) if payers else None


def billing_coverage(db_path=None) -> dict:
    """{restaurant_id: payer id or None} for every restaurant, in two
    queries — the console's bulk billed_by. MRR counts each payer's
    subscription once; a covered location reads "billed by <payer>"."""
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, location_group, owner_email, stripe_customer_id FROM restaurants")]
        live = {r["restaurant_id"] for r in conn.execute(
            "SELECT restaurant_id, status FROM stripe_subscriptions") if is_live_status(r["status"])}
    finally:
        conn.close()

    def _members(r):
        group = (r.get("location_group") or "").strip()
        owner = (r.get("owner_email") or "").strip().lower()
        cust = (r.get("stripe_customer_id") or "").strip()
        if group:
            return [x["id"] for x in rows if (x.get("location_group") or "") == group
                    and (x.get("owner_email") or "").strip().lower() == owner]
        if cust:
            return [x["id"] for x in rows if (x.get("stripe_customer_id") or "") == cust]
        return [r["id"]]

    out = {}
    for r in rows:
        if r["id"] in live:
            out[r["id"]] = r["id"]
            continue
        payers = [m for m in _members(r) if m != r["id"] and m in live]
        out[r["id"]] = min(payers) if payers else None
    return out


# ── invoices (stripe_invoices) ───────────────────────────────────────────────

def _line_is_recurring(line) -> bool:
    if (_g(line, "type") or "") == "subscription":
        return True
    parent = _g(line, "parent") or {}
    if (_g(parent, "type") or "") == "subscription_item_details":
        return True
    return bool(_recurring(_price(line)))


def invoice_facts(inv) -> dict:
    """What an invoice billed, split into setup (one-time) and recurring."""
    lines = _list(_g(inv, "lines"))
    rec = setup = 0
    for ln in lines:
        amt = int(_g(ln, "amount") or 0)
        if _line_is_recurring(ln):
            rec += amt
        else:
            setup += amt
    discounts = _list(_g(inv, "total_discount_amounts"))
    discount = sum(int(_g(d, "amount") or 0) for d in discounts) if discounts else None
    tax = _g(inv, "tax")
    if tax is None:
        taxes = _list(_g(inv, "total_taxes")) or _list(_g(inv, "total_tax_amounts"))
        tax = sum(int(_g(t, "amount") or 0) for t in taxes) if taxes else None
    if not lines and (_g(inv, "billing_reason") or "") in ("subscription_cycle", "subscription_update"):
        # No line items on the event (a trimmed payload): a renewal is the
        # retainer by definition. A subscription_create invoice is not —
        # it carries the setup fee beside a $0 trial line.
        rec = int(_g(inv, "amount_paid") or _g(inv, "amount_due") or 0)
        lines = [None]
    kind = ("mixed" if rec and setup else "recurring" if rec else "setup" if setup else "other")
    parent = _g(inv, "parent") or {}
    sub_details = _g(parent, "subscription_details") or _g(inv, "subscription_details") or {}
    sub_id = _g(inv, "subscription") or _g(sub_details, "subscription")
    if sub_id is not None and not isinstance(sub_id, str):
        sub_id = _g(sub_id, "id")
    charge = _g(inv, "charge")
    if charge is not None and not isinstance(charge, str):
        charge = _g(charge, "id")
    pi = _g(inv, "payment_intent")
    if pi is not None and not isinstance(pi, str):
        pi = _g(pi, "id")
    transitions = _g(inv, "status_transitions") or {}
    customer = _g(inv, "customer")
    if customer is not None and not isinstance(customer, str):
        customer = _g(customer, "id")
    return {
        "invoice_id": _g(inv, "id"),
        "customer_id": customer,
        "subscription_id": sub_id,
        "status": _g(inv, "status"),
        "billing_reason": _g(inv, "billing_reason"),
        "kind": kind,
        "currency": _g(inv, "currency"),
        "subtotal_cents": _cents(_g(inv, "subtotal")),
        "discount_cents": discount,
        "tax_cents": _cents(tax),
        "total_cents": _cents(_g(inv, "total")),
        "amount_due_cents": _cents(_g(inv, "amount_due")),
        "amount_paid_cents": _cents(_g(inv, "amount_paid")),
        "amount_remaining_cents": _cents(_g(inv, "amount_remaining")),
        "recurring_cents": rec if lines else None,
        "setup_cents": setup if lines else None,
        "attempt_count": _cents(_g(inv, "attempt_count")),
        "next_payment_attempt": _iso(_g(inv, "next_payment_attempt")),
        "hosted_invoice_url": _g(inv, "hosted_invoice_url"),
        "invoice_pdf": _g(inv, "invoice_pdf"),
        "charge_id": charge,
        "payment_intent_id": pi,
        "period_start": _iso(_g(inv, "period_start")),
        "period_end": _iso(_g(inv, "period_end")),
        "created": _iso(_g(inv, "created")),
        "paid_at": _iso(_g(transitions, "paid_at")),
    }


def invoice_metadata(inv) -> dict:
    """The subscription's metadata as an invoice carries it (the API has
    moved it twice), falling back to the invoice's own and its lines'."""
    parent = _g(inv, "parent") or {}
    for src in (_g(_g(parent, "subscription_details") or {}, "metadata"),
                _g(_g(inv, "subscription_details") or {}, "metadata"),
                _g(inv, "metadata")):
        if src and _g(src, "restaurant_id"):
            return src
    for ln in _list(_g(inv, "lines")):
        m = _g(ln, "metadata")
        if m and _g(m, "restaurant_id"):
            return m
    return {}


def record_invoice(inv, restaurant_id=None, event_id=None, failed=False, paid=False, db_path=None) -> dict:
    """Upsert one invoice's facts. `failed`/`paid` stamp the moment."""
    f = invoice_facts(inv)
    iid = f.get("invoice_id")
    if not iid:
        return f
    f["restaurant_id"] = restaurant_id
    f["last_event_id"] = event_id
    if failed:
        f["last_failed_at"] = _stamp()
    if paid and not f.get("paid_at"):
        f["paid_at"] = _stamp()
    conn = get_conn(db_path)
    try:
        cols = [k for k in f if k != "invoice_id"]
        # A later event never blanks what an earlier one knew — except the
        # next retry, whose absence on a final failure IS the news.
        conn.execute(
            f"INSERT INTO stripe_invoices (invoice_id, {', '.join(cols)}, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in cols)}, datetime('now')) "
            "ON CONFLICT(invoice_id) DO UPDATE SET "
            + ", ".join((f"{c}=excluded.{c}" if c == "next_payment_attempt"
                         else f"{c}=COALESCE(excluded.{c}, stripe_invoices.{c})") for c in cols)
            + ", updated_at=datetime('now')",
            [iid] + [f[c] for c in cols])
        conn.commit()
    finally:
        conn.close()
    return f


def invoice_row(invoice_id, db_path=None):
    if not invoice_id:
        return None
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT * FROM stripe_invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def restaurant_for_charge(charge_id=None, invoice_id=None, payment_intent_id=None, db_path=None):
    """A charge's restaurant from the invoice it paid, when we have it."""
    conn = get_conn(db_path)
    try:
        for col, val in (("invoice_id", invoice_id), ("charge_id", charge_id),
                         ("payment_intent_id", payment_intent_id)):
            if not val:
                continue
            row = conn.execute(f"SELECT restaurant_id, subscription_id FROM stripe_invoices WHERE {col}=? "
                               "AND restaurant_id IS NOT NULL ORDER BY created DESC LIMIT 1",
                               (val,)).fetchone()
            if row:
                return row["restaurant_id"], row["subscription_id"]
        return None, None
    finally:
        conn.close()


def latest_open_invoice(restaurant_id, db_path=None):
    """The restaurant's newest unpaid invoice with a hosted link, if any."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM stripe_invoices WHERE restaurant_id=? AND status='open' "
            "AND COALESCE(amount_remaining_cents, amount_due_cents, 0) > 0 "
            "ORDER BY created DESC, updated_at DESC LIMIT 1", (restaurant_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def fix_payment_url(restaurant, prefer_invoice=True):
    """Where a client fixes a failed card (#6): the open invoice's hosted page
    (pay what failed, with a new card) when there is one and
    `prefer_invoice`, else a Stripe Billing Portal session (update the card
    on file). None without a Stripe customer, or when Stripe is unreachable.
    Portal sessions are short-lived, so emails link to /pay/<token>/card,
    which calls this at click time."""
    if restaurant is None:
        return None
    if prefer_invoice:
        inv = latest_open_invoice(restaurant.id)
        if inv and inv.get("hosted_invoice_url"):
            return inv["hosted_invoice_url"]
    cid = (getattr(restaurant, "stripe_customer_id", "") or "").strip()
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not cid or not key:
        return None
    import config
    stripe_mod = config.stripe_api(key)
    if prefer_invoice:
        try:
            for i in _list(stripe_mod.Invoice.list(customer=cid, status="open", limit=1)):
                if _g(i, "hosted_invoice_url"):
                    return _g(i, "hosted_invoice_url")
        except Exception as e:
            log.warning(f"open invoice lookup failed for restaurant {restaurant.id}: {e}")
    try:
        s = stripe_mod.billing_portal.Session.create(customer=cid, return_url=config.base_url())
        return _g(s, "url")
    except Exception as e:
        log.warning(f"billing portal session failed for restaurant {restaurant.id}: {e}")
        return None


def first_paid_retainer(restaurant_id, invoice_id, db_path=None) -> bool:
    """True when `invoice_id` is the first paid invoice with a non-zero
    recurring charge for this restaurant — the moment a client converts to
    paying retainer (the 'New paying client' alert keys on it, #155)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT invoice_id FROM stripe_invoices WHERE restaurant_id=? AND status='paid' "
            "AND COALESCE(recurring_cents,0) > 0 AND COALESCE(amount_paid_cents,0) > 0 "
            "ORDER BY COALESCE(paid_at, created) ASC, invoice_id ASC LIMIT 1", (restaurant_id,)).fetchone()
        return bool(row) and row["invoice_id"] == invoice_id
    finally:
        conn.close()


# ── who billing mail goes to ─────────────────────────────────────────────────

def billing_recipients(restaurant_id, db_path=None) -> list:
    """The owner's address and every principal login's, once each."""
    out, seen = [], set()
    conn = get_conn(db_path)
    try:
        r = conn.execute("SELECT owner_email FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        cands = [r["owner_email"]] if r else []
        cands += [u["email"] for u in conn.execute(
            "SELECT email FROM users WHERE restaurant_id=? AND is_admin=0 AND is_active=1 "
            "AND COALESCE(NULLIF(role,''),'client') IN ('client','owner') "
            "AND email NOT LIKE '%@staff.invalid' ORDER BY id", (restaurant_id,))]
    finally:
        conn.close()
    for e in cands:
        k = (e or "").strip().lower()
        if k and "@" in k and k not in seen:
            seen.add(k)
            out.append((e or "").strip())
    return out


def principal_login(restaurant_id, db_path=None):
    """The account holder's login (the SEC-28 rule: an active client/owner
    login whose home is this restaurant, oldest first)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, username, email, last_login FROM users WHERE restaurant_id=? AND is_admin=0 "
            "AND is_active=1 AND COALESCE(NULLIF(role,''),'client') IN ('client','owner') "
            "AND email NOT LIKE '%@staff.invalid' ORDER BY id LIMIT 1", (restaurant_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ── the outbox (owed_sends) ─────────────────────────────────────────────────

OWED_KINDS = ("payment_link", "welcome", "receipt", "dunning", "pay_reminder", "card_update")
# Minutes before the next try after the Nth failure. Six tries across about
# two days, then 'failed' — the console raises it, ops.capture records it.
BACKOFF_MINUTES = (5, 15, 60, 240, 720, 1440)
MAX_ATTEMPTS = len(BACKOFF_MINUTES)
SEND_LOCK_MINUTES = 15
# The post-signing welcome's set-password link lasts
# models.SET_PASSWORD_LINK_HOURS (72): longer than a reset's hour, because it
# is how a brand-new owner first gets in, whenever they read it.


def enqueue(kind, restaurant_id, dedupe_key, to_email=None, payload=None, due_at=None,
            source=None, actor=None, db_path=None, conn=None):
    """Owe one email. Returns (id, created). The dedupe key makes a repeat
    of the event that owed it — a redelivered webhook, a second pass of a
    job — a no-op. Raises on a failed write: the caller's event must then
    be retried rather than acknowledged with the email lost."""
    if kind not in OWED_KINDS:
        raise ValueError(f"unknown owed send kind {kind!r}")
    own = conn is None
    c = get_conn(db_path) if own else conn
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO owed_sends (restaurant_id, kind, dedupe_key, to_email, payload_json, "
            "next_attempt_at, source, actor) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, kind, dedupe_key, to_email, json.dumps(payload or {}, default=str),
             due_at or _stamp(), source, actor))
        created = cur.rowcount == 1
        row = c.execute("SELECT id FROM owed_sends WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if own:
            c.commit()
        return (row["id"] if row else None), created
    finally:
        if own:
            c.close()


def cancel_owed(restaurant_id=None, kind=None, key_prefix=None, reason="", db_path=None) -> int:
    """Stand down owed mail that no longer applies (a paid invoice's
    dunning). Only rows not yet sent or in flight."""
    where, args = ["status='pending'"], []
    if restaurant_id is not None:
        where.append("restaurant_id=?")
        args.append(restaurant_id)
    if kind:
        where.append("kind=?")
        args.append(kind)
    if key_prefix:
        where.append("dedupe_key LIKE ?")
        args.append(key_prefix.replace("%", "") + "%")
    conn = get_conn(db_path)
    try:
        cur = conn.execute(f"UPDATE owed_sends SET status='cancelled', last_error=?, updated_at=datetime('now') "
                           f"WHERE {' AND '.join(where)}", [("cancelled: " + reason)[:300]] + args)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


class _Skip(Exception):
    """A send that no longer applies (the invoice got paid, the owner has
    signed in). Recorded as 'skipped', never retried."""


class _Permanent(Exception):
    """A send that cannot succeed by retrying (no address, no login)."""


def _outcome(res):
    """(state, error, status_code, message_id) from what a sender returned.

    E's contract (fix round): every sender returns an emails.SendResult.
    Until every sender does, a bool and a bare None are read defensively —
    None as sent-but-unverified, so an old sender can never loop the outbox."""
    if res is None:
        return "sent_unverified", "sender returned no delivery result", None, None
    if isinstance(res, bool):
        return ("sent", None, None, None) if res else ("retry", "sender returned False", None, None)
    ok = bool(getattr(res, "ok", False))
    err = getattr(res, "error", None)
    code = getattr(res, "status_code", None)
    mid = getattr(res, "message_id", None)
    if ok:
        return "sent", None, code, mid
    text = str(err or "send failed")
    low = text.lower()
    # The SendResult says which kind of failure it was (fix round E): a
    # refusal (suppressed, rejected, no recipient) will not change by
    # retrying, and a sender that decided there was nothing to send is done.
    if getattr(res, "skipped", False) is True:
        return "skipped", text, code, mid
    if getattr(res, "refused", False) is True or "suppress" in low or code in (400, 422):
        return "failed", text, code, mid
    return "retry", text, code, mid


def _payload(row):
    try:
        return json.loads(row.get("payload_json") or "{}") or {}
    except Exception:
        return {}


def _restaurant(rid):
    return _models.get_restaurant(rid) if rid else None


def _modules(r):
    return [k for k in ("reviews", "labor", "inventory", "marketing") if getattr(r, f"module_{k}", 0)]


def _is_paying(r, db_path=None):
    """Active or past-due, or covered by a paying group member."""
    if (getattr(r, "billing_status", "") or "").lower() in ("active", "past_due", "internal"):
        return True
    return billed_by(r.id, db_path) is not None


def _send_payment_link(row, db_path=None):
    import emails as _emails
    r = _restaurant(row["restaurant_id"])
    if not r:
        raise _Permanent("restaurant gone")
    if _is_paying(r, db_path):
        raise _Skip("already paying")
    mods = _modules(r)
    if not mods:
        raise _Skip("no modules on the plan")
    to = row.get("to_email") or r.owner_email
    if not to:
        raise _Permanent("no owner email")
    return _emails.send_payment_email(to_email=to, restaurant_name=r.name, module_count=len(mods),
                                      restaurant_id=r.id, modules=mods)


def _send_welcome(row, db_path=None):
    import emails as _emails
    p = _payload(row)
    r = _restaurant(row["restaurant_id"])
    if not r:
        raise _Permanent("restaurant gone")
    login = None
    if p.get("user_id"):
        conn = get_conn(db_path)
        try:
            u = conn.execute("SELECT id, username, email, last_login, is_active FROM users WHERE id=?",
                             (int(p["user_id"]),)).fetchone()
        finally:
            conn.close()
        login = dict(u) if u else None
    login = login or principal_login(r.id, db_path)
    if not login or not login.get("is_active", 1):
        raise _Permanent("no active owner login")
    if login.get("last_login"):
        # They are already in. Nothing to hand over, and never a new password.
        raise _Skip("owner has signed in")
    to = login.get("email") or row.get("to_email") or r.owner_email
    if not to:
        raise _Permanent("no address for the owner login")
    # THE welcome (emails.send_welcome_with_set_password_link — the one
    # resend-welcome and checkout provisioning send too): the username and a
    # set-password link minted at send time (models.create_set_password_token,
    # SET_PASSWORD_LINK_HOURS from the send, not the signing), never stored
    # here. No password is reset before or after: the owner chooses theirs
    # through the link (#12). The restaurant's name, modules, Place ID and
    # owner name come from its row.
    return _emails.send_welcome_with_set_password_link(user_id=login["id"], restaurant_id=r.id, to_email=to,
                                                       db_path=db_path)


def _local_mdy(r, stamp):
    from time_utils import mdy, restaurant_tz
    dt = _parse(stamp)
    if dt is None:
        return ""
    try:
        return mdy(dt.astimezone(restaurant_tz(r)))
    except Exception:
        return mdy(dt)


def _send_receipt(row, db_path=None):
    import emails as _emails
    p = _payload(row)
    r = _restaurant(row["restaurant_id"])
    if not r:
        raise _Permanent("restaurant gone")
    inv = invoice_row(p.get("invoice_id"), db_path) or {}
    paid = inv.get("amount_paid_cents") if inv.get("amount_paid_cents") is not None else p.get("amount_cents")
    if not paid:
        raise _Skip("nothing was charged")
    to = row.get("to_email") or r.owner_email
    if not to:
        raise _Permanent("no owner email")
    kind = inv.get("kind") or p.get("kind") or ""
    what = {"setup": "Setup fee", "recurring": "Retainer", "mixed": "Setup fee and retainer"}.get(kind, "Payment")
    return _emails.send_payment_receipt_email(
        to_email=to, restaurant_name=r.name, amount=paid / 100.0,
        paid_on=_local_mdy(r, inv.get("paid_at") or row.get("created_at")), description=what,
        receipt_url=inv.get("hosted_invoice_url"), restaurant_id=r.id,
        currency=(inv.get("currency") or "usd"))


def _send_dunning(row, db_path=None):
    import emails as _emails
    p = _payload(row)
    r = _restaurant(row["restaurant_id"])
    if not r:
        raise _Permanent("restaurant gone")
    inv = invoice_row(p.get("invoice_id"), db_path) or {}
    if (inv.get("status") or "open") in ("paid", "void", "uncollectible"):
        raise _Skip(f"invoice is {inv.get('status')}")
    if (r.billing_status or "").lower() not in ("past_due",):
        raise _Skip(f"account is {r.billing_status}")
    due = inv.get("amount_remaining_cents") or inv.get("amount_due_cents") or p.get("amount_cents") or 0
    return _emails.send_dunning_email(
        to_email=row["to_email"], restaurant_name=r.name, amount=due / 100.0,
        attempt=int(p.get("attempt") or inv.get("attempt_count") or 1),
        pay_url=_emails.pay_link(r.id, "invoice"), card_url=_emails.pay_link(r.id, "card"),
        next_attempt=_local_mdy(r, inv.get("next_payment_attempt") or p.get("next_attempt")),
        owner_name=r.owner_name, restaurant_id=r.id)


def _send_pay_reminder(row, db_path=None):
    import emails as _emails
    p = _payload(row)
    r = _restaurant(row["restaurant_id"])
    if not r:
        raise _Permanent("restaurant gone")
    if _is_paying(r, db_path) or r.converted_at:
        raise _Skip("already paying")
    mods = _modules(r)
    if not mods:
        raise _Skip("no modules on the plan")
    to = row.get("to_email") or r.owner_email
    if not to:
        raise _Permanent("no owner email")
    return _emails.send_pay_reminder_email(
        to_email=to, restaurant_name=r.name, module_count=len(mods),
        monthly_url=_emails.pay_link(r.id, "monthly"), annual_url=_emails.pay_link(r.id, "annual"),
        day=int(p.get("day") or 0), owner_name=r.owner_name, restaurant_id=r.id)


def _send_card_update(row, db_path=None):
    import emails as _emails
    r = _restaurant(row["restaurant_id"])
    if not r:
        raise _Permanent("restaurant gone")
    to = row.get("to_email") or r.owner_email
    if not to:
        raise _Permanent("no owner email")
    inv = latest_open_invoice(r.id, db_path)
    return _emails.send_card_update_email(
        to_email=to, restaurant_name=r.name, card_url=_emails.pay_link(r.id, "card"),
        pay_url=_emails.pay_link(r.id, "invoice") if inv else None,
        amount_due=((inv.get("amount_remaining_cents") or inv.get("amount_due_cents") or 0) / 100.0) if inv else None,
        owner_name=r.owner_name, restaurant_id=r.id)


# String dispatch: owed_sends.kind → sender. Every kind in OWED_KINDS has one.
_SENDERS = {
    "payment_link": _send_payment_link,
    "welcome": _send_welcome,
    "receipt": _send_receipt,
    "dunning": _send_dunning,
    "pay_reminder": _send_pay_reminder,
    "card_update": _send_card_update,
}


def _claim_row(conn, row_id) -> bool:
    cur = conn.execute(
        "UPDATE owed_sends SET status='sending', attempts=attempts+1, "
        "locked_until=datetime('now', ?), updated_at=datetime('now') "
        "WHERE id=? AND (status='pending' OR (status='sending' AND locked_until < datetime('now')))",
        (f"+{SEND_LOCK_MINUTES} minutes", row_id))
    conn.commit()
    return cur.rowcount == 1


def _finish(conn, row, state, error=None, code=None, message_id=None):
    attempts = int(row.get("attempts") or 0) + 1
    if state in ("sent", "sent_unverified"):
        conn.execute("UPDATE owed_sends SET status=?, sent_at=datetime('now'), last_error=?, last_status_code=?, "
                     "message_id=?, locked_until=NULL, updated_at=datetime('now') WHERE id=?",
                     (state, error, code, message_id, row["id"]))
    elif state == "skipped":
        conn.execute("UPDATE owed_sends SET status='skipped', last_error=?, locked_until=NULL, "
                     "updated_at=datetime('now') WHERE id=?", (("skipped: " + (error or ""))[:300], row["id"]))
    elif state == "retry" and attempts < MAX_ATTEMPTS:
        wait = BACKOFF_MINUTES[min(attempts, MAX_ATTEMPTS) - 1]
        conn.execute("UPDATE owed_sends SET status='pending', next_attempt_at=datetime('now', ?), last_error=?, "
                     "last_status_code=?, locked_until=NULL, updated_at=datetime('now') WHERE id=?",
                     (f"+{wait} minutes", (error or "")[:300], code, row["id"]))
    else:
        conn.execute("UPDATE owed_sends SET status='failed', last_error=?, last_status_code=?, locked_until=NULL, "
                     "updated_at=datetime('now') WHERE id=?", ((error or "failed")[:300], code, row["id"]))
    conn.commit()
    return "failed" if state == "retry" and attempts >= MAX_ATTEMPTS else state


def drain_owed_sends(ids=None, kinds=None, limit=50, max_seconds=90, db_path=None) -> dict:
    """Send what is owed and due. Each row is claimed (status 'sending' with a
    lock) before its send, so two drains — the scheduler's and an inline one
    after a webhook — never send one row twice. Returns the sweep counts
    plus `results` [{id, kind, state, error}] for callers reporting one send.
    """
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "retried": 0,
           "results": []}
    where = ["(status='pending' AND next_attempt_at <= datetime('now') "
             "OR status='sending' AND locked_until < datetime('now'))"]
    args = []
    if ids:
        ids = [int(i) for i in ids if i]
        if not ids:
            return out
        where.append(f"id IN ({','.join('?' for _ in ids)})")
        args += ids
    if kinds:
        where.append(f"kind IN ({','.join('?' for _ in kinds)})")
        args += list(kinds)
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM owed_sends WHERE {' AND '.join(where)} ORDER BY next_attempt_at, id LIMIT ?",
            args + [int(limit)])]
    finally:
        conn.close()
    if not rows:
        return out
    if not _sending_allowed():
        # Left owed, untouched: the production drain sends them.
        out["skipped"] = len(rows)
        out["results"] = [{"id": r["id"], "kind": r["kind"], "state": "held",
                           "error": "this server does not send client email (not Railway)"} for r in rows]
        return out
    started = time.time()
    for row in rows:
        if time.time() - started > max_seconds:
            out["hit_bound"] = True
            break
        conn = get_conn(db_path)
        try:
            if not _claim_row(conn, row["id"]):
                continue
        finally:
            conn.close()
        out["attempted"] += 1
        sender = _SENDERS.get(row["kind"])
        error = code = mid = None
        try:
            if sender is None:
                raise _Permanent(f"no sender for {row['kind']}")
            state, error, code, mid = _outcome(sender(row, db_path))
        except _Skip as s:
            state, error = "skipped", str(s)
        except _Permanent as p:
            state, error = "failed", str(p)
        except Exception as e:
            state, error = "retry", f"{type(e).__name__}: {e}"
        conn = get_conn(db_path)
        try:
            final = _finish(conn, row, state, error, code, mid)
        finally:
            conn.close()
        if final in ("sent", "sent_unverified"):
            out["ok"] += 1
        elif final == "skipped":
            out["skipped"] += 1
        elif final == "retry":
            out["retried"] += 1
        else:
            out["failed"] += 1
            _report_failed(row, error)
        out["results"].append({"id": row["id"], "kind": row["kind"], "state": final,
                               "error": (error or None)})
    return out


def _report_failed(row, error):
    """A send the outbox gave up on is an issue, not a print (#12)."""
    try:
        import ops
        ops.capture(RuntimeError(f"{row['kind']} email to {_mask(row.get('to_email'))} was not delivered: "
                                 f"{(error or 'failed')[:200]}"),
                    job=f"owed_send:{row['kind']}",
                    context=f"restaurant_id={row.get('restaurant_id')} owed_send={row['id']}")
    except Exception:
        pass


def run_owed_sends() -> dict:
    """Scheduled every tick: send whatever is owed and due."""
    return drain_owed_sends(limit=100, max_seconds=120)


def owed_sends_for(restaurant_id, limit=40, db_path=None) -> list:
    """The console's view of one client's owed billing mail."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, kind, dedupe_key, to_email, status, attempts, next_attempt_at, last_error, "
            "last_status_code, created_at, sent_at, source, actor FROM owed_sends WHERE restaurant_id=? "
            "ORDER BY id DESC LIMIT ?", (restaurant_id, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ── dunning (#25) ────────────────────────────────────────────────────────────

DUNNING_ATTEMPTS = (1, 2, 3)


def enqueue_dunning(restaurant_id, invoice_id, attempt, amount_cents=None, next_attempt=None,
                    source="stripe", db_path=None):
    """One owed dunning email per recipient — the owner's address and every
    principal login's — for this invoice attempt. Returns (ids, created)."""
    if not invoice_id or attempt not in DUNNING_ATTEMPTS:
        return [], 0
    ids, created = [], 0
    for to in billing_recipients(restaurant_id, db_path):
        rid, made = enqueue("dunning", restaurant_id, f"dunning:{invoice_id}:{attempt}:{to.lower()}",
                            to_email=to, source=source, db_path=db_path,
                            payload={"invoice_id": invoice_id, "attempt": attempt,
                                     "amount_cents": amount_cents, "next_attempt": next_attempt})
        if rid:
            ids.append(rid)
        created += 1 if made else 0
    return ids, created


def run_dunning(db_path=None) -> dict:
    """Hourly. The webhook owes a dunning email on each failed attempt; this
    is the safety net for one it could not (a lost event, a failed write),
    then stands down dunning for invoices since paid, then sends."""
    enq = 0
    conn = get_conn(db_path)
    try:
        open_rows = [dict(r) for r in conn.execute(
            "SELECT i.invoice_id, i.restaurant_id, i.attempt_count, i.amount_remaining_cents, "
            "i.amount_due_cents, i.next_payment_attempt FROM stripe_invoices i "
            "JOIN restaurants r ON r.id = i.restaurant_id "
            "WHERE i.status='open' AND COALESCE(i.attempt_count,0) BETWEEN 1 AND 3 "
            "AND LOWER(COALESCE(r.billing_status,''))='past_due'")]
        paid = [r["invoice_id"] for r in conn.execute(
            "SELECT DISTINCT i.invoice_id FROM stripe_invoices i JOIN owed_sends o "
            "ON o.dedupe_key LIKE 'dunning:' || i.invoice_id || ':%' "
            "WHERE o.status='pending' AND i.status IN ('paid','void','uncollectible')")]
    finally:
        conn.close()
    for inv in paid:
        cancel_owed(kind="dunning", key_prefix=f"dunning:{inv}:", reason="invoice settled", db_path=db_path)
    for row in open_rows:
        _ids, made = enqueue_dunning(row["restaurant_id"], row["invoice_id"], int(row["attempt_count"]),
                                     row["amount_remaining_cents"] or row["amount_due_cents"],
                                     row["next_payment_attempt"], source="run_dunning", db_path=db_path)
        enq += made
    out = drain_owed_sends(kinds=("dunning",), limit=100, max_seconds=120, db_path=db_path)
    out["enqueued"] = enq
    return out


# ── the contract-to-payment chase (#26) ──────────────────────────────────────

# Days after signing on which an unpaid client gets their pay link again.
PAY_REMINDER_DAYS = (2, 5, 9)
# A reminder more than this many days late is not sent (a deploy must not
# mail every client who signed months ago); the console's "signed, never
# paid" issue covers them instead.
PAY_REMINDER_GRACE_DAYS = 2
# The day the console turns an unpaid signature into an issue (#26: after
# the last reminder has had a day to work).
CHASE_ISSUE_DAY = 10


def contract_pipeline(db_path=None, now=None) -> list:
    """Signed, never-paid restaurants and where their chase stands — the
    data the console raises "signed N days ago, never paid" from (C's part
    of #26; trials never expire, owner decision 6)."""
    now = now or _now()
    conn = get_conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, billing_status, contract_signed_at, converted_at, is_demo "
            "FROM restaurants WHERE contract_status='signed' AND contract_signed_at IS NOT NULL "
            "AND converted_at IS NULL AND COALESCE(is_demo,0)=0 "
            "AND LOWER(COALESCE(billing_status,'')) IN ('', 'trial', 'pending')")]
        sent = {}
        for o in conn.execute("SELECT restaurant_id, dedupe_key, status FROM owed_sends "
                              "WHERE kind='pay_reminder'"):
            try:
                day = int(o["dedupe_key"].rsplit(":", 1)[1])
            except (ValueError, IndexError):
                continue
            sent.setdefault(o["restaurant_id"], {})[day] = o["status"]
    finally:
        conn.close()
    cover = billing_coverage(db_path)
    out = []
    for r in rows:
        if cover.get(r["id"]):
            continue                   # covered by a paying location
        signed = _parse(r["contract_signed_at"])
        if not signed:
            continue
        days = (now - signed).days
        out.append({"restaurant_id": r["id"], "name": r["name"], "signed_at": r["contract_signed_at"],
                    "days_since_signed": days, "reminders": sent.get(r["id"], {}),
                    "overdue": days >= CHASE_ISSUE_DAY})
    return out


def run_contract_chase(db_path=None, now=None) -> dict:
    """Daily. Re-send the pay link on days 2, 5 and 9 after signing to a
    client who has not paid; nothing is expired (owner decision 6)."""
    enq = skipped = 0
    for p in contract_pipeline(db_path, now):
        for day in PAY_REMINDER_DAYS:
            if p["days_since_signed"] < day or day in p["reminders"]:
                continue
            if p["days_since_signed"] > day + PAY_REMINDER_GRACE_DAYS:
                skipped += 1
                continue
            r = _restaurant(p["restaurant_id"])
            _id, created = enqueue("pay_reminder", p["restaurant_id"],
                                   f"pay_reminder:{p['restaurant_id']}:{day}",
                                   to_email=getattr(r, "owner_email", None), payload={"day": day},
                                   source="run_contract_chase", db_path=db_path)
            enq += 1 if created else 0
    out = drain_owed_sends(kinds=("pay_reminder",), limit=100, max_seconds=120, db_path=db_path)
    out["enqueued"] = enq
    out["skipped"] = out.get("skipped", 0) + skipped
    return out


# ── nightly reconcile (#115, #15) ────────────────────────────────────────────

RECONCILE_CURSOR_KEY = "stripe_reconcile_cursor"
RECONCILE_MAX_SECONDS = int(os.getenv("STRIPE_RECONCILE_MAX_SECONDS", str(15 * 60)))


def _expected_local(stripe_status, paused):
    s = (stripe_status or "").lower()
    if paused:
        return "paused"
    if s in ("active", "trialing"):
        return "active"
    if s in ("past_due", "paused"):
        return "past_due"
    if s in ENDED_SUB_STATES:
        return "churned"
    return None


def _write_reconcile(rid, local, stripe_status, sub_id, mismatches, db_path=None):
    conn = get_conn(db_path)
    try:
        prev = conn.execute("SELECT mismatches, first_seen_at FROM billing_reconcile WHERE restaurant_id=?",
                            (rid,)).fetchone()
        first = (prev["first_seen_at"] if prev and prev["mismatches"] and mismatches else
                 (_stamp() if mismatches else None))
        conn.execute(
            "INSERT INTO billing_reconcile (restaurant_id, checked_at, local_status, stripe_status, "
            "subscription_id, mismatches, first_seen_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(restaurant_id) DO UPDATE SET checked_at=excluded.checked_at, "
            "local_status=excluded.local_status, stripe_status=excluded.stripe_status, "
            "subscription_id=excluded.subscription_id, mismatches=excluded.mismatches, "
            "first_seen_at=excluded.first_seen_at",
            (rid, _stamp(), local, stripe_status, sub_id, json.dumps(mismatches) if mismatches else None, first))
        conn.commit()
    finally:
        conn.close()


def cancel_subscription(restaurant_id, actor, reason="offboarding", stripe_mod=None, db_path=None) -> dict:
    """Cancel this restaurant's live Stripe subscription now (no further
    invoices) — the offboarding checklist's 'stripe' step (fix round B2 #34,
    carried out by the integration wave: it was a step marked by hand after
    cancelling in the Stripe dashboard). {ok, cancelled: [subscription ids],
    note, error}.

    Refused: on a server that is not production (a laptop holds production's
    Stripe key: _sending_allowed), and for a location COVERED by another's
    subscription — one subscription per group (owner decision 1), so
    cancelling it here would end every location's service; change the plan
    on the paying location instead. Idempotent: nothing live is ok with
    nothing cancelled. The mirror row is updated from Stripe's answer;
    billing_status follows Stripe's customer.subscription.deleted event, as
    every cancellation does (and the nightly reconcile catches a lost one)."""
    r = _restaurant(restaurant_id)
    if not r:
        return {"ok": False, "error": "Restaurant not found."}
    payer = billed_by(restaurant_id, db_path)
    if payer and payer != restaurant_id:
        p = _restaurant(payer)
        return {"ok": False, "covered": True, "billed_by": payer, "error": (
            f"{r.name} is billed with {getattr(p, 'name', 'another location')}'s subscription, which covers every "
            f"location of the group — cancelling it would end all of them. Change the plan on the paying "
            f"location, then mark this step skipped with that note.")}
    mirror = live_subscription(restaurant_id, db_path)
    cid = (r.stripe_customer_id or "").strip()
    if not mirror and not cid:
        return {"ok": True, "cancelled": [], "note": "No Stripe customer and no live subscription: nothing to cancel."}
    if not _sending_allowed():
        return {"ok": False, "local_backend": True, "error": (
            "Refused: this server is not the production server, so it does not change live Stripe "
            "subscriptions. Cancel from production, or in the Stripe dashboard and mark the step done.")}
    if stripe_mod is None:
        key = os.getenv("STRIPE_SECRET_KEY", "")
        if not key:
            return {"ok": False, "error": "Stripe is not configured on this server — nothing was cancelled."}
        import config
        stripe_mod = config.stripe_api(key)
    targets = []
    if mirror and mirror.get("subscription_id"):
        targets.append(mirror["subscription_id"])
    if cid:
        # Anything else live on the customer (a subscription the mirror never
        # heard of) ends too: the account is leaving.
        for sub in _list(stripe_mod.Subscription.list(customer=cid, status="all", limit=20)):
            sid = _g(sub, "id")
            if sid and sid not in targets and is_live_status(_g(sub, "status")):
                targets.append(sid)
    cancelled, errors = [], []
    for sid in targets:
        try:
            done = stripe_mod.Subscription.cancel(sid)
            cancelled.append(sid)
            if mirror and sid == mirror.get("subscription_id") and _g(done, "id"):
                upsert_subscription(restaurant_id, facts=subscription_facts(done), subscription_id=sid,
                                    db_path=db_path)
        except Exception as e:
            text = str(e)
            if "canceled" in text.lower() or "No such subscription" in text:
                continue                    # already over at Stripe: nothing left to end
            errors.append(f"{sid}: {text[:200]}")
    try:
        import admin_events
        admin_events.record_admin_action(
            actor, "billing.subscription_cancelled", restaurant_id=restaurant_id,
            target=f"stripe_customer:{cid}" if cid else None,
            before={"subscriptions": targets, "billing_status": r.billing_status},
            after={"cancelled": cancelled, "errors": errors or None, "reason": reason},
            result="ok" if not errors else ("partial" if cancelled else "failed"))
    except Exception:
        pass
    if errors:
        return {"ok": False, "cancelled": cancelled,
                "error": "Stripe did not cancel " + "; ".join(errors) + ". Cancel it in the Stripe dashboard."}
    return {"ok": True, "cancelled": cancelled,
            "note": ("Cancelled " + ", ".join(cancelled)) if cancelled else "Nothing live to cancel."}


def reconcile_one(restaurant_id, stripe_mod=None, db_path=None) -> dict:
    """Refresh one restaurant's mirror from Stripe and record every way local
    state disagrees with it. Records, never repairs: the console raises each
    mismatch as an issue for a person to decide (#115)."""
    r = _restaurant(restaurant_id)
    if not r:
        return {"skipped": True}
    local = (r.billing_status or "").lower()
    lock = _models.pause_lock(r)
    mismatches = []
    stripe_status = sub_id = None
    cid = (r.stripe_customer_id or "").strip()
    covered_by = billed_by(restaurant_id, db_path)
    if not cid:
        if local in ("active", "past_due") and not (covered_by and covered_by != restaurant_id):
            mismatches.append({"kind": "no_stripe_customer",
                               "detail": f"local status is {local} but no Stripe customer is stored"})
        _write_reconcile(restaurant_id, local, None, None, mismatches, db_path)
        return {"mismatches": mismatches}
    if stripe_mod is None:
        return {"skipped": True}
    try:
        cust = stripe_mod.Customer.retrieve(cid)
        if _g(cust, "deleted"):
            mismatches.append({"kind": "customer_deleted", "detail": f"Stripe customer {cid} is deleted"})
    except Exception as e:
        msg = str(e)
        if "No such customer" in msg or "resource_missing" in msg:
            mismatches.append({"kind": "customer_missing", "detail": f"Stripe has no customer {cid}"})
        else:
            raise
    subs = _list(stripe_mod.Subscription.list(customer=cid, status="all", limit=10))
    mirror = mirror_row(restaurant_id, db_path)
    owned = []
    for s in subs:
        meta_rid = _g(_g(s, "metadata") or {}, "restaurant_id")
        try:
            meta_rid = int(str(meta_rid).strip()) if meta_rid else None
        except ValueError:
            meta_rid = None
        if meta_rid and meta_rid != restaurant_id:
            continue                   # a group member's own subscription
        owned.append(s)
    live = [s for s in owned if (_g(s, "status") or "") in LIVE_SUB_STATES
            and not duplicate_subscription(_g(s, "id"), db_path=db_path)]
    tracked = next((s for s in owned if mirror and _g(s, "id") == mirror.get("subscription_id")), None)
    current = tracked if (tracked is not None and (_g(tracked, "status") in LIVE_SUB_STATES or not live)) else (
        live[0] if live else (owned[0] if owned else None))
    if len(live) > 1:
        mismatches.append({"kind": "multiple_live_subscriptions",
                           "detail": ", ".join(str(_g(s, "id")) for s in live)})
    if current is not None:
        sub_id = _g(current, "id")
        stripe_status = _g(current, "status")
        facts = subscription_facts(current)
        upsert_subscription(restaurant_id, facts=facts, subscription_id=sub_id, db_path=db_path)
        mm = record_module_mismatch(restaurant_id, facts.get("module_keys"), db_path)
        if mm:
            mismatches.append({"kind": "modules", "detail": f"Stripe bills {','.join(mm['stripe'])}; "
                                                            f"flags are {','.join(mm['local']) or 'none'}"})
        paused = bool(_g(current, "pause_collection"))
        want = _expected_local(stripe_status, paused)
        if want and local != want and not (lock and local == "paused"):
            if not (want == "active" and local in ("internal",)):
                mismatches.append({"kind": "status", "detail": f"local {local or '(none)'}, Stripe {stripe_status}"
                                   + (" (collection paused)" if paused else "")})
    elif local in ("active", "past_due") and not (covered_by and covered_by != restaurant_id):
        mismatches.append({"kind": "no_subscription", "detail": f"local status is {local} but Stripe has "
                                                              f"no subscription for {cid}"})
    _write_reconcile(restaurant_id, local, stripe_status, sub_id, mismatches, db_path)
    return {"mismatches": mismatches}


def reconcile_stripe(db_path=None) -> dict:
    """Nightly. Every restaurant with a Stripe customer, or one the product
    thinks is paying: refresh its mirror and record mismatches. Bounded and
    resumable (the run_daily_fetch pattern, via scheduler.resumable_sweep)."""
    out = {"attempted": 0, "ok": 0, "failed": 0, "skipped": 0, "hit_bound": False, "mismatched": 0}
    key = os.getenv("STRIPE_SECRET_KEY", "")
    conn = get_conn(db_path)
    try:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM restaurants WHERE COALESCE(is_demo,0)=0 AND ("
            "COALESCE(stripe_customer_id,'') <> '' OR LOWER(COALESCE(billing_status,'')) IN ('active','past_due') "
            "OR id IN (SELECT restaurant_id FROM stripe_subscriptions))")]
    finally:
        conn.close()
    if not ids:
        return out
    stripe_mod = None
    if key:
        import config
        stripe_mod = config.stripe_api(key)
    else:
        log.warning("reconcile_stripe: STRIPE_SECRET_KEY not set — only local checks run")

    def _one(rid):
        out["attempted"] += 1
        try:
            res = reconcile_one(rid, stripe_mod, db_path)
        except Exception:
            out["failed"] += 1
            raise
        if res.get("skipped"):
            out["skipped"] += 1
        else:
            out["ok"] += 1
            if res.get("mismatches"):
                out["mismatched"] += 1

    import scheduler
    _done, hit = scheduler.resumable_sweep(RECONCILE_CURSOR_KEY, ids, _one, RECONCILE_MAX_SECONDS,
                                           workers=1, job="stripe_reconcile")
    out["hit_bound"] = bool(hit)
    return out


def reconcile_findings(db_path=None) -> list:
    """Every restaurant whose last reconcile found a mismatch."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT b.*, r.name FROM billing_reconcile b LEFT JOIN restaurants r "
                            "ON r.id=b.restaurant_id WHERE b.mismatches IS NOT NULL ORDER BY b.restaurant_id").fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        d = dict(row)
        try:
            d["mismatches"] = json.loads(d["mismatches"] or "[]")
        except Exception:
            d["mismatches"] = []
        out.append(d)
    return out


def billing_history(restaurant_id, limit=100, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM billing_status_history WHERE restaurant_id=? ORDER BY id DESC LIMIT ?",
            (restaurant_id, int(limit)))]
    finally:
        conn.close()


def invoices_for(restaurant_id, limit=24, db_path=None) -> list:
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM stripe_invoices WHERE restaurant_id=? ORDER BY created DESC LIMIT ?",
            (restaurant_id, int(limit)))]
    finally:
        conn.close()
