"""
webhook_routes.py — Stripe and DocuSign webhook handlers
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import Blueprint, request, jsonify, redirect
import os
from datetime import datetime

from models import get_conn, get_restaurant, update_restaurant, log_email
from auth import admin_required
from emails import send_payment_email, send_welcome_email


def _html_doc(fragment, bg="#f7f4ef"):
    """Wrap a bare fragment in a real HTML document so its background fills
    the mail client's viewport instead of stopping at the content's height
    (the half-cut-off look). Imported lazily: emails.py reads RESEND_API_KEY
    at module scope, and a module-level import here could bind it before
    load_dotenv() runs. See emails._html_document."""
    from emails import html_document
    return html_document(fragment, bg)



def _claim_stripe_event(event_id: str) -> bool:
    """True if this is the first time we've seen this Stripe event.

    Stripe delivers at-least-once and retries any non-2xx, so without this a
    retry re-runs the whole handler: a second receipt email to the customer,
    a second "new paying client" alert. The state changes were already
    idempotent by accident (guarded on billing_status != active), the emails
    were not.

    INSERT on a PRIMARY KEY is the claim — two concurrent deliveries of the
    same event cannot both succeed.
    """
    if not event_id:
        return True
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS stripe_events_seen (
            event_id   TEXT PRIMARY KEY,
            event_type TEXT,
            seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.commit()
        try:
            conn.execute("INSERT INTO stripe_events_seen (event_id) VALUES (?)", (event_id,))
            conn.commit()
            claimed = True
        except Exception:
            claimed = False   # duplicate PK — already handled
        conn.close()
        return claimed
    except Exception as e:
        # Fail open: a bookkeeping failure must not drop a real payment event.
        print(f"_claim_stripe_event failed ({event_id}): {e}")
        return True


def _claim_docusign_event(envelope_id: str, status: str) -> bool:
    """True the first time this envelope reaches this status.

    DocuSign Connect retries any non-2xx and can deliver the same
    notification more than once. Without this, a repeat of a single
    "completed" callback re-sends the payment link AND the welcome email
    containing the client's temporary password — to a client who already got
    both, at the least confusing moment possible.

    Deliberately the same shape as _claim_stripe_event above: the primary-key
    insert is the claim, and it fails open, because a bookkeeping outage must
    not swallow the one callback that starts a paying client's account.
    """
    if not envelope_id:
        return True
    key = f"{envelope_id}:{status or ''}"
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS docusign_events_seen (
            event_key   TEXT PRIMARY KEY,
            envelope_id TEXT,
            status      TEXT,
            seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
        conn.commit()
        try:
            conn.execute("INSERT INTO docusign_events_seen (event_key, envelope_id, status) VALUES (?,?,?)",
                         (key, envelope_id, status))
            conn.commit()
            claimed = True
        except Exception:
            claimed = False   # duplicate PK — already handled
        conn.close()
        return claimed
    except Exception as e:
        print(f"_claim_docusign_event failed ({key}): {e}")
        return True


def _restaurant_for_stripe(customer_id: str = "", email: str = ""):
    """Resolve a Stripe event to a restaurant id.

    Matching used to be `WHERE u.email = customer_email LIMIT 1`, which broke
    in two ways the audit caught: an owner who changed their email in the app
    stopped matching entirely (payments silently stopped reconciling and
    billing_status never activated), and a multi-location owner only ever
    resolved to their base restaurant.

    stripe_customer_id is the stable key and is tried first; email is the
    fallback for the very first payment, before we have a customer id stored.
    """
    if not customer_id and not email:
        return None
    try:
        conn = get_conn()
        if customer_id:
            row = conn.execute(
                "SELECT id FROM restaurants WHERE stripe_customer_id=? LIMIT 1",
                (customer_id,)
            ).fetchone()
            if row:
                conn.close()
                return row["id"]
        if email:
            row = conn.execute(
                """SELECT r.id FROM restaurants r
                   JOIN users u ON u.restaurant_id = r.id
                   WHERE lower(u.email)=lower(?) ORDER BY u.is_admin ASC, r.id ASC LIMIT 1""",
                (email,)
            ).fetchone()
            if row:
                conn.close()
                return row["id"]
            # Some accounts carry the billing address on the restaurant only.
            row = conn.execute(
                "SELECT id FROM restaurants WHERE lower(owner_email)=lower(?) ORDER BY id ASC LIMIT 1",
                (email,)
            ).fetchone()
            if row:
                conn.close()
                return row["id"]
        conn.close()
    except Exception as e:
        print(f"_restaurant_for_stripe lookup failed: {e}")
    return None


def _restaurant_from_metadata(meta) -> int:
    """restaurant_id out of Stripe metadata, if it is there and it is real.

    Preferred over customer id and email both: it is set at checkout, it is
    stable, and it survives an owner changing their email. Validated against
    the table rather than trusted, because metadata is only as good as the
    session that set it."""
    try:
        raw = (meta or {}).get("restaurant_id")
        if not raw:
            return None
        rid = int(str(raw).strip())
        conn = get_conn()
        row = conn.execute("SELECT id FROM restaurants WHERE id=?", (rid,)).fetchone()
        conn.close()
        return row["id"] if row else None
    except Exception:
        return None


# The module keys checkout is allowed to grant. "intel" is derived from full
# tier and has no column, so it is not settable here.
_GRANTABLE_MODULES = ("reviews", "labor", "inventory", "marketing")


def _apply_module_entitlement(restaurant_id: int, module_keys: str) -> dict:
    """Set this restaurant's module flags to exactly what was paid for.

    Audit #5: checkout put a module COUNT in metadata and nothing read it, so
    what a client paid for and what they could open were two unconnected
    facts — every flag was a manual admin step, and Stripe would never correct
    a mismatch in either direction.

    Only called with keys that came from OUR checkout metadata, and only for
    the four real columns. Returns the updates applied, or {} if the metadata
    carried nothing usable — an empty list must never be read as "revoke
    everything", because that is also what a missing key looks like.
    """
    keys = {k.strip().lower() for k in (module_keys or "").split(",") if k.strip()}
    keys &= set(_GRANTABLE_MODULES)
    if not keys:
        return {}
    updates = {f"module_{k}": (1 if k in keys else 0) for k in _GRANTABLE_MODULES}
    try:
        update_restaurant(restaurant_id, updates)
        print(f"Entitlement set from Stripe for restaurant {restaurant_id}: {sorted(keys)}")
        return updates
    except Exception as e:
        print(f"Failed to apply entitlement for {restaurant_id}: {e}")
        return {}


def _set_billing_status(restaurant_id: int, status: str, reason: str = ""):
    """Move a restaurant and every location it is billed with to one status."""
    try:
        for _rid in _sibling_restaurant_ids(restaurant_id):
            update_restaurant(_rid, {"billing_status": status})
        print(f"billing_status={status} for {_sibling_restaurant_ids(restaurant_id)} ({reason})")
        return True
    except Exception as e:
        print(f"Failed to set billing_status={status} for {restaurant_id}: {e}")
        return False


def _sibling_restaurant_ids(restaurant_id: int):
    """Every location that shares this restaurant's location_group.

    A multi-location owner pays once for the group, so billing state has to
    land on all of their locations — not just whichever one the paying user
    row happened to point at.
    """
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT location_group, owner_email, stripe_customer_id FROM restaurants WHERE id=?",
            (restaurant_id,)
        ).fetchone()
        if not row:
            conn.close()
            return [restaurant_id]
        group = (row["location_group"] or "").strip()
        owner = (row["owner_email"] or "").strip().lower()
        customer = (row["stripe_customer_id"] or "").strip()
        if group:
            # Scoped to the owner as well as the group name: two unrelated
            # clients typed into the same group must not have one's
            # cancellation churn the other's locations. See
            # models.get_location_group.
            rows = conn.execute(
                "SELECT id FROM restaurants WHERE location_group=? "
                "AND LOWER(TRIM(COALESCE(owner_email,'')))=?",
                (group, owner)
            ).fetchall()
        elif customer:
            # location_group is free text an admin types, and it is unset on
            # every production row today. Without this branch a multi-location
            # owner's single payment activated only the location Stripe
            # resolved to and left the rest on their previous status — which,
            # now that billing is actually enforced, means the entitlement
            # gate 402s a customer who has paid. One Stripe customer is one
            # subscription, so every restaurant billed to it moves together.
            rows = conn.execute(
                "SELECT id FROM restaurants WHERE stripe_customer_id=?", (customer,)
            ).fetchall()
        else:
            conn.close()
            return [restaurant_id]
        conn.close()
        ids = [r["id"] for r in rows]
        return ids or [restaurant_id]
    except Exception:
        return [restaurant_id]


webhook_bp = Blueprint('webhook', __name__)

# Read fresh at call time — see scheduler.py's identical note.
def _resend_key(): return os.getenv("RESEND_API_KEY", "")
FROM_EMAIL            = os.getenv("FROM_EMAIL", "will@cavnar.ai")
WILL_EMAIL            = os.getenv("WILL_EMAIL", "will@cavnar.ai")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")

@webhook_bp.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    import stripe
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature","")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify(error=str(e)), 400

    # Every verified event is kept — the admin console's Billing page reads
    # this as payment history (see admin_events.py).
    try:
        import admin_events as _ae
        _ae.record_stripe(event)
    except Exception:
        pass

    # Stripe retries on any non-2xx and can deliver the same event twice.
    # Everything below this line sends email or changes billing state, so it
    # runs at most once per event id.
    if not _claim_stripe_event(event.get("id", "")):
        print(f"Stripe event {event.get('id')} already handled — skipping duplicate")
        return jsonify(received=True, duplicate=True)

    def send_alert(subject, body):
        """Send alert email to Will."""
        if not _resend_key():
            print(f"ALERT: {subject}\n{body}")
            return
        try:
            import resend as _resend
            _resend.api_key = _resend_key()
            _resend.Emails.send({
                "from": f"Cavnar AI Alerts <{FROM_EMAIL}>",
                "to": [WILL_EMAIL],
                "subject": subject,
                "html": _html_doc(f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
                    <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:20px">
                        <h3 style="color:#0e0c0a;margin:0">Cavnar AI — Payment Alert</h3>
                    </div>
                    <p style="font-size:15px;line-height:1.6">{body}</p>
                    <hr style="border:none;border-top:1px solid #e0dbd0;margin:20px 0"/>
                    <p style="font-size:11px;color:#7a736a">
                        Manage clients at
                        <a href="https://dashboard.cavnar.ai/admin" style="color:#c84b2f">
                            dashboard.cavnar.ai/admin
                        </a>
                    </p>
                </div>"""),
            })
        except Exception as e:
            print(f"Alert email failed: {e}")

    # ── Handle events ──────────────────────────────────────────────────────
    if event["type"] == "checkout.session.completed":
        # The event that actually knows who paid and what for. Before this
        # existed, nothing ran until the first invoice.paid — so the Stripe
        # customer id was unknown in between, and the module count sitting in
        # metadata was never read by anything at all.
        sess        = event["data"]["object"]
        meta        = sess.get("metadata") or {}
        customer_id = sess.get("customer", "") or ""
        sub_id      = sess.get("subscription", "") or ""
        email       = (sess.get("customer_details") or {}).get("email") or sess.get("customer_email") or ""
        rid = _restaurant_from_metadata(meta) or _restaurant_for_stripe(customer_id, email)
        if rid:
            updates = {"billing_status": "active"}
            if customer_id:
                updates["stripe_customer_id"] = customer_id
            try:
                update_restaurant(rid, updates)
                for _sib in _sibling_restaurant_ids(rid):
                    if _sib != rid:
                        update_restaurant(_sib, {"billing_status": "active"})
            except Exception as e:
                print(f"checkout.session.completed: failed to activate {rid}: {e}")
            granted = _apply_module_entitlement(rid, meta.get("module_keys", ""))
            send_alert(
                f"✅ Checkout completed — {meta.get('restaurant') or email}",
                f"""Checkout completed and access provisioned.<br><br>
                <strong>Restaurant:</strong> {meta.get('restaurant') or '(unknown)'} (id {rid})<br>
                <strong>Stripe customer:</strong> {customer_id or '(none)'}<br>
                <strong>Subscription:</strong> {sub_id or '(none)'}<br>
                <strong>Modules granted:</strong> """ + (
                    ", ".join(sorted(k.replace("module_", "") for k, v in granted.items() if v))
                    if granted else "none in metadata — entitlement left as it was, set it in admin")
            )
        else:
            send_alert(
                "⚠ Checkout completed but no restaurant matched",
                f"""A checkout completed and could not be reconciled.<br><br>
                <strong>Customer:</strong> {customer_id}<br>
                <strong>Email:</strong> {email}<br>
                <strong>Metadata:</strong> {meta}<br><br>
                Nothing was provisioned. Set this up by hand at
                <a href="https://dashboard.cavnar.ai/admin">dashboard.cavnar.ai/admin</a>."""
            )

    elif event["type"] == "customer.subscription.updated":
        # Upgrades and downgrades. Without this a plan change in Stripe never
        # reached the module flags, so a client could pay for one module and
        # keep four, or pay for four and keep one, indefinitely.
        sub  = event["data"]["object"]
        meta = sub.get("metadata") or {}
        rid  = _restaurant_from_metadata(meta) or _restaurant_for_stripe(sub.get("customer", ""), "")
        status = (sub.get("status") or "").lower()
        if rid:
            granted = _apply_module_entitlement(rid, meta.get("module_keys", ""))
            # Stripe's own subscription status is authoritative for access.
            if status in ("active", "trialing"):
                _set_billing_status(rid, "active", "subscription.updated")
            elif status == "past_due":
                _set_billing_status(rid, "past_due", "subscription.updated")
            elif status in ("canceled", "unpaid", "incomplete_expired"):
                _set_billing_status(rid, "churned", f"subscription.updated status={status}")
            if granted or status not in ("active", "trialing"):
                send_alert(
                    f"🔁 Subscription changed — {meta.get('restaurant') or rid}",
                    f"""Subscription status is now <strong>{status}</strong>.<br><br>
                    <strong>Restaurant id:</strong> {rid}<br>
                    <strong>Modules:</strong> """ + (
                        ", ".join(sorted(k.replace("module_", "") for k, v in granted.items() if v))
                        if granted else "unchanged (no module_keys in metadata)")
                )
        else:
            print(f"subscription.updated for unmatched customer {sub.get('customer','')}")

    elif event["type"] in ("charge.refunded", "charge.dispute.created"):
        # Money came back. Nothing here used to react at all, so a refunded or
        # disputed customer kept the whole product indefinitely.
        obj         = event["data"]["object"]
        customer_id = obj.get("customer", "") or ""
        email       = obj.get("billing_details", {}).get("email") or obj.get("receipt_email") or ""
        disputed    = event["type"] == "charge.dispute.created"
        amount      = (obj.get("amount_refunded") or obj.get("amount") or 0) / 100
        rid = _restaurant_for_stripe(customer_id, email)
        acted = _set_billing_status(rid, "paused", event["type"]) if rid else False
        send_alert(
            ("⛔ Chargeback opened — " if disputed else "↩ Refund issued — ") + (email or customer_id),
            f"""{'A customer has disputed a charge.' if disputed else 'A charge was refunded.'}<br><br>
            <strong>Amount:</strong> ${amount:,.2f}<br>
            <strong>Customer:</strong> {customer_id or '(none)'}<br>
            <strong>Email:</strong> {email or '(none)'}<br>
            <strong>Access:</strong> """ + (
                f"paused for restaurant {rid} and every location billed with it. "
                "Reactivate in admin if this was expected."
                if acted else
                "NOT changed — no restaurant matched. Handle this by hand at "
                "<a href=\"https://dashboard.cavnar.ai/admin\">dashboard.cavnar.ai/admin</a>.")
        )

    elif event["type"] == "invoice.payment_action_required":
        # 3-D Secure. The client has to authenticate or the payment never
        # lands; silence here looked exactly like a successful renewal.
        inv   = event["data"]["object"]
        email = inv.get("customer_email", "unknown")
        send_alert(
            f"🔐 Payment needs authentication — {email}",
            f"""Stripe needs the client to confirm this payment (3-D Secure).<br><br>
            <strong>Customer:</strong> {email}<br>
            <strong>Amount:</strong> ${(inv.get('amount_due', 0) / 100):.2f}<br><br>
            They should have an email from Stripe. Access is unchanged for now."""
        )

    elif event["type"] == "invoice.payment_failed":
        inv     = event["data"]["object"]
        email   = inv.get("customer_email","unknown")
        amount  = inv.get("amount_due", 0) / 100
        attempt = inv.get("attempt_count", 1)
        next_attempt = inv.get("next_payment_attempt")
        next_str = ""
        if next_attempt:
            from datetime import datetime
            next_str = f" Stripe will retry on {datetime.fromtimestamp(next_attempt).strftime('%B %d')}."

        # past_due is in ACTIVE_BILLING_STATES, so this is a warning state and
        # not a lockout — access continues while Stripe retries, which is the
        # right direction. What it changes is that the state was in the
        # allowlist and nothing ever wrote it: a failing client stayed
        # "active" through the whole retry schedule and then dropped to
        # churned with no step in between, invisible to you and to them.
        _failed_rid = _restaurant_for_stripe(inv.get("customer", "") or "", email)
        if _failed_rid:
            _set_billing_status(_failed_rid, "past_due", "invoice.payment_failed")

        send_alert(
            f"⚠ Payment failed — {email}",
            f"""A client payment has failed and needs your attention.<br><br>
            <strong>Customer:</strong> {email}<br>
            <strong>Amount:</strong> ${amount:.2f}<br>
            <strong>Attempt:</strong> #{attempt}<br>
            <strong>Action needed:</strong> Contact the client to update their payment method.{next_str}<br><br>
            If payment doesn't resolve within 3 days, consider pausing their dashboard access."""
        )

        # The client themselves previously never found out their card was
        # declined except by Will personally reaching out — only on the
        # FIRST attempt, so Stripe's own retry schedule doesn't turn into a
        # repeated-email spam for the same underlying decline.
        if attempt == 1 and email and email != "unknown" and _resend_key():
            try:
                conn = get_conn()
                row = conn.execute(
                    "SELECT r.name, r.owner_name FROM restaurants r JOIN users u ON u.restaurant_id=r.id WHERE u.email=? LIMIT 1",
                    (email,)
                ).fetchone()
                conn.close()
                if row:
                    from emails import send_payment_failed_client_email
                    send_payment_failed_client_email(email, row["name"] or "your restaurant", amount, row["owner_name"])
            except Exception as ce:
                print(f"Client payment-failed email failed: {ce}")

    elif event["type"] == "customer.subscription.deleted":
        sub   = event["data"]["object"]
        email = sub.get("customer_email","unknown") if "customer_email" in sub else "unknown"
        # Try to get customer email from customer ID
        customer_id = sub.get("customer","")
        reason = sub.get("cancellation_details",{}).get("reason","unknown")

        # Actually revoke access. This used to send Will an email asking him
        # to go deactivate the account by hand, which meant a cancelled
        # customer kept the full dashboard, the iOS app and every AI feature
        # until someone read that email — indefinitely, at Cavnar's API cost.
        # auth.login_required/mobile_login_required read billing_status.
        revoked_rid = _restaurant_for_stripe(customer_id, email)
        if revoked_rid:
            try:
                for _rid in _sibling_restaurant_ids(revoked_rid):
                    update_restaurant(_rid, {"billing_status": "churned"})
                print(f"Subscription cancelled — billing_status=churned for {_sibling_restaurant_ids(revoked_rid)}")
            except Exception as _re:
                print(f"Failed to mark restaurant {revoked_rid} churned: {_re}")
        else:
            print(f"Subscription cancelled but no restaurant matched customer {customer_id} / {email}")

        send_alert(
            f"📋 Subscription cancelled — {customer_id}",
            f"""A client subscription has been cancelled.<br><br>
            <strong>Customer ID:</strong> {customer_id}<br>
            <strong>Reason:</strong> {reason}<br>
            <strong>Access:</strong> """ + (
                f"automatically revoked (restaurant {revoked_rid} set to churned)."
                if revoked_rid else
                "NOT revoked — no restaurant matched this Stripe customer. "
                "Deactivate manually at <a href=\"https://dashboard.cavnar.ai/admin\">dashboard.cavnar.ai/admin</a>."
            ) + """<br>
            <strong>Action needed:</strong> If this was unintentional, restore their
            billing status at <a href="https://dashboard.cavnar.ai/admin">dashboard.cavnar.ai/admin</a>."""
        )

    elif event["type"] == "invoice.paid":
        inv         = event["data"]["object"]
        customer_id = inv.get("customer","")
        email       = inv.get("customer_email","unknown")
        amount      = inv.get("amount_paid", 0) / 100
        billing_reason = inv.get("billing_reason","")  # subscription_create, subscription_cycle, etc.
        print(f"Payment received: {email} — ${amount:.2f} ({billing_reason})")
        if customer_id or email:
            try:
                _rid = _restaurant_for_stripe(customer_id, email)
                row = None
                if _rid:
                    conn = get_conn()
                    row = conn.execute(
                        "SELECT id, billing_status FROM restaurants WHERE id=?", (_rid,)
                    ).fetchone()
                    conn.close()
                if row:
                    updates = {"stripe_customer_id": customer_id}
                    # Auto-activate billing status on first real payment
                    # (subscription_cycle = recurring charge, subscription_create = first charge after trial)
                    first_payment = (
                        billing_reason in ("subscription_cycle", "subscription_create")
                        and dict(row)["billing_status"] != "active"
                    )
                    if first_payment:
                        updates["billing_status"] = "active"
                        print(f"Auto-activated billing_status for {email}")
                    elif (dict(row)["billing_status"] or "").lower() == "past_due":
                        # A retry succeeded. Clear the dunning state even when
                        # billing_reason isn't one of the two above, or a
                        # recovered client would sit in past_due forever.
                        updates["billing_status"] = "active"
                        print(f"Payment recovered — cleared past_due for {email}")
                    # A paid invoice reactivates every location in the group —
                    # the same set cancellation churns — so a customer who
                    # pays after lapsing regains access everywhere at once.
                    for _sib in _sibling_restaurant_ids(dict(row)["id"]):
                        update_restaurant(_sib, dict(updates) if _sib == dict(row)["id"]
                                          else {k: v for k, v in updates.items() if k != "stripe_customer_id"})
                    print(f"Saved Stripe customer {customer_id} for {email}")

                    # Notify Will when a client converts from trial to paid
                    if first_payment and _resend_key():
                        try:
                            import resend as _resend
                            _resend.api_key = _resend_key()
                            # Get restaurant name
                            conn2 = get_conn()
                            rname_row = conn2.execute(
                                "SELECT name FROM restaurants WHERE id=?", (dict(row)["id"],)
                            ).fetchone()
                            conn2.close()
                            rname = rname_row["name"] if rname_row else email
                            _resend.Emails.send({
                                "from": f"Cavnar AI Alerts <{FROM_EMAIL}>",
                                "to": [WILL_EMAIL],
                                "subject": f"💳 New paying client — {rname}",
                                "html": _html_doc(f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
                                    <div style="border-top:3px solid #2d6a4f;padding-top:20px;margin-bottom:16px">
                                        <h3 style="color:#0e0c0a;margin:0">New paying client</h3>
                                    </div>
                                    <p style="font-size:15px;line-height:1.6">
                                        <strong>{rname}</strong> just converted from trial to paid.<br><br>
                                        <strong>Email:</strong> {email}<br>
                                        <strong>Amount:</strong> ${amount:.2f}<br>
                                        <strong>Billing:</strong> {billing_reason.replace('_',' ').title()}
                                    </p>
                                    <hr style="border:none;border-top:1px solid #e0dbd0;margin:16px 0"/>
                                    <p style="font-size:11px;color:#7a736a">
                                        <a href="https://dashboard.cavnar.ai/admin" style="color:#c84b2f">View in admin →</a>
                                    </p>
                                </div>"""),
                            })
                            log_email(dict(row)["id"], "Admin Alert", WILL_EMAIL, f"New paying client — {rname}")

                            # Send branded receipt to the client
                            try:
                                from datetime import datetime as _dt
                                receipt_date = _dt.now().strftime("%B %d, %Y")
                                _resend.Emails.send({
                                    "from": f"Will Cavnar <{FROM_EMAIL}>",
                                    "to": [email],
                                    "subject": f"Payment confirmed — Cavnar AI",
                                    "html": _html_doc(f"""<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
                                    <div style="font-family:'DM Sans',sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:white;border-radius:12px;box-sizing:border-box">
                                      <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin-bottom:24px">
                                      <h2 style="font-size:18px;font-weight:600;margin-bottom:8px;color:#0e0c0a">Payment confirmed ✓</h2>
                                      <p style="font-size:14px;color:#4a4540;line-height:1.6;margin-bottom:20px">
                                        Thank you — your payment of <strong>${amount:.2f}</strong> has been received for <strong>{rname}</strong>.
                                      </p>
                                      <div style="background:#f5f3f0;border-radius:8px;padding:16px 20px;margin-bottom:20px">
                                        <div style="font-size:12px;color:#7a736a;margin-bottom:4px">Date</div>
                                        <div style="font-size:14px;font-weight:500;color:#0e0c0a;margin-bottom:12px">{receipt_date}</div>
                                        <div style="font-size:12px;color:#7a736a;margin-bottom:4px">Amount</div>
                                        <div style="font-size:14px;font-weight:500;color:#0e0c0a;margin-bottom:12px">${amount:.2f}</div>
                                        <div style="font-size:12px;color:#7a736a;margin-bottom:4px">Restaurant</div>
                                        <div style="font-size:14px;font-weight:500;color:#0e0c0a">{rname}</div>
                                      </div>
                                      <p style="font-size:13px;color:#4a4540;line-height:1.6;margin-bottom:20px">
                                        Your dashboard is active and all modules are running. Questions? Reply to this email or reach me at will@cavnar.ai.
                                      </p>
                                      <a href="https://dashboard.cavnar.ai" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">Go to dashboard →</a>
                                      <hr style="border:none;border-top:1px solid #e5e0db;margin:24px 0">
                                      <p style="font-size:11px;color:#9ca3af">Cavnar AI · will@cavnar.ai · cavnar.ai</p>
                                    </div>
                                    </div>"""),
                                })
                                log_email(dict(row)["id"], "Payment Receipt", email, f"Payment confirmed — ${amount:.2f}")
                            except Exception as re_err:
                                print(f"Receipt email failed: {re_err}")
                        except Exception as ne:
                            print(f"First payment notification failed: {ne}")
            except Exception as e:
                print(f"Failed to save Stripe customer ID: {e}")

    return jsonify(ok=True)

@webhook_bp.route("/docusign/callback")

@webhook_bp.route("/docusign/callback2")
def docusign_callback():
    """Handle DocuSign OAuth callback — just confirms consent was granted."""
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        return f"""<div style="font-family:sans-serif;max-width:500px;margin:60px auto;padding:24px">
            <h2 style="color:#c84b2f">DocuSign Error</h2>
            <p>Error: {error}</p>
            <p><a href="/admin">Back to admin</a></p>
        </div>"""
    if code:
        return """<div style="font-family:sans-serif;max-width:500px;margin:60px auto;padding:24px;text-align:center">
            <h2 style="color:#2d6a4f">&#10003; DocuSign Connected</h2>
            <p style="color:#3a3530;margin:12px 0">Production consent granted successfully.<br>
            Contracts will now send automatically when you create a client.</p>
            <a href="/admin" style="display:inline-block;margin-top:16px;background:#c84b2f;color:white;padding:10px 24px;border-radius:6px;text-decoration:none;font-weight:600">Back to admin</a>
        </div>"""
    return redirect("/admin")

@webhook_bp.route("/docusign/webhook", methods=["POST"])
def docusign_webhook():
    """Receive DocuSign connect notifications when envelope status changes."""
    # HMAC-SHA256 check — DocuSign Connect signs the raw body with the configured
    # HMAC key and sends it base64-encoded in X-DocuSign-Signature-1. Previously
    # this only checked the header was *present*, which any caller can fake —
    # now it actually verifies the signature matches the body.
    ds_secret = os.getenv("DOCUSIGN_WEBHOOK_SECRET", "")
    if ds_secret:
        import hmac as _hmac_ds, hashlib as _hashlib_ds, base64 as _b64_ds
        auth_header = request.headers.get("X-DocuSign-Signature-1", "")
        raw_bytes = request.get_data()
        expected = _b64_ds.b64encode(
            _hmac_ds.new(ds_secret.encode(), raw_bytes, _hashlib_ds.sha256).digest()
        ).decode()
        if not auth_header or not _hmac_ds.compare_digest(auth_header, expected):
            return jsonify(error="Unauthorized"), 401
    try:
        raw = request.get_data(as_text=True)
        print(f"DocuSign webhook received: {raw[:500]}")
        data = request.get_json(force=True) or {}
        print(f"DocuSign webhook parsed keys: {list(data.keys())}")
        # Try multiple envelope ID locations
        envelope_id = (
            data.get("envelopeId") or
            data.get("data",{}).get("envelopeId","") or
            data.get("data",{}).get("envelopeSummary",{}).get("envelopeId","")
        )
        # Try multiple status locations
        status = (
            data.get("status") or
            data.get("event") or
            data.get("data",{}).get("envelopeSummary",{}).get("status","") or
            data.get("data",{}).get("status","")
        )
        print(f"DocuSign webhook envelope_id={envelope_id} status={status}")

        if envelope_id and status in ("completed", "envelope-completed"):
            if not _claim_docusign_event(envelope_id, "completed"):
                print(f"DocuSign envelope {envelope_id} already processed — ignoring repeat delivery")
                return jsonify(ok=True, duplicate=True), 200
            # Mark contract as signed
            conn = get_conn()
            row = conn.execute(
                """SELECT r.id, r.name, r.owner_email, r.temp_password,
                          r.module_reviews, r.module_labor, r.module_inventory, r.module_marketing,
                          u.username
                   FROM restaurants r
                   JOIN users u ON u.restaurant_id = r.id AND u.is_admin = 0
                   WHERE r.docusign_envelope_id = ? LIMIT 1""",
                (envelope_id,)
            ).fetchone()
            conn.execute(
                "UPDATE restaurants SET contract_status='signed' WHERE docusign_envelope_id=?",
                (envelope_id,)
            )
            conn.commit()
            conn.close()
            print(f"Contract signed: {envelope_id}")
            try:
                import admin_events as _ae
                _ae.record("docusign", "contract.signed", restaurant_id=(row["id"] if row else None),
                           email=(row["owner_email"] if row else None), summary="Contract signed", envelope_id=envelope_id)
            except Exception:
                pass

            if not row:
                print(f"WARNING: No restaurant found for envelope {envelope_id} - emails not sent")
            elif not _resend_key():
                print(f"WARNING: No RESEND_API_KEY - emails not sent")

            if row and _resend_key():
                r = dict(row)
                mods = sum([
                    1 if r.get("module_reviews") else 0,
                    1 if r.get("module_labor") else 0,
                    1 if r.get("module_inventory") else 0,
                    1 if r.get("module_marketing") else 0,
                ])
                _module_keys = [k for k, on in (
                    ("reviews",   r.get("module_reviews")),
                    ("labor",     r.get("module_labor")),
                    ("inventory", r.get("module_inventory")),
                    ("marketing", r.get("module_marketing")),
                ) if on]

                # Send payment email
                try:
                    send_payment_email(
                        to_email=r["owner_email"],
                        restaurant_name=r["name"],
                        module_count=mods,
                        restaurant_id=r["id"],
                        modules=_module_keys,
                    )
                    print(f"Payment email sent to {r['owner_email']} after signing")
                    try:
                        log_email(r["id"], "payment", r["owner_email"], f"Payment link — {r['name']}")
                    except Exception: pass
                except Exception as e:
                    print(f"Payment email failed after signing: {e}")

                # Send welcome email with credentials
                try:
                    tmp_pw = r.get("temp_password") or ""
                    # Fallback if temp_password wasn't stored
                    if not tmp_pw:
                        tmp_pw = "Check your email from Will for your temporary password, or contact will@cavnar.ai"
                    send_welcome_email(
                        to_email=r["owner_email"],
                        restaurant_name=r["name"],
                        username=r["username"],
                        password=tmp_pw,
                        module_reviews=int(r.get("module_reviews") or 0),
                        module_labor=int(r.get("module_labor") or 0),
                        module_inventory=int(r.get("module_inventory") or 0),
                        module_marketing=int(r.get("module_marketing") or 0),
                    )
                    # Clear temp password from DB after sending
                    try:
                        from models import update_restaurant
                        update_restaurant(r["id"], {"temp_password": ""})
                    except Exception:
                        pass
                    print(f"Welcome email sent to {r['owner_email']} after signing")
                    try:
                        log_email(r["id"], "welcome", r["owner_email"], f"Welcome — {r['name']}")
                    except Exception: pass
                except Exception as e:
                    print(f"Welcome email failed after signing: {e}")

        return jsonify(ok=True)
    except Exception as e:
        print(f"DocuSign webhook error: {e}")
        return jsonify(ok=True)  # Always return 200 to DocuSign



# ── Inbound SMS (Twilio) ────────────────────────────────────────────────────
# Public and unauthenticated by necessity — Twilio is the caller. The
# signature check stands in for auth: without it anyone could forge a STOP
# (silencing a guest) or a YES (granting marketing consent on someone's
# behalf, the exact thing guest_marketing's consent model exists to prevent).

@webhook_bp.route("/webhooks/twilio/sms", methods=["POST"])
def twilio_inbound_sms():
    from flask import Response
    from notify import validate_twilio_signature
    from guest_marketing import handle_inbound_sms

    params = request.form.to_dict()
    # Twilio signs the URL it was configured with. Behind Railway's proxy
    # request.url arrives as http://, so rebuild it as https to match.
    url = request.url
    if request.headers.get("X-Forwarded-Proto") == "https" and url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    if not validate_twilio_signature(url, params, request.headers.get("X-Twilio-Signature", "")):
        return Response("", status=403, mimetype="text/xml")

    from_phone = (params.get("From") or "").strip()
    body = params.get("Body") or ""
    if not from_phone:
        return Response("<Response></Response>", mimetype="text/xml")

    try:
        reply = handle_inbound_sms(from_phone, body)
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="twilio_inbound_sms", context=f"from={from_phone[-4:]}")
        except Exception:
            pass
        reply = None

    if reply:
        safe = reply.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Response(f"<Response><Message>{safe}</Message></Response>", mimetype="text/xml")
    return Response("<Response></Response>", mimetype="text/xml")


# ── Resend delivery events (bounces / complaints) ───────────────────────────
# Resend signs with Svix headers. Without consuming these, a hard-bounced
# address is retried forever and a spam complaint is invisible — both of
# which degrade the sending reputation of the domain that also carries 2FA
# and password-reset mail.

RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")

# Statuses worth suppressing on. A soft bounce (full mailbox, temporary
# defer) is deliberately not here — that address may well work tomorrow.
_SUPPRESS_EVENTS = {
    "email.bounced":   "bounced",
    "email.complained": "complained",
}


def _verify_svix(payload_body: bytes, headers) -> bool:
    """Svix signature: HMAC-SHA256 over "{id}.{timestamp}.{body}", keyed by
    the base64 secret after the 'whsec_' prefix. Multiple space-separated
    signatures may be present; any valid one passes."""
    import base64, hashlib, hmac as _hmac
    secret = RESEND_WEBHOOK_SECRET
    svix_id = headers.get("svix-id", "")
    svix_ts = headers.get("svix-timestamp", "")
    svix_sig = headers.get("svix-signature", "")
    if not (secret and svix_id and svix_ts and svix_sig):
        return False
    try:
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except Exception:
        return False
    signed = f"{svix_id}.{svix_ts}.".encode() + payload_body
    expected = base64.b64encode(_hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in svix_sig.split():
        candidate = part.split(",", 1)[1] if "," in part else part
        if _hmac.compare_digest(candidate, expected):
            return True
    return False


@webhook_bp.route("/webhooks/resend", methods=["POST"])
def resend_webhook():
    from models import suppress_email, mark_email_delivery_event

    if not _verify_svix(request.get_data(), request.headers):
        return jsonify(ok=False, error="bad signature"), 403

    event = request.get_json(silent=True) or {}
    etype = event.get("type") or ""
    data = event.get("data") or {}
    message_id = data.get("email_id") or data.get("id") or ""
    to = data.get("to") or []
    recipients = to if isinstance(to, list) else [to]
    detail = ""
    if isinstance(data.get("bounce"), dict):
        detail = data["bounce"].get("message") or data["bounce"].get("type") or ""

    status = {
        "email.bounced": "bounced",
        "email.complained": "complained",
        "email.delivered": "delivered",
        "email.delivery_delayed": "delayed",
    }.get(etype)

    if status:
        mark_email_delivery_event(message_id, status, detail)

    if etype in _SUPPRESS_EVENTS:
        for addr in recipients:
            suppress_email(addr, _SUPPRESS_EVENTS[etype], detail)

    # Always 200 on a verified event — a non-2xx makes Resend retry, and
    # nothing here is worth replaying.
    return jsonify(ok=True)
