"""
admin_events.py — the events the business runs on, kept.

Stripe and DocuSign webhooks used to act (flip billing_status, email Will)
and forget. Every event now lands here too, so the admin console's Billing
page and a client's Subscription tab can show payment and contract history
instead of only the current flag.
"""
import json

_SQL = """
CREATE TABLE IF NOT EXISTS admin_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,          -- stripe | docusign | admin
    event_type TEXT NOT NULL,      -- invoice.paid, customer.subscription.deleted, contract.signed…
    restaurant_id INTEGER,
    customer_id TEXT,
    email TEXT,
    amount REAL,
    summary TEXT,
    payload TEXT,
    created_at TEXT DEFAULT (datetime('now'))
)
"""


def _ensure(conn):
    conn.execute(_SQL)


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


def _stripe_summary(event):
    t = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if t.startswith("invoice."):
        amt = (obj.get("amount_paid") if t == "invoice.paid" else obj.get("amount_due")) or 0
        return f"{t.split('.', 1)[1].replace('_', ' ')} · ${amt / 100:,.2f}" + (f" · attempt {obj.get('attempt_count')}" if obj.get("attempt_count") else "")
    if t.startswith("customer.subscription."):
        status = obj.get("status") or ""
        return f"subscription {t.rsplit('.', 1)[1]}" + (f" · {status}" if status else "")
    if t.startswith("checkout.session."):
        return f"checkout {t.rsplit('.', 1)[1]}"
    if t.startswith("charge."):
        return f"charge {t.rsplit('.', 1)[1]} · ${(obj.get('amount') or 0) / 100:,.2f}"
    return t


def record_stripe(event, db_path=None):
    """One line per Stripe webhook, whatever it was."""
    obj = (event.get("data") or {}).get("object") or {}
    t = event.get("type", "")
    amount = None
    if t.startswith("invoice."):
        amount = ((obj.get("amount_paid") if t == "invoice.paid" else obj.get("amount_due")) or 0) / 100
    elif t.startswith("charge."):
        amount = (obj.get("amount") or 0) / 100
    email = obj.get("customer_email") or (obj.get("customer_details") or {}).get("email")
    customer = obj.get("customer") if isinstance(obj.get("customer"), str) else None
    return record("stripe", t, customer_id=customer, email=email, amount=amount, summary=_stripe_summary(event),
                  payload={"id": event.get("id"), "type": t, "status": obj.get("status"), "billing_reason": obj.get("billing_reason"),
                           "subscription": obj.get("subscription"), "hosted_invoice_url": obj.get("hosted_invoice_url"),
                           "next_payment_attempt": obj.get("next_payment_attempt"), "cancel_at_period_end": obj.get("cancel_at_period_end")},
                  db_path=db_path)


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
