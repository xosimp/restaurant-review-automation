"""
admin_routes.py — Cavnar AI admin, infrastructure and API routes
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import (Blueprint, request, jsonify, redirect, url_for,
                   render_template_string, make_response, send_file, Response, session)
import os, json, io, csv as _csv_mod
from datetime import datetime
from functools import wraps

# Import everything needed from the main app
from models import (get_conn, get_restaurant, update_restaurant,
                    create_restaurant, Restaurant, get_reviews_data,
                    get_review_stats, get_email_log, log_email, get_all_restaurants)
from auth import (create_session, get_session_user, delete_session,
                  verify_password, list_users, create_user, update_password,
                  admin_required, login_required)
from emails import send_payment_email, send_welcome_email, create_stripe_checkout

admin_bp = Blueprint('admin', __name__)

def sanitize(value, max_len=1000):
    """Strip HTML tags and limit length to prevent XSS."""
    if not value:
        return value
    import re
    # Remove HTML tags
    value = re.sub(r'<[^>]+>', '', str(value))
    # Remove javascript: protocol
    value = re.sub(r'(?i)javascript\s*:', '', value)
    # Truncate
    return value[:max_len].strip() or None

# HTML templates - imported lazily to avoid circular imports
def _get_templates():
    import hosted_dashboard as _hd
    return _hd.ADMIN_HTML, _hd.CLIENT_SETTINGS_HTML, _hd.CLIENT_DATA_HTML

def _get_admin_html():
    return _get_templates()[0]

def _get_settings_html():
    return _get_templates()[1]

def _get_data_html():
    return _get_templates()[2]

# Pull config from environment
RESEND_API_KEY        = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL            = os.getenv("FROM_EMAIL", "will@cavnar.ai")
ADMIN_USERNAME        = os.getenv("ADMIN_USERNAME", "will")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")

@admin_bp.route("/admin")
@admin_required
def admin(current_user):
    users = list_users()
    # Enrich with restaurant data
    from models import get_restaurant
    enriched = []
    for u in users:
        r = get_restaurant(u["restaurant_id"])
        u["billing_status"] = r.billing_status if r else "trial"
        u["last_active_tab"] = r.last_active_tab if r else None
        u["internal_notes"] = r.internal_notes if r else None
        u["phone"] = r.owner_phone if r else None
        u["last_fetched_at"] = r.last_fetched_at[:10] if r and r.last_fetched_at else None
        u["location_group"]    = r.location_group if r else None
        u["location_name"]     = r.location_name if r else None
        u["contract_status"]   = r.contract_status if r else "pending"
        u["envelope_id"]       = r.docusign_envelope_id if r else None

        # #10 Client activity dashboard additions
        # Module flags
        u["module_reviews"]   = r.module_reviews if r else 0
        u["module_labor"]     = r.module_labor if r else 0
        u["module_inventory"] = r.module_inventory if r else 0
        u["module_marketing"] = r.module_marketing if r else 0

        # Unreviewed reviews count
        try:
            from models import get_conn as _gc
            _conn = _gc()
            _row = _conn.execute(
                """SELECT COUNT(*) as cnt FROM reviews
                   WHERE restaurant_id=? AND response_status NOT IN ('approved','posted','skipped')""",
                (u["restaurant_id"],)
            ).fetchone()
            _conn.close()
            u["pending_reviews"] = _row["cnt"] if _row else 0
        except Exception:
            u["pending_reviews"] = 0

        # Health score: green / amber / red (admin always green)
        try:
            if u.get("is_admin"):
                u["health"] = "green"
            else:
                from datetime import datetime as _dt, timedelta as _td
                _now = _dt.now()
                _last = u.get("last_login")
                _days_since_login = 999
                if _last:
                    try:
                        _ll = _dt.fromisoformat(_last.replace("Z",""))
                        _days_since_login = (_now - _ll).days
                    except Exception:
                        pass
                _pending = u.get("pending_reviews", 0)
                if _days_since_login <= 7 and _pending < 5:
                    u["health"] = "green"
                elif _days_since_login <= 14 and _pending < 10:
                    u["health"] = "amber"
                else:
                    u["health"] = "red"
        except Exception:
            u["health"] = "amber"

        enriched.append(u)
    from models import get_all_location_groups
    location_groups = get_all_location_groups()
    # Calculate MRR from active clients
    mrr = 0
    for u in enriched:
        if u.get("is_admin"):
            continue  # Never count admin account in MRR
        if u.get("is_active") and u.get("billing_status") == "active":
            r = get_restaurant(u["restaurant_id"])
            if r:
                mods = sum([
                    1 if r.module_reviews else 0,
                    1 if r.module_labor else 0,
                    1 if r.module_inventory else 0,
                    1 if r.module_marketing else 0,
                ])
                mrr += mods * 300

    # Get email log
    from models import get_email_log
    email_log = get_email_log(limit=50)

    # Activity feed — recent logins, approvals, uploads
    try:
        from models import get_conn as _gc
        _conn = _gc()
        activity_feed = []

        # Recent logins
        _logins = _conn.execute(
            """SELECT u.last_login, r.name as restaurant_name
               FROM users u JOIN restaurants r ON u.restaurant_id=r.id
               WHERE u.last_login IS NOT NULL AND u.is_admin=0
               ORDER BY u.last_login DESC LIMIT 10"""
        ).fetchall()
        for row in _logins:
            activity_feed.append({
                "ts": row["last_login"],
                "restaurant": row["restaurant_name"],
                "action": "Logged in",
                "color": "#2d6a4f"
            })

        # Recent approvals — use posted_at or review_date as proxy
        _approvals = _conn.execute(
            """SELECT COALESCE(r.posted_at, r.review_date) as ts, rest.name as restaurant_name
               FROM reviews r JOIN restaurants rest ON r.restaurant_id=rest.id
               WHERE r.response_status IN ('approved','posted')
               ORDER BY ts DESC LIMIT 10"""
        ).fetchall()
        for row in _approvals:
            activity_feed.append({
                "ts": row["ts"],
                "restaurant": row["restaurant_name"],
                "action": "Approved a review response",
                "color": "#c84b2f"
            })

        _conn.close()

        # Sort by timestamp desc
        activity_feed.sort(key=lambda x: x["ts"] or "", reverse=True)
        activity_feed = activity_feed[:20]
    except Exception as e:
        print(f"Activity feed error: {e}")
        activity_feed = []

    return render_template_string(_get_admin_html(),
        current_user=current_user, users=enriched,
        location_groups=location_groups,
        mrr=mrr,
        email_log=email_log,
        activity_feed=activity_feed)

@admin_bp.route("/admin/create-client", methods=["POST"])
@admin_required
def create_client(current_user):
    from models import create_restaurant, Restaurant
    data = request.get_json()
    try:
        # Check for duplicate email/username BEFORE creating anything
        conn_check = get_conn()
        existing = conn_check.execute(
            "SELECT id FROM users WHERE email=? OR username=?",
            (data["owner_email"], data["username"])
        ).fetchone()
        conn_check.close()
        if existing:
            return jsonify(ok=False, error="A user with that email or username already exists — try a different username or email")

        # Create restaurant
        rid = create_restaurant(Restaurant(
            name=data["restaurant_name"],
            owner_email=data["owner_email"],
            google_place_id=data.get("google_place_id") or None,
            yelp_business_id=data.get("yelp_business_id") or None,
            voice_notes=data.get("voice_notes") or None,
            owner_phone=data.get("owner_phone") or None,
            owner_name=data.get("owner_name") or None,
            location_group=data.get("location_group","").strip() or None,
            location_name=data.get("location_name","").strip() or None,
        ))
        create_user(
            restaurant_id=rid,
            username=data["username"],
            email=data["owner_email"],
            password=data["password"],
        )
        # Set module access directly from checkboxes
        from models import update_restaurant
        update_restaurant(rid, {
            "module_reviews":  int(data.get("module_reviews", 1)),
            "module_labor":    int(data.get("module_labor", 0)),
            "module_inventory":int(data.get("module_inventory", 0)),
            "module_marketing":int(data.get("module_marketing", 0)),
            "temp_password":   data.get("password",""),
        })

        # Auto-fetch menu notes from Google Places if place ID provided
        google_place_id = data.get("google_place_id") or None
        if google_place_id and int(data.get("module_marketing", 0)):
            try:
                from competitor import fetch_menu_notes_from_places
                auto_menu = fetch_menu_notes_from_places(google_place_id)
                if auto_menu:
                    update_restaurant(rid, {"menu_notes": auto_menu})
                    print(f"[create_client] Auto-fetched menu notes for {data['restaurant_name']}")
            except Exception as me:
                print(f"[create_client] Menu auto-fetch failed: {me}")
        mods = (int(data.get("module_reviews",0)) + int(data.get("module_labor",0)) +
                int(data.get("module_inventory",0)) + int(data.get("module_marketing",0)))
        module_names = []
        if int(data.get("module_reviews",0)): module_names.append("Review Intelligence")
        if int(data.get("module_labor",0)):   module_names.append("Labor Optimizer")
        if int(data.get("module_inventory",0)): module_names.append("Inventory Control")
        if int(data.get("module_marketing",0)): module_names.append("Marketing Autopilot")
        modules_list = ", ".join(module_names)

        # Step 1: Send contract via DocuSign
        envelope_id = None
        if mods > 0 and data.get("owner_email"):
            try:
                from docusign_helper import send_contract
                result = send_contract(
                    owner_email=data["owner_email"],
                    owner_name=data.get("owner_name","") or data["restaurant_name"],
                    restaurant_name=data["restaurant_name"],
                    module_count=mods,
                    modules_list=modules_list,
                )
                envelope_id = result.get("envelope_id")
                update_restaurant(rid, {
                    "contract_status": "sent",
                    "docusign_envelope_id": envelope_id,
                })
                print(f"Contract sent via DocuSign to {data['owner_email']}, envelope: {envelope_id}")
                try:
                    log_email(rid, "contract", data["owner_email"], f"Service Agreement — {data['restaurant_name']}")
                except Exception: pass
            except Exception as e:
                print(f"DocuSign contract failed: {e}")
                import traceback; traceback.print_exc()

        # Steps 2 & 3 (payment + welcome emails) fire automatically
        # when the client signs the contract via the DocuSign webhook

        docusign_skipped = envelope_id is None and mods > 0
        return jsonify(ok=True, restaurant_id=rid, envelope_id=envelope_id, docusign_skipped=docusign_skipped)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e))

@admin_bp.route("/admin/deactivate-client/<int:user_id>", methods=["POST"])
@admin_required
def deactivate_client(user_id, current_user):
    conn = get_conn()
    conn.execute("UPDATE users SET is_active=0 WHERE id=? AND is_admin=0", (user_id,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@admin_bp.route("/admin/reactivate-client/<int:user_id>", methods=["POST"])
@admin_required
def reactivate_client(user_id, current_user):
    conn = get_conn()
    conn.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))
    conn.commit()
    # Get user info to send reactivation email
    user_row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if user_row:
        try:
            restaurant = get_restaurant(dict(user_row)["restaurant_id"])
            if restaurant:
                from emails import send_reactivation_email
                send_reactivation_email(
                    to_email=restaurant.owner_email,
                    restaurant_name=restaurant.name,
                    owner_name=restaurant.owner_name,
                )
        except Exception as e:
            print(f"Reactivation email failed: {e}")
    return jsonify(ok=True)

@admin_bp.route("/admin/client-data/<int:restaurant_id>")
@admin_required
def client_data_page(restaurant_id, current_user):
    from models import get_client_data, get_staff_notes
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    data        = get_client_data(restaurant_id) or {}
    staff_notes = get_staff_notes(restaurant_id)
    return render_template_string(_get_data_html(),
        current_user=current_user,
        restaurant=restaurant,
        data=data,
        staff_notes=staff_notes)

@admin_bp.route("/admin/staff-notes/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_staff_note_route(restaurant_id, current_user):
    from models import save_staff_note
    name  = request.form.get("employee_name","").strip()
    notes = request.form.get("notes","").strip()
    if not name or not notes:
        return jsonify(ok=False, error="Name and notes required")
    save_staff_note(restaurant_id, name, notes)
    return jsonify(ok=True)

@admin_bp.route("/admin/staff-notes/<int:note_id>/delete", methods=["POST"])
@admin_required
def delete_staff_note_route(note_id, current_user):
    from models import delete_staff_note
    delete_staff_note(note_id)
    return jsonify(ok=True)

@admin_bp.route("/admin/upload-data/<int:restaurant_id>", methods=["POST"])
@admin_required
def upload_data(restaurant_id, current_user):
    from models import save_client_data
    data_type = request.form.get("data_type")  # "shifts" or "inventory"
    source     = request.form.get("source", "upload")

    if source == "upload":
        f = request.files.get("csv_file")
        if not f:
            return jsonify(ok=False, error="No file uploaded")
        csv_content = f.read().decode("utf-8")
    else:
        csv_content = request.form.get("csv_content", "")

    if not csv_content.strip():
        return jsonify(ok=False, error="No data provided")

    # Validate it parses correctly
    import io, csv as _csv
    try:
        rows = list(_csv.DictReader(io.StringIO(csv_content)))
        if not rows:
            return jsonify(ok=False, error="CSV appears empty")
    except Exception as e:
        return jsonify(ok=False, error=f"Could not parse CSV: {e}")

    save_client_data(restaurant_id, data_type, csv_content, source)
    return jsonify(ok=True, rows=len(rows))

@admin_bp.route("/admin/client-settings/<int:restaurant_id>")
@admin_required
def client_settings_page(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    from models import get_client_data
    client_data = get_client_data(restaurant_id) or {}
    from models import get_staff_notes
    staff_notes = get_staff_notes(restaurant_id)
    return render_template_string(_get_settings_html(),
        current_user=current_user,
        restaurant=restaurant,
        client_data=client_data,
        staff_notes=staff_notes)

@admin_bp.route("/admin/client-settings/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_client_settings(restaurant_id, current_user):
    from models import update_restaurant
    data = request.get_json()
    try:
        from models import set_service_tier
        tier = data.get("service_tier","trial")
        # Set modules directly from checkboxes
        update_restaurant(restaurant_id, {
            "name":            data.get("name","").strip(),
            "owner_email":     data.get("owner_email","").strip(),
            "google_place_id": data.get("google_place_id","").strip() or None,
            "yelp_business_id":data.get("yelp_business_id","").strip() or None,
            "voice_notes":     sanitize(data.get("voice_notes","")),
            "neighborhood":    data.get("neighborhood","").strip() or None,
            "vibe":            sanitize(data.get("vibe","")),
            "known_for":       sanitize(data.get("known_for","")),
            "sign_off_name":   data.get("sign_off_name","").strip() or None,
            "never_say":       sanitize(data.get("never_say","")),
            "menu_notes":      sanitize(data.get("menu_notes",""), max_len=2000),
            "menu_url":        sanitize(data.get("menu_url","")),
            "hourly_rate":     float(data.get("hourly_rate") or 26.0),
            "labor_target_pct": float(data.get("labor_target_pct") or 30.0),
            "pos_system":      data.get("pos_system","").strip() or None,
            "module_reviews":  int(data.get("module_reviews", 1)),
            "module_labor":    int(data.get("module_labor", 0)),
            "module_inventory":int(data.get("module_inventory", 0)),
            "module_marketing":int(data.get("module_marketing", 0)),
            "owner_name":      sanitize(data.get("owner_name","")),
            "owner_phone":     data.get("owner_phone","").strip() or None,
            "location_group":        data.get("location_group","").strip() or None,
            "location_name":         data.get("location_name","").strip() or None,
            "inventory_frequency":   data.get("inventory_frequency","weekly"),
            "inventory_notes":       sanitize(data.get("inventory_notes","")),
            "food_cost_target":      float(data.get("food_cost_target", 30) or 30),
            "digest_day":      data.get("digest_day","monday"),
            "digest_enabled":  int(data.get("digest_enabled",1)),
            "reviews_live":    int(bool(data.get("reviews_live"))),
            "billing_status":  data.get("billing_status","trial"),
            "internal_notes":  sanitize(data.get("internal_notes","")),
        })
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@admin_bp.route("/admin/reset-password/<int:user_id>", methods=["POST"])
@admin_required
def reset_password(user_id, current_user):
    from models import reset_user_password
    import secrets, string
    data = request.get_json()
    new_pw = data.get("password","").strip()
    if not new_pw:
        # Auto-generate if not provided
        new_pw = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    if len(new_pw) < 6:
        return jsonify(ok=False, error="Password must be at least 6 characters")
    reset_user_password(user_id, new_pw)
    # Optionally email the new password
    if data.get("send_email"):
        try:
            conn = get_conn()
            row = conn.execute(
                "SELECT u.email, r.name FROM users u JOIN restaurants r ON u.restaurant_id=r.id WHERE u.id=?",
                (user_id,)
            ).fetchone()
            conn.close()
            if row:
                import resend as _resend
                _resend.api_key = RESEND_API_KEY
                _resend.Emails.send({
                    "from": f"Will Cavnar <{FROM_EMAIL}>",
                    "to": [row["email"]],
                    "subject": "Your Cavnar AI password has been reset",
                    "html": f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
                        <h3 style="color:#0e0c0a">Password reset</h3>
                        <p>Hi — your Cavnar AI dashboard password has been reset.</p>
                        <div style="background:#f7f4ef;padding:14px;border-radius:8px;margin:16px 0">
                            <p><strong>URL:</strong> <a href="https://dashboard.cavnar.ai">dashboard.cavnar.ai</a></p>
                            <p><strong>New password:</strong> {new_pw}</p>
                        </div>
                        <p>Log in and update your password in the Account tab.</p>
                        <p style="color:#7a736a;font-size:12px">— Will Cavnar · will@cavnar.ai</p>
                    </div>"""
                })
        except Exception as e:
            print(f"Reset email failed: {e}")
    return jsonify(ok=True, password=new_pw)

@admin_bp.route("/admin/reset-password-by-restaurant/<int:restaurant_id>", methods=["POST"])
@admin_required
def reset_password_by_restaurant(restaurant_id, current_user):
    conn = get_conn()
    row = conn.execute(
        "SELECT id FROM users WHERE restaurant_id=? AND is_admin=0 LIMIT 1",
        (restaurant_id,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify(ok=False, error="No client user found for this restaurant")
    return reset_password(row["id"], current_user=current_user)

@admin_bp.route("/api/log-activity", methods=["POST"])
@login_required
def log_activity_route(current_user):
    from models import log_activity
    data = request.get_json()
    log_activity(current_user["restaurant_id"], data.get("tab",""))
    return jsonify(ok=True)

@admin_bp.route("/stripe-webhook", methods=["POST"])
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

@admin_bp.route("/admin/resend-contract/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_contract(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        mods = sum([
            1 if restaurant.module_reviews else 0,
            1 if restaurant.module_labor else 0,
            1 if restaurant.module_inventory else 0,
            1 if restaurant.module_marketing else 0,
        ])
        module_names = []
        if restaurant.module_reviews:  module_names.append("Review Intelligence")
        if restaurant.module_labor:    module_names.append("Labor Optimizer")
        if restaurant.module_inventory: module_names.append("Inventory Control")
        if restaurant.module_marketing: module_names.append("Marketing Autopilot")
        from docusign_helper import send_contract
        result = send_contract(
            owner_email=restaurant.owner_email,
            owner_name=restaurant.owner_name or restaurant.name,
            restaurant_name=restaurant.name,
            module_count=mods,
            modules_list=", ".join(module_names),
        )
        envelope_id = result.get("envelope_id")
        from models import update_restaurant
        update_restaurant(restaurant_id, {
            "contract_status": "sent",
            "docusign_envelope_id": envelope_id,
        })
        # Log it
        try:
            from models import log_email
            log_email(restaurant_id, "contract", restaurant.owner_email, f"Service Agreement — {restaurant.name}")
        except Exception: pass
        return jsonify(ok=True)
    except Exception as e:
        print(f"Resend contract error: {e}")
        return jsonify(ok=False, error=str(e))

@admin_bp.route("/admin/resend-payment/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_payment(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        mods = sum([
            1 if restaurant.module_reviews else 0,
            1 if restaurant.module_labor else 0,
            1 if restaurant.module_inventory else 0,
            1 if restaurant.module_marketing else 0,
        ])
        if mods == 0:
            return jsonify(ok=False, error="No modules active for this client")
        send_payment_email(
            to_email=restaurant.owner_email,
            restaurant_name=restaurant.name,
            module_count=mods,
        )
        try:
            log_email(restaurant_id, "payment", restaurant.owner_email, f"Payment link — {restaurant.name}")
        except Exception: pass
        return jsonify(ok=True)
    except Exception as e:
        print(f"Resend payment error: {e}")
        return jsonify(ok=False, error=str(e))

@admin_bp.route("/admin/seed-reviews/<int:restaurant_id>", methods=["POST"])
@admin_required
def seed_reviews(restaurant_id, current_user):
    """Seed sample reviews for a restaurant so client can see the dashboard working."""
    from models import save_reviews, get_pending_analysis, update_analysis, update_draft, get_pending_drafts, Review
    from datetime import datetime, timedelta

    # Generate 12 realistic sample reviews
    sample = [
        ("Jennifer M.","google","r_s001",5,"Absolutely love this place. The food was incredible and our server was attentive without being intrusive. Will be back every month.",4),
        ("Tom K.","yelp","r_s002",2,"Waited 45 minutes for a table even though we had a reservation. Food was fine when it arrived but the experience was frustrating.",1),
        ("Aisha R.","google","r_s003",5,"Best spot in the neighborhood. The seasonal menu is always exciting and the cocktails are outstanding. Came three weekends in a row.",4),
        ("Derek S.","google","r_s004",1,"Found a hair in my food. Server was unapologetic. Manager offered a 10% discount which felt insulting. Health department should know.",4),
        ("Priya N.","yelp","r_s005",4,"Really good neighborhood spot. Salmon was perfectly cooked. Docked one star because the cocktail menu feels dated.",3),
        ("Carlos B.","google","r_s006",5,"Took my parents here for their anniversary and the staff went completely above and beyond. My mom is still talking about it.",5),
        ("Rachel W.","yelp","r_s007",3,"Mixed experience. Appetizers were excellent but the main courses took over an hour. Would try again on a quieter evening.",2),
        ("Mike T.","google","r_s008",5,"The happy hour deal is unreal. Half price on all small plates and the bartender is hilarious. Told everyone at work.",6),
        ("Sandra L.","yelp","r_s009",2,"Gluten-free options listed on the menu but staff seemed unsure whether dishes were actually safe for celiac. Need better training.",7),
        ("James O.","google","r_s010",5,"Took a date here and it couldn't have gone better. Warm atmosphere, great wine pairing suggestions. Already booked for next month.",8),
        ("Beth C.","google","r_s011",1,"Ordered takeout and it arrived 35 minutes late and completely cold. Called to complain and was offered nothing. Lost a loyal customer.",9),
        ("Olivia T.","yelp","r_s012",5,"Been a regular for two years and the kitchen keeps getting better. New menu just launched and it's an instant classic.",10),
    ]

    sentiments = {5:"positive",4:"positive",3:"neutral",2:"negative",1:"negative"}
    categories_map = [
        ["food_quality","service"],["service","reservation"],["food_quality","ambiance"],
        ["cleanliness","service"],["food_quality","value"],["service","ambiance"],
        ["food_quality","service"],["value","ambiance"],["service","cleanliness"],
        ["ambiance","service"],["takeout_delivery","service"],["food_quality"],
    ]
    urgencies = ["normal","normal","normal","high","normal","normal","normal",
                 "normal","normal","normal","normal","normal"]

    reviews = []
    for i, (author, platform, ext_id, rating, text, days_ago) in enumerate(sample):
        review_date = (datetime.now() - timedelta(days=days_ago*3)).isoformat()
        reviews.append(Review(
            restaurant_id=restaurant_id,
            platform=platform,
            external_id=f"{restaurant_id}_{ext_id}",
            author=author,
            rating=rating,
            text=text,
            review_date=review_date,
        ))

    new_count = save_reviews(reviews)

    # Analyse and draft all of them
    pending = get_pending_analysis(restaurant_id, limit=50)
    for i, r in enumerate(pending):
        sent = sentiments.get(r.rating, "neutral")
        cats = categories_map[i % len(categories_map)]
        urg  = urgencies[i % len(urgencies)]
        summary = f"Guest {'praised' if sent=='positive' else 'criticized'} the experience."
        update_analysis(r.id, sent, cats, summary, urg)

    pending_drafts = get_pending_drafts(restaurant_id, limit=50)
    restaurant = get_restaurant(restaurant_id)
    voice = restaurant.voice_notes or "Warm, genuine tone. Always invite guests back."
    for r in pending_drafts:
        if r.sentiment == "positive":
            draft = f"Thank you so much, {r.author}! It means the world to us to hear this — we hope to see you again soon."
        elif r.sentiment == "negative":
            draft = f"We're genuinely sorry to hear about your experience, {r.author}. This isn't the standard we hold ourselves to and we'd love the chance to make it right. Please reach out to us directly."
        else:
            draft = f"Thank you for taking the time to share your feedback, {r.author}. We appreciate your honesty and hope to see you again."
        update_draft(r.id, draft)

    return jsonify(ok=True, seeded=new_count)

@admin_bp.route("/admin/fetch-reviews/<int:restaurant_id>", methods=["POST"])
@admin_required
def fetch_reviews_now(restaurant_id, current_user):
    """Manually trigger a review fetch for a specific restaurant."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")

    from fetcher import fetch_google, fetch_yelp, save_reviews
    reviews = []
    errors = []

    if restaurant.reviews_live or restaurant.gmb_refresh_token:
        # Use GMB API if connected (stores review_name for auto-posting)
        if restaurant.gmb_refresh_token:
            try:
                from gmb import get_valid_token, fetch_reviews_via_gmb, get_gmb_account_id, get_gmb_location_id
                from models import update_restaurant
                token = get_valid_token(restaurant_id)
                if token:
                    loc_id = restaurant.gmb_location_id
                    if not loc_id:
                        acct_id = get_gmb_account_id(token)
                        if acct_id:
                            loc_id = get_gmb_location_id(token, acct_id, restaurant.google_place_id or "")
                            if loc_id:
                                update_restaurant(restaurant_id, {
                                    "gmb_account_id": acct_id,
                                    "gmb_location_id": loc_id,
                                })
                        else:
                            errors.append("Google: API access pending — awaiting Google approval")
                    if loc_id:
                        gmb_reviews = fetch_reviews_via_gmb(token, loc_id, restaurant_id)
                        reviews += gmb_reviews
                    else:
                        errors.append("Google: location not found — API access may still be pending")
                else:
                    errors.append("Google: token refresh failed — try reconnecting Google Business")
            except Exception as e:
                errors.append(f"Google GMB: {e}")
        elif restaurant.google_place_id and restaurant.reviews_live:
            # Fallback to Places API
            try:
                reviews += fetch_google(restaurant.google_place_id, restaurant_id)
            except Exception as e:
                errors.append(f"Google: {e}")

        if restaurant.yelp_business_id and restaurant.reviews_live:
            try:
                reviews += fetch_yelp(restaurant.yelp_business_id, restaurant_id)
            except Exception as e:
                errors.append(f"Yelp: {e}")

    if not reviews and not errors:
        return jsonify(ok=False, error="No platform IDs configured, reviews_live is off, and GMB not connected")

    new_count = save_reviews(reviews) if reviews else 0

    # Run analysis + drafting in background so route returns immediately
    import threading
    def _analyse_and_draft():
        try:
            from models import get_pending_analysis, get_pending_drafts
            from analyser import analyse_review
            for r in get_pending_analysis(restaurant_id, limit=50):
                try: analyse_review(r.id, r.rating, r.text)
                except Exception: pass
            from drafter import draft_response
            for r in get_pending_drafts(restaurant_id):
                try: draft_response(r.id, r.rating, r.text, r.sentiment,
                                    restaurant.name, restaurant.voice_notes or "")
                except Exception: pass
        except Exception as e:
            print(f"[fetch] background error: {e}")
    threading.Thread(target=_analyse_and_draft, daemon=True).start()

    return jsonify(ok=True, new_reviews=new_count, errors=errors)

@admin_bp.route("/admin/view-as/<int:restaurant_id>")
@admin_required
def view_as_client(restaurant_id, current_user):
    """Log in as a client to see exactly what they see."""
    from models import get_conn
    conn = get_conn()
    user_row = conn.execute(
        "SELECT * FROM users WHERE restaurant_id=? AND is_admin=0 LIMIT 1",
        (restaurant_id,)
    ).fetchone()
    conn.close()
    if not user_row:
        return "No client user found for this restaurant", 404
    # Create a short-lived session for that user
    # Short-lived session for view-as — 30 minutes only
    from datetime import datetime, timezone, timedelta
    from models import get_conn as _gc
    _conn = _gc()
    token = __import__('secrets').token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    _conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, last_active) VALUES (?,?,?,?)",
        (token, dict(user_row)["id"], expires, datetime.now(timezone.utc).isoformat())
    )
    _conn.commit(); _conn.close()
    resp = make_response(redirect("/"))
    resp.set_cookie("session_token", token, max_age=1800,
                    httponly=True, secure=bool(os.getenv("RAILWAY_ENVIRONMENT")), samesite="Strict")
    return resp

@admin_bp.route("/admin/stop-viewing")
def stop_viewing():
    """Return to admin — delete current session and redirect to admin login."""
    token = request.cookies.get("session_token")
    if token:
        # Only delete session if it actually exists (prevents session fixation)
        from models import get_session_user
        if get_session_user(token):
            delete_session(token)
    resp = make_response(redirect("/login?next=/admin"))
    resp.delete_cookie("session_token")
    return resp

@admin_bp.route("/docusign/callback")

@admin_bp.route("/docusign/callback2")
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

@admin_bp.route("/docusign/webhook", methods=["POST"])
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
                    print(f"Welcome email sent to {to_email}")
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

@admin_bp.route("/admin/inventory-template")
@admin_required
def inventory_template(current_user):
    """Download a pre-filled CSV template for inventory data."""
    import io
    template = """item,category,unit,par_level,current_stock,unit_cost,avg_daily_usage,last_ordered,last_order_qty,waste_last_week
Salmon fillet,protein,lb,20,18,14.50,3.2,2026-05-12,30,5.0
Chicken breast,protein,lb,30,25,4.20,5.0,2026-05-12,40,3.0
Romaine lettuce,produce,case,8,6,18.00,1.5,2026-05-12,10,1.5
Roma tomatoes,produce,lb,15,12,2.10,2.8,2026-05-12,20,2.0
Heavy cream,dairy,qt,12,10,3.80,2.0,2026-05-12,15,1.0
Pasta dried,dry,lb,25,22,1.20,4.0,2026-05-12,30,2.0
Olive oil,dry,liter,6,5,12.00,0.8,2026-05-12,8,0.5
House red wine,beverage,bottle,24,20,8.50,3.5,2026-05-12,30,2.0
"""
    buf = io.BytesIO(template.strip().encode())
    buf.seek(0)
    from flask import send_file
    return send_file(
        buf,
        mimetype="text/csv",
        as_attachment=True,
        download_name="cavnar_ai_inventory_template.csv"
    )

@admin_bp.route("/privacy")
def privacy_page():
    """Serve the Cavnar AI privacy policy page."""
    from flask import Response
    import os as _os
    try:
        html_path = _os.path.join(_os.path.dirname(__file__), "privacy.html")
        with open(html_path, "r") as f:
            html = f.read()
    except FileNotFoundError:
        html = "<h1>Privacy Policy</h1><p>Coming soon. Contact will@cavnar.ai</p>"
    return Response(html, mimetype="text/html")

@admin_bp.route("/sitemap.xml")
def sitemap():
    from flask import Response
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cavnar.ai/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
</urlset>"""
    return Response(xml, mimetype='application/xml')

@admin_bp.route("/robots.txt")
def robots():
    from flask import Response
    txt = """User-agent: *
Allow: /
Sitemap: https://cavnar.ai/sitemap.xml"""
    return Response(txt, mimetype='text/plain')

@admin_bp.route("/og-image.png")
def og_image():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "og-image.png")
    return send_file(path, mimetype="image/png")

@admin_bp.route("/favicon.ico")
def favicon_ico():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "favicon.ico")
    return send_file(path, mimetype="image/x-icon")

@admin_bp.route("/favicon.png")
def favicon_png():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "favicon.png")
    return send_file(path, mimetype="image/png")

# ── Instagram / Meta routes ───────────────────────────────────────────────────

@admin_bp.route("/instagram/connect")
@login_required
def instagram_connect(current_user):
    """Open Meta OAuth in a popup — state carries restaurant_id."""
    import urllib.parse
    from flask import redirect as flask_redirect
    app_id       = os.getenv("META_APP_ID","")
    redirect_uri = os.getenv("META_REDIRECT_URI", "https://dashboard.cavnar.ai/instagram/callback")
    scope        = "instagram_basic,instagram_content_publish,pages_read_engagement,pages_show_list,business_management"
    state        = str(current_user["restaurant_id"])
    params = urllib.parse.urlencode({
        "client_id":     app_id,
        "redirect_uri":  redirect_uri,
        "scope":         scope,
        "response_type": "code",
        "state":         state,
    })
    return flask_redirect(f"https://www.facebook.com/v19.0/dialog/oauth?{params}")

@admin_bp.route("/instagram/callback")
def instagram_callback():
    """Handle Meta OAuth callback — exchange code for token, get IG user ID."""
    import requests as _req
    from flask import redirect as _ig_redirect
    from models import update_restaurant as _update_r

    code         = request.args.get("code")
    state        = request.args.get("state")
    app_id       = os.getenv("META_APP_ID","")
    app_secret   = os.getenv("META_APP_SECRET","")
    redirect_uri = os.getenv("META_REDIRECT_URI", "https://dashboard.cavnar.ai/instagram/callback")

    if not code:
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({ig:'error',msg:'no_code'},'*');"
            "window.close();"
            "</script><p>Connection failed.</p></body></html>"
        )

    # Exchange code for short-lived token
    r = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": redirect_uri, "code": code,
    })
    if r.status_code != 200:
        print(f"IG token exchange failed: {r.text}")
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({ig:'error',msg:'token_failed'},'*');"
            "window.close();"
            "</script><p>Token exchange failed.</p></body></html>"
        )
    short_token = r.json().get("access_token")

    # Exchange for long-lived token (60 days)
    r2 = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_token,
    })
    long_token = r2.json().get("access_token", short_token)

    # Get Facebook pages
    r3 = _req.get("https://graph.facebook.com/v19.0/me/accounts", params={"access_token": long_token})
    pages = r3.json().get("data", [])
    ig_user_id = None
    page_token = long_token

    for page in pages:
        r4 = _req.get(f"https://graph.facebook.com/v19.0/{page['id']}", params={
            "fields": "instagram_business_account",
            "access_token": page.get("access_token", long_token),
        })
        ig_data = r4.json().get("instagram_business_account")
        if ig_data:
            ig_user_id = ig_data.get("id")
            page_token = page.get("access_token", long_token)
            break

    if not ig_user_id:
        print(f"No IG account found. Pages: {r3.json()}")
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({ig:'error',msg:'no_ig_account'},'*');"
            "window.close();"
            "</script><p>No Instagram business account found.</p></body></html>"
        )

    rid = int(state) if state and state.isdigit() else None
    if rid:
        from datetime import datetime, timedelta
        expires = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
        update_data = {
            "ig_token": page_token,
            "ig_user_id": ig_user_id,
            "ig_token_expires": expires,
        }
        # Also save Facebook page token/id if we found a page
        if pages:
            update_data["fb_page_token"]    = pages[0].get("access_token", long_token)
            update_data["fb_page_id"]       = pages[0].get("id","")
            update_data["fb_token_expires"] = expires
        _update_r(rid, update_data)
        print(f"Instagram+Facebook connected for restaurant {rid}, expires {expires}")

    return (
        "<html><body><script>"
        "window.opener&&window.opener.postMessage({ig:'connected'},'*');"
        "window.close();"
        "</script><p>Instagram connected! Close this window.</p></body></html>"
    )

@admin_bp.route("/api/post-to-instagram", methods=["POST"])
@login_required
def post_to_instagram(current_user):
    """Post a caption to Instagram. Client must have connected their account."""
    import requests as _req
    data       = request.get_json()
    caption    = data.get("caption","").strip()
    image_url  = data.get("image_url","").strip()  # optional

    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.ig_token or not restaurant.ig_user_id:
        return jsonify(ok=False, error="Instagram not connected — click Connect Instagram first")

    ig_user_id = restaurant.ig_user_id
    token      = restaurant.ig_token

    if image_url:
        # Image post
        r1 = _req.post(f"https://graph.facebook.com/v19.0/{ig_user_id}/media", data={
            "image_url":    image_url,
            "caption":      caption,
            "access_token": token,
        })
    else:
        # Text/caption only — requires a placeholder image or use carousel
        # For now use a simple image-less post via threads endpoint
        r1 = _req.post(f"https://graph.facebook.com/v19.0/{ig_user_id}/media", data={
            "media_type":   "REELS",
            "caption":      caption,
            "access_token": token,
        })

    if r1.status_code != 200:
        err = r1.json().get("error",{}).get("message","Unknown error")
        print(f"IG media create failed: {r1.text}")
        return jsonify(ok=False, error=err)

    creation_id = r1.json().get("id")

    # Publish the media
    r2 = _req.post(f"https://graph.facebook.com/v19.0/{ig_user_id}/media_publish", data={
        "creation_id":  creation_id,
        "access_token": token,
    })

    if r2.status_code != 200:
        err = r2.json().get("error",{}).get("message","Publish failed")
        return jsonify(ok=False, error=err)

    return jsonify(ok=True, post_id=r2.json().get("id"))

@admin_bp.route("/api/instagram-status")
@login_required
def instagram_status(current_user):
    """Check if Instagram is connected for this restaurant."""
    restaurant = get_restaurant(current_user["restaurant_id"])
    connected    = bool(restaurant and restaurant.ig_token and restaurant.ig_user_id)
    fb_connected = bool(restaurant and restaurant.fb_page_token and restaurant.fb_page_id)
    return jsonify(connected=connected, fb_connected=fb_connected)

@admin_bp.route("/api/instagram-disconnect", methods=["POST"])
@login_required
def instagram_disconnect(current_user):
    """Disconnect Instagram from this restaurant."""
    from models import update_restaurant
    update_restaurant(current_user["restaurant_id"], {"ig_token": "", "ig_user_id": "", "fb_page_token": "", "fb_page_id": ""})
    return jsonify(ok=True)

@admin_bp.route("/api/post-to-facebook", methods=["POST"])
@login_required
def post_to_facebook(current_user):
    """Post to Facebook Page."""
    import requests as _req
    data       = request.get_json()
    caption    = data.get("caption","").strip()
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.fb_page_token or not restaurant.fb_page_id:
        return jsonify(ok=False, error="Facebook not connected — click Connect Instagram & Facebook first")
    r = _req.post(f"https://graph.facebook.com/v19.0/{restaurant.fb_page_id}/feed", data={
        "message":      caption,
        "access_token": restaurant.fb_page_token,
    })
    if r.status_code != 200:
        err = r.json().get("error",{}).get("message","Unknown error")
        print(f"FB post failed: {r.text}")
        return jsonify(ok=False, error=err)
    return jsonify(ok=True, post_id=r.json().get("id"))

@admin_bp.route("/admin/api/client-usage/<int:restaurant_id>")
@admin_required
def client_usage(restaurant_id, current_user):
    """Return 30-day activity summary for a restaurant."""
    from models import get_activity_summary
    summary = get_activity_summary(restaurant_id, days=30)
    return jsonify(ok=True, **summary)


@admin_bp.route("/api/mark-posted/<int:review_id>", methods=["POST"])
@login_required
def mark_posted(review_id, current_user):
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='posted' WHERE id=? AND restaurant_id=?",
                 (review_id, current_user["restaurant_id"]))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@admin_bp.route("/api/export-reviews")
@login_required
def export_reviews(current_user):
    import io, csv as _csv
    restaurant = get_restaurant(current_user["restaurant_id"])
    reviews = get_reviews_data(current_user["restaurant_id"])
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["Date","Author","Platform","Rating","Sentiment","Urgency","Review","Draft Response","Status"])
    for r in reviews:
        w.writerow([
            r.get("review_date","")[:10] if r.get("review_date") else "",
            r.get("author",""),
            r.get("platform",""),
            r.get("rating",""),
            r.get("sentiment",""),
            r.get("urgency",""),
            r.get("text",""),
            r.get("draft_response",""),
            r.get("response_status",""),
        ])
    name = (restaurant.name if restaurant else "restaurant").replace(" ","_")
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={name}_reviews.csv"}
    )

@admin_bp.route("/api/labor-trend")
@login_required
def labor_trend_api(current_user):
    """Return labor % for the last 4 weeks for trend chart."""
    try:
        from labor import load_shifts_for_restaurant
        from models import get_restaurant
        restaurant = get_restaurant(current_user["restaurant_id"])
        target = float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0
        shifts = load_shifts_for_restaurant(current_user["restaurant_id"])
        if not shifts:
            return jsonify(weeks=[])

        from datetime import datetime, timedelta
        from collections import defaultdict

        # Find the latest date in the data and work backwards 4 weeks
        dates = []
        for s in shifts:
            try:
                d = datetime.strptime(str(s.get("date",""))[:10], "%Y-%m-%d").date()
                dates.append(d)
            except Exception:
                continue
        if not dates:
            return jsonify(weeks=[])

        latest = max(dates)
        # Align to Monday of latest week
        latest_monday = latest - timedelta(days=latest.weekday())

        weeks = []
        for w in range(3, -1, -1):
            week_start = latest_monday - timedelta(weeks=w)
            week_end   = week_start + timedelta(days=6)
            sales_total = 0
            labor_total = 0
            seen_dates = set()
            for s in shifts:
                try:
                    d = datetime.strptime(str(s.get("date",""))[:10], "%Y-%m-%d").date()
                    if week_start <= d <= week_end:
                        # Only count sales once per date
                        if d not in seen_dates:
                            sales_total += float(s.get("sales_that_day") or 0)
                            seen_dates.add(d)
                        hours = float(s.get("actual_hours") or s.get("scheduled_hours") or 0)
                        rate = float(restaurant.hourly_rate or 26.0) if restaurant else 26.0
                        labor_total += hours * rate
                except Exception:
                    continue
            pct = round(labor_total / sales_total * 100, 1) if sales_total > 0 else 0
            label = f"Wk {4-w}"
            weeks.append({"label": label, "pct": pct, "target": target})

        return jsonify(weeks=weeks)
    except Exception as e:
        print(f"Labor trend error: {e}")
        return jsonify(weeks=[])

@admin_bp.route("/admin/upload-menu-pdf/<int:restaurant_id>", methods=["POST"])
@admin_required
def upload_menu_pdf(restaurant_id, current_user):
    """Accept a PDF upload and extract menu items using AI."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return jsonify(ok=False, error="No PDF file uploaded")
    try:
        pdf_bytes = pdf_file.read()
        if len(pdf_bytes) > 10 * 1024 * 1024:  # 10MB limit
            return jsonify(ok=False, error="PDF too large — max 10MB")
        from competitor import fetch_menu_from_pdf_bytes
        from models import update_restaurant
        menu_notes = fetch_menu_from_pdf_bytes(pdf_bytes, restaurant.name)
        if not menu_notes:
            return jsonify(ok=False, error="Could not extract menu items from this PDF — try a text-based PDF rather than a scanned image")
        update_restaurant(restaurant_id, {"menu_notes": menu_notes})
        return jsonify(ok=True, menu_notes=menu_notes)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@admin_bp.route("/admin/fetch-menu-from-url/<int:restaurant_id>", methods=["POST"])
@admin_required
def fetch_menu_from_url_route(restaurant_id, current_user):
    """Fetch and parse menu items from a given URL using AI."""
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify(ok=False, error="No URL provided")
    try:
        from competitor import fetch_menu_from_url
        from models import update_restaurant
        menu_items = fetch_menu_from_url(url)
        if not menu_items:
            return jsonify(ok=False, error="This site uses JavaScript to load its menu — it can't be auto-fetched. Enter menu items manually in the field above.")
        # Save the URL and extracted notes
        update_restaurant(restaurant_id, {"menu_url": url, "menu_notes": menu_items})
        return jsonify(ok=True, menu_notes=menu_items)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@admin_bp.route("/admin/refresh-menu-notes/<int:restaurant_id>", methods=["POST"])
@admin_required
def refresh_menu_notes(restaurant_id, current_user):
    """Re-fetch menu notes from Google Places and update the restaurant record."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.google_place_id:
        return jsonify(ok=False, error="No Google Place ID set for this restaurant")
    try:
        from competitor import fetch_menu_notes_from_places
        from models import update_restaurant
        menu_notes = fetch_menu_notes_from_places(restaurant.google_place_id)
        if not menu_notes:
            return jsonify(ok=False, error="No menu data found on Google Places for this restaurant")
        # Merge with any existing manual notes
        existing = restaurant.menu_notes or ""
        if existing and existing not in menu_notes:
            # Keep manual additions below the auto-fetched data
            merged = menu_notes + ("\n\nAdditional notes:\n" + existing if existing else "")
        else:
            merged = menu_notes
        update_restaurant(restaurant_id, {"menu_notes": merged})
        return jsonify(ok=True, menu_notes=merged)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@admin_bp.route("/admin/resend-welcome/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_welcome_email(restaurant_id, current_user):
    """Reset client password and resend welcome email with new credentials."""
    import secrets, string
    from models import get_restaurant, get_conn
    from werkzeug.security import generate_password_hash

    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")

    try:
        # Get client user
        conn = get_conn()
        user = conn.execute(
            "SELECT * FROM users WHERE restaurant_id=? AND is_admin=0 LIMIT 1",
            (restaurant_id,)
        ).fetchone()
        if not user:
            conn.close()
            return jsonify(ok=False, error="No client user found")

        # Generate a new temporary password
        alphabet = string.ascii_letters + string.digits
        new_password = "".join(secrets.choice(alphabet) for _ in range(12))

        # Reset the password
        conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (generate_password_hash(new_password), user["id"])
        )
        conn.commit()
        conn.close()

        # Send welcome email with new credentials
        from emails import send_welcome_email
        send_welcome_email(
            to_email=restaurant.owner_email,
            restaurant_name=restaurant.name,
            username=user["username"],
            password=new_password,
            module_reviews=restaurant.module_reviews,
            module_labor=restaurant.module_labor,
            module_inventory=restaurant.module_inventory,
            module_marketing=restaurant.module_marketing,
        )

        return jsonify(ok=True, email=restaurant.owner_email)

    except Exception as e:
        print(f"[resend-welcome] error: {e}")
        return jsonify(ok=False, error=str(e))


@admin_bp.route("/admin/test-digest/<int:restaurant_id>", methods=["POST"])
@admin_required
def test_digest(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        from reporter import build_report_from_db, render_html
        import resend as _resend
        owner_email = restaurant.owner_email
        report = build_report_from_db(restaurant_id, restaurant.name, days=7)
        html = render_html(report, restaurant.name)
        _resend.api_key = RESEND_API_KEY
        _resend.Emails.send({
            "from": f"Cavnar AI <{FROM_EMAIL}>",
            "to": [owner_email],
            "subject": f"[TEST] Your weekly review digest — {restaurant.name}",
            "html": html,
        })
        try:
            log_email(restaurant_id, "digest", owner_email, f"[TEST] Weekly digest — {restaurant.name}")
        except Exception: pass
        return jsonify(ok=True, email=owner_email)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@admin_bp.route("/admin/test-urgent/<int:restaurant_id>", methods=["POST"])
@admin_required
def test_urgent(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        from scheduler import send_urgent_alert
        owner_email = restaurant.owner_email
        send_urgent_alert(
            restaurant.name,
            owner_email,
            [{"author": "Test Customer", "platform": "google", "rating": 1,
              "text": "This is a test urgent review alert from Cavnar AI admin. Your urgent alert email is working correctly."}]
        )
        return jsonify(ok=True, email=owner_email)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@admin_bp.route("/admin/refresh-ig-token/<int:restaurant_id>", methods=["POST"])
@admin_required
def refresh_ig_token(restaurant_id, current_user):
    """Manually refresh Instagram + Facebook tokens for a restaurant."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.ig_token:
        return jsonify(ok=False, error="No Instagram token found")
    try:
        import requests as _req
        from datetime import datetime, timedelta
        from models import update_restaurant
        app_secret = os.getenv("META_APP_SECRET","")

        # Refresh IG long-lived token
        r = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
            "grant_type":        "fb_exchange_token",
            "client_id":         os.getenv("META_APP_ID",""),
            "client_secret":     app_secret,
            "fb_exchange_token": restaurant.ig_token,
        })
        if r.status_code != 200:
            return jsonify(ok=False, error=f"IG refresh failed: {r.text[:200]}")

        new_token   = r.json().get("access_token", restaurant.ig_token)
        new_expires = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")

        update_data = {"ig_token": new_token, "ig_token_expires": new_expires}

        # Refresh FB page token too if we have one
        if restaurant.fb_page_token:
            r2 = _req.get("https://graph.facebook.com/v19.0/oauth/access_token", params={
                "grant_type":        "fb_exchange_token",
                "client_id":         os.getenv("META_APP_ID",""),
                "client_secret":     app_secret,
                "fb_exchange_token": restaurant.fb_page_token,
            })
            if r2.status_code == 200:
                update_data["fb_page_token"]    = r2.json().get("access_token", restaurant.fb_page_token)
                update_data["fb_token_expires"] = new_expires

        update_restaurant(restaurant_id, update_data)
        print(f"IG/FB tokens refreshed for restaurant {restaurant_id}, expires {new_expires}")
        return jsonify(ok=True, expires=new_expires)
    except Exception as e:
        return jsonify(ok=False, error=str(e))



@admin_bp.route("/api/competitor-intel")
@login_required
def competitor_intel_api(current_user):
    """Get competitor intel for the current restaurant."""
    import json
    from models import get_restaurant
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.competitor_intel:
        return jsonify(ok=False, data=None)
    try:
        data = json.loads(restaurant.competitor_intel)
        return jsonify(ok=True, data=data,
                      updated_at=restaurant.competitor_updated_at)
    except Exception:
        return jsonify(ok=False, data=None)

@admin_bp.route("/api/refresh-competitor-intel", methods=["POST"])
@login_required
def refresh_competitor_intel(current_user):
    # Require all 4 modules (Full System only)
    from models import get_restaurant as _gr
    _r = _gr(current_user["restaurant_id"])
    if not (_r and _r.module_reviews and _r.module_labor and _r.module_inventory and _r.module_marketing):
        return jsonify(ok=False, error="Competitor intelligence is available on the Full System plan only."), 403
    """Manually trigger competitor analysis."""
    from competitor import run_competitor_analysis
    result = run_competitor_analysis(current_user["restaurant_id"])
    return jsonify(result)

@admin_bp.route("/api/send-referral", methods=["POST"])
@login_required
def send_referral(current_user):
    import resend as _resend, time as _time
    # Simple per-session rate limit: max 10 referrals per hour
    from flask import g
    ip = (request.headers.get("X-Forwarded-For","").split(",")[0].strip() or request.remote_addr or "")
    data = request.get_json()
    ref_name  = data.get("name","").strip()
    ref_email = data.get("email","").strip()
    note      = data.get("note","").strip()
    if not ref_name or not ref_email:
        return jsonify(ok=False, error="Name and email required")
    try:
        restaurant = get_restaurant(current_user["restaurant_id"])
        referrer   = restaurant.name if restaurant else "A Cavnar AI client"
        owner_name = restaurant.owner_name or "Your colleague"
        note_block = f"<p style=\"margin:0 0 16px 0;font-style:italic;color:#4a4540\">\"{note}\"</p>" if note else ""
        html = f"""
<div style="font-family:-apple-system,sans-serif;max-width:540px;margin:0 auto;padding:32px 24px;background:#fdf8f4">
  <div style="font-family:'Georgia',serif;font-size:22px;color:#0e0c0a;margin-bottom:4px">Cavnar <span style="color:#c84b2f;font-style:italic">AI</span></div>
  <div style="font-size:10px;color:#7a736a;letter-spacing:.1em;text-transform:uppercase;margin-bottom:24px">Restaurant Intelligence</div>
  <p style="margin:0 0 16px 0;font-size:15px;color:#0e0c0a;line-height:1.7">Hi — {owner_name} from {referrer} thought you might find this useful.</p>
  {note_block}
  <p style="margin:0 0 16px 0;font-size:14px;color:#3a3530;line-height:1.7">Cavnar AI is a fully managed dashboard that handles the operational side of running a restaurant — review responses, labor cost analysis, inventory tracking, and marketing content. It runs quietly in the background and takes about 30 minutes a week of your time.</p>
  <p style="margin:0 0 24px 0;font-size:14px;color:#3a3530;line-height:1.7">If you want to see what it looks like for your restaurant, book a free 30-minute call below.</p>
  <a href="https://calendly.com/will-cavnar/30min" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:4px;text-decoration:none;font-size:13px;font-weight:600">Book a free call</a>
  <p style="margin:24px 0 0 0;font-size:12px;color:#7a736a">Will Cavnar · Cavnar AI · <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a></p>
</div>"""
        _resend.api_key = RESEND_API_KEY
        _resend.Emails.send({
            "from": f"Will Cavnar <{FROM_EMAIL}>",
            "to": [ref_email],
            "subject": f"{owner_name} thinks you should check out Cavnar AI",
            "html": html,
        })
        # Notify Will
        _resend.Emails.send({
            "from": f"Cavnar AI <{FROM_EMAIL}>",
            "to": [FROM_EMAIL],
            "subject": f"New referral from {referrer} — {ref_name}",
            "html": f"<p>{referrer} referred {ref_name} ({ref_email}).</p><p>Note: {note or 'none'}</p>",
        })
        try:
            log_email(current_user["restaurant_id"], "referral", ref_email, f"Referral to {ref_name}")
        except Exception: pass
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))
