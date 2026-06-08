"""
hosted_dashboard.py — Cavnar AI hosted client dashboard
Multi-client, login-protected, Railway-deployable

Run locally:  python3 hosted_dashboard.py
Deploy:       Railway (connect GitHub repo, set env vars)
"""
import os, json
from datetime import datetime, timedelta
from functools import wraps
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
app.register_blueprint(admin_bp)
app.register_blueprint(webhook_bp)
app.register_blueprint(social_bp)
_secret_key = os.getenv("SECRET_KEY", "")
if not _secret_key:
    _secret_key = os.urandom(32).hex()
    print("WARNING: SECRET_KEY not set — sessions will invalidate on every restart. Set SECRET_KEY in Railway env vars.")
app.secret_key = _secret_key

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "will")

# ── Login rate limiting ────────────────────────────────────────────────────────
# Tracks failed login attempts per IP: {ip: [timestamp, timestamp, ...]}
_login_attempts = {}
_MAX_ATTEMPTS   = 5      # max failures before lockout
_LOCKOUT_SECS   = 300    # 5 minute lockout

def _get_client_ip():
    """Get real client IP, respecting Railway's proxy headers."""
    return (request.headers.get("X-Forwarded-For","").split(",")[0].strip()
            or request.remote_addr or "unknown")

def _is_rate_limited(ip):
    """Return True if IP has exceeded failed login attempts."""
    import time
    now = time.time()
    attempts = _login_attempts.get(ip, [])
    # Keep only attempts within lockout window
    recent = [t for t in attempts if now - t < _LOCKOUT_SECS]
    _login_attempts[ip] = recent
    return len(recent) >= _MAX_ATTEMPTS

def _record_failed_attempt(ip):
    import time
    _login_attempts.setdefault(ip, []).append(time.time())

def _clear_attempts(ip):
    _login_attempts.pop(ip, None)

def _generate_csrf():
    import secrets
    return secrets.token_hex(16)

def _verify_csrf():
    """Verify CSRF token on state-changing requests. Returns True if valid."""
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return True
    # Skip CSRF for webhooks and OAuth callbacks — they have their own auth
    skip_paths = ['/stripe-webhook', '/docusign/webhook', '/docusign/callback',
                  '/docusign/callback2', '/auth/google/callback', '/instagram/callback']
    if any(request.path.startswith(p) for p in skip_paths):
        return True
    # Get token from cookie and from header/form
    cookie_token = request.cookies.get('csrf_token', '')
    # Accept from X-CSRF-Token header (JS fetch) or form field
    header_token = request.headers.get('X-CSRF-Token', '')
    form_token = request.form.get('csrf_token', '')
    submitted = header_token or form_token
    if not cookie_token or not submitted:
        return False
    return cookie_token == submitted
PORT           = int(os.getenv("PORT", 8080))
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
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs, current_user=user)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user or not user["is_admin"]:
            return redirect(url_for("login"))
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
                "submit": {"message": f"Pay ${module_count*500} setup today. ${module_count*300}/mo starts in 30 days."}
            },
            metadata={"restaurant": restaurant_name, "modules": str(module_count)},
        )
        return session.url

    except Exception as e:
        import traceback
        print(f"[STRIPE ERROR] Checkout creation failed for {restaurant_name}: {e}")
        traceback.print_exc()
        return None


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "GET":
        sent = request.args.get("sent")
        return render_template('forgot_password.html', sent=sent)

    # POST — send reset email
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return render_template('rate_limited.html'), 429
    _record_failed_attempt(ip)  # Count each forgot-password POST

    email = request.form.get("email", "").strip().lower()
    if email:
        try:
            from models import create_reset_token
            import resend as _resend
            token = create_reset_token(email)
            if token:
                reset_url = f"https://dashboard.cavnar.ai/reset-password/{token}"
                _resend.api_key = os.getenv("RESEND_API_KEY", "")
                _resend.Emails.send({
                    "from": f"Cavnar AI <{os.getenv('FROM_EMAIL', 'will@cavnar.ai')}>",
                    "to": [email],
                    "subject": "Reset your Cavnar AI password",
                    "html": f"""
                    <div style="font-family:'DM Sans',sans-serif;max-width:480px;margin:0 auto;padding:32px 24px">
                      <div style="font-size:20px;font-weight:600;margin-bottom:24px">Cavnar <em style="color:#c84b2f;font-style:italic">AI</em></div>
                      <h2 style="font-size:18px;font-weight:600;margin-bottom:12px;color:#0e0c0a">Reset your password</h2>
                      <p style="font-size:14px;color:#4a4540;line-height:1.6;margin-bottom:24px">
                        Click the button below to reset your password. This link expires in 1 hour.
                      </p>
                      <a href="{reset_url}" style="display:inline-block;background:#c84b2f;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600">Reset password →</a>
                      <p style="font-size:12px;color:#7a736a;margin-top:24px">If you didn't request this, ignore this email — your password won't change.</p>
                      <hr style="border:none;border-top:1px solid #e5e0db;margin:24px 0">
                      <p style="font-size:11px;color:#9ca3af">Cavnar AI · will@cavnar.ai · cavnar.ai</p>
                    </div>"""
                })
        except Exception as e:
            print(f"[forgot-password] error: {e}")
    # Always redirect to sent page (don't reveal if email exists)
    from flask import redirect as _redir
    return _redir("/forgot-password?sent=1")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    from models import validate_reset_token, consume_reset_token
    valid = validate_reset_token(token)

    if request.method == "GET":
        return render_template('reset_password.html', valid=valid)

    # POST — set new password
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        from flask import redirect as _redir
        return _redir("/forgot-password?sent=1"), 429

    if not valid:
        from flask import redirect as _redir
        return _redir("/forgot-password")

    password = request.form.get("password", "")
    confirm  = request.form.get("confirm", "")

    if len(password) < 8 or password != confirm:
        from flask import redirect as _redir
        return _redir(f"/reset-password/{token}")

    success = consume_reset_token(token, password)
    if success:
        from flask import redirect as _redir
        return _redir("/login?reset=1")
    from flask import redirect as _redir
    return _redir("/forgot-password")


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


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        ip = _get_client_ip()

        if _is_rate_limited(ip):
            return render_template('login.html',
                error="Too many failed attempts. Please wait 5 minutes and try again.")
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        user = verify_password(username, password)
        if not user:
            _record_failed_attempt(ip)
            return render_template('login.html', error="Invalid username or password")
        _clear_attempts(ip)
        next_url = request.args.get("next", "/admin" if user["is_admin"] else "/")

        # Check if 2FA is enabled and device not remembered
        try:
            from models import get_restaurant
            _rid = user.get("restaurant_id")
            rest = get_restaurant(_rid) if _rid and not user.get("is_admin") else None
            _device_cookie = request.cookies.get("device_token_" + str(_rid), "")
            _2fa_on = rest and rest.two_fa_enabled and not user.get("is_admin")
            _device_ok = _device_cookie and _device_cookie == getattr(rest, "two_fa_device_token", "")
        except Exception as _e_2fa:
            _2fa_on = False
            _device_ok = False

        if _2fa_on and not _device_ok:
            # Generate and send 2FA code
            import random, datetime as _dt2
            from models import update_restaurant, get_restaurant
            code = str(random.randint(100000, 999999))
            expires = (_dt2.datetime.now() + _dt2.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            update_restaurant(_rid, {"two_fa_code": code, "two_fa_expires": expires})
            # Send email
            try:
                rest2 = get_restaurant(_rid)
                email = rest2.owner_email or ""
                if "@" in email:
                    from emails import send_2fa_code
                    owner = rest2.owner_name or None
                    send_2fa_code(email, rest2.name or "your restaurant", code, owner)
                    masked = email[:2] + "***@" + email.split("@")[-1] if "@" in email else email
                else:
                    masked = "your registered email"
            except Exception:
                masked = "your registered email"
            # Store pending token in DB so it works across Gunicorn workers
            import secrets as _sec3
            pending = _sec3.token_hex(24)
            # Encode uid into pending token: "uid:token"
            pending_signed = str(_rid) + ":" + pending
            import base64 as _b64
            pending_encoded = _b64.urlsafe_b64encode(pending_signed.encode()).decode()
            import secrets as _sec4
            csrf3 = _sec4.token_hex(16)
            resp3 = make_response(render_template('two_fa.html',
                masked_email=masked, error=None,
                pending_token=pending_encoded, next_url=next_url, csrf_token=csrf3))
            resp3.set_cookie("csrf_token", csrf3, httponly=True, samesite="Lax")
            return resp3

        _ua = request.headers.get("User-Agent", "")
        token = create_session(user["id"], ip_address=ip, user_agent=_ua)
        # Send login notification email if enabled
        try:
            from models import get_restaurant as _gr_ln
            _rest_ln = _gr_ln(user.get("restaurant_id")) if user.get("restaurant_id") else None
            if _rest_ln and getattr(_rest_ln, "login_notify", 0) and _rest_ln.owner_email:
                from emails import send_login_notification
                send_login_notification(_rest_ln.owner_email, _rest_ln.name or "", ip, _ua)
        except Exception as _ln_e:
            print(f"[LoginNotify] {_ln_e}")
        resp = make_response(redirect(next_url))
        _on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
        resp.set_cookie("session_token", token, max_age=30*24*3600,
                        httponly=True, secure=_on_railway, samesite="Lax")
        return resp
    import secrets as _sec2
    csrf2 = _sec2.token_hex(16)
    resp2 = make_response(render_template('login.html', error=None))
    resp2.set_cookie("csrf_token", csrf2, httponly=True, samesite="Lax")
    return resp2

@app.route("/verify-2fa", methods=["GET","POST"])
def verify_2fa():
    import flask as _fl3
    import datetime as _dt3
    from models import get_restaurant, update_restaurant
    if request.method == "POST":
        pending_token = request.form.get("pending_token","")
        code_entered  = request.form.get("code","").strip()
        next_url      = request.form.get("next_url", "/")
        remember      = request.form.get("remember_device","")
        # Decode uid from token
        try:
            import base64 as _b64_v
            decoded = _b64_v.urlsafe_b64decode(pending_token.encode()).decode()
            uid_str, _ = decoded.split(":", 1)
            uid = int(uid_str)
        except Exception:
            return redirect("/login")
        if not uid:
            return redirect("/login")
        rest = get_restaurant(uid)
        if not rest:
            return redirect("/login")
        # Check code
        now = _dt3.datetime.now()
        try:
            expires = _dt3.datetime.strptime(rest.two_fa_expires, "%Y-%m-%d %H:%M:%S")
        except Exception:
            expires = now
        import secrets as _sec5
        csrf4 = _sec5.token_hex(16)
        try:
            _rest_v = get_restaurant(uid)
            _email_v = _rest_v.owner_email if _rest_v else ""
            masked = _email_v[:2] + "***@" + _email_v.split("@")[-1] if "@" in _email_v else "your registered email"
        except Exception as _e_v:
            print(f"[verify_2fa] error: {_e_v}")
            masked = "your registered email"
        if rest.two_fa_code != code_entered:
            resp_err = make_response(render_template('two_fa.html',
                masked_email=masked, error="Incorrect code. Try again.",
                pending_token=pending_token, next_url=next_url, csrf_token=csrf4))
            resp_err.set_cookie("csrf_token", csrf4, httponly=True, samesite="Lax")
            return resp_err
        if now > expires:
            resp_exp = make_response(render_template('two_fa.html',
                masked_email=masked, error="Code expired. Request a new one.",
                pending_token=pending_token, next_url=next_url, csrf_token=csrf4))
            resp_exp.set_cookie("csrf_token", csrf4, httponly=True, samesite="Lax")
            return resp_exp
        # Code correct — clear it and create session
        update_restaurant(uid, {"two_fa_code": "", "two_fa_expires": ""})
        _fl3.session.pop("pending_uid", None)
        _fl3.session.pop("pending_token", None)
        _ip_2fa = _get_client_ip()
        _ua_2fa = request.headers.get("User-Agent", "")
        # uid is restaurant_id — look up the actual user_id for the session
        _user_for_session = get_user_by_restaurant_id(uid)
        if not _user_for_session:
            return redirect("/login")
        token = create_session(_user_for_session["id"], ip_address=_ip_2fa, user_agent=_ua_2fa)
        # Login notification
        try:
            _rest_ln2 = get_restaurant(uid)
            if _rest_ln2 and getattr(_rest_ln2, "login_notify", 0) and _rest_ln2.owner_email:
                from emails import send_login_notification
                send_login_notification(_rest_ln2.owner_email, _rest_ln2.name or "", _ip_2fa, _ua_2fa)
        except Exception as _ln2_e:
            print(f"[LoginNotify2FA] {_ln2_e}")
        _on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
        resp_ok = make_response(redirect(next_url or "/"))
        resp_ok.set_cookie("session_token", token, max_age=30*24*3600,
                           httponly=True, secure=_on_railway, samesite="Lax")
        if remember == "1":
            import secrets as _sec6
            dev_tok = _sec6.token_hex(32)
            update_restaurant(uid, {"two_fa_device_token": dev_tok})
            resp_ok.set_cookie("device_token_"+str(uid), dev_tok,
                               max_age=30*24*3600, httponly=True,
                               secure=_on_railway, samesite="Lax")
        return resp_ok
    return redirect("/login")

@app.route("/resend-2fa", methods=["POST"])
def resend_2fa():
    import flask as _fl4, random, datetime as _dt4
    from models import get_restaurant, update_restaurant
    data_r = request.get_json() or {}
    pending_token_r = data_r.get("pending_token", "")
    try:
        import base64 as _b64_r
        decoded_r = _b64_r.urlsafe_b64decode(pending_token_r.encode()).decode()
        uid_r, _ = decoded_r.split(":", 1)
        uid = int(uid_r)
    except Exception:
        return jsonify(ok=False, error="Session expired — please log in again")
    if not uid:
        return jsonify(ok=False, error="Session expired")
    rest = get_restaurant(uid)
    if not rest:
        return jsonify(ok=False)
    code = str(random.randint(100000, 999999))
    expires = (_dt4.datetime.now() + _dt4.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    update_restaurant(uid, {"two_fa_code": code, "two_fa_expires": expires})
    try:
        email = rest.owner_email or ""
        if "@" in email:
            from emails import send_2fa_code
            send_2fa_code(email, rest.name or "your restaurant", code, rest.owner_name)
    except Exception:
        pass
    return jsonify(ok=True)

@app.route("/logout")
def logout():
    token = request.cookies.get("session_token")
    if token:
        delete_session(token)
    resp = make_response(redirect("/login"))
    resp.delete_cookie("session_token")
    return resp

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
        _mkt_pub   = _conn_mkt.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND post_platform IS NOT NULL", (rid,)).fetchone()[0] or 0
        _mkt_month = _conn_mkt.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')", (rid,)).fetchone()[0] or 0
        _conn_mkt.close()
        mkt_stats = {"generated": _mkt_gen, "published": _mkt_pub, "this_month": _mkt_month}
    except Exception:
        mkt_stats = {"generated": 0, "published": 0, "this_month": 0}

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
        competitor_data=competitor_data,
        competitor_updated_at=restaurant.competitor_updated_at if restaurant else None)

@app.route("/approve/<int:rid>", methods=["POST"])
@login_required
def approve(rid, current_user):
    approve_response(rid)
    try:
        from models import log_event
        log_event(current_user["restaurant_id"], "review_approved", {"review_id": rid})
    except Exception:
        pass
    # Auto-post to Google in background thread — don't block the response
    try:
        from gmb import is_connected
        conn = get_conn()
        row = conn.execute(
            "SELECT platform, draft_response, review_name FROM reviews WHERE id=? AND restaurant_id=?",
            (rid, current_user["restaurant_id"])
        ).fetchone()
        conn.close()
        if row and row["platform"] == "google" and row["review_name"] and row["draft_response"]:
            if is_connected(current_user["restaurant_id"]):
                import threading as _t_gmb
                _rid_capture = rid
                _rest_id_capture = current_user["restaurant_id"]
                _review_name = row["review_name"]
                _draft = row["draft_response"]
                def _post_gmb_bg():
                    try:
                        from gmb import post_reply
                        result = post_reply(_rest_id_capture, _review_name, _draft)
                        if result["ok"]:
                            from models import mark_posted
                            mark_posted(_rid_capture)
                            print(f"[GMB] Auto-posted review {_rid_capture} ✓")
                        else:
                            print(f"[GMB] Auto-post failed for review {_rid_capture}: {result['error']}")
                    except Exception as _ge:
                        print(f"[GMB] Background post error: {_ge}")
                _t_gmb.Thread(target=_post_gmb_bg, daemon=True).start()
                return jsonify(ok=True, auto_posted=True)
    except Exception as e:
        print(f"[GMB] approve auto-post error: {e}")
    return jsonify(ok=True, auto_posted=False)

@app.route("/skip/<int:rid>", methods=["POST"])
@login_required
def skip(rid, current_user):
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='skipped' WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

def format_insight_html(text):
    import re as _re
    if not text:
        return 'Analysis unavailable.'
    # Try splitting on explicit Recommendations: heading first
    parts = _re.split(r'(?i)recommendations?:', text, maxsplit=1)
    if len(parts) == 2:
        intro = parts[0].strip()
        recs_raw = parts[1].strip()
        recs = [r.strip() for r in _re.split(r'\n+', recs_raw) if r.strip()]
    else:
        # Look for lines that start with 1. 2. 3. or are standalone short sentences after a paragraph
        lines = text.strip().split('\n')
        para_lines = []
        rec_lines = []
        in_recs = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if _re.match(r'^[123][\.\)]\s+', line):
                in_recs = True
                rec_lines.append(line)
            elif in_recs and _re.match(r'^[0-9][\.\)]\s+', line):
                rec_lines.append(line)
            elif in_recs:
                # Stop - closing sentence or non-numbered line after recs
                in_recs = False
                para_lines.append(line)
            else:
                para_lines.append(line)
        if not rec_lines:
            return '<p style="margin:0;line-height:1.7">' + text + '</p>'
        intro = ' '.join(para_lines).strip()
        recs = rec_lines
    html = ''
    if intro:
        html += '<p style="margin:0 0 10px 0;line-height:1.7">' + intro + '</p>'
    html += '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#c84b2f;margin-bottom:8px">Recommendations</div>'
    num = 1
    for rec in recs:
        clean = _re.sub(r'^[\d.\-)]+\s*', '', rec).strip()
        if not clean:
            continue
        html += ('<div style="display:flex;gap:10px;margin-bottom:8px;align-items:flex-start">'
            '<span style="flex-shrink:0;width:20px;height:20px;border-radius:50%;background:#c84b2f;color:white;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center">'
            + str(num) +
            '</span><span style="line-height:1.6;color:#b7791f;font-weight:500">' + clean + '</span></div>')
        num += 1
    return html

@app.route("/api/review-stats")
@login_required
def review_stats_api(current_user):
    from models import get_review_stats as _grs
    try:
        stats = _grs(current_user["restaurant_id"])
        return jsonify(**stats)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.route("/api/sentiment-trend")
@login_required
def sentiment_trend_api(current_user):
    from models import get_sentiment_trend as _gst
    try:
        data = _gst(current_user["restaurant_id"], weeks=8)
        return jsonify(weeks=data)
    except Exception as e:
        return jsonify(weeks=[], error=str(e))

@app.route("/api/review-insight")
@login_required
def review_insight_api(current_user):
    try:
        import os, json, anthropic as _anth
        from models import get_restaurant, get_review_stats, get_top_issues
        from zoneinfo import ZoneInfo as _ZI_ri
        from datetime import datetime as _dt_ri, timedelta as _td_ri
        _client_ri = _anth.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY",""))
        rid = current_user["restaurant_id"]
        restaurant = get_restaurant(rid)
        rstats = get_review_stats(rid)
        top_issues = get_top_issues(rid, days=90, limit=5)
        now_chi = _dt_ri.now(_ZI_ri('America/Chicago'))
        today_str = now_chi.strftime("%B %d, %Y")
        # Week-over-week comparison
        from models import get_conn as _gc_ri
        _conn_ri = _gc_ri()
        last_week = _conn_ri.execute("""
            SELECT COUNT(*) as cnt, AVG(rating) as avg_r,
                   SUM(sentiment='negative') as neg
            FROM reviews
            WHERE restaurant_id=? AND fetched_at >= datetime('now','-14 days')
              AND fetched_at < datetime('now','-7 days')
        """, (rid,)).fetchone()
        this_week = _conn_ri.execute("""
            SELECT COUNT(*) as cnt, AVG(rating) as avg_r,
                   SUM(sentiment='negative') as neg,
                   SUM(urgency='high' AND response_status NOT IN ('posted','skipped')) as urgent
            FROM reviews
            WHERE restaurant_id=? AND fetched_at >= datetime('now','-7 days')
        """, (rid,)).fetchone()
        # Recent urgent reviews text
        urgent_rows = _conn_ri.execute("""
            SELECT text FROM reviews
            WHERE restaurant_id=? AND urgency='high'
              AND response_status NOT IN ('posted','skipped')
            ORDER BY fetched_at DESC LIMIT 2
        """, (rid,)).fetchall()
        _conn_ri.close()
        wow_str = ""
        if last_week and last_week["cnt"] > 0 and this_week and this_week["cnt"] > 0:
            diff = (this_week["cnt"] or 0) - last_week["cnt"]
            rdiff = round(((this_week["avg_r"] or 0) - (last_week["avg_r"] or 0)), 1)
            wow_str = f"vs last week: {'+' if diff>=0 else ''}{diff} reviews, avg rating {'up' if rdiff>0 else 'down' if rdiff<0 else 'unchanged'} {abs(rdiff) if rdiff!=0 else ''}."
        urgent_texts = "; ".join(f'"{r["text"][:80]}"' for r in urgent_rows) if urgent_rows else "none"
        issues_str = ", ".join(f"{i['label']} ({i['count']})" for i in top_issues) if top_issues else "no data"
        owner_name = restaurant.owner_name if restaurant else None
        rest_name  = restaurant.name if restaurant else "this restaurant"
        name_line  = f"Owner: {owner_name}" if owner_name else ""
        prompt = (
            f"You are a restaurant reputation assistant. Output ONLY a 3-line snapshot.\n\n"
            f"Restaurant: {rest_name} | Today: {today_str}\n"
            f"Data: {rstats['total']} reviews | {rstats['avg_rating']}★ avg | "
            f"{rstats['positive']} pos / {rstats['negative']} neg / {rstats['neutral']} neutral | "
            f"{rstats['urgent']} urgent | response rate {rstats['response_rate']}%\n"
            f"Top topics: {issues_str} | {wow_str}\n"
            f"Urgent excerpts: {urgent_texts}\n\n"
            "Return EXACTLY this format — 3 lines:\n"
            "\U0001f4ca This week: [1 punchy sentence on the most important number. Be specific.]\n"
            "\u26a0\ufe0f Watch: [1 sentence on the biggest risk — urgent review theme, low response rate, or negative pattern. Skip if nothing urgent.]\n"
            "\u2705 Do today: [1 concrete action — e.g. 'Respond to Amanda L.s 1-star review about cold food.' Never generic.]\n\n"
            "Rules: no markdown, no extra lines, no preamble. Each line max 20 words. Never invent data."
        )

        msg = _client_ri.messages.create(
            model=os.getenv("CLAUDE_MODEL","claude-haiku-4-5-20251001"),
            max_tokens=200,
            messages=[{"role":"user","content":prompt}]
        )
        insight = msg.content[0].text.strip()
        # Strip any markdown
        import re as _re_ri
        insight = _re_ri.sub(r'\*\*(.+?)\*\*', lambda m: m.group(1), insight)
        insight = _re_ri.sub(r'\*(.+?)\*',   lambda m: m.group(1), insight)
        return jsonify(insight=insight)
    except Exception as _re:
        import traceback
        print(f"[review-insight ERROR] {_re}\n{traceback.format_exc()}")
        return jsonify(insight="Analysis unavailable — check back shortly.", error=str(_re)), 500

@app.route("/api/send-2fa-test", methods=["POST"])
@login_required
def send_2fa_test(current_user):
    """Send a test 2FA code to verify email before enabling."""
    import random, datetime as _dt5
    from models import get_restaurant, update_restaurant
    rest = get_restaurant(current_user["restaurant_id"])
    if not rest:
        return jsonify(ok=False, error="Restaurant not found")
    email = rest.owner_email or ""
    if not email or "@" not in email:
        return jsonify(ok=False, error="No email address found. Contact will@cavnar.ai to update your account email.")
    code = str(random.randint(100000, 999999))
    expires = (_dt5.datetime.now() + _dt5.timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    update_restaurant(current_user["restaurant_id"], {"two_fa_code": code, "two_fa_expires": expires})
    try:
        from emails import send_2fa_code
        send_2fa_code(email, rest.name or "your restaurant", code, rest.owner_name)
    except Exception as e:
        return jsonify(ok=False, error=f"Failed to send email: {str(e)[:60]}")
    masked = email[:2] + "***@" + email.split("@")[-1]
    return jsonify(ok=True, masked=masked)

@app.route("/api/verify-2fa-setup", methods=["POST"])
@login_required
def verify_2fa_setup(current_user):
    """Verify the test code and enable 2FA."""
    import datetime as _dt6
    from models import get_restaurant, update_restaurant
    data = request.get_json() or {}
    code = data.get("code", "").strip()
    rest = get_restaurant(current_user["restaurant_id"])
    if not rest:
        return jsonify(ok=False, error="Not found")
    if rest.two_fa_code != code:
        return jsonify(ok=False, error="Incorrect code. Try again.")
    try:
        exp_str = (rest.two_fa_expires or "").strip()
        expired = True
        for fmt in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"]:
            try:
                expires = _dt6.datetime.strptime(exp_str, fmt)
                expired = _dt6.datetime.now() > expires
                break
            except Exception:
                continue
        if expired:
            return jsonify(ok=False, error="Code expired. Click resend.")
    except Exception:
        return jsonify(ok=False, error="Code expired. Click resend.")
    update_restaurant(current_user["restaurant_id"], {
        "two_fa_enabled": 1, "two_fa_code": "", "two_fa_expires": ""
    })
    return jsonify(ok=True)

@app.route("/api/toggle-2fa", methods=["POST"])
@login_required
def toggle_2fa(current_user):
    from models import update_restaurant
    data = request.get_json() or {}
    enabled = 1 if data.get("enabled") else 0
    update_restaurant(current_user["restaurant_id"], {"two_fa_enabled": enabled})
    return jsonify(ok=True)

@app.route("/api/recent-topics")
@login_required
def recent_topics_api(current_user):
    try:
        from marketing import get_recent_content
        recent = get_recent_content(current_user["restaurant_id"], limit=8)
        topics = [r["topic"] for r in recent if r.get("topic")][:8]
        return jsonify(topics=topics)
    except Exception as e:
        return jsonify(topics=[])

@app.route("/api/mkt-stats")
@login_required
def mkt_stats_api(current_user):
    rid = current_user["restaurant_id"]
    try:
        conn = get_conn()
        conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
            created_at TEXT DEFAULT (datetime('now')))""")
        gen   = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=?", (rid,)).fetchone()[0] or 0
        pub   = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND post_platform IS NOT NULL", (rid,)).fetchone()[0] or 0
        month = conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND created_at >= date('now','start of month')", (rid,)).fetchone()[0] or 0
        conn.close()
        return jsonify(ok=True, generated=gen, published=pub, this_month=month)
    except Exception as e:
        return jsonify(ok=False, generated=0, published=0, this_month=0)

@app.route("/api/mkt-insight")
@login_required
def mkt_insight_api(current_user):
    try:
        from marketing import get_profile_for_restaurant, get_recent_content, get_upcoming_holidays, generate_content
        from models import get_restaurant
        from datetime import datetime
        from zoneinfo import ZoneInfo
        restaurant = get_restaurant(current_user["restaurant_id"])
        name = restaurant.name if restaurant else "your restaurant"
        owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
        rid = current_user["restaurant_id"]
        p = get_profile_for_restaurant(rid)
        recent = get_recent_content(rid, limit=5)
        now = datetime.now(ZoneInfo("America/Chicago"))
        upcoming = get_upcoming_holidays(now.replace(tzinfo=None))
        recent_str = ", ".join(r["topic"] for r in recent) if recent else "none yet"
        greeting = f"{owner}," if owner else "Hi,"
        never_clause = f"Never use these words or phrases: {p['never_say']}." if p.get("never_say") else ""
        menu_clause = f"Current menu/specials: {p['menu_notes']}." if p.get("menu_notes") else ""
        skip_h = [h.strip().lower() for h in (p.get("skip_holidays") or "").split(",") if h.strip()]
        if skip_h and upcoming:
            upcoming = ", ".join(h for h in upcoming.split(", ") if not any(s in h.lower() for s in skip_h)) or None
        prompt = f"""You are the Cavnar AI Marketing Consultant for {name}.
Write a short, punchy weekly marketing brief for {owner or "the owner"} — 3-4 sentences max.

Restaurant: {p["name"]} in {p["neighborhood"]}.
Vibe: {p["vibe"]}.
Known for: {p["known_for"]}.
Brand voice: {p["voice"]}.
{menu_clause}
{never_clause}
ALL upcoming holidays in next 30 days (mention ALL of them, not just one): {upcoming if upcoming else "none"}.
Recent content generated (do NOT repeat these): {recent_str}.

Structure exactly like this — no headers, no bullets, just two short paragraphs:
Paragraph 1: Start with "{greeting}" then give 1 specific marketing opportunity this week tied to the season, upcoming holidays, or a gap in recent content.
Paragraph 2: One concrete content suggestion with a specific angle. Reference real menu items if provided. End with a one-line encouragement.

Tone: warm, direct, like a trusted advisor. Match the brand voice exactly. No corporate language. Under 110 words total. If multiple holidays are coming up, mention both briefly."""
        import anthropic as _anth
        _client = _anth.Anthropic(api_key=__import__("os").getenv("ANTHROPIC_API_KEY"))
        msg = _client.messages.create(
            model=__import__("os").getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        insight = msg.content[0].text.strip()
        return jsonify(insight=format_insight_html(insight))
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[MktInsight] ERROR: {str(e)}")
        return jsonify(insight=f"Marketing brief unavailable — check back shortly.")

@app.route("/api/labor-insight")
@login_required
def labor_insight_api(current_user):
    try:
        from labor import analyse_shifts_for_restaurant, get_claude_insights
        from models import get_restaurant
        restaurant = get_restaurant(current_user["restaurant_id"])
        name  = restaurant.name if restaurant else "your restaurant"
        owner = restaurant.owner_name if restaurant and restaurant.owner_name else None
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        from models import get_staff_notes as _gsn_labor
        _staff_notes_labor = _gsn_labor(current_user["restaurant_id"])
        insight = get_claude_insights(analysis, restaurant_name=name, owner_name=owner,
                                      restaurant_id=current_user["restaurant_id"],
                                      staff_notes=_staff_notes_labor if _staff_notes_labor else None)
        return jsonify(insight=format_insight_html(insight))
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(insight=f"Unable to load analysis. Error: {str(e)[:100]}")

@app.route("/api/inv-insight")
@login_required
def inv_insight_api(current_user):
    try:
        from inventory import load_inventory_for_restaurant, analyse_inventory, get_claude_insights
        restaurant = get_restaurant(current_user["restaurant_id"])
        items, _is_live = load_inventory_for_restaurant(current_user["restaurant_id"])
        analysis = analyse_inventory(items)
        owner_name = restaurant.owner_name if restaurant else None
        insight = get_claude_insights(analysis, owner_name=owner_name, restaurant_name=restaurant.name if restaurant else None, restaurant_id=current_user["restaurant_id"])
        return jsonify(insight=format_insight_html(insight))
    except Exception as _inv_e:
        import traceback
        print(f"[inv-insight ERROR] {_inv_e}\n{traceback.format_exc()}")
        return jsonify(insight="Analysis unavailable — check server logs.", error=str(_inv_e)), 500

@app.route("/api/generate-content", methods=["POST"])
@login_required
def gen_content(current_user):
    data = request.get_json()
    from marketing import generate_content, mark_calendar_idea_used
    user = get_current_user()
    rid = user["restaurant_id"] if user else None
    content_type = data.get("type","instagram_post")
    topic = data.get("topic","")
    result = generate_content(content_type, topic, restaurant_id=rid)
    if data.get("from_calendar") and rid:
        try:
            mark_calendar_idea_used(rid, content_type, topic)
        except Exception:
            pass
    return jsonify(content=result)

@app.route("/api/content-calendar")
@login_required
def content_calendar(current_user):
    from marketing import get_content_calendar_ideas
    user = get_current_user()
    return jsonify(ideas=get_content_calendar_ideas(
        restaurant_id=user["restaurant_id"] if user else None))

@app.route("/api/change-password", methods=["POST"])
@login_required
def change_password(current_user):
    data = request.get_json()
    user = verify_password(current_user["username"], data.get("current",""))
    if not user:
        return jsonify(ok=False, error="Current password is incorrect")
    new_pw = data.get("new_password","")
    if len(new_pw) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters")
    update_password(current_user["id"], new_pw)
    return jsonify(ok=True)

@app.route("/api/update-email", methods=["POST"])
@login_required
def update_email_route(current_user):
    data = request.get_json()
    new_email = (data.get("new_email") or "").strip().lower()
    current_pw = data.get("current_password", "")
    if not new_email:
        return jsonify(ok=False, error="Email address is required")
    import re as _re_email
    if not _re_email.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", new_email):
        return jsonify(ok=False, error="Enter a valid email address")
    # Verify current password
    user = verify_password(current_user["username"], current_pw)
    if not user:
        return jsonify(ok=False, error="Current password is incorrect")
    # Check email not already taken by another user
    conn = get_conn()
    existing = conn.execute("SELECT id FROM users WHERE email=? AND id!=?", (new_email, current_user["id"])).fetchone()
    if existing:
        conn.close()
        return jsonify(ok=False, error="That email is already in use")
    # Update users.email
    conn.execute("UPDATE users SET email=? WHERE id=?", (new_email, current_user["id"]))
    # Update restaurant.owner_email so notifications/digest still work
    conn.execute("UPDATE restaurants SET owner_email=? WHERE id=?", (new_email, current_user["restaurant_id"]))
    conn.commit()
    conn.close()
    return jsonify(ok=True)

@app.route("/api/sessions", methods=["GET"])
@login_required
def list_sessions(current_user):
    token = request.cookies.get("session_token", "")
    sessions = get_sessions_for_user(current_user["id"], current_token=token)
    # Parse UA into a readable label
    def _parse_ua(ua):
        ua = ua or ""
        if "iPhone" in ua: return "iPhone"
        if "iPad" in ua: return "iPad"
        if "Android" in ua:
            import re as _re_ua
            m = _re_ua.search(r'Android[\s/]+([\d.]+)', ua)
            return "Android" + (" " + m.group(1) if m else "")
        if "Windows NT" in ua:
            import re as _re_ua2
            m = _re_ua2.search(r'Windows NT ([\d.]+)', ua)
            nt = {"10.0":"10","6.3":"8.1","6.2":"8","6.1":"7"}.get(m.group(1) if m else "", "")
            return "Windows" + (" " + nt if nt else "")
        if "Macintosh" in ua or "Mac OS X" in ua:
            import re as _re_ua3
            m = _re_ua3.search(r'Mac OS X ([\d_]+)', ua)
            ver = m.group(1).replace("_", ".") if m else ""
            return "Mac" + (" " + ver if ver else "")
        if "CrOS" in ua: return "Chromebook"
        if "Linux" in ua: return "Linux"
        return None  # unknown — will be handled below
    def _parse_browser(ua):
        ua = ua or ""
        import re as _re_b
        if "Edg/" in ua:
            m = _re_b.search(r'Edg/([\d.]+)', ua)
            return "Edge" + (" " + m.group(1).split(".")[0] if m else "")
        if "OPR/" in ua or "Opera/" in ua: return "Opera"
        if "Chrome/" in ua:
            m = _re_b.search(r'Chrome/([\d.]+)', ua)
            return "Chrome" + (" " + m.group(1).split(".")[0] if m else "")
        if "Firefox/" in ua:
            m = _re_b.search(r'Firefox/([\d.]+)', ua)
            return "Firefox" + (" " + m.group(1).split(".")[0] if m else "")
        if "Safari/" in ua:
            m = _re_b.search(r'Version/([\d.]+)', ua)
            return "Safari" + (" " + m.group(1).split(".")[0] if m else "")
        return None  # unknown
    def _fmt_ct(ts):
        """Convert UTC sqlite timestamp to CT M/D/YY h:MM AM/PM."""
        if not ts:
            return ""
        try:
            from datetime import datetime as _dt_s, timezone as _tz_s, timedelta as _td_s
            from zoneinfo import ZoneInfo as _ZI_s
            # SQLite stores as 'YYYY-MM-DD HH:MM:SS' UTC
            dt_utc = _dt_s.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz_s.utc)
            dt_ct = dt_utc.astimezone(_ZI_s("America/Chicago"))
            hour = dt_ct.hour % 12 or 12
            ampm = "AM" if dt_ct.hour < 12 else "PM"
            return "{}/{}/{} {}:{:02d} {} CT".format(
                dt_ct.month, dt_ct.day, str(dt_ct.year)[2:],
                hour, dt_ct.minute, ampm)
        except Exception:
            return ts[:16]
    live_ua = request.headers.get("User-Agent", "")
    live_ip = _get_client_ip()
    for s in sessions:
        ua = s.pop("user_agent", "") or ""
        # For the current session, always use the live request UA/IP
        # (handles sessions created before UA/IP tracking was added)
        if s.get("is_current"):
            ua = live_ua or ua
            if not s.get("ip_address") or s["ip_address"].lower() in ("", "unknown"):
                s["ip_address"] = live_ip
        device = _parse_ua(ua)
        browser = _parse_browser(ua)
        if device and browser:
            s["device"] = device
            s["browser"] = browser
        elif device:
            s["device"] = device
            s["browser"] = "Browser"
        elif browser:
            s["device"] = "Unknown device"
            s["browser"] = browser
        else:
            s["device"] = "Unknown device"
            s["browser"] = ""
        s["last_active"] = _fmt_ct(s.get("last_active", ""))
    return jsonify(sessions=sessions)


@app.route("/api/sessions/revoke-others", methods=["POST"])
@login_required
def revoke_other_sessions_route(current_user):
    token = request.cookies.get("session_token", "")
    revoke_other_sessions(current_user["id"], current_token=token)
    return jsonify(ok=True)


@app.route("/api/toggle-login-notify", methods=["POST"])
@login_required
def toggle_login_notify(current_user):
    from models import update_restaurant
    data = request.get_json()
    enabled = 1 if data.get("enabled") else 0
    update_restaurant(current_user["restaurant_id"], {"login_notify": enabled})
    return jsonify(ok=True)


# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route("/api/regenerate-draft/<int:review_id>", methods=["POST"])
@login_required
def regenerate_draft(review_id, current_user):
    """Regenerate AI draft for a review."""
    from models import get_conn, update_draft
    conn = get_conn()
    row = conn.execute("SELECT * FROM reviews WHERE id=? AND restaurant_id=?",
                       (review_id, current_user["restaurant_id"])).fetchone()
    conn.close()
    if not row:
        return jsonify(ok=False, error="Review not found")
    r = dict(row)
    restaurant = get_restaurant(current_user["restaurant_id"])
    try:
        import anthropic
        client = anthropic.Anthropic()
        sentiment_note = {"positive":"positive","negative":"negative","neutral":"neutral"}.get(r.get("sentiment","neutral"),"neutral")

        # Pull approved examples to teach the AI this owner's style
        from models import get_approved_examples
        examples = get_approved_examples(current_user["restaurant_id"], limit=4)
        examples_block = ""
        if examples:
            ex_lines = "\n".join([
                f"  Review ({e['rating']}★): \"{e['review']}\"\n  Response: \"{e['response']}\""
                for e in examples
            ])
            examples_block = f"\n\nHere are {len(examples)} recent responses this owner approved — match this exact tone and style:\n{ex_lines}"

        # Extract reviewer first name if available
        reviewer_name = ""
        if r.get("review_name"):
            first = r["review_name"].strip().split()[0]
            # Only use if it looks like a real name (not "A Google User" etc)
            if len(first) > 1 and first.lower() not in ("a","an","the","anonymous","user","google","yelp"):
                reviewer_name = first

        # Pull recurring negative themes to address patterns
        theme_context = ""
        if sentiment_note == "negative":
            try:
                from models import get_conn as _gc_r
                _conn_r = _gc_r()
                recent_neg = _conn_r.execute("""
                    SELECT text FROM reviews
                    WHERE restaurant_id=? AND sentiment='negative'
                    AND response_status NOT IN ('skipped')
                    ORDER BY fetched_at DESC LIMIT 5
                """, (current_user["restaurant_id"],)).fetchall()
                _conn_r.close()
                if len(recent_neg) >= 3:
                    theme_context = f"\n\nNote: This restaurant has had {len(recent_neg)} negative reviews recently. If this review shares themes with common complaints, acknowledge the pattern and note that it's being actively addressed."
            except Exception:
                pass

        # Platform-specific guidance
        platform = r.get("platform", "google")
        if platform == "google":
            platform_guidance = "This is a Google review — naturally include the restaurant name once for SEO. Keep it professional and inviting."
        elif platform == "yelp":
            platform_guidance = "This is a Yelp review — be conversational and genuine. Do NOT include the restaurant name repeatedly."
        else:
            platform_guidance = "Keep the response professional and genuine."

        # Length calibration by rating
        rating = r.get("rating", 3)
        if rating >= 4:
            length_guidance = "Keep it brief and warm — 25-40 words is ideal for a positive review. Don't over-explain."
        elif rating == 3:
            length_guidance = "Keep it 40-60 words — acknowledge both the positives and address any concerns mentioned."
        else:
            length_guidance = "This needs a fuller response — 60-80 words. Acknowledge specific complaints mentioned, apologize sincerely, and explain what will be done differently."

        # Build reviewer address
        reviewer_line = f"Address the reviewer as {reviewer_name} by name at the start." if reviewer_name else "Do not invent a name — start without one."

        prompt = f"""Write a professional, warm restaurant response to this {sentiment_note} review.

Restaurant: {restaurant.name}
Platform: {platform_guidance}
Voice guidance: {restaurant.voice_notes or "Warm, genuine, never corporate. Always invite guests back."}
Sign off as: {restaurant.sign_off_name or restaurant.name}
Never use: {restaurant.never_say or ""}{examples_block}
{reviewer_line}
Length: {length_guidance}

IMPORTANT: If the reviewer mentions specific complaints (cold food, slow service, wrong order, etc.) — address each one directly by name. Do not give a generic apology.{theme_context}

Review (rating: {r["rating"]}/5):
{r["text"]}

Write ONLY the response, no preamble. Sound like a real person, not a PR firm."""

        msg = client.messages.create(
            model=os.getenv("CLAUDE_MODEL","claude-haiku-4-5-20251001"),
            max_tokens=300,
            messages=[{"role":"user","content":prompt}]
        )
        new_draft = msg.content[0].text.strip()
        update_draft(review_id, new_draft)
        # Reset status to drafted
        conn = get_conn()
        conn.execute("UPDATE reviews SET response_status='drafted' WHERE id=?", (review_id,))
        conn.commit(); conn.close()
        return jsonify(ok=True, draft=new_draft)
    except Exception as e:
        return jsonify(ok=False, error=str(e))

@app.route("/api/save-draft/<int:review_id>", methods=["POST"])
@login_required
def save_draft(review_id, current_user):
    """Save a manually edited draft."""
    from models import update_draft
    data = request.get_json()
    draft = data.get("draft","").strip()
    if not draft:
        return jsonify(ok=False, error="Draft cannot be empty")
    conn = get_conn()
    row = conn.execute("SELECT id FROM reviews WHERE id=? AND restaurant_id=?",
                       (review_id, current_user["restaurant_id"])).fetchone()
    conn.close()
    if not row:
        return jsonify(ok=False, error="Review not found")
    update_draft(review_id, draft)
    conn = get_conn()
    conn.execute("UPDATE reviews SET response_status='drafted' WHERE id=?", (review_id,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@app.route("/api/labor-trend")
@login_required
def labor_trend_api(current_user):
    """Return labor % history for the trend chart."""
    try:
        from models import get_labor_history
        history = get_labor_history(current_user["restaurant_id"], limit=8)
        if not history:
            return jsonify(weeks=[])
        weeks = []
        for h in history[::-1]:  # oldest first = left to right
            try:
                start = datetime.strptime(h["period_start"], "%Y-%m-%d")
                label = start.strftime("%-m/%-d")
            except Exception:
                label = h.get("period_start", "")[:5]
            weeks.append({
                "label": label,
                "pct": round(h["labor_pct"], 1),
                "labor": h["total_labor"],
                "sales": h["total_sales"],
            })
        return jsonify(weeks=weeks)
    except Exception as e:
        return jsonify(weeks=[], error=str(e))

@app.route("/api/labor-gap")
@login_required
def labor_gap_api(current_user):
    try:
        from labor import analyse_shifts_for_restaurant, calculate_monthly_gap
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        gap = calculate_monthly_gap(analysis)
        return jsonify(gap)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e), over_target=False, monthly_gap=0,
                      current_pct=0, target_pct=30)

@app.route("/api/download-schedule")
@login_required
def download_schedule(current_user):
    import io
    try:
        from labor import (analyse_shifts_for_restaurant, load_shifts_for_restaurant,
                           generate_optimized_schedule, get_hourly_rate)
        from models import get_restaurant
        restaurant = get_restaurant(current_user["restaurant_id"])
        shifts   = load_shifts_for_restaurant(current_user["restaurant_id"])
        if not shifts:
            return jsonify(ok=False, error="No shift data available — upload shifts CSV first"), 400
        analysis = analyse_shifts_for_restaurant(current_user["restaurant_id"])
        rate     = get_hourly_rate(current_user["restaurant_id"])
        owner    = restaurant.owner_name if restaurant and restaurant.owner_name else None
        target   = restaurant.labor_target_pct if restaurant else 30.0
        from models import get_staff_notes
        staff_notes = get_staff_notes(current_user["restaurant_id"])
        csv_text = generate_optimized_schedule(
            analysis, shifts,
            restaurant_name=restaurant.name if restaurant else "Restaurant",
            hourly_rate=rate,
            owner_name=owner,
            staff_notes=staff_notes if staff_notes else None,
            labor_target=target
        )
        # Clean up any markdown Claude might add
        lines = [l for l in csv_text.split("\n") if l.strip() and not l.startswith("#") and not l.startswith("```")]
        csv_clean = "\n".join(lines)
        name = (restaurant.name if restaurant else "Restaurant").replace(" ","_")
        return send_file(
            io.BytesIO(csv_clean.encode()),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"optimized_schedule_{name}.csv"
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=str(e)), 500

@app.route("/api/billing-info")
@login_required
def billing_info(current_user):
    """Fetch billing status from Stripe for the current client."""
    import stripe as _stripe
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.stripe_customer_id:
        return jsonify(ok=False, reason="no_customer")

    stripe_key = os.getenv("STRIPE_SECRET_KEY","")
    if not stripe_key:
        return jsonify(ok=False, reason="no_key")

    try:
        _stripe.api_key = stripe_key
        # Get active subscriptions for this customer
        subs = _stripe.Subscription.list(
            customer=restaurant.stripe_customer_id,
            status="active",
            limit=5
        )
        if not subs.data:
            # Check for trialing
            subs = _stripe.Subscription.list(
                customer=restaurant.stripe_customer_id,
                status="trialing",
                limit=5
            )

        if not subs.data:
            return jsonify(ok=True, status="inactive", message="No active subscription found")

        sub = subs.data[0]
        from datetime import datetime
        next_date = datetime.fromtimestamp(sub.current_period_end).strftime("%-m/%-d/%Y")
        amount    = sum(i.price.unit_amount for i in sub["items"].data) / 100
        status    = sub.status  # active, trialing, past_due, canceled

        # Get payment method
        pm_desc = "Card on file"
        try:
            customer = _stripe.Customer.retrieve(
                restaurant.stripe_customer_id,
                expand=["invoice_settings.default_payment_method"]
            )
            pm = customer.invoice_settings.default_payment_method
            if pm and pm.card:
                pm_desc = f"{pm.card.brand.title()} ending {pm.card.last4}"
        except Exception:
            pass

        # Customer portal link
        try:
            portal = _stripe.billing_portal.Session.create(
                customer=restaurant.stripe_customer_id,
                return_url="https://dashboard.cavnar.ai"
            )
            portal_url = portal.url
        except Exception:
            portal_url = None

        return jsonify(
            ok=True,
            status=status,
            next_date=next_date,
            amount=f"${amount:,.0f}/mo",
            payment_method=pm_desc,
            portal_url=portal_url,
            trial_end=datetime.fromtimestamp(sub.trial_end).strftime("%-m/%-d/%Y") if sub.trial_end else None,
        )
    except Exception as e:
        print(f"Stripe billing info error: {e}")
        return jsonify(ok=False, reason="stripe_error", error=str(e))

@app.route("/api/update-digest-day", methods=["POST"])
@login_required
def update_digest_day(current_user):
    """Let client update their own weekly digest day."""
    data = request.get_json()
    day  = data.get("day","monday").lower()
    valid = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    if day not in valid:
        return jsonify(ok=False, error="Invalid day")
    update_restaurant(current_user["restaurant_id"], {
        "digest_day": day,
        "digest_enabled": int(data.get("enabled", 1))
    })
    return jsonify(ok=True)

@app.route("/api/dismiss-welcome", methods=["POST"])
@login_required
def dismiss_welcome(current_user):
    """Mark user as having seen welcome banner by updating last_login."""
    try:
        from auth import update_last_login
        update_last_login(current_user["id"])
    except Exception as _e:
        print(f"[dismiss-welcome] update_last_login error: {_e}")
    try:
        from models import log_event
        log_event(current_user["restaurant_id"], "login")
    except Exception:
        pass
    return jsonify(ok=True)

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

@app.route("/auth/google/connect")
@login_required
def gmb_connect(current_user):
    """Start Google OAuth flow for the logged-in client."""
    from gmb import get_auth_url
    if not os.getenv("GOOGLE_CLIENT_ID"):
        return jsonify(ok=False, error="Google OAuth not configured"), 500
    url = get_auth_url(current_user["restaurant_id"])
    from flask import redirect as _redirect
    return _redirect(url)


@app.route("/auth/google/callback")
def gmb_callback():
    """Handle Google OAuth callback — exchange code, store tokens, discover location."""
    from gmb import exchange_code, get_gmb_account_id, get_gmb_location_id
    from models import update_restaurant, get_restaurant
    from datetime import datetime, timezone, timedelta

    code          = request.args.get("code")
    restaurant_id = request.args.get("state")
    error         = request.args.get("error")

    if error or not code or not restaurant_id:
        msg = error or "No code returned"
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'error',msg:'" + msg + "'},'*');"
            "window.close();"
            "</script><p>Connection failed. Close this window.</p></body></html>"
        )

    try:
        restaurant_id = int(restaurant_id)
        tokens        = exchange_code(code)
        access_token  = tokens["access_token"]
        refresh_token = tokens.get("refresh_token", "")
        expires_in    = tokens.get("expires_in", 3600)
        expires_at    = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

        account_id  = get_gmb_account_id(access_token)
        location_id = None
        if account_id:
            r = get_restaurant(restaurant_id)
            location_id = get_gmb_location_id(access_token, account_id, r.google_place_id or "")

        update_restaurant(restaurant_id, {
            "gmb_access_token":  access_token,
            "gmb_refresh_token": refresh_token,
            "gmb_token_expires": expires_at,
            "gmb_account_id":    account_id or "",
            "gmb_location_id":   location_id or "",
        })

        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'connected'},'*');"
            "window.close();"
            "</script><p>Google Business connected! Close this window.</p></body></html>"
        )

    except Exception as e:
        print(f"[GMB] OAuth callback error: {e}")
        return (
            "<html><body><script>"
            "window.opener&&window.opener.postMessage({gmb:'error',msg:'Connection error'},'*');"
            "window.close();"
            "</script><p>Connection error. Close this window.</p></body></html>"
        )


@app.route("/auth/google/disconnect", methods=["POST"])
@login_required
def gmb_disconnect(current_user):
    """Disconnect Google Business from this restaurant."""
    from models import update_restaurant
    update_restaurant(current_user["restaurant_id"], {
        "gmb_access_token":  "",
        "gmb_refresh_token": "",
        "gmb_account_id":    "",
        "gmb_location_id":   "",
        "gmb_token_expires": "",
    })
    return jsonify(ok=True)


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


@app.route("/client/sample-template/<template_type>")
@login_required
def download_sample_template(current_user, template_type):
    """Serve sample CSV templates for clients to download."""
    from flask import Response
    if template_type == "shifts":
        csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"
        csv += "2026-06-01,Monday,Jane Smith,Server,11:00,17:00,6,6.0,4200,\n"
        csv += "2026-06-01,Monday,Mark Jones,Cook,10:00,18:00,8,8.2,4200,\n"
        csv += "2026-06-02,Tuesday,Jane Smith,Server,17:00,23:00,6,5.8,4800,\n"
        csv += "2026-06-02,Tuesday,Mark Jones,Cook,10:00,18:00,8,8.0,4800,\n"
        return Response(csv, mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=sample_shifts_template.csv"})
    elif template_type == "inventory":
        csv = "item,category,par_level,current_stock,unit_cost,avg_daily_usage,last_order_qty,waste_last_week\n"
        csv += "Chicken Breast,Protein,30,22,5.80,6.0,30,3.5\n"
        csv += "Romaine Lettuce,Produce,20,28,2.50,3.5,25,8.0\n"
        csv += "Heavy Cream,Dairy,12,9,3.80,1.8,12,1.5\n"
        csv += "Pasta Rigatoni,Pantry,15,19,2.80,2.2,15,1.8\n"
        return Response(csv, mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=sample_inventory_template.csv"})
    return "Template not found", 404


# ── Client self-serve data upload ────────────────────────────────────────────
@app.route("/client/upload-data", methods=["POST"])
@login_required
def client_upload_data(current_user):
    """
    Client-facing upload endpoint. Validates CSV, saves it, triggers re-analysis.
    login_required (not admin_required) so clients can upload their own data.
    """
    import io, csv as _csv
    from models import save_client_data, log_email

    # File size limit: 5MB max
    file = request.files.get("file")
    if file and file.content_length and file.content_length > 5 * 1024 * 1024:
        return jsonify(ok=False, error="File too large. Maximum size is 5MB."), 413

    restaurant_id = current_user["restaurant_id"]
    data_type     = request.form.get("data_type")  # "shifts" or "inventory"

    if data_type not in ("shifts", "inventory"):
        return jsonify(ok=False, error="Invalid data type")

    f = request.files.get("csv_file")
    if not f:
        return jsonify(ok=False, error="No file uploaded")

    try:
        csv_content = f.read().decode("utf-8")
    except Exception:
        return jsonify(ok=False, error="Could not read file — make sure it's a CSV")

    if not csv_content.strip():
        return jsonify(ok=False, error="File appears empty")

    # Validate it parses
    try:
        rows = list(_csv.DictReader(io.StringIO(csv_content)))
        if not rows:
            return jsonify(ok=False, error="CSV has no data rows")
    except Exception as e:
        return jsonify(ok=False, error=f"Could not parse CSV: {e}")

    # Validate required columns exist
    headers = [h.strip().lower() for h in (rows[0].keys() if rows else [])]

    if data_type == "shifts":
        required = ["date", "employee", "actual_hours"]
        optional_sales = ["sales", "sales_that_day", "revenue"]
        missing = [c for c in required if c not in headers]
        has_sales = any(c in headers for c in optional_sales)
        if missing:
            return jsonify(ok=False, error=(
                f"Your shifts CSV is missing required columns: {', '.join(missing)}. "
                f"Required columns are: date, employee, actual_hours. "
                f"Also recommended: sales (daily revenue for that date). "
                f"Download the sample template from the Labor tab for reference."
            ))
        if not has_sales:
            # Warn but don't block — labor % just won't show
            pass

    elif data_type == "inventory":
        required = ["item", "current_stock", "par_level", "unit_cost", "waste_last_week"]
        missing = [c for c in required if c not in headers]
        if missing:
            return jsonify(ok=False, error=(
                f"Your inventory CSV is missing required columns: {', '.join(missing)}. "
                f"Required columns are: item, current_stock, par_level, unit_cost, waste_last_week. "
                f"Also recommended: avg_daily_usage, last_order_qty. "
                f"Download the sample template from the Inventory tab for reference."
            ))

    # Save it
    save_client_data(restaurant_id, data_type, csv_content, source="upload")

    # Trigger immediate re-analysis so dashboard reflects new data right away
    try:
        if data_type == "shifts":
            from labor import analyse_shifts_for_restaurant
            analyse_shifts_for_restaurant(restaurant_id)
        elif data_type == "inventory":
            from models import get_restaurant
            r = get_restaurant(restaurant_id)
            if r:
                pass  # inventory analysis runs on next dashboard load
    except Exception as e:
        pass  # non-fatal — data is saved, analysis will run on next load

    # Log it and notify Will on first-ever upload
    try:
        from models import get_restaurant, get_client_data
        r = get_restaurant(restaurant_id)
        label = "Labor CSV upload" if data_type == "shifts" else "Inventory CSV upload"
        log_email(restaurant_id, label, current_user.get("email",""), f"{label} — {r.name if r else ''}")

        # Check if this is the client's first upload of this type
        import os as _os, resend as _resend
        _resend_key = _os.getenv("RESEND_API_KEY", "")
        _will_email = _os.getenv("WILL_EMAIL", "will@cavnar.ai")
        _from_email = _os.getenv("FROM_EMAIL", "will@cavnar.ai")
        if _resend_key and r:
            _resend.api_key = _resend_key
            _module = "shift schedule" if data_type == "shifts" else "inventory"
            _resend.Emails.send({
                "from": f"Cavnar AI Alerts <{_from_email}>",
                "to": [_will_email],
                "subject": f"📂 {r.name} just uploaded their {_module} data",
                "html": f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
                    <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:16px">
                        <h3 style="color:#0e0c0a;margin:0">Client data uploaded</h3>
                    </div>
                    <p style="font-size:15px;line-height:1.6">
                        <strong>{r.name}</strong> just uploaded their {_module} CSV ({len(rows)} rows).<br><br>
                        Good time to check their dashboard looks right and send a quick note.
                    </p>
                    <hr style="border:none;border-top:1px solid #e0dbd0;margin:16px 0"/>
                    <p style="font-size:11px;color:#7a736a">
                        <a href="https://dashboard.cavnar.ai/admin" style="color:#c84b2f">View in admin →</a>
                    </p>
                </div>"""
            })
    except Exception:
        pass

    return jsonify(ok=True, rows=len(rows), message=f"{len(rows)} rows loaded successfully")


# ── Startup ───────────────────────────────────────────────────────────────────

# ── Ryan seed (module-level — runs under Gunicorn AND direct python) ─────────


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
