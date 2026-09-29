"""
admin_events.py — the events the business runs on, kept.

Stripe and DocuSign webhooks used to act (flip billing_status, email Will)
and forget. Every event now lands here too, so the admin console's Billing
page and a client's Subscription tab can show payment and contract history
instead of only the current flag.
"""
import json

def _ensure(conn):
    """admin_events is created by models.init_db now; kept so callers read the same."""
    return None


def _resolve_restaurant(conn, customer_id=None, email=None, envelope_id=None):
    try:
        if customer_id:
            r = conn.execute("SELECT id FROM restaurants WHERE stripe_customer_id=? LIMIT 1", (customer_id,)).fetchone()
            if r:
                return r[0]
        if envelope_id:
            r = conn.execute("SELECT id FROM restaurants WHERE docusign_envelope_id=? LIMIT 1", (envelope_id,)).fetchone()
            if r:
                return r[0]
        if email:
            r = conn.execute("SELECT r.id FROM restaurants r LEFT JOIN users u ON u.restaurant_id=r.id "
                             "WHERE lower(r.owner_email)=lower(?) OR lower(u.email)=lower(?) LIMIT 1", (email, email)).fetchone()
            if r:
                return r[0]
    except Exception:
        pass
    return None


def record(source, event_type, restaurant_id=None, customer_id=None, email=None, amount=None,
           summary=None, payload=None, envelope_id=None, db_path=None):
    """Never raises — a history table must not break the webhook it records."""
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        _ensure(conn)
        if restaurant_id is None:
            restaurant_id = _resolve_restaurant(conn, customer_id, email, envelope_id)
        body = None
        if payload is not None:
            try:
                body = json.dumps(payload, default=str)[:4000]
            except Exception:
                body = str(payload)[:4000]
        conn.execute(
            "INSERT INTO admin_events (source, event_type, restaurant_id, customer_id, email, amount, summary, payload) VALUES (?,?,?,?,?,?,?,?)",
            (source, event_type, restaurant_id, customer_id, email, amount, (summary or "")[:300], body),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def _stripe_amount_cents(t, obj):
    """The money an event is about: what an invoice collected (paid) or
    asks for, what a refund returned — amount_refunded, never the charge's
    full amount (#50: a $5 goodwill refund on a $750 charge read "$750.00
    refunded") — and what a dispute holds."""
    if t.startswith("invoice."):
        return (obj.get("amount_paid") if t == "invoice.paid" else obj.get("amount_due")) or 0
    if t == "charge.refunded":
        return obj.get("amount_refunded") or 0
    if t.startswith("charge."):
        return obj.get("amount") or 0
    return None


def _stripe_summary(event):
    t = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if t.startswith("invoice."):
        amt = _stripe_amount_cents(t, obj)
        return f"{t.split('.', 1)[1].replace('_', ' ')} · ${amt / 100:,.2f}" + (f" · attempt {obj.get('attempt_count')}" if obj.get("attempt_count") else "")
    if t.startswith("customer.subscription."):
        status = obj.get("status") or ""
        return f"subscription {t.rsplit('.', 1)[1]}" + (f" · {status}" if status else "")
    if t.startswith("checkout.session."):
        return f"checkout {t.rsplit('.', 1)[1]}"
    if t == "charge.refunded":
        full = obj.get("amount") or 0
        back = obj.get("amount_refunded") or 0
        return (f"charge refunded · ${back / 100:,.2f}" + ("" if back >= full > 0 else f" of ${full / 100:,.2f}"))
    if t.startswith("charge."):
        return f"charge {t.split('.', 1)[1].replace('.', ' ')} · ${(obj.get('amount') or 0) / 100:,.2f}"
    return t


def _stripe_restaurant(conn, event):
    """Which restaurant a Stripe event is about (#50) — the same order the
    webhook acts in: the restaurant_id Stripe carries back in metadata (set
    at checkout; survives an owner changing their email), then the
    subscription the event belongs to (the stripe_subscriptions mirror),
    then the invoice or charge we already hold, then the stored customer id.
    An email is the last resort, and only for this ledger's attribution."""
    import billing_jobs as _bj
    obj = (event.get("data") or {}).get("object") or {}
    t = event.get("type", "")

    def _valid(rid):
        try:
            rid = int(str(rid).strip())
        except (TypeError, ValueError):
            return None
        r = conn.execute("SELECT id FROM restaurants WHERE id=?", (rid,)).fetchone()
        return r[0] if r else None

    meta = obj.get("metadata") or {}
    if t.startswith("invoice."):
        meta = _bj.invoice_metadata(obj) or meta
    rid = _valid(meta.get("restaurant_id")) if meta.get("restaurant_id") else None
    if rid:
        return rid
    sub_id = obj.get("id") if t.startswith("customer.subscription.") else (
        _bj.invoice_facts(obj).get("subscription_id") if t.startswith("invoice.") else obj.get("subscription"))
    if isinstance(sub_id, str) and sub_id:
        r = conn.execute("SELECT restaurant_id FROM stripe_subscriptions WHERE subscription_id=?",
                         (sub_id,)).fetchone()
        if r:
            return r[0]
    if t.startswith("charge."):
        charge_id = obj.get("charge") if t.startswith("charge.dispute.") else obj.get("id")
        for col, val in (("charge_id", charge_id), ("invoice_id", obj.get("invoice"))):
            if isinstance(val, str) and val:
                r = conn.execute(f"SELECT restaurant_id FROM stripe_invoices WHERE {col}=? "
                                 "AND restaurant_id IS NOT NULL LIMIT 1", (val,)).fetchone()
                if r:
                    return r[0]
    customer = obj.get("customer") if isinstance(obj.get("customer"), str) else None
    if customer:
        r = conn.execute("SELECT id FROM restaurants WHERE stripe_customer_id=? "
                         "ORDER BY (id IN (SELECT restaurant_id FROM stripe_subscriptions)) DESC, id LIMIT 1",
                         (customer,)).fetchone()
        if r:
            return r[0]
    email = (obj.get("customer_email") or (obj.get("customer_details") or {}).get("email")
             or (obj.get("billing_details") or {}).get("email"))
    return _resolve_restaurant(conn, None, email, None) if email else None


def record_stripe(event, restaurant_id=None, db_path=None):
    """One line per Stripe event, however often Stripe delivers it (#50).

    The webhook calls this after its duplicate check, and the row carries the
    event id as external_id under a UNIQUE (source, external_id) index — so a
    redelivery, or the retry after a failed dispatch, never adds a second
    line. Invoice rows carry the invoice's facts (subtotal, discount, tax,
    currency, setup vs recurring) for the Billing history. Never raises."""
    try:
        from models import get_conn, DB_PATH
        import billing_jobs as _bj
        obj = (event.get("data") or {}).get("object") or {}
        t = event.get("type", "")
        conn = get_conn(db_path or DB_PATH)
        try:
            rid = restaurant_id or _stripe_restaurant(conn, event)
            cents = _stripe_amount_cents(t, obj)
            amount = (cents / 100) if cents is not None else None
            email = obj.get("customer_email") or (obj.get("customer_details") or {}).get("email")
            customer = obj.get("customer") if isinstance(obj.get("customer"), str) else None
            payload = {"id": event.get("id"), "type": t, "status": obj.get("status"),
                       "billing_reason": obj.get("billing_reason"), "subscription": obj.get("subscription"),
                       "hosted_invoice_url": obj.get("hosted_invoice_url"),
                       "next_payment_attempt": obj.get("next_payment_attempt"),
                       "cancel_at_period_end": obj.get("cancel_at_period_end")}
            if t.startswith("invoice."):
                f = _bj.invoice_facts(obj)
                payload.update({k: f.get(k) for k in ("subscription_id", "kind", "currency", "subtotal_cents",
                                                      "discount_cents", "tax_cents", "total_cents",
                                                      "setup_cents", "recurring_cents", "attempt_count")})
            elif t.startswith("customer.subscription."):
                details = obj.get("cancellation_details") or {}
                payload.update({"cancellation_reason": details.get("reason"),
                                "canceled_at": obj.get("canceled_at"), "ended_at": obj.get("ended_at"),
                                "changed": sorted(((event.get("data") or {}).get("previous_attributes") or {}).keys())})
            elif t == "charge.refunded":
                payload.update({"charge_amount": obj.get("amount"), "amount_refunded": obj.get("amount_refunded"),
                                "full_refund": bool(obj.get("refunded"))})
            elif t.startswith("charge.dispute."):
                payload.update({"charge": obj.get("charge"), "reason": obj.get("reason")})
            try:
                body = json.dumps(payload, default=str)[:4000]
            except Exception:
                body = str(payload)[:4000]
            conn.execute(
                "INSERT OR IGNORE INTO admin_events (source, event_type, restaurant_id, customer_id, email, amount, "
                "summary, payload, external_id) VALUES (?,?,?,?,?,?,?,?,?)",
                ("stripe", t, rid, customer, email, amount, _stripe_summary(event)[:300], body,
                 event.get("id") or None))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def recent(limit=100, restaurant_id=None, db_path=None):
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    _ensure(conn)
    if restaurant_id:
        rows = conn.execute("SELECT * FROM admin_events WHERE restaurant_id=? ORDER BY id DESC LIMIT ?", (restaurant_id, limit)).fetchall()
    else:
        rows = conn.execute("SELECT e.*, r.name AS restaurant FROM admin_events e LEFT JOIN restaurants r ON r.id=e.restaurant_id ORDER BY e.id DESC LIMIT ?", (limit,)).fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    for o in out:
        o.pop("payload", None)
    return out


def audit_admin_write():
    """before_request for every blueprint that carries /admin writes: a row in
    admin_events with the actor, before the write runs, so a denied or failed
    write is on the record too (security audit Z2). Reads are not logged;
    view-as logs itself. admin_bp and status_bp both register it — the
    status page's admin writes used to live outside the record (SEC-23)."""
    from flask import request
    if request.method in ("GET", "HEAD", "OPTIONS") or not (request.path or "").startswith("/admin"):
        return None
    try:
        from auth import get_current_user
        u = get_current_user() or {}
        rid = (request.view_args or {}).get("restaurant_id")
        # The restaurant rides in the payload, not the column: the per-client
        # events view filters on restaurant_id and must keep showing the
        # route's own event first (e.g. alert_cap.set), not the audit row.
        record("audit", f"admin_write:{request.endpoint or request.path}",
               summary=f"{u.get('username') or 'anonymous'} {request.method} {request.path}",
               payload={"restaurant_id": rid, "actor": u.get("username"), "role": u.get("role")})
    except Exception:
        pass
    return None
