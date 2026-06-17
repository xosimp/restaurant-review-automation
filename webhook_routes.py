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

webhook_bp = Blueprint('webhook', __name__)

RESEND_API_KEY        = os.getenv("RESEND_API_KEY", "")
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

    def send_alert(subject, body):
        """Send alert email to Will."""
        if not RESEND_API_KEY:
            print(f"ALERT: {subject}\n{body}")
            return
        try:
            import resend as _resend
            _resend.api_key = RESEND_API_KEY
            _resend.Emails.send({
                "from": f"Cavnar AI Alerts <{FROM_EMAIL}>",
                "to": [WILL_EMAIL],
                "subject": subject,
                "html": f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
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
                </div>"""
            })
        except Exception as e:
            print(f"Alert email failed: {e}")

    # ── Handle events ──────────────────────────────────────────────────────
    if event["type"] == "invoice.payment_failed":
        inv     = event["data"]["object"]
        email   = inv.get("customer_email","unknown")
        amount  = inv.get("amount_due", 0) / 100
        attempt = inv.get("attempt_count", 1)
        next_attempt = inv.get("next_payment_attempt")
        next_str = ""
        if next_attempt:
            from datetime import datetime
            next_str = f" Stripe will retry on {datetime.fromtimestamp(next_attempt).strftime('%B %d')}."

        send_alert(
            f"⚠ Payment failed — {email}",
            f"""A client payment has failed and needs your attention.<br><br>
            <strong>Customer:</strong> {email}<br>
            <strong>Amount:</strong> ${amount:.2f}<br>
            <strong>Attempt:</strong> #{attempt}<br>
            <strong>Action needed:</strong> Contact the client to update their payment method.{next_str}<br><br>
            If payment doesn't resolve within 3 days, consider pausing their dashboard access."""
        )

    elif event["type"] == "customer.subscription.deleted":
        sub   = event["data"]["object"]
        email = sub.get("customer_email","unknown") if "customer_email" in sub else "unknown"
        # Try to get customer email from customer ID
        customer_id = sub.get("customer","")
        reason = sub.get("cancellation_details",{}).get("reason","unknown")

        send_alert(
            f"📋 Subscription cancelled — {customer_id}",
            f"""A client subscription has been cancelled.<br><br>
            <strong>Customer ID:</strong> {customer_id}<br>
            <strong>Reason:</strong> {reason}<br>
            <strong>Action needed:</strong> If this was unintentional, contact the client.
            If they are churning, deactivate their dashboard access at
            <a href="https://dashboard.cavnar.ai/admin">dashboard.cavnar.ai/admin</a>."""
        )

    elif event["type"] == "invoice.paid":
        inv         = event["data"]["object"]
        customer_id = inv.get("customer","")
        email       = inv.get("customer_email","unknown")
        amount      = inv.get("amount_paid", 0) / 100
        billing_reason = inv.get("billing_reason","")  # subscription_create, subscription_cycle, etc.
        print(f"Payment received: {email} — ${amount:.2f} ({billing_reason})")
        if customer_id and email:
            try:
                conn = get_conn()
                row = conn.execute(
                    "SELECT r.id, r.billing_status FROM restaurants r JOIN users u ON u.restaurant_id=r.id WHERE u.email=? LIMIT 1",
                    (email,)
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
                    update_restaurant(dict(row)["id"], updates)
                    print(f"Saved Stripe customer {customer_id} for {email}")

                    # Notify Will when a client converts from trial to paid
                    if first_payment and RESEND_API_KEY:
                        try:
                            import resend as _resend
                            _resend.api_key = RESEND_API_KEY
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
                                "html": f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
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
                                </div>"""
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
                                    "html": f"""<div style="font-family:'DM Sans',sans-serif;max-width:480px;margin:0 auto;padding:32px 24px">
                                      <div style="font-size:20px;font-weight:600;margin-bottom:24px">Cavnar <em style="color:#c84b2f;font-style:italic">AI</em></div>
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
                                    </div>"""
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
    # Basic authentication check — DocuSign sends a secret header if configured
    ds_secret = os.getenv("DOCUSIGN_WEBHOOK_SECRET", "")
    if ds_secret:
        auth_header = request.headers.get("X-DocuSign-Signature-1", "")
        if not auth_header:
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

            if not row:
                print(f"WARNING: No restaurant found for envelope {envelope_id} - emails not sent")
            elif not RESEND_API_KEY:
                print(f"WARNING: No RESEND_API_KEY - emails not sent")

            if row and RESEND_API_KEY:
                r = dict(row)
                mods = sum([
                    1 if r.get("module_reviews") else 0,
                    1 if r.get("module_labor") else 0,
                    1 if r.get("module_inventory") else 0,
                    1 if r.get("module_marketing") else 0,
                ])

                # Send payment email
                try:
                    send_payment_email(
                        to_email=r["owner_email"],
                        restaurant_name=r["name"],
                        module_count=mods,
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

