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
import os
import random
from datetime import datetime, timedelta

from flask import Blueprint, request, jsonify

from auth import (
    verify_password, create_session, delete_session,
    revoke_other_sessions, mobile_login_required, get_user_by_restaurant_id,
    update_last_login,
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


_APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
_apple_jwk_client = None


def _verify_apple_identity_token(identity_token, bundle_id):
    """Verifies the JWT ASAuthorizationController hands the app against
    Apple's own public signing keys — no shared secret involved, unlike
    Google's OAuth code exchange. Raises on any failure (bad signature,
    wrong audience/issuer, expired); callers turn that into a 401."""
    import jwt as _pyjwt
    global _apple_jwk_client
    if _apple_jwk_client is None:
        _apple_jwk_client = _pyjwt.PyJWKClient(_APPLE_JWKS_URL)
    signing_key = _apple_jwk_client.get_signing_key_from_jwt(identity_token)
    return _pyjwt.decode(
        identity_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=bundle_id,
        issuer="https://appleid.apple.com",
    )


@mobile_bp.route("/apple-signin", methods=["POST"])
def mobile_apple_signin():
    """Native Sign In with Apple — unlike Google's web-redirect OAuth flow,
    ASAuthorizationController hands the app a signed identity token directly
    on-device, so there's no callback/deep-link involved: the app just POSTs
    the token here. Matches by apple_user_id (Apple's stable per-app user
    identifier) first, falling back to email — Apple only includes the
    user's email on their very first-ever Sign In with Apple for this app,
    so apple_user_id is the only reliable match on every login after that."""
    data = request.get_json() or {}
    identity_token = data.get("identity_token") or ""
    if not identity_token:
        return jsonify(ok=False, error="Missing Apple identity token"), 400

    bundle_id = os.getenv("APNS_BUNDLE_ID", "ai.cavnar.CavnarAI")
    try:
        payload = _verify_apple_identity_token(identity_token, bundle_id)
    except Exception:
        return jsonify(ok=False, error="Couldn't verify Sign in with Apple. Try again."), 401

    apple_user_id = payload.get("sub", "")
    email = (payload.get("email") or "").lower().strip()
    if not apple_user_id:
        return jsonify(ok=False, error="Apple didn't return a valid identity."), 401

    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE apple_user_id=? AND is_active=1 LIMIT 1", (apple_user_id,)
    ).fetchone()
    if not row and email:
        row = conn.execute(
            "SELECT * FROM users WHERE LOWER(email)=? AND is_active=1 LIMIT 1", (email,)
        ).fetchone()
    if row and not row["apple_user_id"]:
        conn.execute("UPDATE users SET apple_user_id=? WHERE id=?", (apple_user_id, row["id"]))
        conn.commit()
    conn.close()

    if not row:
        return jsonify(ok=False, error="No account found for that Apple ID. Contact will@cavnar.ai."), 401

    user = dict(row)
    ip = _get_client_ip()
    ua = request.headers.get("User-Agent", "Cavnar-iOS")
    token = create_session(user["id"], ip_address=ip, user_agent=ua, device_type="ios")
    update_last_login(user["id"])
    _send_login_notification(user, ip, ua)
    return jsonify(ok=True, token=token, user=_public_user(user))


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


@mobile_bp.route("/me")
@mobile_login_required
def mobile_me(current_user):
    """Resolves a bearer token to its User — used after Google Sign-In,
    where the token arrives via a cavnarai:// deep link rather than the
    /login response body, so the app has no `user` object yet to complete
    the session with."""
    return jsonify(ok=True, user=_public_user(current_user))


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

    # Total value delivered — the Home tab's chart card. Snapshot recorded
    # opportunistically right here (upsert-on-conflict, so a second load
    # the same day is a no-op) rather than via a separate scheduled job —
    # see value_delivered.py.
    from value_delivered import compute_total_value_delivered, record_value_snapshot, get_value_history
    total_value = compute_total_value_delivered(rid)
    try:
        record_value_snapshot(rid, total_value)
    except Exception:
        pass  # the chart just has one fewer data point — never worth failing Home over
    value_history = get_value_history(rid, days=365)

    return {
        "ok": True,
        "username": current_user.get("username"),
        "restaurant_name": restaurant.name,
        "location_name": restaurant.location_name or None,
        "brand_color": restaurant.brand_color or None,
        "reviews_awaiting_approval": rstats.get("awaiting_approval", 0),
        "modules": modules_out,
        "needs_attention": needs_attention,
        "total_value_delivered": total_value,
        "value_history": value_history,
    }, 200


@mobile_bp.route("/home")
@mobile_login_required
def mobile_home(current_user):
    payload, status = _do_mobile_home(current_user)
    return jsonify(**payload), status


# ── Reviews ───────────────────────────────────────────────────────────────

def _do_mobile_reviews(restaurant_id, filter_by="all", search="", category=None, platform=None):
    from models import get_reviews_data
    reviews = get_reviews_data(restaurant_id, filter_by, search, category=category, platform=platform)
    return {"ok": True, "reviews": reviews}, 200


@mobile_bp.route("/reviews")
@mobile_login_required
def mobile_reviews(current_user):
    payload, status = _do_mobile_reviews(
        current_user["restaurant_id"],
        request.args.get("filter", "all"),
        request.args.get("search", ""),
        request.args.get("category") or None,
        request.args.get("platform") or None,
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


@mobile_bp.route("/reviews/<int:review_id>/undo", methods=["POST"])
@mobile_login_required
def mobile_undo_review(review_id, current_user):
    payload, status = _capi._do_undo(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/reviews/<int:review_id>/retract", methods=["POST"])
@mobile_login_required
def mobile_retract_review(review_id, current_user):
    payload, status = _capi._do_retract(review_id, current_user["restaurant_id"])
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


@mobile_bp.route("/reviews/<int:review_id>/delete", methods=["POST"])
@mobile_login_required
def mobile_delete_review(review_id, current_user):
    payload, status = _capi._do_delete_review(review_id, current_user["restaurant_id"])
    return jsonify(**payload), status


# ── Reviews Analytics ────────────────────────────────────────────────────
# Mirrors the web Reviews tab's Analytics sub-tab (dashboard.html's
# rv-panel-analytics): response performance, rate-vs-benchmark (computed
# client-side from review-stats, already available), topic sentiment
# heatmap, 8-week sentiment trend, and the AI one-line insight.

@mobile_bp.route("/reviews/response-performance")
@mobile_login_required
def mobile_response_performance(current_user):
    from models import get_response_performance
    days = int(request.args.get("days", 90))
    if days not in (30, 60, 90, 180):
        days = 90
    try:
        data = get_response_performance(current_user["restaurant_id"], days=days)
        return jsonify(ok=True, data=data)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@mobile_bp.route("/reviews/platform-breakdown")
@mobile_login_required
def mobile_platform_breakdown(current_user):
    from models import get_platform_breakdown
    try:
        data = get_platform_breakdown(current_user["restaurant_id"])
        return jsonify(ok=True, data=data)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@mobile_bp.route("/reviews/topic-heatmap")
@mobile_login_required
def mobile_topic_heatmap(current_user):
    from models import get_topic_heatmap
    days = int(request.args.get("days", 90))
    if days not in (30, 60, 90, 180):
        days = 90
    try:
        data = get_topic_heatmap(current_user["restaurant_id"], days=days)
        return jsonify(ok=True, data=data)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@mobile_bp.route("/reviews/sentiment-trend")
@mobile_login_required
def mobile_sentiment_trend(current_user):
    from models import get_sentiment_trend
    try:
        data = get_sentiment_trend(current_user["restaurant_id"], weeks=8)
        return jsonify(ok=True, weeks=data)
    except Exception as e:
        return jsonify(ok=False, weeks=[], error=str(e)), 500


@mobile_bp.route("/reviews/insight")
@mobile_login_required
def mobile_review_insight(current_user):
    payload, status = _capi._do_review_insight(current_user["restaurant_id"])
    return jsonify(ok=True, **payload), status


# ── Response templates ───────────────────────────────────────────────────

@mobile_bp.route("/templates")
@mobile_login_required
def mobile_list_templates(current_user):
    from models import get_response_templates
    return jsonify(ok=True, templates=get_response_templates(current_user["restaurant_id"]))


@mobile_bp.route("/templates", methods=["POST"])
@mobile_login_required
def mobile_create_template(current_user):
    from models import create_response_template
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    body = (data.get("body") or "").strip()
    if not title or not body:
        return jsonify(ok=False, error="Title and body required"), 400
    if len(title) > 120:
        return jsonify(ok=False, error="Title too long (120 chars max)"), 400
    category = data.get("category", "general")
    if category not in ("general", "positive", "negative", "neutral"):
        category = "general"
    tid = create_response_template(current_user["restaurant_id"], title, body, category)
    return jsonify(ok=True, id=tid)


@mobile_bp.route("/templates/<int:tid>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_template(tid, current_user):
    from models import delete_response_template
    delete_response_template(tid, current_user["restaurant_id"])
    return jsonify(ok=True)


@mobile_bp.route("/templates/<int:tid>/use", methods=["POST"])
@mobile_login_required
def mobile_use_template(tid, current_user):
    from models import increment_template_use
    increment_template_use(tid, restaurant_id=current_user["restaurant_id"])
    return jsonify(ok=True)


# ── Send review request ──────────────────────────────────────────────────

@mobile_bp.route("/send-review-request", methods=["POST"])
@mobile_login_required
def mobile_send_review_request(current_user):
    payload, status = _capi._do_send_review_request(current_user["restaurant_id"], request.get_json() or {})
    return jsonify(**payload), status


@mobile_bp.route("/review-request-stats")
@mobile_login_required
def mobile_review_request_stats(current_user):
    from models import get_review_request_stats
    return jsonify(ok=True, **get_review_request_stats(current_user["restaurant_id"]))


# ── Notifications ─────────────────────────────────────────────────────────

@mobile_bp.route("/notifications")
@mobile_login_required
def mobile_notifications(current_user):
    import datetime as _dt
    payload, status = _capi._do_get_notifications(current_user["restaurant_id"])
    # Mark as seen — same stamp-on-read behavior as Changelog (see
    # mobile_changelog below), so the bell's unread badge clears once the
    # client has actually opened the list, not before.
    update_restaurant(current_user["restaurant_id"], {
        "notifications_seen_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    })
    return jsonify(**payload), status


@mobile_bp.route("/notifications/unread-count")
@mobile_login_required
def mobile_notifications_unread_count(current_user):
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    since = restaurant.notifications_seen_at if restaurant else None
    conn = get_conn()
    if since:
        count = conn.execute(
            "SELECT COUNT(*) FROM alert_log WHERE restaurant_id=? AND fired_at > ?",
            (rid, since)
        ).fetchone()[0]
    else:
        count = conn.execute(
            "SELECT COUNT(*) FROM alert_log WHERE restaurant_id=?", (rid,)
        ).fetchone()[0]
    conn.close()
    return jsonify(ok=True, count=count)


# ── Changelog ─────────────────────────────────────────────────────────────

@mobile_bp.route("/changelog")
@mobile_login_required
def mobile_changelog(current_user):
    from models import get_changelog
    import datetime as _dt
    entries = get_changelog()
    # Mark as seen — same stamp-on-read behavior as the web route, so the
    # unread badge clears once the owner has actually opened the list.
    update_restaurant(current_user["restaurant_id"], {
        "changelog_seen_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    })
    return jsonify(ok=True, entries=entries)


@mobile_bp.route("/changelog/unread-count")
@mobile_login_required
def mobile_changelog_unread_count(current_user):
    from models import get_changelog
    restaurant = get_restaurant(current_user["restaurant_id"])
    since = restaurant.changelog_seen_at if restaurant else None
    unread = get_changelog(since=since) if since else get_changelog()
    return jsonify(ok=True, count=len(unread))


# ── Ask Cavnar ────────────────────────────────────────────────────────────

@mobile_bp.route("/ask-cavnar", methods=["POST"])
@mobile_login_required
def mobile_ask_cavnar(current_user):
    data = request.get_json() or {}
    payload, status = _capi._do_ask_cavnar(
        current_user["restaurant_id"], data.get("question"), history=data.get("history")
    )
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


@mobile_bp.route("/food-cost/analytics")
@mobile_login_required
def mobile_food_cost_analytics(current_user):
    """Mirrors the web Food Cost tab's Analytics sub-tab: AI insight plus the
    same waste_items/overstock breakdowns dashboard.html bakes into its donut
    charts at render time — both already computed by analyse_inventory(),
    just not previously exposed as JSON."""
    from inventory import load_inventory_for_restaurant, analyse_inventory, get_claude_insights
    rid = current_user["restaurant_id"]
    try:
        restaurant = get_restaurant(rid)
        items, _is_live = load_inventory_for_restaurant(rid)
        analysis = analyse_inventory(items)
        cached = _capi._cache_get("mobile-inv-insight:" + str(rid))
        if cached:
            insight = cached
        else:
            insight = get_claude_insights(
                analysis, owner_name=restaurant.owner_name if restaurant else None,
                restaurant_name=restaurant.name if restaurant else None,
                restaurant_id=rid, items=items,
            )
            _capi._cache_set("mobile-inv-insight:" + str(rid), insight)
        return jsonify(
            ok=True,
            insight=insight,
            **_insight_json(insight),
            waste_items=analysis.get("waste_items", []),
            overstock=analysis.get("overstock", []),
            recoverable_monthly=analysis.get("recoverable_monthly", 0),
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e), insight="Analysis unavailable — check back shortly.",
                       insight_intro="Analysis unavailable — check back shortly.",
                       insight_recommendations=[], insight_forecast=None,
                       waste_items=[], overstock=[]), 500


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

def _staff_constraints_index(restaurant_id):
    """Fuzzy name → constraint-note lookup (full name, first name, first+
    last-initial, with/without trailing period) — same indexing
    hosted_dashboard.py's web Labor tab builds, so "OT allowed" detection
    matches exactly between web and mobile regardless of how a name is
    spelled in the shifts CSV vs. the staff note."""
    from models import get_staff_notes
    index = {}
    try:
        for n in (get_staff_notes(restaurant_id) or []):
            name = (n.get("employee_name") or "").lower().strip().rstrip(".")
            if not name:
                continue
            index[name] = n.get("notes")
            parts = name.split()
            if parts:
                index[parts[0]] = n.get("notes")
            if len(parts) >= 2:
                index[parts[0] + " " + parts[1].rstrip(".")] = n.get("notes")
                index[parts[0] + " " + parts[1].rstrip(".") + "."] = n.get("notes")
    except Exception:
        pass
    return index


def _has_ot_allowance(employee, constraints_index):
    key = (employee or "").lower().strip().rstrip(".")
    note = constraints_index.get(key) or constraints_index.get(key.split(" ")[0], "")
    note_l = (note or "").lower()
    return bool(note) and (
        "overtime" in note_l or "extra hours" in note_l
        or " ot " in (" " + note_l + " ") or note_l.startswith("ot ") or note_l.endswith(" ot")
    )


def _do_mobile_labor(restaurant_id):
    from labor import analyse_shifts_for_restaurant

    restaurant = get_restaurant(restaurant_id)
    target = float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0
    hourly_rate = float(restaurant.hourly_rate or 26.0) if restaurant else 26.0
    try:
        analysis = analyse_shifts_for_restaurant(restaurant_id)
    except Exception:
        analysis = {}

    overall_pct = analysis.get("overall_labor_pct", 0)

    constraints_index = _staff_constraints_index(restaurant_id)
    employee_hours = {
        emp: round(d.get("actual", 0), 1)
        for emp, d in (analysis.get("employee_hours") or {}).items()
    }
    overtime_risk = [
        {
            "employee": o.get("employee"), "hours": o.get("hours"),
            "week": o.get("week"), "status": o.get("status"),
            "total_hours": employee_hours.get(o.get("employee"), o.get("hours", 0)),
            "ot_allowed": _has_ot_allowance(o.get("employee"), constraints_index),
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

    # Overtime premium — 0.5x blended rate on hours over 40/week, same
    # formula hosted_dashboard.py's web dashboard uses for its "overtime
    # premium" savings tile.
    ot_premium = 0.0
    for o in analysis.get("overtime_risk", []):
        if o.get("status") == "overtime":
            ot_premium += max(0, o.get("hours", 0) - 40) * hourly_rate * 0.5

    date_range = analysis.get("date_range") or {}
    period_days = 14
    try:
        if date_range.get("start") and date_range.get("end"):
            _start = datetime.strptime(date_range["start"], "%Y-%m-%d")
            _end = datetime.strptime(date_range["end"], "%Y-%m-%d")
            period_days = max((_end - _start).days + 1, 1)
    except Exception:
        pass
    total_sales = analysis.get("total_sales", 0)
    monthly_sales_est = (total_sales / period_days * 30) if period_days else 0
    potential_savings = analysis.get("potential_savings", 0)
    labor_monthly = round(potential_savings * 4.33)
    # 0.345 = midpoint of the 33-36% full-service industry range (NRA 2024
    # Restaurant Operations Data Abstract) — was 0.32, a leftover from the
    # stale pre-pandemic 28-32% benchmark already corrected everywhere else
    # this figure appears (labor.py's AI prompt, the web dashboard, iOS's
    # own benchmark band).
    labor_vs_industry_monthly = max(0, round((0.345 - overall_pct / 100) * monthly_sales_est))

    savings_breakdown = {
        "labor_monthly": labor_monthly,
        "labor_annual": labor_monthly * 12,
        "labor_overtime": round(ot_premium),
        "labor_vs_industry_monthly": labor_vs_industry_monthly,
        "labor_vs_industry_annual": labor_vs_industry_monthly * 12,
    }

    # Upcoming holiday/event scheduling forecast — same 21-day window and
    # holiday-string parsing client_api.py's schedule builder and
    # hosted_dashboard.py's web Labor tab both already use.
    labor_upcoming = []
    try:
        import re
        from marketing import get_upcoming_holidays
        from time_utils import restaurant_now
        now = restaurant_now(restaurant, naive=True)
        hol_str = get_upcoming_holidays(now)
        if hol_str:
            for chunk in hol_str.split(", "):
                m = re.search(r'\((\w+ \d+)\)$', chunk)
                if not m:
                    continue
                try:
                    hdate = datetime.strptime(m.group(1) + " " + str(now.year), "%b %d %Y")
                    if hdate < now:
                        hdate = hdate.replace(year=now.year + 1)
                    days_away = (hdate - now).days
                    if 0 <= days_away <= 21:
                        labor_upcoming.append({
                            "name": chunk[:chunk.rfind("(")].strip(),
                            "date_str": hdate.strftime("%B %-d"),
                            "days_away": days_away,
                        })
                except Exception:
                    pass
    except Exception:
        pass

    return {
        "ok": True,
        "is_live": bool(analysis.get("is_live")),
        "overall_labor_pct": overall_pct,
        "target": target,
        "on_track": overall_pct <= target,
        "potential_savings": potential_savings,
        "overtime_risk": overtime_risk,
        "role_summary": role_summary,
        "date_range": date_range,
        "overstaffed_days": analysis.get("overstaffed_days") or [],
        "understaffed_days": analysis.get("understaffed_days") or [],
        "dow_summary": analysis.get("dow_summary") or {},
        "savings_breakdown": savings_breakdown,
        "labor_upcoming": labor_upcoming,
    }, 200


@mobile_bp.route("/labor")
@mobile_login_required
def mobile_labor(current_user):
    payload, status = _do_mobile_labor(current_user["restaurant_id"])
    return jsonify(**payload), status


@mobile_bp.route("/labor/trend")
@mobile_login_required
def mobile_labor_trend(current_user):
    """Mirrors client_api.py's labor-trend — same 8-week labor-% history the
    web Labor tab's Analytics sub-tab charts."""
    from models import get_labor_history
    from datetime import datetime as _dt
    try:
        history = get_labor_history(current_user["restaurant_id"], limit=8)
        weeks = []
        for h in history[::-1]:  # oldest first = left to right
            try:
                start = _dt.strptime(h["period_start"], "%Y-%m-%d")
                label = start.strftime("%-m/%-d")
            except Exception:
                label = h.get("period_start", "")[:5]
            weeks.append({
                "label": label,
                "pct": round(h["labor_pct"], 1),
                "labor": h["total_labor"],
                "sales": h["total_sales"],
                "start": h["period_start"],
                "end": h["period_end"],
            })
        return jsonify(ok=True, weeks=weeks)
    except Exception as e:
        return jsonify(ok=False, weeks=[], error=str(e)), 500


@mobile_bp.route("/labor/gap")
@mobile_login_required
def mobile_labor_gap(current_user):
    from labor import analyse_shifts_for_restaurant, calculate_monthly_gap
    try:
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        gap = calculate_monthly_gap(analysis)
        return jsonify(ok=True, **gap)
    except Exception as e:
        return jsonify(ok=False, error=str(e), over_target=False, monthly_gap=0,
                       current_pct=0, target_pct=30), 500


def _insight_json(insight_text):
    """Structured {intro, recommendations, forecast} fields for a raw AI
    insight string — the same parsing client_api.format_insight_html() uses
    to build the web's HTML, just handed back as JSON so the iOS app can
    render its own native equivalent instead of a plain text blob."""
    intro, recs, forecast = _capi.parse_insight_sections(insight_text)
    return {"insight_intro": intro, "insight_recommendations": recs, "insight_forecast": forecast}


@mobile_bp.route("/labor/insight")
@mobile_login_required
def mobile_labor_insight(current_user):
    """Same AI insight the web Labor tab shows, structured into
    intro/recommendations/forecast fields so the app can render the same
    numbered-circle layout the web dashboard uses."""
    from labor import analyse_shifts_for_restaurant, get_claude_insights
    from models import get_restaurant, get_staff_notes
    rid = current_user["restaurant_id"]
    cached = _capi._cache_get("mobile-labor-insight:" + str(rid))
    if cached:
        return jsonify(ok=True, insight=cached, **_insight_json(cached))
    try:
        restaurant = get_restaurant(rid)
        name = restaurant.name if restaurant else "your restaurant"
        owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
        analysis = analyse_shifts_for_restaurant(rid)
        staff_notes = get_staff_notes(rid)
        insight = get_claude_insights(
            analysis, restaurant_name=name, owner_name=owner, restaurant_id=rid,
            staff_notes=staff_notes if staff_notes else None,
        )
        _capi._cache_set("mobile-labor-insight:" + str(rid), insight)
        return jsonify(ok=True, insight=insight, **_insight_json(insight))
    except Exception as e:
        return jsonify(ok=False, insight="Analysis unavailable — check back shortly.",
                       insight_intro="Analysis unavailable — check back shortly.",
                       insight_recommendations=[], insight_forecast=None, error=str(e)), 500


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


@mobile_bp.route("/labor/schedule-history")
@mobile_login_required
def mobile_schedule_history(current_user):
    """Every schedule this restaurant has ever generated, newest first —
    a durable server-side record on the Account tab, independent of
    whatever the Labor tab's own client-side caching does."""
    from models import get_schedule_history
    try:
        history = get_schedule_history(current_user["restaurant_id"])
        return jsonify(ok=True, history=history)
    except Exception as e:
        return jsonify(ok=False, history=[], error=str(e)), 500


@mobile_bp.route("/labor/schedule-history/<int:history_id>")
@mobile_login_required
def mobile_schedule_history_detail(history_id, current_user):
    """Full record for one past generation, including the CSV parsed into
    preview_rows the same shape the Labor tab's own schedule result uses
    (so the iOS detail screen can reuse the same row-rendering component).
    Scoped to current_user's restaurant_id — get_schedule_history_detail()
    returns None for an id that belongs to a different tenant, same as a
    genuinely missing id, rather than confirming which ids exist."""
    from models import get_schedule_history_detail
    detail = get_schedule_history_detail(history_id, current_user["restaurant_id"])
    if not detail:
        return jsonify(ok=False, error="Not found"), 404

    _COLS = ["date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes"]
    preview_rows = []
    for _line in (detail.get("schedule_csv") or "").split("\n")[1:]:
        _line = _line.strip()
        if not _line:
            continue
        _parts = _line.split(",", 7)
        if len(_parts) < 6:
            continue
        preview_rows.append({_COLS[i]: _parts[i].strip() for i in range(min(len(_parts), 8))})

    return jsonify(ok=True, **detail, preview_rows=preview_rows)


@mobile_bp.route("/labor/schedule-history/<int:history_id>", methods=["DELETE"])
@mobile_login_required
def mobile_schedule_history_delete(history_id, current_user):
    """The only deletion path for schedule_history rows -- nothing in this
    codebase ever removes one automatically. Scoped to current_user's
    restaurant_id the same way the detail route is."""
    from models import delete_schedule_history
    deleted = delete_schedule_history(history_id, current_user["restaurant_id"])
    if not deleted:
        return jsonify(ok=False, error="Not found"), 404
    return jsonify(ok=True)


@mobile_bp.route("/labor/availability")
@mobile_login_required
def mobile_labor_availability(current_user):
    """Client-scoped counterpart to admin_routes.py's /admin/staff-
    availability/<id> — same models.py CRUD, gated by the restaurant's own
    mobile session instead of internal admin auth. Feeds the same AI
    scheduler input client_api.py's _build_schedule_result() already reads
    (staff_availability=...), so entries saved here are respected by the
    next "Generate schedule" run with no extra wiring."""
    from models import get_staff_availability, init_staff_availability
    import json as _json
    init_staff_availability()
    rows = get_staff_availability(current_user["restaurant_id"]) or []
    entries = [
        {
            "employee_name": r.get("employee_name"),
            "available_days": _json.loads(r.get("available_days") or "[]"),
            "unavailable_days": _json.loads(r.get("unavailable_days") or "[]") if r.get("unavailable_days") else [],
            "notes": r.get("notes"),
        }
        for r in rows
    ]
    return jsonify(ok=True, availability=entries)


@mobile_bp.route("/labor/availability", methods=["POST"])
@mobile_login_required
def mobile_labor_availability_save(current_user):
    from models import save_staff_availability, init_staff_availability
    data = request.get_json(silent=True) or {}
    name = (data.get("employee_name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Employee name is required."), 400
    init_staff_availability()
    save_staff_availability(
        current_user["restaurant_id"], name,
        available_days=data.get("available_days") or [],
        unavailable_days=data.get("unavailable_days") or [],
        notes=(data.get("notes") or "").strip() or None,
    )
    return jsonify(ok=True)


@mobile_bp.route("/labor/availability/delete", methods=["POST"])
@mobile_login_required
def mobile_labor_availability_delete(current_user):
    from models import delete_staff_availability
    data = request.get_json(silent=True) or {}
    name = (data.get("employee_name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Employee name is required."), 400
    delete_staff_availability(current_user["restaurant_id"], name)
    return jsonify(ok=True)


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


# ── Guest Text Club ───────────────────────────────────────────────────────
# Mirrors the web Marketing tab's Guest Text Club section (guest contacts,
# SMS campaign draft/send, join link). Gated on the same marketing-module
# check client_api.py's own routes use, via the same helpers.

@mobile_bp.route("/guest-contacts")
@mobile_login_required
def mobile_guest_contacts(current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import get_guest_contacts
    return jsonify(ok=True, contacts=get_guest_contacts(rid))


@mobile_bp.route("/guest-contacts", methods=["POST"])
@mobile_login_required
def mobile_add_guest_contact(current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import add_guest_contact_manual
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not name:
        return jsonify(ok=False, error="Name required"), 400
    if not phone:
        return jsonify(ok=False, error="Phone number required"), 400
    contact_id = add_guest_contact_manual(rid, phone, name=name)
    return jsonify(ok=True, id=contact_id)


@mobile_bp.route("/guest-contacts/<int:contact_id>", methods=["DELETE"])
@mobile_login_required
def mobile_delete_guest_contact(contact_id, current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import delete_guest_contact
    delete_guest_contact(contact_id, rid)
    return jsonify(ok=True)


@mobile_bp.route("/guest-contacts/<int:contact_id>/mark-visit", methods=["POST"])
@mobile_login_required
def mobile_mark_guest_visit(contact_id, current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from guest_marketing import mark_guest_visit
    mark_guest_visit(contact_id, rid)
    return jsonify(ok=True)


@mobile_bp.route("/guest-campaign/draft", methods=["POST"])
@mobile_login_required
def mobile_guest_campaign_draft(current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from ai_utils import ai_rate_limited
    if ai_rate_limited(f"guestcampaign:{rid}", max_calls=8, window_secs=60):
        return jsonify(ok=False, error="Too many requests — please wait a moment and try again."), 429
    data = request.get_json() or {}
    try:
        from guest_marketing import draft_campaign_message
        restaurant = get_restaurant(rid)
        message = draft_campaign_message(restaurant, campaign_type=data.get("type", "general"), topic=data.get("topic", ""))
        return jsonify(ok=True, message=message)
    except Exception as e:
        return jsonify(ok=False, error="Couldn't draft a message right now — try again in a moment."), 500


@mobile_bp.route("/guest-campaign/send", methods=["POST"])
@mobile_login_required
def mobile_guest_campaign_send(current_user):
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    from ai_utils import ai_rate_limited
    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify(ok=False, error="Message required"), 400
    if ai_rate_limited(f"guestcampaignsend:{rid}", max_calls=3, window_secs=300):
        return jsonify(ok=False, error="Too many campaigns sent recently — please wait a few minutes."), 429
    try:
        from guest_marketing import send_campaign
        result = send_campaign(rid, message)
        return jsonify(ok=True, **result)
    except Exception as e:
        return jsonify(ok=False, error="Couldn't send the campaign — try again in a moment."), 500


@mobile_bp.route("/marketing/performance")
@mobile_login_required
def mobile_marketing_performance(current_user):
    """Mirrors client_api.py's mkt-performance — summarizes real Meta post
    metrics already stored by refresh_post_metrics(), never calls Meta
    itself."""
    rid = current_user["restaurant_id"]
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now')))""")
        for col in ("reach", "impressions", "engaged", "likes", "comments", "shares"):
            try:
                conn.execute(f"ALTER TABLE marketing_content_log ADD COLUMN {col} INTEGER DEFAULT 0")
            except Exception:
                pass
        conn.commit()

        published = conn.execute(
            "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL",
            (rid,)
        ).fetchone()[0] or 0

        totals = conn.execute("""
            SELECT COALESCE(SUM(reach),0) as reach, COALESCE(SUM(impressions),0) as impressions,
                   COALESCE(SUM(likes),0) as likes, COALESCE(SUM(comments),0) as comments,
                   COALESCE(SUM(shares),0) as shares
            FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL
        """, (rid,)).fetchone()

        rows = conn.execute("""
            SELECT topic, post_platform, reach, impressions, likes, comments, shares
            FROM marketing_content_log
            WHERE restaurant_id=? AND post_id IS NOT NULL
              AND (reach > 0 OR impressions > 0 OR likes > 0 OR comments > 0)
        """, (rid,)).fetchall()
        conn.close()

        top_post = None
        if rows:
            best = max(rows, key=lambda r: (r["reach"] or 0) + (r["impressions"] or 0))
            top_post = {
                "topic": best["topic"], "platform": best["post_platform"],
                "reach": best["reach"] or 0, "likes": best["likes"] or 0,
                "comments": best["comments"] or 0, "shares": best["shares"] or 0,
            }

        total_engagement = (totals["likes"] or 0) + (totals["comments"] or 0) + (totals["shares"] or 0)
        return jsonify(
            ok=True,
            published=published,
            has_data=bool(rows),
            total_reach=(totals["reach"] or 0) + (totals["impressions"] or 0),
            total_engagement=total_engagement,
            top_post=top_post,
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500


@mobile_bp.route("/marketing/insight")
@mobile_login_required
def mobile_marketing_insight(current_user):
    payload, status = _capi._do_mkt_insight(current_user["restaurant_id"], raw=True)
    extra = _insight_json(payload.get("insight", "")) if payload.get("insight") else {}
    return jsonify(ok=True, **payload, **extra), status


@mobile_bp.route("/marketing/post-to-instagram", methods=["POST"])
@mobile_login_required
def mobile_post_to_instagram(current_user):
    from social_routes import _do_post_to_instagram
    data = request.get_json() or {}
    payload, status = _do_post_to_instagram(
        current_user["restaurant_id"], data.get("caption", ""), data.get("image_url", ""), data.get("topic", "")
    )
    return jsonify(**payload), status


@mobile_bp.route("/marketing/post-to-facebook", methods=["POST"])
@mobile_login_required
def mobile_post_to_facebook(current_user):
    from social_routes import _do_post_to_facebook
    data = request.get_json() or {}
    payload, status = _do_post_to_facebook(
        current_user["restaurant_id"], data.get("caption", ""), data.get("topic", "")
    )
    return jsonify(**payload), status


@mobile_bp.route("/guest-join-link")
@mobile_login_required
def mobile_guest_join_link(current_user):
    """The web's /api/guest-qr renders a downloadable PNG — mobile just
    returns the join URL and lets the app render its own QR code (CoreImage
    has a QR filter built in) rather than round-tripping an image."""
    rid = current_user["restaurant_id"]
    if not _capi._restaurant_has_marketing_module(rid):
        return jsonify(ok=False, error=_capi._NO_MARKETING_MODULE_ERROR), 403
    join_url = request.url_root.rstrip("/") + f"/join/{rid}"
    return jsonify(ok=True, join_url=join_url)


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


@mobile_bp.route("/intel/ai-visibility")
@mobile_login_required
def mobile_ai_visibility(current_user):
    """Mirrors the web Intel tab's AI Visibility sub-tab: how often the
    restaurant appears in real Perplexity answers to "where should I eat"
    style queries, plus a 10-item GBP/profile completeness checklist. Shares
    the same 3-call/60s rate limit as the web route since each call fires
    real, billable Perplexity queries."""
    payload, status = _capi._do_ai_visibility(current_user["restaurant_id"])
    return jsonify(**payload), status


# ── Account / Settings ──────────────────────────────────────────────────────
# The web dashboard's Account tab in one place: profile, security, POS/social
# connection status, alert contacts, billing. Connecting a POS/social account
# is an OAuth redirect flow that belongs on desktop (see dashboard.html's
# gmbConnect/igConnect/openToastClientModal etc.) — mobile only reads
# connection status here; the "connect" action, if ever added, would open
# the same web OAuth URL in a system browser rather than reimplement OAuth.

def _session_label(session):
    if session.get("device_type") == "ios":
        return "iPhone (Cavnar AI app)"
    ua = session.get("user_agent") or ""
    if "iPhone" in ua:
        return "iPhone (browser)"
    if "iPad" in ua:
        return "iPad (browser)"
    if "Android" in ua:
        return "Android"
    if "Macintosh" in ua:
        return "Mac"
    if "Windows" in ua:
        return "Windows"
    return "Web browser"


def _do_mobile_account(current_user):
    from notify import get_alert_contacts
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}, 404

    profile = {
        "restaurant_name": restaurant.name,
        "location_name": restaurant.location_name or None,
        "owner_name": restaurant.owner_name or None,
        "owner_email": restaurant.owner_email or None,
        "owner_phone": restaurant.owner_phone or None,
        "neighborhood": restaurant.neighborhood or None,
        "vibe": restaurant.vibe or None,
        "known_for": restaurant.known_for or None,
        "voice_notes": restaurant.voice_notes or None,
        "never_say": restaurant.never_say or None,
        "menu_notes": restaurant.menu_notes or None,
    }
    account = {
        "username": current_user["username"],
        "email": current_user["email"],
        "two_fa_enabled": bool(restaurant.two_fa_enabled),
        "login_notify": bool(getattr(restaurant, "login_notify", 0)),
    }
    connections = {
        "google_business": {
            "connected": bool(getattr(restaurant, "gmb_refresh_token", None)),
        },
        "instagram": {
            "connected": bool(getattr(restaurant, "ig_token", None)),
        },
        "toast": {
            "connected": bool(getattr(restaurant, "toast_restaurant_guid", None)),
            "last_synced": getattr(restaurant, "toast_last_synced", None),
        },
        "square": {
            "connected": bool(getattr(restaurant, "square_location_id", None)),
            "last_synced": getattr(restaurant, "square_last_synced", None),
        },
        "clover": {
            "connected": bool(getattr(restaurant, "clover_merchant_id", None)),
            "last_synced": getattr(restaurant, "clover_last_synced", None),
        },
    }
    alerts = {
        "contacts": get_alert_contacts(rid),
        "settings": {
            "alert_1star": bool(getattr(restaurant, "alert_1star", 0)),
            "alert_2star": bool(getattr(restaurant, "alert_2star", 0)),
            "alert_health": bool(getattr(restaurant, "alert_health", 0)),
            "alert_neg_spike": bool(getattr(restaurant, "alert_neg_spike", 0)),
            "alert_negative_trend": bool(getattr(restaurant, "alert_negative_trend", 0)),
            "alert_no_response": bool(getattr(restaurant, "alert_no_response", 0)),
            "alert_5star": bool(getattr(restaurant, "alert_5star", 0)),
            "alert_labor_over": bool(getattr(restaurant, "alert_labor_over", 0)),
            "urgent_via_sms": bool(getattr(restaurant, "urgent_via_sms", 0)),
            "urgent_via_email": bool(getattr(restaurant, "urgent_via_email", 0)),
            "digest_enabled": bool(getattr(restaurant, "digest_enabled", 1)),
            "digest_day": getattr(restaurant, "digest_day", "monday"),
        },
    }
    return {
        "ok": True,
        "profile": profile,
        "account": account,
        "connections": connections,
        "alerts": alerts,
    }, 200


@mobile_bp.route("/account")
@mobile_login_required
def mobile_account(current_user):
    payload, status = _do_mobile_account(current_user)
    return jsonify(**payload), status


@mobile_bp.route("/account/sessions")
@mobile_login_required
def mobile_account_sessions(current_user):
    from auth import get_sessions_for_user
    sessions = get_sessions_for_user(current_user["id"], current_token=_bearer_token())
    for s in sessions:
        s["label"] = _session_label(s)
    return jsonify(ok=True, sessions=sessions)


@mobile_bp.route("/account/change-password", methods=["POST"])
@mobile_login_required
def mobile_change_password(current_user):
    from auth import update_password
    data = request.get_json() or {}
    user = verify_password(current_user["username"], data.get("current", ""))
    if not user:
        return jsonify(ok=False, error="Current password is incorrect"), 400
    new_pw = data.get("new_password", "")
    if len(new_pw) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters"), 400
    update_password(current_user["id"], new_pw)
    return jsonify(ok=True)


@mobile_bp.route("/account/2fa/send-test", methods=["POST"])
@mobile_login_required
def mobile_send_2fa_test(current_user):
    import random as _random
    rid = current_user["restaurant_id"]
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    email = restaurant.owner_email or ""
    if not email or "@" not in email:
        return jsonify(ok=False, error="No email address found. Contact will@cavnar.ai to update your account email."), 400
    code = str(_random.randint(100000, 999999))
    expires = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    update_restaurant(rid, {"two_fa_code": code, "two_fa_expires": expires})
    try:
        from emails import send_2fa_code
        send_2fa_code(email, restaurant.name or "your restaurant", code, restaurant.owner_name)
    except Exception as e:
        return jsonify(ok=False, error=f"Failed to send email: {str(e)[:60]}"), 500
    masked = email[:2] + "***@" + email.split("@")[-1]
    return jsonify(ok=True, masked=masked)


@mobile_bp.route("/account/2fa/verify", methods=["POST"])
@mobile_login_required
def mobile_verify_2fa_setup(current_user):
    rid = current_user["restaurant_id"]
    data = request.get_json() or {}
    code = (data.get("code") or "").strip()
    restaurant = get_restaurant(rid)
    if not restaurant:
        return jsonify(ok=False, error="Not found"), 404
    if restaurant.two_fa_code != code:
        return jsonify(ok=False, error="Incorrect code. Try again."), 400
    expired = True
    exp_str = (restaurant.two_fa_expires or "").strip()
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"]:
        try:
            expires = datetime.strptime(exp_str, fmt)
            expired = datetime.now() > expires
            break
        except Exception:
            continue
    if expired:
        return jsonify(ok=False, error="Code expired. Try again."), 400
    update_restaurant(rid, {"two_fa_enabled": 1, "two_fa_code": "", "two_fa_expires": ""})
    return jsonify(ok=True)


@mobile_bp.route("/account/2fa/disable", methods=["POST"])
@mobile_login_required
def mobile_disable_2fa(current_user):
    update_restaurant(current_user["restaurant_id"], {"two_fa_enabled": 0})
    return jsonify(ok=True)


@mobile_bp.route("/account/login-notify", methods=["POST"])
@mobile_login_required
def mobile_toggle_login_notify(current_user):
    data = request.get_json() or {}
    update_restaurant(current_user["restaurant_id"], {"login_notify": int(bool(data.get("enabled")))})
    return jsonify(ok=True)


@mobile_bp.route("/account/alert-settings", methods=["POST"])
@mobile_login_required
def mobile_save_alert_settings(current_user):
    from notify import get_alert_contacts, add_alert_contact, delete_alert_contact
    data = request.get_json() or {}
    rid = current_user["restaurant_id"]

    # SMS requires real, server-verified consent — same rule as the web
    # endpoint (client_api.save_alert_settings): the client's checkbox is a
    # UX nicety, not enforcement, since anyone can call this API directly.
    sms_requested = bool(data.get("urgent_via_sms"))
    sms_consented = bool(data.get("sms_consent"))
    sms_on = sms_requested and sms_consented

    new_contacts = (data.get("contacts") or [])[:2]
    existing = get_alert_contacts(rid)
    for ec in existing:
        delete_alert_contact(ec["id"])
    for nc in new_contacts:
        phone = _capi._normalize_phone(nc.get("phone") or "")
        name = (nc.get("name") or "").strip()
        if phone:
            add_alert_contact(rid, name, phone, sms_consent=sms_on)

    update_restaurant(rid, {
        "alert_1star": int(bool(data.get("alert_1star"))),
        "alert_2star": int(bool(data.get("alert_2star"))),
        "alert_health": int(bool(data.get("alert_health"))),
        "alert_neg_spike": int(bool(data.get("alert_neg_spike"))),
        "alert_negative_trend": int(bool(data.get("alert_negative_trend"))),
        "alert_no_response": int(bool(data.get("alert_no_response"))),
        "alert_5star": int(bool(data.get("alert_5star"))),
        "alert_labor_over": int(bool(data.get("alert_labor_over"))),
        "urgent_via_sms": int(sms_on),
        "urgent_via_email": int(bool(data.get("urgent_via_email"))),
        "digest_enabled": int(bool(data.get("digest_enabled"))),
        "digest_day": data.get("digest_day", "monday"),
    })
    return jsonify(ok=True)


@mobile_bp.route("/account/digest-day", methods=["POST"])
@mobile_login_required
def mobile_update_digest_day(current_user):
    data = request.get_json() or {}
    day = (data.get("day") or "monday").lower()
    valid = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    if day not in valid:
        return jsonify(ok=False, error="Invalid day"), 400
    update_restaurant(current_user["restaurant_id"], {
        "digest_day": day,
        "digest_enabled": int(data.get("enabled", 1)),
    })
    return jsonify(ok=True)


def _do_mobile_billing(restaurant_id):
    import os as _os
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not getattr(restaurant, "stripe_customer_id", None):
        return {"ok": False, "reason": "no_customer"}, 200

    stripe_key = _os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        return {"ok": False, "reason": "no_key"}, 200

    try:
        import stripe as _stripe
        _stripe.api_key = stripe_key
        subs = _stripe.Subscription.list(customer=restaurant.stripe_customer_id, status="active", limit=5)
        if not subs.data:
            subs = _stripe.Subscription.list(customer=restaurant.stripe_customer_id, status="trialing", limit=5)
        if not subs.data:
            return {"ok": True, "status": "inactive", "message": "No active subscription found"}, 200

        sub = subs.data[0]
        next_date = datetime.fromtimestamp(sub.current_period_end).strftime("%-m/%-d/%Y")
        amount = sum(i.price.unit_amount for i in sub["items"].data) / 100

        pm_desc = "Card on file"
        try:
            customer = _stripe.Customer.retrieve(
                restaurant.stripe_customer_id,
                expand=["invoice_settings.default_payment_method"],
            )
            pm = customer.invoice_settings.default_payment_method
            if pm and pm.card:
                pm_desc = f"{pm.card.brand.title()} ending {pm.card.last4}"
        except Exception:
            pass

        try:
            portal = _stripe.billing_portal.Session.create(
                customer=restaurant.stripe_customer_id,
                return_url="https://dashboard.cavnar.ai",
            )
            portal_url = portal.url
        except Exception:
            portal_url = None

        return {
            "ok": True,
            "status": sub.status,
            "next_date": next_date,
            "amount": f"${amount:,.0f}/mo",
            "payment_method": pm_desc,
            "portal_url": portal_url,
            "trial_end": datetime.fromtimestamp(sub.trial_end).strftime("%-m/%-d/%Y") if sub.trial_end else None,
        }, 200
    except Exception as e:
        return {"ok": False, "reason": "stripe_error", "error": str(e)}, 200


@mobile_bp.route("/account/billing")
@mobile_login_required
def mobile_billing(current_user):
    payload, status = _do_mobile_billing(current_user["restaurant_id"])
    return jsonify(**payload), status
