"""
admin_routes.py — Cavnar AI admin, infrastructure and API routes
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import (Blueprint, request, jsonify, redirect, url_for,
                   render_template, make_response, send_file, Response, session)
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
        u["pos_system"]            = r.pos_system if r else None
        u["toast_restaurant_guid"] = r.toast_restaurant_guid if r else None
        u["gmb_connected"]         = bool(r and r.gmb_refresh_token)

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

    return render_template('admin.html',
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
    return render_template('client_data.html',
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
    return render_template('client_settings.html',
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
            "skip_holidays":   sanitize(data.get("skip_holidays","")),
            "custom_competitors": sanitize(data.get("custom_competitors","")),
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
            "hours_notes":           sanitize(data.get("hours_notes",""), max_len=1000),
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

@admin_bp.route("/api/review-count")
@login_required
def review_count_api(current_user):
    """Lightweight polling endpoint for new review detection."""
    from models import get_review_stats
    stats = get_review_stats(current_user["restaurant_id"])
    return jsonify(
        total=stats.get("total", 0),
        pending=stats.get("awaiting_approval", 0),
        urgent=stats.get("urgent", 0)
    )


@admin_bp.route("/api/log-activity", methods=["POST"])
@login_required
def log_activity_route(current_user):
    from models import log_activity
    data = request.get_json()
    log_activity(current_user["restaurant_id"], data.get("tab",""))
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
    from drafter import draft_response as _draft_fn
    from models import get_approved_examples as _get_ex
    approved_examples = _get_ex(restaurant_id, limit=4)
    for r in pending_drafts:
        try:
            _draft_fn(
                r.id, r.rating, r.text, r.sentiment,
                restaurant.name,
                voice_notes=restaurant.voice_notes or "",
                restaurant_id=restaurant_id,
                approved_examples=approved_examples,
                sign_off=restaurant.sign_off_name or restaurant.name,
                never_say=restaurant.never_say or "",
            )
        except Exception as _e:
            print(f"[seed] draft error [{r.id}]: {_e}")

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

    if restaurant.gmb_refresh_token or restaurant.reviews_live:
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
                        # Also capture official GBP overall rating
                        try:
                            from gmb import fetch_location_rating
                            fetch_location_rating(restaurant_id, token, loc_id)
                        except Exception:
                            pass
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
            from models import get_pending_analysis, get_pending_drafts, get_approved_examples
            from analyser import analyse_review
            from drafter import draft_response
            for r in get_pending_analysis(restaurant_id, limit=50):
                try: analyse_review(r.id, r.rating, r.text)
                except Exception: pass
            approved_examples = get_approved_examples(restaurant_id, limit=4)
            for r in get_pending_drafts(restaurant_id):
                try:
                    draft_response(
                        r.id, r.rating, r.text, r.sentiment,
                        restaurant.name,
                        voice_notes=restaurant.voice_notes or "",
                        restaurant_id=restaurant_id,
                        approved_examples=approved_examples,
                        sign_off=restaurant.sign_off_name or restaurant.name,
                        never_say=restaurant.never_say or "",
                    )
                except Exception: pass
        except Exception as e:
            print(f"[fetch] background error: {e}")
    threading.Thread(target=_analyse_and_draft, daemon=True).start()

    return jsonify(ok=True, new_reviews=new_count, errors=errors)

@admin_bp.route("/admin/redraft-all/<int:restaurant_id>", methods=["POST"])
@admin_required
def redraft_all(restaurant_id, current_user):
    """Regenerate AI drafts for all existing reviews — resets non-posted to pending first."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    conn = get_conn()
    # Reset all drafted/pending reviews so they queue for re-drafting
    conn.execute(
        "UPDATE reviews SET response_status='pending', draft_response=NULL WHERE restaurant_id=? AND response_status IN ('drafted','pending')",
        (restaurant_id,)
    )
    conn.commit()
    conn.close()
    import threading
    def _redraft():
        try:
            from models import get_pending_drafts, get_approved_examples, get_conn as _gc
            from analyser import analyse_review
            from drafter import draft_response
            # Re-analyse anything missing sentiment
            _conn = _gc()
            unanalysed = _conn.execute(
                "SELECT id, rating, text FROM reviews WHERE restaurant_id=? AND (sentiment IS NULL OR sentiment='') AND processed=0",
                (restaurant_id,)
            ).fetchall()
            _conn.close()
            for r in unanalysed:
                try: analyse_review(r["id"], r["rating"], r["text"])
                except Exception: pass
            approved_examples = get_approved_examples(restaurant_id, limit=4)
            for r in get_pending_drafts(restaurant_id, limit=50):
                try:
                    draft_response(
                        r.id, r.rating, r.text, r.sentiment,
                        restaurant.name,
                        voice_notes=restaurant.voice_notes or "",
                        restaurant_id=restaurant_id,
                        approved_examples=approved_examples,
                        sign_off=restaurant.sign_off_name or restaurant.name,
                        never_say=restaurant.never_say or "",
                    )
                except Exception as _e:
                    print(f"[redraft-all] error [{r.id}]: {_e}")
        except Exception as e:
            print(f"[redraft-all] background error: {e}")
    threading.Thread(target=_redraft, daemon=True).start()
    return jsonify(ok=True)

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

@admin_bp.route("/terms")
def terms_page():
    from flask import Response
    import os as _os
    try:
        html_path = _os.path.join(_os.path.dirname(__file__), "terms.html")
        with open(html_path, "r") as f:
            html = f.read()
    except FileNotFoundError:
        html = "<h1>Terms of Service</h1><p>Coming soon. Contact will@cavnar.ai</p>"
    return Response(html, mimetype="text/html")

@admin_bp.route("/.well-known/security.txt")
def security_txt():
    from flask import Response
    content = (
        "Contact: mailto:will@cavnar.ai\n"
        "Preferred-Languages: en\n"
        "Policy: https://cavnar.ai/privacy\n"
        "Expires: 2027-01-01T00:00:00.000Z\n"
    )
    return Response(content, mimetype="text/plain")

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

@admin_bp.route("/api/inv-trend")
@login_required
def inv_trend_api(current_user):
    """Return weekly waste cost for up to 6 weeks for trend chart."""
    try:
        from models import get_conn as _gc_it
        import json as _json_it
        conn = _gc_it()
        rows = conn.execute("""
            SELECT week_end, waste_json FROM inventory_history
            WHERE restaurant_id=? AND week_end IS NOT NULL
            ORDER BY week_end DESC LIMIT 6
        """, (current_user["restaurant_id"],)).fetchall()
        conn.close()

        if not rows:
            return jsonify(weeks=[])

        # Reverse so oldest is first (left-to-right on chart)
        rows = list(reversed(rows))
        weeks = []
        for row in rows:
            try:
                data = _json_it.loads(row["waste_json"])
                waste = round(float(data.get("total_waste_cost", 0)), 2)
                # Format label: "5/27" from "2026-05-27"
                parts = (row["week_end"] or "").split("-")
                label = f"{int(parts[1])}/{int(parts[2])}" if len(parts) == 3 else row["week_end"]
                weeks.append({"label": label, "waste": waste, "week_end": row["week_end"]})
            except Exception:
                continue

        return jsonify(weeks=weeks)
    except Exception as e:
        return jsonify(weeks=[], error=str(e))

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
            return jsonify(ok=False, error="Could not extract menu from this URL. The site may block automated requests or use JavaScript to load content. Try the PDF upload option instead, or enter items manually.")
        # Save the URL and extracted notes
        update_restaurant(restaurant_id, {"menu_url": url, "menu_notes": menu_items})
        return jsonify(ok=True, menu_notes=menu_items)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@admin_bp.route("/admin/refresh-menu-notes/<int:restaurant_id>", methods=["POST"])
@admin_required
def refresh_menu_notes(restaurant_id, current_user):
    """Re-fetch menu notes from Google Places API and update the restaurant record."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    if not restaurant.google_place_id:
        return jsonify(ok=False, error="No Google Place ID set for this restaurant")
    try:
        from competitor import fetch_menu_notes_from_places
        from models import update_restaurant
        menu_notes = fetch_menu_notes_from_places(restaurant.google_place_id)
        if not menu_notes or len(menu_notes) < 30:
            # Build helpful message with where to find menu data manually
            yelp_url = f"https://www.yelp.com/biz/{restaurant.yelp_business_id}" if restaurant.yelp_business_id else ""
            tips = "Google Places has no menu data for this restaurant. "
            if yelp_url:
                tips += f"Try: 1) Upload a menu PDF, 2) Paste their menu URL and click Fetch, or 3) Copy dishes from their Yelp page ({yelp_url}) into the notes field manually."
            else:
                tips += "Try: 1) Upload a menu PDF, 2) Paste their menu URL and click Fetch, or 3) Enter key dishes manually in the notes field."
            return jsonify(ok=False, error=tips)
        existing = restaurant.menu_notes or ""
        merged = menu_notes if not existing else menu_notes + ("\n\nAdditional notes:\n" + existing if existing not in menu_notes else "")
        update_restaurant(restaurant_id, {"menu_notes": merged})
        has_url = "Menu URL:" in merged
        return jsonify(ok=True, menu_notes=merged,
                       message="\u2713 Updated from Google Places" + (" — menu URL found, dishes extracted" if has_url else ""))
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


@admin_bp.route("/api/admin/seed-labor-history", methods=["POST"])
@admin_required
def seed_labor_history(current_user):
    """One-time: seed June 2025 labor_daily_history for Gia Mia (id=2)."""
    from datetime import date, timedelta
    from models import init_db
    init_db()  # ensure table exists

    restaurant_id = int(request.json.get("restaurant_id", 2))
    DAY_TEMPLATES = {
        0: {"sales": 8200,  "hours": 71},
        1: {"sales": 8800,  "hours": 76},
        2: {"sales": 10500, "hours": 91},
        3: {"sales": 12200, "hours": 105},
        4: {"sales": 16400, "hours": 142},
        5: {"sales": 18800, "hours": 163},
        6: {"sales": 13200, "hours": 114},
    }
    HOLIDAY_OVERRIDES = {
        "2025-06-15": {"sales": 22400, "hours": 194},  # Father's Day 2025
    }
    DAYS_OF_WEEK = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    conn = get_conn()
    inserted = 0
    d = date(2025, 6, 2)
    while d <= date(2025, 6, 29):
        ds = d.strftime("%Y-%m-%d")
        tmpl = HOLIDAY_OVERRIDES.get(ds, DAY_TEMPLATES[d.weekday()])
        sales = float(tmpl["sales"])
        hours = float(tmpl["hours"])
        labor_cost = round(hours * 26, 2)
        labor_pct  = round(labor_cost / sales * 100, 2)
        conn.execute("""
            INSERT OR REPLACE INTO labor_daily_history
              (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, total_hours, saved_at)
            VALUES (?,?,?,?,?,?,?,datetime('now'))
        """, (restaurant_id, ds, DAYS_OF_WEEK[d.weekday()], labor_pct, labor_cost, sales, hours))
        inserted += 1
        d += timedelta(days=1)
    conn.commit()
    # Also set revenue target, labor target, and hours/operations notes
    gia_mia_hours = (
        "Open 11:00am daily. "
        "Cooks arrive 8:30am (2.5h before open for prep). "
        "Servers arrive 10:00am (1h before open for side work and opening duties). "
        "Hosts and bussers arrive 10:30am. "
        "Kitchen close: Mon-Thu 9:30pm, Fri-Sat 11:30pm, Sun 9:00pm. "
        "Last guest: Mon-Thu 10:00pm, Fri-Sat midnight, Sun 9:30pm. "
        "Closer policy: always keep 2 servers + 1 bartender until close; "
        "cut remaining servers earlier when projected sales are lower (slow Mon/Tue = cut non-closers by 8:30pm), "
        "keep full floor on high-volume nights (Fri/Sat = cut non-closers by 9:30pm only). "
        "Fri/Sat closers: 3 servers + 2 bartenders. "
        "Cooks: 1 cook always stays through kitchen close; cut others 1h early on slow nights, keep all on Fri/Sat."
    )
    conn.execute("""
        UPDATE restaurants SET monthly_revenue_target=?, labor_target_pct=?, hours_notes=?
        WHERE id=?
    """, (365000.0, 23.0, gia_mia_hours, restaurant_id))
    conn.commit()

    # Seed realistic Gia Mia shift CSV — 2 weeks reflecting actual metrics
    # ~$91,250/wk revenue, 22.46% labor at $26/hr blended ≈ 788h/wk total
    # Staff: 20 employees across server/cook/bartender/host roles
    gia_mia_csv = """date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes
2026-06-01,Monday,Sofia R.,Server,4:30pm,9:30pm,5.0,5.0,8000,
2026-06-01,Monday,Marcus T.,Server,4:30pm,10:00pm,5.5,5.5,8000,
2026-06-01,Monday,Jamie L.,Server,11:00am,4:00pm,5.0,5.0,8000,
2026-06-01,Monday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-01,Monday,Elena V.,Server,4:00pm,9:00pm,5.0,5.0,8000,
2026-06-01,Monday,Derek M.,Bartender,4:00pm,11:00pm,7.0,7.0,8000,
2026-06-01,Monday,Tomas H.,Bartender,4:30pm,10:00pm,5.5,5.5,8000,
2026-06-01,Monday,Carlos B.,Cook,10:30am,7:00pm,8.5,8.5,8000,
2026-06-01,Monday,Amy C.,Cook,3:00pm,10:00pm,7.0,7.0,8000,
2026-06-01,Monday,Raj P.,Cook,11:00am,7:00pm,8.0,8.0,8000,
2026-06-01,Monday,James H.,Host,11:00am,5:30pm,6.5,6.5,8000,
2026-06-01,Monday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-02,Tuesday,Sofia R.,Server,4:30pm,10:00pm,5.5,5.5,9000,
2026-06-02,Tuesday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-02,Tuesday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-02,Tuesday,Elena V.,Server,4:00pm,9:30pm,5.5,5.5,9000,
2026-06-02,Tuesday,Nina W.,Server,4:30pm,10:00pm,5.5,5.5,9000,
2026-06-02,Tuesday,Derek M.,Bartender,4:00pm,11:30pm,7.5,7.5,9000,
2026-06-02,Tuesday,Tomas H.,Bartender,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-02,Tuesday,Carlos B.,Cook,10:30am,7:00pm,8.5,8.5,9000,
2026-06-02,Tuesday,Amy C.,Cook,2:30pm,10:30pm,8.0,8.0,9000,
2026-06-02,Tuesday,Raj P.,Cook,11:00am,7:30pm,8.5,8.5,9000,
2026-06-02,Tuesday,James H.,Host,11:00am,5:30pm,6.5,6.5,9000,
2026-06-02,Tuesday,Lena S.,Host,4:00pm,10:30pm,6.5,6.5,9000,
2026-06-03,Wednesday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Marcus T.,Server,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-03,Wednesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Priya K.,Server,11:00am,5:00pm,6.0,6.0,11000,
2026-06-03,Wednesday,Elena V.,Server,4:00pm,10:30pm,6.5,6.5,11000,
2026-06-03,Wednesday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-03,Wednesday,Marco D.,Server,5:00pm,10:30pm,5.5,5.5,11000,
2026-06-03,Wednesday,Derek M.,Bartender,4:00pm,11:30pm,7.5,7.5,11000,
2026-06-03,Wednesday,Tomas H.,Bartender,4:30pm,11:30pm,7.0,7.0,11000,
2026-06-03,Wednesday,Carlos B.,Cook,10:00am,7:00pm,9.0,9.0,11000,
2026-06-03,Wednesday,Amy C.,Cook,2:00pm,11:00pm,9.0,9.0,11000,
2026-06-03,Wednesday,Raj P.,Cook,11:00am,7:30pm,8.5,8.5,11000,
2026-06-03,Wednesday,Leo K.,Cook,3:00pm,10:00pm,7.0,7.0,11000,
2026-06-03,Wednesday,James H.,Host,11:00am,5:30pm,6.5,6.5,11000,
2026-06-03,Wednesday,Lena S.,Host,4:00pm,11:00pm,7.0,7.0,11000,
2026-06-04,Thursday,Sofia R.,Server,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Marcus T.,Server,4:30pm,11:30pm,7.0,7.0,13000,
2026-06-04,Thursday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Priya K.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Elena V.,Server,4:00pm,11:00pm,7.0,7.0,13000,
2026-06-04,Thursday,Nina W.,Server,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-04,Thursday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,13000,
2026-06-04,Thursday,Gina F.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-04,Thursday,Derek M.,Bartender,4:00pm,12:00am,8.0,8.0,13000,
2026-06-04,Thursday,Tomas H.,Bartender,4:30pm,11:30pm,7.0,7.0,13000,
2026-06-04,Thursday,Kim T.,Bartender,6:00pm,12:00am,6.0,6.0,13000,
2026-06-04,Thursday,Carlos B.,Cook,10:00am,7:30pm,9.5,9.5,13000,
2026-06-04,Thursday,Amy C.,Cook,2:00pm,11:00pm,9.0,9.0,13000,
2026-06-04,Thursday,Raj P.,Cook,11:00am,8:00pm,9.0,9.0,13000,
2026-06-04,Thursday,Leo K.,Cook,3:00pm,11:00pm,8.0,8.0,13000,
2026-06-04,Thursday,James H.,Host,11:00am,6:00pm,7.0,7.0,13000,
2026-06-04,Thursday,Lena S.,Host,4:00pm,11:30pm,7.5,7.5,13000,
2026-06-05,Friday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-05,Friday,Marcus T.,Server,4:30pm,12:00am,7.5,7.5,18000,
2026-06-05,Friday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-05,Friday,Priya K.,Server,4:30pm,11:30pm,7.0,7.0,18000,
2026-06-05,Friday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-05,Friday,Nina W.,Server,4:30pm,12:00am,7.5,7.5,18000,
2026-06-05,Friday,Marco D.,Server,5:00pm,12:00am,7.0,7.0,18000,
2026-06-05,Friday,Gina F.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-05,Friday,Tony A.,Busser,4:30pm,12:00am,7.5,7.5,18000,
2026-06-05,Friday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-05,Friday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,18000,
2026-06-05,Friday,Tomas H.,Bartender,4:30pm,12:00am,7.5,7.5,18000,
2026-06-05,Friday,Kim T.,Bartender,5:30pm,12:00am,6.5,6.5,18000,
2026-06-05,Friday,Carlos B.,Cook,10:00am,8:00pm,10.0,10.0,18000,
2026-06-05,Friday,Amy C.,Cook,12:00pm,10:00pm,10.0,10.0,18000,
2026-06-05,Friday,Raj P.,Cook,10:30am,8:30pm,10.0,10.0,18000,
2026-06-05,Friday,Leo K.,Cook,3:00pm,12:00am,9.0,9.0,18000,
2026-06-05,Friday,James H.,Host,11:00am,6:00pm,7.0,7.0,18000,
2026-06-05,Friday,Lena S.,Host,4:30pm,12:00am,7.5,7.5,18000,
2026-06-06,Saturday,Sofia R.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Marcus T.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Priya K.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Elena V.,Server,11:00am,5:30pm,6.5,6.5,20000,
2026-06-06,Saturday,Nina W.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Marco D.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-06,Saturday,Gina F.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Tony A.,Busser,11:00am,6:00pm,7.0,7.0,20000,
2026-06-06,Saturday,Sam V.,Busser,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Tomas H.,Bartender,4:30pm,12:00am,7.5,7.5,20000,
2026-06-06,Saturday,Kim T.,Bartender,5:00pm,12:00am,7.0,7.0,20000,
2026-06-06,Saturday,Carlos B.,Cook,9:00am,7:00pm,10.0,10.0,20000,
2026-06-06,Saturday,Amy C.,Cook,11:00am,9:00pm,10.0,10.0,20000,
2026-06-06,Saturday,Raj P.,Cook,9:30am,7:30pm,10.0,10.0,20000,
2026-06-06,Saturday,Leo K.,Cook,2:00pm,12:00am,10.0,10.0,20000,
2026-06-06,Saturday,James H.,Host,11:00am,7:00pm,8.0,8.0,20000,
2026-06-06,Saturday,Lena S.,Host,4:30pm,12:00am,7.5,7.5,20000,
2026-06-07,Sunday,Sofia R.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,12250,
2026-06-07,Sunday,Priya K.,Server,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Elena V.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-07,Sunday,Marco D.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,12250,
2026-06-07,Sunday,Tomas H.,Bartender,4:00pm,11:00pm,7.0,7.0,12250,
2026-06-07,Sunday,Kim T.,Bartender,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-07,Sunday,Carlos B.,Cook,9:00am,6:30pm,9.5,9.5,12250,
2026-06-07,Sunday,Amy C.,Cook,11:00am,8:00pm,9.0,9.0,12250,
2026-06-07,Sunday,Raj P.,Cook,10:00am,7:00pm,9.0,9.0,12250,
2026-06-07,Sunday,Leo K.,Cook,2:00pm,10:00pm,8.0,8.0,12250,
2026-06-07,Sunday,James H.,Host,11:00am,5:30pm,6.5,6.5,12250,
2026-06-07,Sunday,Lena S.,Host,4:00pm,10:30pm,6.5,6.5,12250,
2026-06-08,Monday,Sofia R.,Server,4:30pm,9:30pm,5.0,5.0,8000,
2026-06-08,Monday,Marcus T.,Server,4:30pm,10:00pm,5.5,5.5,8000,
2026-06-08,Monday,Jamie L.,Server,11:00am,4:00pm,5.0,5.0,8000,
2026-06-08,Monday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,8000,
2026-06-08,Monday,Elena V.,Server,4:00pm,9:00pm,5.0,5.0,8000,
2026-06-08,Monday,Derek M.,Bartender,4:00pm,11:00pm,7.0,7.0,8000,
2026-06-08,Monday,Tomas H.,Bartender,4:30pm,10:00pm,5.5,5.5,8000,
2026-06-08,Monday,Carlos B.,Cook,10:30am,7:00pm,8.5,8.5,8000,
2026-06-08,Monday,Amy C.,Cook,3:00pm,10:00pm,7.0,7.0,8000,
2026-06-08,Monday,Raj P.,Cook,11:00am,7:00pm,8.0,8.0,8000,
2026-06-08,Monday,James H.,Host,11:00am,5:30pm,6.5,6.5,8000,
2026-06-08,Monday,Lena S.,Host,4:00pm,10:00pm,6.0,6.0,8000,
2026-06-09,Tuesday,Sofia R.,Server,4:30pm,10:00pm,5.5,5.5,9000,
2026-06-09,Tuesday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Priya K.,Server,11:00am,4:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Elena V.,Server,4:00pm,9:30pm,5.5,5.5,9000,
2026-06-09,Tuesday,Nina W.,Server,4:30pm,10:00pm,5.5,5.5,9000,
2026-06-09,Tuesday,Derek M.,Bartender,4:00pm,11:30pm,7.5,7.5,9000,
2026-06-09,Tuesday,Tomas H.,Bartender,4:30pm,10:30pm,6.0,6.0,9000,
2026-06-09,Tuesday,Carlos B.,Cook,10:30am,7:00pm,8.5,8.5,9000,
2026-06-09,Tuesday,Amy C.,Cook,2:30pm,10:30pm,8.0,8.0,9000,
2026-06-09,Tuesday,Raj P.,Cook,11:00am,7:30pm,8.5,8.5,9000,
2026-06-09,Tuesday,James H.,Host,11:00am,5:30pm,6.5,6.5,9000,
2026-06-09,Tuesday,Lena S.,Host,4:00pm,10:30pm,6.5,6.5,9000,
2026-06-10,Wednesday,Sofia R.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Marcus T.,Server,4:30pm,11:00pm,6.5,6.5,11000,
2026-06-10,Wednesday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,11000,
2026-06-10,Wednesday,Priya K.,Server,11:00am,5:00pm,6.0,6.0,11000,
2026-06-10,Wednesday,Elena V.,Server,4:00pm,10:30pm,6.5,6.5,11000,
2026-06-10,Wednesday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,11000,
2026-06-10,Wednesday,Marco D.,Server,5:00pm,10:30pm,5.5,5.5,11000,
2026-06-10,Wednesday,Derek M.,Bartender,4:00pm,11:30pm,7.5,7.5,11000,
2026-06-10,Wednesday,Tomas H.,Bartender,4:30pm,11:30pm,7.0,7.0,11000,
2026-06-10,Wednesday,Carlos B.,Cook,10:00am,7:00pm,9.0,9.0,11000,
2026-06-10,Wednesday,Amy C.,Cook,2:00pm,11:00pm,9.0,9.0,11000,
2026-06-10,Wednesday,Raj P.,Cook,11:00am,7:30pm,8.5,8.5,11000,
2026-06-10,Wednesday,Leo K.,Cook,3:00pm,10:00pm,7.0,7.0,11000,
2026-06-10,Wednesday,James H.,Host,11:00am,5:30pm,6.5,6.5,11000,
2026-06-10,Wednesday,Lena S.,Host,4:00pm,11:00pm,7.0,7.0,11000,
2026-06-11,Thursday,Sofia R.,Server,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-11,Thursday,Marcus T.,Server,4:30pm,11:30pm,7.0,7.0,13000,
2026-06-11,Thursday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Priya K.,Server,11:00am,5:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Elena V.,Server,4:00pm,11:00pm,7.0,7.0,13000,
2026-06-11,Thursday,Nina W.,Server,4:30pm,11:00pm,6.5,6.5,13000,
2026-06-11,Thursday,Marco D.,Server,5:00pm,11:00pm,6.0,6.0,13000,
2026-06-11,Thursday,Gina F.,Server,4:30pm,10:30pm,6.0,6.0,13000,
2026-06-11,Thursday,Derek M.,Bartender,4:00pm,12:00am,8.0,8.0,13000,
2026-06-11,Thursday,Tomas H.,Bartender,4:30pm,11:30pm,7.0,7.0,13000,
2026-06-11,Thursday,Kim T.,Bartender,6:00pm,12:00am,6.0,6.0,13000,
2026-06-11,Thursday,Carlos B.,Cook,10:00am,7:30pm,9.5,9.5,13000,
2026-06-11,Thursday,Amy C.,Cook,2:00pm,11:00pm,9.0,9.0,13000,
2026-06-11,Thursday,Raj P.,Cook,11:00am,8:00pm,9.0,9.0,13000,
2026-06-11,Thursday,Leo K.,Cook,3:00pm,11:00pm,8.0,8.0,13000,
2026-06-11,Thursday,James H.,Host,11:00am,6:00pm,7.0,7.0,13000,
2026-06-11,Thursday,Lena S.,Host,4:00pm,11:30pm,7.5,7.5,13000,
2026-06-12,Friday,Sofia R.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Marcus T.,Server,4:30pm,12:00am,7.5,7.5,18000,
2026-06-12,Friday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Priya K.,Server,4:30pm,11:30pm,7.0,7.0,18000,
2026-06-12,Friday,Elena V.,Server,11:00am,4:30pm,5.5,5.5,18000,
2026-06-12,Friday,Nina W.,Server,4:30pm,12:00am,7.5,7.5,18000,
2026-06-12,Friday,Marco D.,Server,5:00pm,12:00am,7.0,7.0,18000,
2026-06-12,Friday,Gina F.,Server,11:00am,5:00pm,6.0,6.0,18000,
2026-06-12,Friday,Tony A.,Busser,4:30pm,12:00am,7.5,7.5,18000,
2026-06-12,Friday,Sam V.,Busser,11:00am,5:30pm,6.5,6.5,18000,
2026-06-12,Friday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,18000,
2026-06-12,Friday,Tomas H.,Bartender,4:30pm,12:00am,7.5,7.5,18000,
2026-06-12,Friday,Kim T.,Bartender,5:30pm,12:00am,6.5,6.5,18000,
2026-06-12,Friday,Carlos B.,Cook,10:00am,8:00pm,10.0,10.0,18000,
2026-06-12,Friday,Amy C.,Cook,12:00pm,10:00pm,10.0,10.0,18000,
2026-06-12,Friday,Raj P.,Cook,10:30am,8:30pm,10.0,10.0,18000,
2026-06-12,Friday,Leo K.,Cook,3:00pm,12:00am,9.0,9.0,18000,
2026-06-12,Friday,James H.,Host,11:00am,6:00pm,7.0,7.0,18000,
2026-06-12,Friday,Lena S.,Host,4:30pm,12:00am,7.5,7.5,18000,
2026-06-13,Saturday,Sofia R.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Marcus T.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Jamie L.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Priya K.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Elena V.,Server,11:00am,5:30pm,6.5,6.5,20000,
2026-06-13,Saturday,Nina W.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Marco D.,Server,11:00am,5:00pm,6.0,6.0,20000,
2026-06-13,Saturday,Gina F.,Server,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Tony A.,Busser,11:00am,6:00pm,7.0,7.0,20000,
2026-06-13,Saturday,Sam V.,Busser,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Tomas H.,Bartender,4:30pm,12:00am,7.5,7.5,20000,
2026-06-13,Saturday,Kim T.,Bartender,5:00pm,12:00am,7.0,7.0,20000,
2026-06-13,Saturday,Carlos B.,Cook,9:00am,7:00pm,10.0,10.0,20000,
2026-06-13,Saturday,Amy C.,Cook,11:00am,9:00pm,10.0,10.0,20000,
2026-06-13,Saturday,Raj P.,Cook,9:30am,7:30pm,10.0,10.0,20000,
2026-06-13,Saturday,Leo K.,Cook,2:00pm,12:00am,10.0,10.0,20000,
2026-06-13,Saturday,James H.,Host,11:00am,7:00pm,8.0,8.0,20000,
2026-06-13,Saturday,Lena S.,Host,4:30pm,12:00am,7.5,7.5,20000,
2026-06-14,Sunday,Sofia R.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Marcus T.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Jamie L.,Server,11:00am,4:30pm,5.5,5.5,12250,
2026-06-14,Sunday,Priya K.,Server,4:00pm,10:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Elena V.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Nina W.,Server,4:30pm,10:30pm,6.0,6.0,12250,
2026-06-14,Sunday,Marco D.,Server,11:00am,5:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Derek M.,Bartender,11:00am,7:00pm,8.0,8.0,12250,
2026-06-14,Sunday,Tomas H.,Bartender,4:00pm,11:00pm,7.0,7.0,12250,
2026-06-14,Sunday,Kim T.,Bartender,5:00pm,11:00pm,6.0,6.0,12250,
2026-06-14,Sunday,Carlos B.,Cook,9:00am,6:30pm,9.5,9.5,12250,
2026-06-14,Sunday,Amy C.,Cook,11:00am,8:00pm,9.0,9.0,12250,
2026-06-14,Sunday,Raj P.,Cook,10:00am,7:00pm,9.0,9.0,12250,
2026-06-14,Sunday,Leo K.,Cook,2:00pm,10:00pm,8.0,8.0,12250,
2026-06-14,Sunday,James H.,Host,11:00am,5:30pm,6.5,6.5,12250,
2026-06-14,Sunday,Lena S.,Host,4:00pm,10:30pm,6.5,6.5,12250,"""

    # Save as client shifts_csv
    from models import get_client_data, upsert_client_data
    existing = get_client_data(restaurant_id) or {}
    existing["shifts_csv"] = gia_mia_csv
    upsert_client_data(restaurant_id, existing)

    conn.close()
    return jsonify(ok=True, inserted=inserted, restaurant_id=restaurant_id, labor_target_set=23.0, monthly_revenue_target_set=365000, shifts_seeded=True)
