"""
hosted_dashboard.py — Cavnar AI hosted client dashboard
Multi-client, login-protected, Railway-deployable

Run locally:  python3 hosted_dashboard.py
Deploy:       Railway (connect GitHub repo, set env vars)
"""
import os, json
from datetime import datetime, timedelta
from functools import wraps
PORT = int(os.getenv("PORT", 5000))
from flask import (Flask, render_template, request,
                   jsonify, redirect, url_for, make_response, send_file, session)
from emails import send_payment_email, send_welcome_email
from models import (init_db, get_conn, approve_response,
                    get_reviews_since, get_restaurant,
                    get_review_stats, get_reviews_data, get_top_issues,
                    get_platform_breakdown, get_sentiment_trend)
from auth import (init_auth, verify_password, create_session,
                  get_session_user, delete_session, create_user,
                  list_users, update_password,
                  get_sessions_for_user, revoke_other_sessions,
                  get_user_by_restaurant_id)
from dotenv import load_dotenv
import pathlib
load_dotenv(pathlib.Path(__file__).parent / ".env")

# ── Sentry error monitoring ───────────────────────────────────────────────────
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
_SENTRY_DSN = os.getenv("SENTRY_DSN", "")
if _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.1,   # 10% of requests for performance tracing
        profiles_sample_rate=0.0, # off — not needed yet
        environment=os.getenv("RAILWAY_ENVIRONMENT", "production"),
        send_default_pii=False,   # never send PII to Sentry
    )

app = Flask(__name__)

def _check_duplicate_routes():
    """Crash loudly at startup if any URL rule is registered more than once."""
    from collections import Counter
    rules = [r.rule for r in app.url_map.iter_rules()]
    dupes = [r for r, n in Counter(rules).items() if n > 1]
    if dupes:
        raise RuntimeError(f"DUPLICATE ROUTES DETECTED — fix before deploying: {dupes}")
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB global upload limit

@app.template_filter("format_intel")
def format_intel_filter(text):
    """Parse structured competitor intel into formatted HTML matching labor/inventory style."""
    import re
    from markupsafe import Markup, escape as _esc
    if not text:
        return '<p style="color:var(--ink3);font-size:13px">Analysis unavailable.</p>'

    # Normalize: strip markdown, em-dashes to hyphens, ensure section headers on own lines
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'[–—]', '-', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING WELL):', '\nWHAT COMPETITORS ARE DOING WELL:\n', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING POORLY):', '\nWHAT COMPETITORS ARE DOING POORLY:\n', text)
    text = re.sub(r'(?i)Recommendations?:', '\nRecommendations:\n', text)

    html_parts = []
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    intro_lines = []
    section_lines = []
    in_section = False
    for line in lines:
        if re.match(r"^(WHAT COMPETITORS|Recommendations?:?)", line, re.I):
            in_section = True
        if in_section:
            section_lines.append(line)
        else:
            intro_lines.append(line)

    if intro_lines:
        html_parts.append('<p style="font-size:13px;color:#374151;line-height:1.7;margin-bottom:14px">' + str(_esc(" ".join(intro_lines))) + "</p>")

    current_section = None
    bullets = []
    rec_lines = []

    def flush_bullets(section_name, b_list):
        if not b_list:
            return ""
        is_good = "WELL" in section_name.upper()
        color = "#16a34a" if is_good else "#dc2626"
        icon = "✓" if is_good else "✗"
        out = '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:' + color + ';margin:14px 0 8px">' + section_name + "</div>"
        for b in b_list:
            out += (
                '<div style="display:flex;gap:8px;margin-bottom:6px;align-items:flex-start">'
                + '<span style="flex-shrink:0;color:' + color + ';font-weight:700;font-size:13px">' + icon + "</span>"
                + '<span style="font-size:13px;color:#374151;line-height:1.6">' + str(_esc(b)) + "</span></div>"
            )
        return out

    for line in section_lines:
        if re.match(r"WHAT COMPETITORS ARE DOING WELL", line, re.I):
            if current_section and bullets:
                html_parts.append(flush_bullets(current_section, bullets))
            current_section = "What competitors are doing well"
            bullets = []
        elif re.match(r"WHAT COMPETITORS ARE DOING POORLY", line, re.I):
            if current_section and bullets:
                html_parts.append(flush_bullets(current_section, bullets))
            current_section = "What competitors are doing poorly"
            bullets = []
        elif re.search(r"Recommendations?", line, re.I) and not line.startswith("-") and not re.match(r"^[0-9]", line):
            if current_section and bullets:
                html_parts.append(flush_bullets(current_section, bullets))
            current_section = "recommendations"
            bullets = []
        elif line.startswith("-") and current_section != "recommendations":
            b = re.sub(r'\*+', '', line.lstrip("- ")).strip()
            if b:
                bullets.append(b)
        elif re.match(r"^[0-9]+[.)]\s+", line):
            rec_lines.append(re.sub(r'\*+', '', re.sub(r"^[0-9]+[.)]\s+", "", line)).strip())
        elif current_section == "recommendations" and line and not re.search(r"Recommendations?", line, re.I):
            cleaned = re.sub(r'\*+', '', line).strip()
            if cleaned:
                rec_lines.append(cleaned)

    if current_section and current_section != "recommendations" and bullets:
        html_parts.append(flush_bullets(current_section, bullets))

    if rec_lines:
        html_parts.append('<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#c84b2f;margin:14px 0 8px">Recommendations</div>')
        for i, rec in enumerate(rec_lines, 1):
            html_parts.append(
                '<div style="display:flex;gap:10px;margin-bottom:8px;align-items:flex-start">'
                + '<span style="flex-shrink:0;width:20px;height:20px;border-radius:50%;background:#c84b2f;color:white;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center">' + str(i) + "</span>"
                + '<span style="line-height:1.6;color:#b7791f;font-weight:500">' + str(_esc(rec)) + "</span></div>"
            )

    if not html_parts:
        return '<p style="font-size:13px;color:#374151;line-height:1.7">' + str(_esc(text)) + "</p>"

    return Markup("".join(html_parts))


@app.template_filter("extract_recs")
def extract_recs_filter(text):
    """Parse recommendation lines from competitor insight. Returns list of strings."""
    import re
    if not text:
        return []
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'[–—]', '-', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING WELL):', '\nWHAT COMPETITORS ARE DOING WELL:\n', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING POORLY):', '\nWHAT COMPETITORS ARE DOING POORLY:\n', text)
    text = re.sub(r'(?i)Recommendations?:', '\nRecommendations:\n', text)
    recs = []
    in_recs = False
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^Recommendations?:\s*$", line, re.I):
            in_recs = True
            continue
        if in_recs:
            # Split any inline numbered items on this line before processing
            parts = re.split(r'(?<=\S)\s+(?=\d+\.\s+[A-Z])', line)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                part = re.sub(r'^[0-9]+[.)]\s+', '', part).strip()
                if part and not re.match(r'^(WHAT COMPETITORS|Recommendations?)', part, re.I):
                    recs.append(part)
    return recs[:3]


@app.template_filter("format_intel_body")
def format_intel_body_filter(text):
    """Same as format_intel but omits recommendations — only intro + well/poorly."""
    import re
    from markupsafe import Markup, escape as _esc
    if not text:
        return Markup('')
    text = re.sub(r'\*+', '', text)
    text = re.sub(r'[–—]', '-', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING WELL):', '\nWHAT COMPETITORS ARE DOING WELL:\n', text)
    text = re.sub(r'(?i)(WHAT COMPETITORS ARE DOING POORLY):', '\nWHAT COMPETITORS ARE DOING POORLY:\n', text)
    text = re.sub(r'(?i)Recommendations?:', '\nRecommendations:\n', text)
    html_parts = []
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    intro_lines = []
    section_lines = []
    in_section = False
    for line in lines:
        if re.match(r"^(WHAT COMPETITORS|Recommendations?:?)", line, re.I):
            in_section = True
        if in_section:
            section_lines.append(line)
        else:
            intro_lines.append(line)
    if intro_lines:
        html_parts.append('<p style="font-size:13px;color:#374151;line-height:1.7;margin-bottom:14px">' + str(_esc(" ".join(intro_lines))) + "</p>")
    current_section = None
    bullets = []

    def _flush(section_name, b_list):
        if not b_list:
            return ""
        is_good = "WELL" in section_name.upper()
        color = "#16a34a" if is_good else "#dc2626"
        icon = "✓" if is_good else "✗"
        out = '<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:' + color + ';margin:14px 0 8px">' + section_name + "</div>"
        for b in b_list:
            out += (
                '<div style="display:flex;gap:8px;margin-bottom:6px;align-items:flex-start">'
                + '<span style="flex-shrink:0;color:' + color + ';font-weight:700;font-size:13px">' + icon + "</span>"
                + '<span style="font-size:13px;color:#374151;line-height:1.6">' + str(_esc(b)) + "</span></div>"
            )
        return out

    for line in section_lines:
        if re.match(r"WHAT COMPETITORS ARE DOING WELL", line, re.I):
            if current_section and bullets:
                html_parts.append(_flush(current_section, bullets))
            current_section = "What competitors are doing well"
            bullets = []
        elif re.match(r"WHAT COMPETITORS ARE DOING POORLY", line, re.I):
            if current_section and bullets:
                html_parts.append(_flush(current_section, bullets))
            current_section = "What competitors are doing poorly"
            bullets = []
        elif re.search(r"Recommendations?", line, re.I) and not line.startswith("-") and not re.match(r"^[0-9]", line):
            if current_section and bullets:
                html_parts.append(_flush(current_section, bullets))
            bullets = []
            break
        elif line.startswith("-"):
            b = re.sub(r'\*+', '', line.lstrip("- ")).strip()
            if b:
                bullets.append(b)
    if current_section and bullets:
        html_parts.append(_flush(current_section, bullets))
    if not html_parts:
        return Markup('<p style="font-size:13px;color:#374151;line-height:1.7">' + str(_esc(text)) + "</p>")
    return Markup("".join(html_parts))


def inv_banner_gradient(annual_waste, annual_recoverable):
    """Compute a red-to-green CSS gradient based on waste severity and recovery opportunity.
    Industry benchmarks: <$5K excellent | $5-15K normal | $15-30K concerning | >$30K serious
    Recovery %: >60% deep green | 40-60% medium | 20-40% muted | <20% near neutral
    """
    # Red intensity 0.0-1.0
    if annual_waste < 5000:
        red_i = 0.15
    elif annual_waste < 15000:
        red_i = 0.15 + (annual_waste - 5000) / 10000 * 0.45
    elif annual_waste < 30000:
        red_i = 0.60 + (annual_waste - 15000) / 15000 * 0.30
    else:
        red_i = 0.90
    # Green intensity 0.0-1.0
    rec_pct = (annual_recoverable / annual_waste * 100) if annual_waste > 0 else 0
    if rec_pct > 60:
        grn_i = 1.0
    elif rec_pct > 40:
        grn_i = 0.65 + (rec_pct - 40) / 20 * 0.35
    elif rec_pct > 20:
        grn_i = 0.35 + (rec_pct - 20) / 20 * 0.30
    else:
        grn_i = 0.35
    # Red: #2a0a0a (mild) → #8b1a1a (serious) — wide visible range
    rh = f"#{int(42+red_i*(139-42)):02x}{int(10+red_i*(26-10)):02x}{int(10+red_i*(26-10)):02x}"
    # Green: #162b1e (muted) → #1a6640 (deep saturated)
    gh = f"#{int(22+grn_i*(26-22)):02x}{int(43+grn_i*(102-43)):02x}{int(30+grn_i*(64-30)):02x}"
    return f"linear-gradient(to right,{rh} 0%,{gh} 65%,{gh} 100%)"

@app.route("/health")
def health():
    """Health check for UptimeRobot and Railway. Verifies DB is reachable."""
    try:
        conn = get_conn()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return jsonify(status="ok", db="ok"), 200
    except Exception as e:
        return jsonify(status="error", db=str(e)), 500

@app.template_filter("format_num")
def format_num(v):
    try: return f"{float(v):,.0f}"
    except: return v

@app.template_filter('format_date')
def format_date_filter(d):
    """Format YYYY-MM-DD or ISO date string as M/D/YY (e.g. 6/8/26)."""
    if not d:
        return ''
    try:
        from datetime import datetime as _dt
        s = str(d)[:10]
        dt = _dt.strptime(s, '%Y-%m-%d')
        return f"{dt.month}/{dt.day}/{str(dt.year)[2:]}"
    except Exception:
        return str(d)[:10]

@app.after_request
def add_security_headers(response):
    """Add security headers to every response."""
    response.headers["X-Content-Type-Options"]    = "nosniff"
    response.headers["X-Frame-Options"]           = "DENY"
    response.headers["X-XSS-Protection"]          = "1; mode=block"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]        = "geolocation=(), microphone=(), camera=()"
    response.headers["Content-Security-Policy"]   = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://static.cloudflareinsights.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "font-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://api.anthropic.com; "
        "frame-ancestors 'none';"
    )
    return response

# Register blueprints
from admin_routes import admin_bp
from webhook_routes import webhook_bp
from social_routes import social_bp
from auth_routes import auth_bp
from client_api import client_bp
app.register_blueprint(admin_bp)
app.register_blueprint(webhook_bp)
app.register_blueprint(social_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(client_bp)
_secret_key = os.getenv("SECRET_KEY", "")
if not _secret_key:
    _secret_key = os.urandom(32).hex()
    print("WARNING: SECRET_KEY not set — sessions will invalidate on every restart. Set SECRET_KEY in Railway env vars.")
app.secret_key = _secret_key

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "will")

RESEND_API_KEY          = os.getenv("RESEND_API_KEY", "")
FROM_EMAIL              = os.getenv("FROM_EMAIL", "will@cavnar.ai")
STRIPE_WEBHOOK_SECRET   = os.getenv("STRIPE_WEBHOOK_SECRET", "")
WILL_EMAIL              = os.getenv("WILL_EMAIL", "will@cavnar.ai")

# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_current_user():
    token = request.cookies.get("session_token")
    return get_session_user(token) if token else None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs, current_user=user)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or not user["is_admin"]:
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs, current_user=user)
    return decorated

# ── Data helpers ──────────────────────────────────────────────────────────────


TIER_LABELS = {
    "trial":             "Trial",
    "starter_reviews":   "Starter Module — Review Intelligence",
    "starter_labor":     "Starter Module — Labor Optimizer",
    "starter_inventory": "Starter Module — Inventory Control",
    "starter_marketing": "Starter Module — Marketing Autopilot",
    "full":              "Full System",
}

TIER_PRICES = {
    "trial":             {"setup": None,     "retainer": None},
    "starter_reviews":   {"setup": "$500",   "retainer": "$300/mo"},
    "starter_labor":     {"setup": "$500",   "retainer": "$300/mo"},
    "starter_inventory": {"setup": "$500",   "retainer": "$300/mo"},
    "starter_marketing": {"setup": "$500",   "retainer": "$300/mo"},
    "full":              {"setup": "$2,000", "retainer": "$1,500/mo"},
}


def create_stripe_checkout(module_count: int, owner_email: str,
                            restaurant_name: str,
                            billing_period: str = "monthly"):
    """
    Dynamically create a Stripe checkout session for any module count.
    Returns the checkout URL or None on failure.
    Pricing:
      Monthly: $500/module setup (one-time) + $300/mo/module retainer (30-day trial).
      Annual:  $500/module setup (one-time) + $3,000/yr/module retainer (30-day trial).
    """
    import stripe as _stripe
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe_key:
        print("[STRIPE ERROR] STRIPE_SECRET_KEY not set in environment")
        return None
    if module_count == 0:
        return None

    _stripe.api_key = stripe_key
    setup_amount = module_count * 500 * 100   # in cents (same for both plans)
    # Annual = $3,000/module/yr (equivalent to $250/mo — 2 months free)
    # Monthly = $300/module/mo
    if billing_period == "annual":
        retainer_amount   = module_count * 3000 * 100  # annual in cents
        retainer_interval = "year"
        trial_days        = 30
    else:
        retainer_amount   = module_count * 300 * 100   # monthly in cents
        retainer_interval = "month"
        trial_days        = 30

    try:
        # Ensure products exist (create once, reuse by name)
        def get_or_create_price(product_name, unit_amount, recurring=False, interval="month"):
            # Search for existing product
            products = _stripe.Product.search(query=f'name:"{product_name}"', limit=1)
            if products.data:
                product_id = products.data[0].id
            else:
                product_id = _stripe.Product.create(name=product_name).id

            # Create a fresh price each time (amount may vary)
            kwargs = dict(
                product=product_id,
                unit_amount=unit_amount,
                currency="usd",
            )
            if recurring:
                kwargs["recurring"] = {"interval": interval}
            return _stripe.Price.create(**kwargs).id

        period_label = "Annual" if billing_period == "annual" else "Monthly"
        setup_price_id   = get_or_create_price(
            f"Cavnar AI Setup — {module_count} Module{'s' if module_count>1 else ''}",
            setup_amount
        )
        retainer_price_id = get_or_create_price(
            f"Cavnar AI Retainer {period_label} — {module_count} Module{'s' if module_count>1 else ''}",
            retainer_amount,
            recurring=True,
            interval=retainer_interval
        )

        session = _stripe.checkout.Session.create(
            customer_email=owner_email,
            payment_method_types=["card"],
            line_items=[
                {"price": setup_price_id,    "quantity": 1},
                {"price": retainer_price_id, "quantity": 1},
            ],
            mode="subscription",
            subscription_data={
                "trial_period_days": trial_days,
                "metadata": {
                    "restaurant": restaurant_name,
                    "modules": str(module_count),
                    "billing_period": billing_period,
                }
            },
            success_url="https://dashboard.cavnar.ai?payment=success",
            cancel_url="https://dashboard.cavnar.ai?payment=cancelled",
            custom_text={
                "submit": {"message": f"Pay ${module_count*500} setup today. {'$' + str(module_count*3000) + '/yr' if billing_period == 'annual' else '$' + str(module_count*300) + '/mo'} starts in 30 days."}
            },
            metadata={"restaurant": restaurant_name, "modules": str(module_count)},
        )
        return session.url

    except Exception as e:
        import traceback
        print(f"[STRIPE ERROR] Checkout creation failed for {restaurant_name}: {e}")
        traceback.print_exc()
        return None


@app.route("/sitemap.xml")
def sitemap():
    from flask import Response
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://cavnar.ai/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>
  <url><loc>https://cavnar.ai/privacy</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>
</urlset>"""
    return Response(xml, mimetype="application/xml")


@app.route("/robots.txt")
def robots():
    from flask import Response
    txt = """User-agent: *
Allow: /
Disallow: /admin
Disallow: /login
Disallow: /api/
Sitemap: https://cavnar.ai/sitemap.xml"""
    return Response(txt, mimetype="text/plain")


@app.route("/")
@login_required
def index(current_user):
    if current_user.get("is_admin"):
        return redirect("/admin")
    from labor import analyse_shifts_for_restaurant
    from inventory import load_inventory_for_restaurant, analyse_inventory
    from marketing import CONTENT_TYPES
    rid     = current_user["restaurant_id"]
    rfilter = request.args.get("filter","all")
    rsearch = request.args.get("search","")
    restaurant = get_restaurant(rid)
    rstats     = get_review_stats(rid)
    reviews    = get_reviews_data(rid, rfilter, rsearch)
    top_issues        = get_top_issues(rid, days=90)
    platform_breakdown = get_platform_breakdown(rid)
    sentiment_trend    = get_sentiment_trend(rid, weeks=8)
    try:
        labor = analyse_shifts_for_restaurant(rid)
        # Pre-sort role_summary for Jinja (dictsort attribute not supported in this Jinja2)
        labor['role_summary_sorted'] = sorted(
            labor.get('role_summary', {}).items(),
            key=lambda x: x[1].get('labor_pct', 0),
            reverse=True
        )
        labor['role_max_pct'] = max((v.get('labor_pct', 0) for v in labor.get('role_summary', {}).values()), default=30.0)
        try:
            from models import get_staff_notes as _gsn_dash
            _sn_dash = _gsn_dash(current_user["restaurant_id"])
            if _sn_dash:
                _sc = {}
                for _n in _sn_dash:
                    _name = _n['employee_name'].lower().strip().rstrip('.')
                    _sc[_name] = _n['notes']
                    # Also index by first name and first+initial for fuzzy matching
                    _parts = _name.split()
                    if _parts:
                        _sc[_parts[0]] = _n['notes']
                    if len(_parts) >= 2:
                        _sc[_parts[0] + ' ' + _parts[1].rstrip('.')] = _n['notes']
                        _sc[_parts[0] + ' ' + _parts[1].rstrip('.') + '.'] = _n['notes']
                labor['staff_constraints'] = _sc
            else:
                labor['staff_constraints'] = {}
        except Exception:
            labor['staff_constraints'] = {}
        # Staff notes for constraint-aware overtime display

        # Add period-over-period delta
        try:
            from models import get_labor_history as _glh_delta
            _hist = _glh_delta(rid, limit=2)
            if len(_hist) >= 2:
                labor['trend_delta'] = round(labor['overall_labor_pct'] - _hist[1]['labor_pct'], 1)
            else:
                labor['trend_delta'] = None
        except Exception:
            labor['trend_delta'] = None
    except Exception as e:
        print(f"Labor analysis error: {e}")
        labor = {"is_live":False,"total_labor_cost":0,"total_sales":0,"overall_labor_pct":0,
                 "overstaffed_days":[],"understaffed_days":[],"overtime_risk":[],
                 "dow_summary":{},"potential_savings":0,"labor_target":30.0,
                 "by_day":{},"employee_hours":{},"role_summary":{},"role_summary_sorted":[],"role_max_pct":0,"trend_delta":None,"staff_constraints":{}}
    try:
        _inv_items, _inv_live = load_inventory_for_restaurant(rid)
        inv = analyse_inventory(_inv_items)
        inv['is_live'] = _inv_live
        inv['banner_gradient'] = inv_banner_gradient(inv['annual_waste_projection'], inv['annual_recoverable'])
    except Exception as e:
        print(f"Inventory analysis error: {e}")
        inv = {"total_waste_cost_week":0,"monthly_waste_projection":0,
               "recoverable_monthly":0,"total_stock_value":0,
               "waste_items":[],"overstock":[],"critical_low":[],
               "reorder_soon":[],"order_reduction":[],"total_items":0,
               "annual_waste_projection":0,"annual_recoverable":0,"waste_rate_pct":0,"benchmark_label":"—","benchmark_color":"#999","benchmark_detail":"Upload inventory to see benchmark",
               "week_start":"—","week_end":"—","last_updated":"—",
               "banner_gradient":"linear-gradient(to right,#2a0808 0%,#0d331f 100%)",
               "is_live":False}
    # Show welcome banner if user has never logged in before (last_login is None)
    from auth import get_user_by_id
    _user_row = get_user_by_id(current_user["id"]) if not current_user.get("is_admin") else None
    show_welcome = bool(_user_row and not _user_row.get("last_login"))
    # Load competitor intel if available
    competitor_data = None
    if (restaurant and restaurant.google_place_id and restaurant.competitor_intel
            and restaurant.module_reviews and restaurant.module_labor
            and restaurant.module_inventory and restaurant.module_marketing):
        import json as _json
        try:
            competitor_data = _json.loads(restaurant.competitor_intel)
        except Exception:
            competitor_data = None

    # Labor overtime premium cost (0.5× blended rate on hours over 40/week)
    _hourly_rate = float(restaurant.hourly_rate or 26.0) if restaurant else 26.0
    _ot_premium = 0
    for _ot in labor.get("overtime_risk", []):
        if _ot.get("status") == "overtime":
            _ot_premium += max(0, _ot.get("hours", 0) - 40) * _hourly_rate * 0.5
    labor_overtime_cost = int(round(_ot_premium))

    # Marketing activity stats
    try:
        _conn_mkt = get_conn()
        _conn_mkt.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now')))""")
        _mkt_gen   = _conn_mkt.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (rid,)).fetchone()[0] or 0
        _mkt_pub   = _conn_mkt.execute("SELECT COUNT(DISTINCT topic) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL", (rid,)).fetchone()[0] or 0
        _mkt_month = _conn_mkt.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')", (rid,)).fetchone()[0] or 0
        # Months active = months since restaurant created_at (floor, min 1)
        _created_row = _conn_mkt.execute("SELECT created_at FROM restaurants WHERE id=?", (rid,)).fetchone()
        _conn_mkt.close()
        if _created_row and _created_row[0]:
            from datetime import datetime as _dt_mkt
            try:
                _created = _dt_mkt.fromisoformat(_created_row[0][:10])
                _now_mkt = _dt_mkt.now()
                _months_active = max(1, (_now_mkt.year - _created.year) * 12 + (_now_mkt.month - _created.month))
            except Exception:
                _months_active = 1
        else:
            _months_active = 1
        # Agency equivalent: $1,500/mo social media manager baseline
        _mkt_agency_value = _months_active * 1500 if int(restaurant.module_marketing or 0) else 0
        mkt_stats = {
            "generated":     _mkt_gen,
            "published":     _mkt_pub,
            "this_month":    _mkt_month,
            "months_active": _months_active,
            "agency_value":  _mkt_agency_value,
            "avg_per_month": round(_mkt_pub / _months_active, 1) if _months_active else 0,
        }
    except Exception:
        mkt_stats = {"generated": 0, "published": 0, "this_month": 0,
                     "months_active": 1, "agency_value": 0, "avg_per_month": 0}

    # ── Total savings breakdown ────────────────────────────────────────────────
    # Reviews value: each managed response saves ~$5 vs outsourcing to a rep service
    _reviews_value = int(rstats.get("responded", 0)) * 5
    # Labor value: weekly potential savings × 4.33 (monthly) + overtime premium recovered
    _labor_monthly = int(round(labor.get("potential_savings", 0) * 4.33))
    _labor_value   = _labor_monthly + labor_overtime_cost
    # Inventory value: monthly recoverable waste
    _inv_value = int(inv.get("recoverable_monthly", 0))
    # Marketing value: agency equivalent already computed
    _mkt_value = mkt_stats["agency_value"]
    # Only count modules that are active
    _mod_r = int(restaurant.module_reviews or 0)
    _mod_l = int(restaurant.module_labor or 0)
    _mod_i = int(restaurant.module_inventory or 0)
    _mod_m = int(restaurant.module_marketing or 0)
    savings_breakdown = {
        "reviews":   _reviews_value if _mod_r else 0,
        "labor":     _labor_value   if _mod_l else 0,
        "inventory": _inv_value     if _mod_i else 0,
        "marketing": _mkt_value     if _mod_m else 0,
        "total":     ((_reviews_value if _mod_r else 0) +
                      (_labor_value   if _mod_l else 0) +
                      (_inv_value     if _mod_i else 0) +
                      (_mkt_value     if _mod_m else 0)),
        # Monthly recurring savings (labor + inventory are monthly; reviews/marketing already cumulative)
        "labor_monthly":    _labor_monthly if _mod_l else 0,
        "inv_monthly":      _inv_value     if _mod_i else 0,
        "labor_annual":     int(_labor_monthly * 12) if _mod_l else 0,
        "inv_annual":       int(_inv_value * 12)     if _mod_i else 0,
        "labor_overtime":   labor_overtime_cost       if _mod_l else 0,
    }

    import secrets as _sec
    csrf_token = request.cookies.get('csrf_token') or _sec.token_hex(16)
    return render_template('dashboard.html',
        show_welcome=show_welcome,
        csrf_token=csrf_token,
        current_user=current_user, restaurant=restaurant,
        rstats=rstats, reviews=reviews, rfilter=rfilter, rsearch=rsearch, top_issues=top_issues, platform_breakdown=platform_breakdown, sentiment_trend=sentiment_trend,
        labor=labor, inv=inv, ctypes=CONTENT_TYPES,
        mod_reviews=int(restaurant.module_reviews or 0),
        mod_labor=int(restaurant.module_labor or 0),
        mod_inventory=int(restaurant.module_inventory or 0),
        mod_marketing=int(restaurant.module_marketing or 0),
        now=datetime.now().strftime("%b %d, %Y"),
        viewing_as=current_user.get("is_admin", 0),
        labor_target=float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0,
        labor_overtime_cost=labor_overtime_cost,
        mkt_stats=mkt_stats,
        savings_breakdown=savings_breakdown,
        competitor_data=competitor_data,
        competitor_updated_at=restaurant.competitor_updated_at if restaurant else None)

@app.errorhandler(404)
def page_not_found(e):
    from flask import Response
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Page Not Found — Cavnar AI</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    body{margin:0;background:#f7f4ef;font-family:'DM Sans',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
    .wrap{max-width:420px;padding:40px 24px}
    .logo{font-family:'DM Serif Display',serif;font-size:28px;color:#0e0c0a;margin-bottom:32px}
    .logo span{color:#c84b2f;font-style:italic}
    h1{font-family:'DM Serif Display',serif;font-size:64px;color:#0e0c0a;margin:0 0 8px;line-height:1}
    p{font-size:15px;color:#7a736a;line-height:1.6;margin:0 0 24px}
    a.btn{display:inline-block;background:#c84b2f;color:white;padding:10px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="logo">Cavnar <span>AI</span></div>
    <h1>404</h1>
    <p>This page doesn't exist. If you think something's wrong, email <a href="mailto:will@cavnar.ai" style="color:#c84b2f">will@cavnar.ai</a>.</p>
    <a href="/login" class="btn">Back to dashboard</a>
  </div>
</body>
</html>"""
    return Response(html, status=404, mimetype="text/html")

@app.errorhandler(500)
def server_error(e):
    from flask import Response
    html = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Something went wrong — Cavnar AI</title>
  <link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    body{margin:0;background:#f7f4ef;font-family:'DM Sans',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center}
    .wrap{max-width:420px;padding:40px 24px}
    .logo{font-family:'DM Serif Display',serif;font-size:28px;color:#0e0c0a;margin-bottom:32px}
    .logo span{color:#c84b2f;font-style:italic}
    h1{font-family:'DM Serif Display',serif;font-size:40px;color:#0e0c0a;margin:0 0 8px}
    p{font-size:15px;color:#7a736a;line-height:1.6;margin:0 0 24px}
    a.btn{display:inline-block;background:#c84b2f;color:white;padding:10px 24px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="logo">Cavnar <span>AI</span></div>
    <h1>Something went wrong</h1>
    <p>The server ran into an issue. It's been logged automatically. Email <a href="mailto:will@cavnar.ai" style="color:#c84b2f">will@cavnar.ai</a> if it keeps happening.</p>
    <a href="/login" class="btn">Back to dashboard</a>
  </div>
</body>
</html>"""
    return Response(html, status=500, mimetype="text/html")

# ── Module-level init (runs under gunicorn/Railway AND direct python) ────────

try:
    from models import init_db as _init_db, ensure_columns as _ec, init_email_log as _iel, init_onboarding_emails as _ioe
    from models import init_staff_notes as _isn
    from auth import init_auth as _init_auth
    _init_db()
    _init_auth()
    _isn()
    _ec()
    _iel()
    _ioe()
    print("DB init OK")
except Exception as _e:
    print(f"DB init error: {_e}")

# ── Admin account seed (module-level so it runs under Gunicorn too) ──────────
try:
    from models import get_conn as _gc_boot, create_restaurant as _cr_boot, Restaurant as _R_boot
    from auth import create_user as _cu_boot
    _conn_boot = _gc_boot()
    _existing_admin = _conn_boot.execute(
        "SELECT id FROM users WHERE username=?", (os.getenv("ADMIN_USERNAME","will"),)
    ).fetchone()
    _conn_boot.close()
    if not _existing_admin:
        _admin_pw = os.getenv("ADMIN_PASSWORD", "changeme123")
        _conn_boot2 = _gc_boot()
        _r_boot = _conn_boot2.execute("SELECT id FROM restaurants LIMIT 1").fetchone()
        _conn_boot2.close()
        if not _r_boot:
            _rid_boot = _cr_boot(_R_boot(name="Cavnar AI Admin", owner_email="will@cavnar.ai"))
        else:
            _rid_boot = _r_boot[0]
        _cu_boot(_rid_boot, os.getenv("ADMIN_USERNAME","will"), "will@cavnar.ai", _admin_pw, is_admin=True)
        _conn_boot3 = _gc_boot()
        _conn_boot3.execute("UPDATE restaurants SET billing_status='internal' WHERE id=?", (_rid_boot,))
        _conn_boot3.commit(); _conn_boot3.close()
        print(f"Admin account created: {os.getenv('ADMIN_USERNAME','will')}")
except Exception as _boot_e:
    print(f"Admin seed error: {_boot_e}")

try:
    from scheduler import start_scheduler as _ss
    _ss()
    print("Scheduler started OK")
except Exception as _e:
    print(f"Scheduler start error: {_e}")

# Enable WAL mode for concurrent access
try:
    from models import get_conn as _gc
    _wc = _gc(); _wc.execute("PRAGMA journal_mode=WAL"); _wc.commit(); _wc.close()
except Exception: pass

# ── Google My Business OAuth ─────────────────────────────────────────────────


# ── Sample CSV template downloads ────────────────────────────────────────────
@app.route("/og-image-v2.png")
def og_image():
    import os
    # Try multiple paths — Railway deploys to various locations
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "og-image-v2.png"),
        os.path.join(os.getcwd(), "og-image-v2.png"),
        "/app/og-image-v2.png",
    ]
    for path in candidates:
        if os.path.exists(path):
            return send_file(path, mimetype="image/png", max_age=86400)
    # Fallback: return a redirect to a placeholder
    return "", 404


def _do_seed_ryan():
    try:
        from models import create_restaurant, Restaurant
        ryan_rid = create_restaurant(Restaurant(
                name="Ryan's Charthouse",
                owner_email="ryancavnar@gmail.com",
                google_place_id="ChIJSzCXdo8R3ogRodiPcpYYLGw",
                neighborhood="Melbourne, Florida — on the Indian River Lagoon waterfront",
                vibe="casual-elegant waterfront seafood and steakhouse with lagoon views and sunset patio",
                known_for="prime rib, fresh seafood, waterfront patio, happy hour, early bird specials",
                yelp_business_id="chart-house-melbourne",
                module_reviews=1, module_labor=1,
                module_inventory=1, module_marketing=1,
                billing_status="trial",
        ))
        ryan_pw = os.getenv("RYAN_TEST_PASSWORD", "charthouse123")
        create_user(ryan_rid, "ryan", "ryancavnar@gmail.com", ryan_pw, is_admin=False)
        print(f"\n  Test client created: ryan / {ryan_pw}\n")

        # Seed sample reviews for Ryan's Charthouse with realistic Chart House content
        from models import get_conn as _gc_r
        _conn_r = _gc_r()
        import json as _json_r
        # categories hand-assigned to match review content — avoids needing API call on seed
        # 32 reviews spread across 8 weeks — 4 per week with realistic sentiment mix
        # Format: (platform, ext_id, rating, text, sentiment, name, categories, urgency, week_offset)
        sample_reviews = [
                # Week 8 (oldest, -49 days)
                ("google","rr_w8a",5,"Stunning sunset views and the prime rib was cooked to perfection. Our server was attentive all night.","positive","Karen B.",["food_quality","service","ambiance"],"normal"),
                ("google","rr_w8b",4,"Really good food and great atmosphere. Service was a touch slow but nothing major.","positive","James T.",["food_quality","ambiance","service"],"normal"),
                ("yelp",  "rr_w8c",2,"Waited forever for our table despite a reservation. Food was fine but the wait killed the experience.","negative","Patricia M.",["wait_time","reservation"],"normal"),
                ("yelp",  "rr_w8d",3,"Decent food, nothing special. The lobster bisque was good but the entrees were just okay for the price.","neutral","Steven R.",["food_quality","value"],"normal"),
                # Week 7 (-42 days)
                ("google","rr_w7a",5,"Best anniversary dinner we've had. The filet was incredible and the lagoon view at sunset was magical.","positive","Michelle H.",["food_quality","ambiance"],"normal"),
                ("google","rr_w7b",5,"Came for the early bird special and left completely satisfied. Great value for waterfront dining.","positive","Donald C.",["food_quality","value","ambiance"],"normal"),
                ("yelp",  "rr_w7c",1,"Service was absolutely terrible. Rude staff, wrong order, and the manager was dismissive when we complained.","negative","Sandra W.",["service"],"high"),
                ("google","rr_w7d",4,"Solid seafood and nice atmosphere. The shrimp cocktail appetizer was a highlight.","positive","Gary L.",["food_quality","ambiance"],"normal"),
                # Week 6 (-35 days)
                ("google","rr_w6a",5,"The Chart House never disappoints. Prime rib was perfect as always. Our server Danny was exceptional.","positive","Nancy P.",["food_quality","service"],"normal"),
                ("yelp",  "rr_w6b",4,"Great happy hour specials. The firecracker shrimp and craft cocktails were excellent.","positive","Kevin S.",["food_quality","value"],"normal"),
                ("google","rr_w6c",2,"Food came out cold and the restaurant was understaffed. Not worth the premium price.","negative","Betty A.",["food_quality","service","value"],"normal"),
                ("yelp",  "rr_w6d",3,"Mixed experience — some dishes excellent, others disappointing. The view makes up for a lot though.","neutral","Brian N.",["food_quality","ambiance"],"normal"),
                # Week 5 (-28 days)
                ("google","rr_w5a",5,"Absolutely wonderful dining experience. The seafood was fresh and the service was impeccable.","positive","Dorothy K.",["food_quality","service"],"normal"),
                ("google","rr_w5b",4,"Good food and great location on the lagoon. Will definitely return for special occasions.","positive","Charles V.",["food_quality","ambiance"],"normal"),
                ("yelp",  "rr_w5c",2,"Overpriced for what you get. Portion sizes have shrunk and the quality isn't what it used to be.","negative","Helen J.",["value","food_quality"],"normal"),
                ("google","rr_w5d",3,"Decent experience but nothing memorable. Service was fine, food was average for the price point.","neutral","Frank M.",["food_quality","service","value"],"normal"),
                # Week 4 (-21 days)
                ("google","rr_w4a",5,"Celebrated my retirement here. The whole team made it special — incredible food and service all around.","positive","Ruth C.",["food_quality","service","ambiance"],"normal"),
                ("yelp",  "rr_w4b",5,"The mud pie dessert alone is worth the trip. Everything was delicious and the lagoon views are stunning.","positive","Edward H.",["food_quality","ambiance"],"normal"),
                ("google","rr_w4c",1,"Found what appeared to be a hair in my salmon. Staff were apologetic but offered no real resolution.","negative","Carol D.",["food_quality","cleanliness","service"],"high"),
                ("yelp",  "rr_w4d",4,"Really enjoyed the happy hour. Great selection of appetizers at reasonable prices for this location.","positive","Mark S.",["food_quality","value"],"normal"),
                # Week 3 (-14 days)
                ("google","rr_w3a",5,"Outstanding in every way. The Chilean sea bass was the best I've ever had. Will be back monthly.","positive","Linda F.",["food_quality","service"],"normal"),
                ("google","rr_w3b",4,"Great waterfront ambiance and solid food. The prime rib was excellent as always.","positive","Paul B.",["food_quality","ambiance"],"normal"),
                ("yelp",  "rr_w3c",2,"Service has declined noticeably. Took 20 minutes to get water and our server seemed overwhelmed.","negative","Barbara G.",["service","wait_time"],"normal"),
                ("google","rr_w3d",3,"Good location and nice views but the food is inconsistent. Some visits great, others mediocre.","neutral","Thomas E.",["food_quality","ambiance"],"normal"),
                # Week 2 (-7 days)
                ("google","rr_w2a",5,"Absolutely incredible dinner. The Chilean sea bass melted in my mouth and our server was phenomenal.","positive","Jennifer M.",["food_quality","service"],"normal"),
                ("google","rr_w2b",2,"Waited 40 minutes past our reservation. The prime rib was overcooked and came out cold.","negative","David K.",["wait_time","food_quality","reservation"],"normal"),
                ("yelp",  "rr_w2c",5,"Celebrated my anniversary here. The filet and lobster combo was perfect. Sunset views stunning.","positive","Sarah T.",["food_quality","ambiance"],"normal"),
                ("google","rr_w2d",4,"Great happy hour on the patio. Firecracker shrimp and cocktails were excellent.","positive","Mike R.",["food_quality","value"],"normal"),
                # Week 1 (current, 0 days)
                ("yelp",  "rr_w1a",1,"Food was cold, service was rude, and the lobster bisque tasted like it came from a can. Will not return.","negative","Amanda L.",["food_quality","service","value"],"high"),
                ("google","rr_w1b",5,"The Chart House Cut prime rib is legendary. Been coming here for 10 years and it never disappoints.","positive","Robert H.",["food_quality","ambiance"],"normal"),
                ("google","rr_w1c",3,"Hit or miss experience. Tuna tartare was excellent but my mahi came out overcooked.","neutral","Lisa C.",["food_quality","service"],"normal"),
                ("yelp",  "rr_w1d",5,"Best restaurant on the lagoon. The mud pie dessert is a must. Server Danny made the evening special.","positive","Tom W.",["food_quality","service"],"normal"),
        ]
        from zoneinfo import ZoneInfo as _ZI_r
        from datetime import datetime as _dt_r, timedelta as _td_r2
        _now_r = _dt_r.now(_ZI_r('America/Chicago'))
        # Map week number from ext_id suffix to day offset
        _wk_map = {"w8":-49,"w7":-42,"w6":-35,"w5":-28,"w4":-21,"w3":-14,"w2":-7,"w1":0}
        for platform, ext_id, rating, text, sentiment, name, cats, urgency in sample_reviews:
                _wk = ext_id[3:5]
                _offset = _wk_map.get(_wk, 0)
                _rev_dt = (_now_r + _td_r2(days=_offset)).strftime('%Y-%m-%dT%H:%M:%S')
                _conn_r.execute("""
                        INSERT OR REPLACE INTO reviews
                        (restaurant_id, platform, external_id, author, rating, text, sentiment,
                            categories, urgency, fetched_at, review_date, response_status, processed, review_name)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (ryan_rid, platform, ext_id, name, rating, text, sentiment,
                            _json_r.dumps(cats), urgency, _rev_dt, _rev_dt, "pending", 1, name))
        _conn_r.commit()
        _conn_r.close()
        print("  Ryan's 32 reviews seeded with categories and week spread.\n")

        # Seed labor history FIRST (no API calls, instant) — always replace on fresh deploy
        try:
            from models import save_labor_snapshot as _sls_r2, get_conn as _gc_lh
            # Clear existing labor history for Ryan and reseed fresh
            _clh = _gc_lh()
            _clh.execute("DELETE FROM labor_history WHERE restaurant_id=?", (ryan_rid,))
            _clh.commit(); _clh.close()
            if True:
                _now_r2 = datetime.now()
                _lh_r2 = [(-49,34.2,42800,125200),(-42,33.1,44100,133200),(-35,31.8,45600,143400),
                           (-28,32.5,43200,132900),(-21,31.2,46800,150000),(-14,30.8,47200,153200),
                           (-7,30.9,45900,148500),(0,30.9,45900,148500)]
                for _off_r2,_pct_r2,_lab_r2,_sal_r2 in _lh_r2:
                    _sls_r2(ryan_rid,
                        (_now_r2+timedelta(days=_off_r2)).strftime("%Y-%m-%d"),
                        (_now_r2+timedelta(days=_off_r2+13)).strftime("%Y-%m-%d"),
                        _pct_r2, _lab_r2, _sal_r2)
                print("  Ryan labor history seeded.\n")
        except Exception as _lh_e2:
            print(f"  Ryan labor history seed error: {_lh_e2}")

        # Draft responses — hardcoded on Railway (fast), real API locally (quality)
        try:
            _on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
            if _on_railway:
                _drafts_map = {
                    "positive": [
                        "Thank you so much for the kind words — it truly means the world to our team. We can't wait to welcome you back to the lagoon!",
                        "What a wonderful review — we're so glad you had a great experience. Please come see us again soon!",
                        "This made our whole team smile. Thank you for sharing your experience — see you next time!",
                        "We're so grateful for guests like you. Thank you for the kind review and we hope to see you back very soon!",
                    ],
                    "negative": [
                        "We're truly sorry to hear about your experience and we take this feedback very seriously. Please reach out to us directly at ryans@charthouse.com so we can make this right.",
                        "This is not the standard we hold ourselves to and we sincerely apologize. We'd love the chance to speak with you directly — please contact us at ryans@charthouse.com.",
                        "We're sorry your visit didn't meet expectations. Your feedback has been shared with our management team.",
                    ],
                    "neutral": [
                        "Thank you for taking the time to share your experience. We appreciate the honest feedback and hope to exceed your expectations on your next visit.",
                        "Thanks for visiting and for the thoughtful review. We'd love to show you an even better experience next time.",
                    ],
                }
                _d_idx = {"positive": 0, "negative": 0, "neutral": 0}
                _conn_d = get_conn()
                _pending_d = _conn_d.execute(
                    "SELECT id, sentiment FROM reviews WHERE restaurant_id=? AND (draft_response IS NULL OR draft_response='')",
                    (ryan_rid,)
                ).fetchall()
                for _rev_d in _pending_d:
                    _sk = _rev_d["sentiment"] if _rev_d["sentiment"] in _drafts_map else "neutral"
                    _dl = _drafts_map[_sk]
                    _dt = _dl[_d_idx[_sk] % len(_dl)]
                    _d_idx[_sk] += 1
                    _conn_d.execute(
                        "UPDATE reviews SET draft_response=?, response_status='drafted' WHERE id=?",
                        (_dt, _rev_d["id"])
                    )
                _conn_d.commit(); _conn_d.close()
                print("  Ryan's reviews drafted (hardcoded — Railway).\n")
            else:
                from drafter import draft_pending
                draft_pending(ryan_rid, limit=50)
                print("  Ryan's reviews seeded and drafted (API — local).\n")
        except Exception as _de:
            print(f"  Draft error: {_de}")

        # Seed 6 weeks of inventory history so the trend chart is always populated for testing
        try:
                import json as _json_ryan_inv
                from datetime import timedelta as _td_ryan
                _ryan_inv_weeks = [
                        (241.80, ["Romaine Lettuce", "Bread Rolls", "Roma Tomatoes", "Baby Spinach"]),
                        (218.50, ["Bread Rolls", "Roma Tomatoes", "Sourdough Loaf"]),
                        (309.20, ["Romaine Lettuce", "Bread Rolls", "Salmon Fillet", "Baby Spinach"]),
                        (284.70, ["Roma Tomatoes", "Bread Rolls", "Fresh Herbs Mix"]),
                        (253.10, ["Bread Rolls", "Baby Spinach", "Romaine Lettuce"]),
                        (267.45, ["Romaine Lettuce", "Bread Rolls", "Roma Tomatoes", "Baby Spinach"]),
                ]
                from zoneinfo import ZoneInfo as _ZI_ryan_inv
                from datetime import datetime as _dt_ryan_inv
                # Use today as week_end anchor — matches how analyse_inventory saves snapshots
                _today_ryan = _dt_ryan_inv.now(_ZI_ryan_inv('America/Chicago')).date()
                _conn_ryan_inv = get_conn()
                _conn_ryan_inv.execute("""CREATE TABLE IF NOT EXISTS inventory_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        restaurant_id INTEGER NOT NULL,
                        waste_json TEXT,
                        week_end TEXT,
                        saved_at TEXT DEFAULT (datetime('now'))
                )""")
                try:
                        _conn_ryan_inv.execute("ALTER TABLE inventory_history ADD COLUMN week_end TEXT")
                except Exception:
                        pass
                _conn_ryan_inv.commit()
                for _wi, (_waste, _items) in enumerate(_ryan_inv_weeks):
                        # Go back 5,4,3,2,1,0 weeks from today — same cadence as real weekly uploads
                        _week_end = (_today_ryan - _td_ryan(weeks=(5 - _wi))).isoformat()
                        _snap = _json_ryan_inv.dumps({"total_waste_cost": _waste, "top_items": _items})
                        _ex = _conn_ryan_inv.execute(
                                "SELECT id FROM inventory_history WHERE restaurant_id=? AND week_end=?",
                                (ryan_rid, _week_end)
                        ).fetchone()
                        if _ex:
                                _conn_ryan_inv.execute(
                                        "UPDATE inventory_history SET waste_json=? WHERE id=?",
                                        (_snap, _ex["id"])
                                )
                        else:
                                _conn_ryan_inv.execute(
                                        "INSERT INTO inventory_history (restaurant_id, waste_json, week_end) VALUES (?,?,?)",
                                        (ryan_rid, _snap, _week_end)
                                )
                _conn_ryan_inv.commit()
                _conn_ryan_inv.close()
                print("  Ryan's inventory trend history seeded (6 weeks).\n")
        except Exception as _ryan_inv_e:
                print(f"  Ryan inventory seed error: {_ryan_inv_e}")

        # Seed rich inventory CSV for Ryan so the order list shows all row types
        try:
                from models import save_client_data as _scd_ryan
                _ryan_inv_csv = """item,category,unit,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week
Chilean Sea Bass,Protein,lb,12,2,28.50,2.2,12,0.5
Prime Rib,Protein,lb,20,3,18.75,3.8,20,1.2
Lobster Tail,Protein,lb,8,1,42.00,1.4,8,0.3
Shrimp 16/20,Protein,lb,15,6,14.20,2.6,15,1.8
Salmon Fillet,Protein,lb,14,10,16.50,2.1,14,2.4
Chicken Breast,Protein,lb,18,22,5.80,2.8,18,0.6
Filet Mignon,Protein,lb,10,12,32.00,1.5,10,0.4
Romaine Lettuce,Produce,head,24,30,2.50,3.2,24,9.5
Roma Tomatoes,Produce,lb,16,22,1.80,2.4,20,7.2
Baby Spinach,Produce,lb,10,14,4.20,1.2,10,4.8
Fresh Herbs Mix,Produce,bunch,6,8,5.50,0.6,6,3.1
Lemons,Produce,each,40,18,0.60,5.5,40,2.0
Asparagus,Produce,lb,12,9,3.80,1.8,12,1.1
Russet Potatoes,Produce,lb,25,12,0.80,4.2,25,3.5
Heavy Cream,Dairy,qt,10,16,3.80,1.4,10,0.8
Butter Unsalted,Dairy,lb,12,18,4.50,1.6,12,0.4
Parmesan Cheese,Dairy,lb,6,9,8.20,0.8,6,0.6
Bread Rolls,Bakery,each,80,52,0.45,14.0,80,22.0
Sourdough Loaf,Bakery,loaf,20,26,3.20,3.0,20,8.5
Pasta Rigatoni,Pantry,lb,18,22,2.80,2.5,18,1.2
Olive Oil Extra Virgin,Pantry,bottle,8,11,14.50,0.9,8,0.2
Beef Stock,Pantry,qt,10,14,4.80,1.4,10,0.3
White Wine Chardonnay,Beverage,bottle,16,20,8.50,2.0,16,0.0
House Cabernet,Beverage,bottle,20,24,9.20,2.8,20,0.0
Sparkling Water,Beverage,case,6,9,22.00,0.8,6,0.0"""
                _scd_ryan(ryan_rid, "inventory", _ryan_inv_csv, source="upload")
                print("  Ryan's rich inventory CSV seeded.\n")
        except Exception as _ryan_csv_e:
                print(f"  Ryan inventory CSV seed error: {_ryan_csv_e}")


    except Exception as _do_seed_e:
        print(f"  Ryan full seed error: {_do_seed_e}")

def _seed_ryan_background():
    try:
        import time as _t_ryan
        # Wait for DB to be ready — poll instead of blind sleep
        for _attempt in range(10):
            try:
                _test = get_conn()
                _test.execute("SELECT 1").fetchone()
                _test.close()
                break
            except Exception:
                _t_ryan.sleep(1)
        conn = get_conn()
        ryan_exists = conn.execute(
                "SELECT id FROM users WHERE email=?", ("ryancavnar@gmail.com",)
        ).fetchone()
        conn.close()
        if not ryan_exists:
            _do_seed_ryan()
    except Exception as _bg_e:
        print(f"  Ryan seed background error: {_bg_e}")


import threading as _t_seed
_seed_thread = _t_seed.Thread(target=_seed_ryan_background, daemon=True)
_seed_thread.start()

if __name__ == "__main__":
    print(f"\n  Hosted dashboard → http://localhost:{PORT}")
    print(f"  Admin panel      → http://localhost:{PORT}/admin\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
# redeploy Sat Jun  6 16:35:16 CDT 2026
