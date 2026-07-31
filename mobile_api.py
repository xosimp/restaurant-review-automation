"""
mobile_api.py — JSON API for the Cavnar AI iOS app.

A separate blueprint from client_api.py's client_bp, for two concrete
reasons:

1. CSRF. client_bp is wrapped in csrf_protect() (see csrf.py), a cookie-based
   double-submit scheme a bearer-token client has no cookie jar to carry.
   This blueprint is never passed to csrf_protect() — the same exemption
   pattern webhook_bp already uses for its own HMAC-verified callers.
2. Auth. login_required (auth.py) only reads a session cookie, and its
   redirect-vs-JSON branch hardcodes an "/api/" path prefix that a
   "/mobile/api/" route wouldn't match. Rather than teach a decorator three
   other blueprints depend on to also handle bearer tokens, this blueprint
   gets its own: auth.mobile_login_required.

Business logic is NOT duplicated: every route below that has a web
equivalent calls the exact same _do_*() helper client_api.py's own routes
call, so there's one implementation behind both the dashboard and the app.
"""
import base64
import hmac
import random
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

from auth import (
    verify_password, create_session, delete_session,
    revoke_other_sessions, mobile_login_required, get_user_by_restaurant_id,
)
from auth_routes import _is_rate_limited, _record_failed_attempt, _clear_attempts, _get_client_ip
from models import get_restaurant, update_restaurant, get_conn

import client_api as _capi

mobile_bp = Blueprint('mobile_api', __name__, url_prefix='/mobile/api')


def _public_user(user):
    """Fields safe to hand to the client — never the password hash."""
    return {
        "id": user["id"], "username": user["username"], "email": user["email"],
        "restaurant_id": user["restaurant_id"], "role": user.get("role") or "client",
        "is_admin": bool(user.get("is_admin")),
    }


def _bearer_token():
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else ""


def _send_login_notification(user, ip, user_agent):
    try:
        rid = user.get("restaurant_id")
        rest = get_restaurant(rid) if rid else None
        if rest and getattr(rest, "login_notify", 0) and rest.owner_email:
            from emails import send_login_notification
            send_login_notification(rest.owner_email, rest.name or "", ip, user_agent)
    except Exception as e:
        print(f"[LoginNotify-mobile] {e}")


# ── Auth ──────────────────────────────────────────────────────────────────

@mobile_bp.route("/login", methods=["POST"])
def mobile_login():
    """Mirrors auth_routes.login()'s POST branch, minus the HTML/redirect/CSRF
    parts a native client has no use for. Same IP rate limiting (shared with
    the web login's in-memory limiter — one brute-force counter, not two),
    same 2FA pending-token scheme, same create_session() — just JSON in,
    JSON out, and device_type='ios' on the resulting session."""
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return jsonify(ok=False, error="Too many failed attempts. Please wait 5 minutes and try again."), 429
    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = verify_password(username, password)
    if not user:
        _record_failed_attempt(ip)
        return jsonify(ok=False, error="Invalid username or password"), 401
    _clear_attempts(ip)

    rid = user.get("restaurant_id")
    rest = get_restaurant(rid) if rid and not user.get("is_admin") else None
    # iOS persists the "remember this device" value in Keychain (rather than
    # the web's device_token_<rid> cookie) and resends it here.
    device_token = (data.get("device_token") or "").strip()
    two_fa_on = bool(rest and rest.two_fa_enabled and not user.get("is_admin"))
    device_ok = bool(device_token and device_token == (rest.two_fa_device_token or ""))

    if two_fa_on and not device_ok:
        code = str(random.randint(100000, 999999))
        expires = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
        import secrets as _secrets
        pending = _secrets.token_hex(24)
        update_restaurant(rid, {"two_fa_code": code, "two_fa_expires": expires, "two_fa_pending": pending})
        masked = "your registered email"
        try:
            email = rest.owner_email or ""
            if "@" in email:
                from emails import send_2fa_code
                send_2fa_code(email, rest.name or "your restaurant", code, rest.owner_name)
                masked = email[:2] + "***@" + email.split("@")[-1]
        except Exception:
            pass
        pending_encoded = base64.urlsafe_b64encode(f"{rid}:{pending}".encode()).decode()
        return jsonify(ok=True, requires_2fa=True, pending_token=pending_encoded, masked_email=masked)

    ua = request.headers.get("User-Agent", "Cavnar-iOS")
    token = create_session(user["id"], ip_address=ip, user_agent=ua, device_type="ios")
    _send_login_notification(user, ip, ua)
    return jsonify(ok=True, requires_2fa=False, token=token, user=_public_user(user))


@mobile_bp.route("/verify-2fa", methods=["POST"])
def mobile_verify_2fa():
    """Mirrors auth_routes.verify_2fa()'s POST branch — same pending-token
    decode, same HMAC-compared pending secret (proves this code was actually
    issued by OUR /login call for this restaurant, not just a guessed id),
    same rate limiting, same single-use code clearing. On success, creates an
    ios-tagged session and, if remember_device is set, mints the same
    two_fa_device_token value the web flow does."""
    ip = _get_client_ip()
    if _is_rate_limited("2fa:" + ip):
        return jsonify(ok=False, error="Too many attempts. Please wait 5 minutes and try again."), 429
    data = request.get_json() or {}
    pending_token = data.get("pending_token", "")
    code_entered = (data.get("code") or "").strip()
    remember = bool(data.get("remember_device"))

    try:
        decoded = base64.urlsafe_b64decode(pending_token.encode()).decode()
        rid_str, pending_secret = decoded.split(":", 1)
        rid = int(rid_str)
    except Exception:
        return jsonify(ok=False, error="Session expired — please log in again."), 401
    rest = get_restaurant(rid) if rid else None
    if not rest:
        return jsonify(ok=False, error="Session expired — please log in again."), 401

    stored_pending = rest.two_fa_pending or ""
    if not stored_pending or not hmac.compare_digest(stored_pending, pending_secret):
        _record_failed_attempt("2fa:" + ip)
        return jsonify(ok=False, error="Session expired — please log in again."), 401

    if not (rest.two_fa_code and hmac.compare_digest(rest.two_fa_code, code_entered)):
        _record_failed_attempt("2fa:" + ip)
        return jsonify(ok=False, error="Incorrect code. Try again."), 401

    try:
        expires = datetime.strptime(rest.two_fa_expires, "%Y-%m-%d %H:%M:%S")
    except Exception:
        expires = datetime.now()
    if datetime.now() > expires:
        return jsonify(ok=False, error="Code expired. Request a new one."), 401

    _clear_attempts("2fa:" + ip)
    update_restaurant(rid, {"two_fa_code": "", "two_fa_expires": "", "two_fa_pending": ""})
    user = get_user_by_restaurant_id(rid)
    if not user:
        return jsonify(ok=False, error="Session expired — please log in again."), 401

    ua = request.headers.get("User-Agent", "Cavnar-iOS")
    token = create_session(user["id"], ip_address=ip, user_agent=ua, device_type="ios")
    _send_login_notification(user, ip, ua)

    device_token = None
    if remember:
        import secrets as _secrets
        device_token = _secrets.token_hex(32)
        update_restaurant(rid, {"two_fa_device_token": device_token})

    return jsonify(ok=True, token=token, device_token=device_token, user=_public_user(user))


@mobile_bp.route("/logout", methods=["POST"])
@mobile_login_required
def mobile_logout(current_user):
    token = _bearer_token()
    if token:
        delete_session(token)
    return jsonify(ok=True)


@mobile_bp.route("/sessions/revoke-others", methods=["POST"])
@mobile_login_required
def mobile_revoke_other_sessions(current_user):
    revoke_other_sessions(current_user["id"], _bearer_token())
    return jsonify(ok=True)


# ── Home ──────────────────────────────────────────────────────────────────
# No JSON equivalent of this exists on the web side — index() (hosted_
# dashboard.py) computes a much larger set of desktop-oriented figures
# (agency-equivalent marketing value, full savings-breakdown ledger, the
# onboarding checklist, holiday/event banners) and renders them straight into
# Jinja. This is a deliberately trimmed aggregate for the phone: the KPI
# numbers an owner actually glances at, plus the same "needs attention" list
# the web Home tab shows, built from the exact same underlying data
# (get_review_stats, analyse_shifts_for_restaurant, analyse_inventory) so the
# two surfaces never disagree — just less of it, and no marketing copy.

def _intel_home_kpi(restaurant):
    """No stored numeric 'average competitor rating' exists — competitor_intel
    is free-text AI analysis, not structured data. Rather than fabricate a
    number, the Home tile surfaces how many recommendations are ready to
    read (the full Intel screen does the real side-by-side comparison)."""
    if not getattr(restaurant, "competitor_intel", None):
        return {"value": "—", "sublabel": "no data yet"}
    try:
        from competitor_intel_format import parse_competitor_intel
        parsed = parse_competitor_intel(restaurant.competitor_intel)
        n = len(parsed.get("recommendations", []))
        return {"value": str(n), "sublabel": f"recommendation{'' if n == 1 else 's'} ready"}
    except Exception:
        return {"value": "—", "sublabel": "no data yet"}


def _do_mobile_home(current_user):
    from models import get_review_stats, get_active_modules
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404

    active_modules = get_active_modules(restaurant)
    active_keys = {m["key"] for m in active_modules}

    rstats = get_review_stats(rid) if "reviews" in active_keys else {}
    labor = None
    if "labor" in active_keys:
        try:
            from labor import analyse_shifts_for_restaurant
            labor = analyse_shifts_for_restaurant(rid)
        except Exception:
            labor = None

    modules_out = []
    for m in active_modules:
        key = m["key"]
        kpi = None
        if key == "reviews":
            kpi = {
                "value": f"{rstats.get('responded', 0)}/{rstats.get('total', 0)}",
                "sublabel": f"{rstats.get('response_rate', 0)}% response rate",
            }
        elif key == "labor":
            labor_target = float(restaurant.labor_target_pct or 30.0)
            overall_pct = (labor or {}).get("overall_labor_pct", 0)
            on_track = overall_pct <= labor_target
            kpi = {
                "value": f"{round(overall_pct, 1)}%",
                "sublabel": "on track" if on_track else f"over {int(labor_target)}% target",
            }
        elif key == "inventory":
            try:
                from inventory import load_inventory_for_restaurant, analyse_inventory
                _items, _live = load_inventory_for_restaurant(rid)
                inv = analyse_inventory(_items)
            except Exception:
                inv = {}
            kpi = {
                "value": f"${int(inv.get('recoverable_monthly', 0))}",
                "sublabel": "recoverable / mo",
            }
        elif key == "marketing":
            try:
                conn = get_conn()
                conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
                    content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
                    created_at TEXT DEFAULT (datetime('now')))""")
                this_month = conn.execute(
                    "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')",
                    (rid,)
                ).fetchone()[0] or 0
                conn.close()
            except Exception:
                this_month = 0
            kpi = {"value": str(this_month), "sublabel": "pieces this month"}
        elif key == "intel":
            kpi = _intel_home_kpi(restaurant)

        modules_out.append({"key": key, "label": m["label"], "icon": key, "status": m["status"], "kpi": kpi})

    # "Needs attention" — same three checks and thresholds as the web Home
    # tab's card list (templates/dashboard.html, id="home-attention-list").
    # `module` names the module key a tap should navigate into — the mobile
    # app no longer has one tab per module, so this is a key into the
    # Modules grid/registry, not a literal tab name.
    needs_attention = []
    if "reviews" in active_keys and rstats.get("awaiting_approval", 0) > 0:
        n = rstats["awaiting_approval"]
        needs_attention.append({
            "type": "reviews_awaiting_approval", "module": "reviews",
            "title": f"{n} review{'' if n == 1 else 's'} awaiting approval",
            "detail": "AI responses drafted — publish in one click",
        })
    if "labor" in active_keys and labor and labor.get("overtime_risk"):
        ot_count = sum(1 for o in labor["overtime_risk"] if o.get("status") == "overtime")
        if ot_count > 0:
            needs_attention.append({
                "type": "labor_overtime", "module": "labor",
                "title": f"{ot_count} staff member{'' if ot_count == 1 else 's'} in overtime",
                "detail": f"Est. ${ot_count * 38}+ extra in OT wages this week",
            })
    if "reviews" in active_keys and rstats.get("total", 0) > 0 and rstats.get("response_rate", 0) < 50:
        needs_attention.append({
            "type": "low_response_rate", "module": "reviews",
            "title": f"Response rate at {rstats['response_rate']}%",
            "detail": "Restaurants at 80%+ get 2x more new guests",
        })

    return {
        "ok": True,
        "restaurant_name": restaurant.name,
        "location_name": restaurant.location_name or None,
        "brand_color": restaurant.brand_color or None,
        "modules": modules_out,
        "needs_attention": needs_attention,
    }, 200


@mobile_bp.route("/home")
@mobile_login_required
def mobile_home(current_user):
    payload, status = _do_mobile_home(current_user)
    return jsonify(**payload), status


# ── Reviews ───────────────────────────────────────────────────────────────

def _do_mobile_reviews(restaurant_id, filter_by="all", search=""):
    from models import get_reviews_data
    reviews = get_reviews_data(restaurant_id, filter_by, search)
    return {"ok": True, "reviews": reviews}, 200


@mobile_bp.route("/reviews")
@mobile_login_required
def mobile_reviews(current_user):
    payload, status = _do_mobile_reviews(
        current_user["restaurant_id"],
        request.args.get("filter", "all"),
        request.args.get("search", ""),
    )
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/approve", methods=["POST"])
@mobile_login_required
def mobile_approve_review(review_id, current_user):
    payload, status = _capi._do_approve(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/skip", methods=["POST"])
@mobile_login_required
def mobile_skip_review(review_id, current_user):
    payload, status = _capi._do_skip(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/regenerate-draft", methods=["POST"])
@mobile_login_required
def mobile_regenerate_draft(review_id, current_user):
    payload, status = _capi._do_regenerate_draft(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/save-draft", methods=["POST"])
@mobile_login_required
def mobile_save_draft(review_id, current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_save_draft(review_id, current_user["restaurant_id"], data.get("draft", ""))
    return jsonify(**payload), status


@mobile_bp.route("/review-stats")
@mobile_login_required
def mobile_review_stats(current_user):
    payload, status = _capi._do_review_stats(current_user["restaurant_id"])
    return jsonify(**payload), status


# ── Notifications ─────────────────────────────────────────────────────────

@mobile_bp.route("/notifications")
@mobile_login_required
def mobile_notifications(current_user):
    payload, status = _capi._do_get_notifications(current_user["restaurant_id"])
    return jsonify(**payload), status


# ── Ask Cavnar ────────────────────────────────────────────────────────────

@mobile_bp.route("/ask-cavnar", methods=["POST"])
@mobile_login_required
def mobile_ask_cavnar(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_ask_cavnar(current_user["restaurant_id"], data.get("question"))
    return jsonify(**payload), status


# ── Food cost quick-entry ─────────────────────────────────────────────────

@mobile_bp.route("/food-cost/quickcount", methods=["POST"])
@mobile_login_required
def mobile_food_cost_quickcount(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_food_cost_quickcount(current_user["restaurant_id"], data.get("items", []))
    return jsonify(**payload), status


@mobile_bp.route("/food-cost/custom-item", methods=["POST"])
@mobile_login_required
def mobile_save_food_cost_custom_item(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_save_food_cost_custom_item(current_user["restaurant_id"], data.get("name"), data.get("unit"))
    return jsonify(**payload), status


@mobile_bp.route("/food-cost/custom-item", methods=["DELETE"])
@mobile_login_required
def mobile_delete_food_cost_custom_item(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_delete_food_cost_custom_item(current_user["restaurant_id"], data.get("name"))
    return jsonify(**payload), status


# ── Restaurant switcher ───────────────────────────────────────────────────

@mobile_bp.route("/switch-location", methods=["POST"])
@mobile_login_required
def mobile_switch_location(current_user):
    data = request.get_json() or {}
    target_id = int(data.get("restaurant_id", 0))
    payload, status = _capi._do_switch_location(current_user, target_id, _bearer_token())
    return jsonify(**payload), status


@mobile_bp.route("/group-locations")
@mobile_login_required
def mobile_group_locations(current_user):
    payload, status = _capi._do_group_locations(current_user)
    return jsonify(**payload), status


# ── Push device-token registration ─────────────────────────────────────────

@mobile_bp.route("/device-tokens", methods=["POST"])
@mobile_login_required
def mobile_register_device_token(current_user):
    from push import register_device_token
    data = request.get_json() or {}
    apns_token = (data.get("apns_token") or "").strip()
    environment = data.get("environment") or "production"
    if not apns_token:
        return jsonify(ok=False, error="apns_token required"), 400
    if environment not in ("sandbox", "production"):
        return jsonify(ok=False, error="environment must be 'sandbox' or 'production'"), 400
    register_device_token(current_user["id"], current_user["restaurant_id"], apns_token, environment)
    return jsonify(ok=True)


@mobile_bp.route("/device-tokens/<apns_token>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_device_token(apns_token, current_user):
    from push import get_device_tokens, remove_device_token
    # apns_tokens are long random hex strings issued by Apple, not sequential
    # ids — effectively unguessable — but scope the delete to the caller's
    # own restaurant anyway rather than trusting any authenticated bearer.
    owned = any(t["apns_token"] == apns_token for t in get_device_tokens(current_user["restaurant_id"]))
    if not owned:
        return jsonify(ok=False, error="Device token not found"), 404
    remove_device_token(apns_token)
    return jsonify(ok=True)


# ── Labor ─────────────────────────────────────────────────────────────────

def _do_mobile_labor(restaurant_id):
    from labor import analyse_shifts_for_restaurant

    restaurant = get_restaurant(restaurant_id)
    target = float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0
    try:
        analysis = analyse_shifts_for_restaurant(restaurant_id)
    except Exception:
        analysis = {}

    overall_pct = analysis.get("overall_labor_pct", 0)
    overtime_risk = [
        {
            "employee": o.get("employee"), "hours": o.get("hours"),
            "week": o.get("week"), "status": o.get("status"),
        }
        for o in analysis.get("overtime_risk", [])
    ]
    role_summary = sorted(
        (
            {
                "role": role, "hours": d.get("hours", 0), "labor_cost": d.get("labor_cost", 0),
                "headcount": d.get("headcount", 0), "labor_pct": d.get("labor_pct", 0),
            }
            for role, d in (analysis.get("role_summary") or {}).items()
        ),
        key=lambda r: r["labor_pct"], reverse=True,
    )

    return {
        "ok": True,
        "is_live": bool(analysis.get("is_live")),
        "overall_labor_pct": overall_pct,
        "target": target,
        "on_track": overall_pct <= target,
        "potential_savings": analysis.get("potential_savings", 0),
        "overtime_risk": overtime_risk,
        "role_summary": role_summary,
    }, 200


@mobile_bp.route("/labor")
@mobile_login_required
def mobile_labor(current_user):
    payload, status = _do_mobile_labor(current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/labor/generate-schedule", methods=["POST"])
@mobile_login_required
def mobile_generate_schedule(current_user):
    """Reuses client_api.py's existing background-job machinery
    (_run_schedule_job / _schedule_jobs) rather than building a second job
    system — the same async-generate-then-poll pattern the web Labor tab
    already relies on."""
    import threading
    import uuid
    from ai_utils import ai_rate_limited

    rid = current_user["restaurant_id"]
    if ai_rate_limited(f"schedule:{rid}", max_calls=3, window_secs=60):
        return jsonify(ok=False, error="Too many schedule generations — please wait a moment and try again."), 429
    job_id = str(uuid.uuid4())
    _capi._schedule_jobs[job_id] = {"status": "pending", "result": None}
    t = threading.Thread(target=_capi._run_schedule_job, args=(job_id, rid), daemon=True)
    t.start()
    return jsonify(ok=True, job_id=job_id)


@mobile_bp.route("/labor/schedule-status/<job_id>")
@mobile_login_required
def mobile_schedule_status(job_id, current_user):
    """Mirrors client_api.py's schedule_status() route body, reading the
    same shared _schedule_jobs dict — unlike the web route this one does
    require auth (mobile_login_required), a small deliberate hardening over
    the web version's job-id-is-unguessable-so-no-login-needed approach,
    since bearer auth costs nothing extra to check here."""
    job = _capi._schedule_jobs.get(job_id)
    if not job:
        return jsonify(ok=False, status="error", error="Job not found"), 404
    if job["status"] == "pending":
        return jsonify(ok=True, status="pending")
    try:
        result = dict(job["result"])
        result["status"] = job["status"]
        _capi._schedule_jobs.pop(job_id, None)
        return jsonify(**result)
    except Exception as e:
        return jsonify(ok=False, status="error", error=str(e)), 500


# ── Marketing ─────────────────────────────────────────────────────────────

def _do_mobile_marketing_stats(restaurant_id):
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now')))""")
        generated = conn.execute(
            "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (restaurant_id,)
        ).fetchone()[0] or 0
        published = conn.execute(
            "SELECT COUNT(DISTINCT topic) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL",
            (restaurant_id,)
        ).fetchone()[0] or 0
        this_month = conn.execute(
            "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')",
            (restaurant_id,)
        ).fetchone()[0] or 0
        conn.close()
        return {"generated": generated, "published": published, "this_month": this_month}
    except Exception:
        return {"generated": 0, "published": 0, "this_month": 0}


def _do_mobile_marketing(restaurant_id):
    from marketing import get_content_calendar_ideas
    stats = _do_mobile_marketing_stats(restaurant_id)
    try:
        calendar = get_content_calendar_ideas(restaurant_id=restaurant_id)
    except Exception:
        calendar = []
    return {"ok": True, "stats": stats, "calendar": calendar}, 200


@mobile_bp.route("/marketing")
@mobile_login_required
def mobile_marketing(current_user):
    payload, status = _do_mobile_marketing(current_user["restaurant_id"])
    return jsonify(**payload), status


def _do_mobile_generate_content(restaurant_id, content_type, topic):
    from marketing import generate_content
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"gencontent:{restaurant_id}", max_calls=8, window_secs=60):
        return {"ok": False, "error": "Too many requests — please wait a moment and try again."}, 429
    try:
        result = generate_content(content_type or "instagram_post", topic or "", restaurant_id=restaurant_id)
        return {"ok": True, "content": result}, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@mobile_bp.route("/marketing/generate-content", methods=["POST"])
@mobile_login_required
def mobile_generate_content(current_user):
    data = request.get_json() or {}
    payload, status = _do_mobile_generate_content(
        current_user["restaurant_id"], data.get("type"), data.get("topic")
    )
    return jsonify(**payload), status


# ── Intel ─────────────────────────────────────────────────────────────────

def _do_mobile_intel(restaurant_id):
    """Read-only — refreshing competitor intel is a long-running, desktop-
    triggered operation (see ai-visibility/competitor refresh in
    client_api.py); mobile just reads whatever that last produced."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404
    if not getattr(restaurant, "competitor_intel", None):
        return {"ok": True, "has_data": False, "intro": None, "recommendations": [], "sections": []}, 200
    try:
        from competitor_intel_format import parse_competitor_intel
        parsed = parse_competitor_intel(restaurant.competitor_intel)
        return {
            "ok": True,
            "has_data": True,
            "intro": parsed.get("intro"),
            "recommendations": parsed.get("recommendations", []),
            "sections": [{"name": name, "bullets": bullets} for name, bullets in parsed.get("sections", [])],
        }, 200
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


@mobile_bp.route("/intel")
@mobile_login_required
def mobile_intel(current_user):
    payload, status = _do_mobile_intel(current_user["restaurant_id"])
    return jsonify(**payload), status
