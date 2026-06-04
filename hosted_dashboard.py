"""
hosted_dashboard.py — Cavnar AI hosted client dashboard
Multi-client, login-protected, Railway-deployable

Run locally:  python3 hosted_dashboard.py
Deploy:       Railway (connect GitHub repo, set env vars)
"""
import os, json
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template_string, request,
                   jsonify, redirect, url_for, make_response, send_file, session)
from emails import send_payment_email, send_welcome_email
from models import (init_db, get_conn, approve_response,
                    get_reviews_since, get_restaurant,
                    get_review_stats, get_reviews_data, get_top_issues,
                    get_platform_breakdown)
from auth import (init_auth, verify_password, create_session,
                  get_session_user, delete_session, create_user,
                  list_users, update_password)
from dotenv import load_dotenv
import pathlib
load_dotenv(pathlib.Path(__file__).parent / ".env")

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB global upload limit

@app.template_filter("format_intel")
def format_intel_filter(text):
    """Parse structured competitor intel into formatted HTML matching labor/inventory style."""
    import re
    if not text:
        return '<p style="color:var(--ink3);font-size:13px">Analysis unavailable.</p>'

    html_parts = []
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    # Split into intro and sections
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
        intro_text = " ".join(intro_lines)
        from markupsafe import escape as _esc
        html_parts.append('<p style="font-size:13px;color:#374151;line-height:1.7;margin-bottom:14px">' + str(_esc(intro_text)) + "</p>")

    # Parse sections
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
            from markupsafe import escape as _esc
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
        elif re.match(r"Recommendations?:?\s*$", line, re.I):
            if current_section and bullets:
                html_parts.append(flush_bullets(current_section, bullets))
            current_section = "recommendations"
            bullets = []
        elif line.startswith("-") and current_section != "recommendations":
            bullets.append(re.sub(r'\*+', '', line.lstrip("- ")).strip())
        elif re.match(r"^[0-9]+[.)]\s+", line):
            rec_lines.append(re.sub(r'\*+', '', re.sub(r"^[0-9]+[.)]\s+", "", line)).strip())
        elif current_section == "recommendations" and line and not re.match(r"Recommendations?:?", line, re.I):
            rec_lines.append(re.sub(r'\*+', '', line).strip())

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
        return '<p style="font-size:13px;color:#374151;line-height:1.7">' + text + "</p>"

    from markupsafe import Markup, escape as _esc
    # Sanitize: escape any HTML that came through from AI output in text nodes
    # html_parts contains our own safe HTML structure — only the text content needs escaping
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

# Register admin blueprint
from admin_routes import admin_bp
app.register_blueprint(admin_bp)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(32).hex())

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "will")

# ── Login rate limiting ────────────────────────────────────────────────────────
# Tracks failed login attempts per IP: {ip: [timestamp, timestamp, ...]}
_login_attempts = {}
_MAX_ATTEMPTS   = 5      # max failures before lockout
_LOCKOUT_SECS   = 300    # 5 minute lockout

# API rate limiting - per IP
_api_requests   = {}
_API_MAX        = 30     # max requests per window
_API_WINDOW     = 60     # 60 second window

def _is_api_rate_limited(ip):
    import time
    now = time.time()
    reqs = _api_requests.get(ip, [])
    recent = [t for t in reqs if now - t < _API_WINDOW]
    _api_requests[ip] = recent
    if len(recent) >= _API_MAX:
        return True
    _api_requests[ip].append(now)
    return False

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

# ── Templates ─────────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cavnar AI — Restaurant Intelligence</title>
<meta property="og:type" content="website">
<meta property="og:url" content="https://cavnar.ai">
<meta property="og:title" content="Cavnar AI — Restaurant Intelligence">
<meta property="og:description" content="AI-powered reviews, labor, inventory, and marketing for independent restaurants. Fully managed. No learning curve.">
<meta property="og:image" content="https://dashboard.cavnar.ai/og-image-v2.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:site_name" content="Cavnar AI">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Cavnar AI — Restaurant Intelligence">
<meta name="twitter:description" content="AI-powered reviews, labor, inventory, and marketing for independent restaurants. Fully managed. No learning curve.">
<meta name="twitter:image" content="https://dashboard.cavnar.ai/og-image-v2.png">
<meta name="description" content="AI-powered reviews, labor, inventory, and marketing for independent restaurants. Fully managed. No learning curve.">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="shortcut icon" href="/favicon.ico">
<meta name="theme-color" content="#0e0c0a">
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#0e0c0a;--ink2:#3a3530;--ink3:#7a736a;--paper:#f7f4ef;--paper2:#edeae3;--paper3:#e0dbd0;--ember:#c84b2f;--r:8px}
body{font-family:'DM Sans',sans-serif;background:var(--ink);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:var(--paper);border-radius:12px;padding:40px;width:100%;max-width:380px}
.logo{font-family:'DM Serif Display',serif;font-size:24px;color:var(--ink);margin-bottom:4px}
.logo em{color:var(--ember);font-style:italic}
.sub{font-size:12px;color:var(--ink3);margin-bottom:32px;letter-spacing:.06em;text-transform:uppercase}
.form-group{margin-bottom:16px}
label{display:block;font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin-bottom:5px}
input{width:100%;padding:11px 14px;border:1px solid var(--paper3);border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:14px;color:var(--ink);background:white;outline:none;transition:border .15s}
input:focus{border-color:var(--ember)}
.btn{width:100%;padding:12px;background:var(--ember);color:white;border:none;border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;cursor:pointer;transition:background .15s;margin-top:8px}
.btn:hover{background:#a83d25}
.error{background:#fdf0ef;border:1px solid #f5c6c2;border-radius:var(--r);padding:10px 14px;font-size:13px;color:var(--ember);margin-bottom:16px}
.footer-note{font-size:11px;color:var(--ink3);text-align:center;margin-top:20px}
</style>
</head>
<body>
<div class="card">
  <div class="logo">Cavnar <em>AI</em></div>
  <div class="sub">Restaurant Intelligence Dashboard</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  {% if request.args.get('reset') %}<div style="background:#f0faf4;border:1px solid #a7d7b8;border-radius:6px;padding:10px 14px;font-size:13px;color:#2d6a4f;margin-bottom:14px;text-align:center">✓ Password updated — sign in with your new password.</div>{% endif %}
  <form method="POST">
    <div class="form-group">
      <label>Username</label>
      <input type="text" name="username" placeholder="your-restaurant" autofocus required>
    </div>
    <div class="form-group">
      <label>Password</label>
      <input type="password" name="password" placeholder="••••••••" required>
    </div>
    <button class="btn" type="submit">Sign in</button>
  </form>
  <div class="footer-note" style="text-align:center;margin-top:12px">
    <a href="/forgot-password" style="font-size:12px;color:#7a736a;text-decoration:none">Forgot your password?</a>
  </div>
  <div class="footer-note">Need access? Contact will@cavnar.ai</div>
</div>
</body>
</html>"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ restaurant.name }} — Cavnar AI</title>
<meta name="csrf-token" content="{{csrf_token}}">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="shortcut icon" href="/favicon.ico">
<meta name="theme-color" content="#0e0c0a">
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#0e0c0a;--ink2:#3a3530;--ink3:#7a736a;--paper:#f7f4ef;--paper2:#edeae3;--paper3:#e0dbd0;--ember:#c84b2f;--ember2:#e8956a;--green:#2d6a4f;--green-bg:#eaf4ee;--red:#c0392b;--red-bg:#fdf0ef;--amber:#b7791f;--amber-bg:#fef9ec;--blue:#1a56cc;--blue-bg:#e8f0fe;--r:8px}
body{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);font-size:14px;line-height:1.6}
.hdr{background:var(--ink);height:56px;display:flex;align-items:center;padding:0 28px;justify-content:space-between;position:sticky;top:0;z-index:100}
.hdr-left{display:flex;align-items:center;gap:16px}
.hdr-logo{font-family:'DM Serif Display',serif;font-size:16px;color:var(--paper)}
.hdr-logo em{color:var(--ember2);font-style:italic}
.hdr-restaurant{font-size:12px;color:var(--ink3);padding-left:16px;border-left:1px solid #2a2520}
.hdr-right{display:flex;align-items:center;gap:16px}
.hdr-user{font-size:12px;color:var(--ink3)}
.logout-btn{font-size:11px;color:var(--ink3);text-decoration:none;padding:5px 10px;border:1px solid #2a2520;border-radius:4px;transition:all .15s}
.logout-btn:hover{color:var(--paper);border-color:var(--paper)}
.tabs{display:flex;gap:2px;padding:0 28px;background:var(--ink);border-top:1px solid #1a1714}
.tab{padding:8px 16px;border-radius:0;border:none;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:500;cursor:pointer;color:rgba(250,248,245,.45);background:transparent;transition:all .2s;border-bottom:2px solid transparent}
.tab:hover{color:var(--paper)}
.tab.active{color:var(--paper);border-bottom-color:var(--ember)}
.tab .badge{font-size:10px;padding:1px 5px;border-radius:10px;background:rgba(255,255,255,.1);margin-left:4px}
.panel{display:none;padding:24px 28px;max-width:1080px}
.panel.active{display:block}
.slabel{font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);margin-bottom:8px}
.stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-bottom:20px}
.stat{background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:12px;text-align:center}
.stat.hi{background:var(--red-bg);border-color:#f5c6c2}
.stat.ok{background:var(--green-bg);border-color:#b7dfca}
.stat.warn{background:var(--amber-bg);border-color:#f6d860}
.stat-n{font-family:'DM Serif Display',serif;font-size:26px;line-height:1}
.stat-l{font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.05em;margin-top:2px}
.stat.hi .stat-n{color:var(--red)}
.stat.ok .stat-n{color:var(--green)}
.stat.warn .stat-n{color:var(--amber)}
.card{background:white;border:1px solid var(--paper3);border-radius:var(--r);overflow:hidden;margin-bottom:10px;box-shadow:0 1px 3px rgba(14,12,10,.05)}
.card.urgent{border-left:3px solid var(--red)}
.card.approved{border-left:3px solid var(--green)}
.card.posted{border-left:3px solid #1a56cc;opacity:.85}
.card-hd{display:flex;align-items:flex-start;gap:10px;padding:12px 14px 8px}
.avatar{width:34px;height:34px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-family:'DM Serif Display',serif;font-size:13px;color:white;flex-shrink:0}
.card-meta{flex:1;min-width:0}
.card-author{font-weight:500;font-size:13px}
.card-sub{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--ink3);margin-top:1px;flex-wrap:wrap}
.stars{color:#d4a017;letter-spacing:1px;font-size:12px}
.pbadge{font-size:9px;font-weight:500;padding:1px 5px;border-radius:8px;text-transform:uppercase}
.pg{background:var(--blue-bg);color:var(--blue)}
.py{background:var(--red-bg);color:var(--red)}
.schip{font-size:9px;font-weight:500;padding:2px 7px;border-radius:20px;text-transform:uppercase;margin-left:auto;flex-shrink:0}
.sp{background:var(--green-bg);color:var(--green)}
.sn{background:var(--red-bg);color:var(--red)}
.su{background:var(--amber-bg);color:var(--amber)}
.ubanner{background:var(--red-bg);border-top:1px solid #f5c6c2;padding:5px 14px;font-size:11px;font-weight:500;color:var(--red)}
.card-body{padding:0 14px 12px}
.rtext{font-size:12px;color:var(--ink2);line-height:1.6;border-left:2px solid var(--paper3);padding-left:8px;margin-bottom:8px}
.cats{display:flex;flex-wrap:wrap;gap:3px;margin-bottom:8px}
.cat{font-size:9px;padding:2px 6px;border-radius:20px;background:var(--paper2);color:var(--ink3);text-transform:capitalize}
.draft-box{background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;padding:8px 10px}
.draft-lbl{font-size:9px;font-weight:500;color:var(--ink3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:4px}
.draft-txt{font-size:12px;color:var(--ink);line-height:1.55;font-style:italic}
.draft-actions{display:flex;gap:6px;margin-top:7px}
.btn{padding:6px 14px;border-radius:6px;border:none;font-family:'DM Sans',sans-serif;font-size:11px;font-weight:600;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:4px}
.btn-approve{background:var(--green);color:white}
.btn-approve:hover{background:#1f5c40}
.btn-skip{background:transparent;color:var(--ink3);border:1px solid var(--paper3)}
.btn-skip:hover{background:var(--paper2)}
.btn-approved{background:var(--green-bg);color:var(--green);border:1px solid #b7dfca;cursor:default}
.toolbar{display:flex;align-items:center;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.search-wrap{position:relative;max-width:260px}
.search-ico{position:absolute;left:9px;top:50%;transform:translateY(-50%);color:var(--ink3);pointer-events:none;width:12px;height:12px}
.search-input{width:100%;padding:7px 10px 7px 28px;border:1px solid var(--paper3);border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:12px;background:white;outline:none}
.search-input:focus{border-color:var(--ember)}
.filter-pills{display:flex;gap:4px;flex-wrap:wrap}
.fpill{padding:4px 10px;border-radius:20px;border:1px solid var(--paper3);font-size:11px;cursor:pointer;background:white;font-family:'DM Sans',sans-serif;transition:all .15s}
.fpill:hover{background:var(--paper2)}
.fpill.active{background:var(--ink);color:white;border-color:var(--ink)}
.fpill.active-red{background:var(--red);color:white;border-color:var(--red)}
.count-lbl{margin-left:auto;font-size:11px;color:var(--ink3)}
.no-data{text-align:center;padding:40px;color:var(--ink3);font-family:'DM Serif Display',serif;font-style:italic;font-size:15px}
.insight{background:var(--ink);border-radius:var(--r);padding:16px 18px;margin-bottom:16px}
.insight-lbl{font-size:9px;color:var(--ink3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:8px}
.insight-text{font-size:12px;line-height:1.7;color:rgba(250,248,245,.85);white-space:pre-wrap}
.insight-loading{color:var(--ink3);font-style:italic;font-size:12px}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3);padding:7px 10px;border-bottom:1px solid var(--paper3);background:var(--paper2)}
.tbl td{padding:8px 10px;border-bottom:1px solid var(--paper3);vertical-align:top}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover td{background:var(--paper2)}
.pill{display:inline-block;font-size:9px;padding:2px 7px;border-radius:20px;font-weight:500}
.pill-red{background:var(--red-bg);color:var(--red)}
.pill-amber{background:var(--amber-bg);color:var(--amber)}
.pill-green{background:var(--green-bg);color:var(--green)}
.day-bars{display:flex;align-items:flex-end;gap:6px;height:80px;margin:8px 0 3px}
.day-bar-wrap{flex:1;display:flex;flex-direction:column;align-items:center}
.day-bar{width:100%;border-radius:3px 3px 0 0;min-height:3px}
.ct-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:6px;margin-bottom:16px}
.ct-btn{background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:12px 10px;cursor:pointer;transition:all .2s;text-align:left}
.ct-btn:hover,.ct-btn.selected{border-color:var(--ember);background:#fdf0ef}
.ct-label{font-weight:600;font-size:12px;margin-bottom:2px}
.ct-desc{font-size:10px;color:var(--ink3);line-height:1.4}
.topic-row{display:flex;gap:8px;margin-bottom:12px;align-items:center}
.topic-input{flex:1;padding:8px 12px;border:1px solid var(--paper3);border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:12px;background:white;outline:none}
.topic-input:focus{border-color:var(--ember)}
.output-box{background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:14px;min-height:100px;font-size:12px;line-height:1.7;color:var(--ink2);white-space:pre-wrap}
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:5px;margin-top:10px}

.cal-card{background:white;border:1px solid var(--paper3);border-radius:6px;padding:12px;font-size:12px;display:flex;flex-direction:column}
.cal-day{background:white;border:1px solid var(--paper3);border-radius:6px;padding:8px 6px;font-size:10px}
.cal-day-name{font-weight:500;color:var(--ink2);margin-bottom:3px}
.cal-platform{font-size:8px;text-transform:uppercase;letter-spacing:.05em;color:var(--ink3);margin-bottom:2px}
.btn-primary{background:var(--ember);color:white;padding:8px 16px;border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:11px;font-weight:600;border:none;cursor:pointer;transition:background .15s}
.btn-primary:hover{background:#a83d25}
.btn-secondary{background:white;color:var(--ink2);border:1px solid var(--paper3);padding:8px 16px;border-radius:var(--r);font-family:'DM Sans',sans-serif;font-size:11px;font-weight:500;cursor:pointer;transition:all .15s}
.btn-secondary:hover{background:var(--paper2)}
.toast{position:fixed;bottom:20px;right:20px;background:var(--ink);color:var(--paper);padding:9px 16px;border-radius:7px;font-size:12px;z-index:999;opacity:0;transform:translateY(5px);transition:all .3s;pointer-events:none}
.toast.show{opacity:1;transform:translateY(0)}
.change-pw-section{background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:18px;margin-bottom:16px;max-width:400px}
.form-group{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}
.form-label{font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--ink3)}
.form-input{padding:9px 12px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--ink);background:white;outline:none;transition:border .15s}
.form-input:focus{border-color:var(--ember)}

/* ── Mobile responsive ────────────────────────────────────────────────────── */
@media (max-width: 640px) {
  /* Header */
  .hdr { padding: 8px 14px; }
  .hdr-logo { font-size: 18px; }
  .hdr-user { display: none; }

  /* Tabs — horizontal scroll */
  .tabs { padding: 0 8px; overflow-x: auto; -webkit-overflow-scrolling: touch; flex-wrap: nowrap; gap: 0; }
  .tab { padding: 8px 12px; font-size: 11px; white-space: nowrap; flex-shrink: 0; }
  .tab .badge { display: none; }

  /* Panels */
  .panel { padding: 14px 12px; }
  .panel.active { display: block; }

  /* Stat row — 2 columns on mobile */
  .stat-row { grid-template-columns: repeat(2, 1fr); gap: 6px; }
  .stat { padding: 10px 8px; }
  .stat-n { font-size: 22px !important; }
  .stat-l { font-size: 10px; }

  /* Two-col — stack on mobile */
  .two-col { grid-template-columns: 1fr; }

  /* Cards */
  .card { padding: 12px; }

  /* Tables — horizontal scroll */
  .card > table, .tbl { font-size: 11px; }
  div:has(> .tbl) { overflow-x: auto; -webkit-overflow-scrolling: touch; }

  /* Toolbar */
  .toolbar { gap: 6px; }
  .search-input { width: 100%; }

  /* Draft actions */
  .draft-actions { flex-wrap: wrap; gap: 4px; }
  .btn { font-size: 11px; padding: 5px 10px; }

  /* CT grid (marketing content types) */
  .ct-grid { grid-template-columns: repeat(2, 1fr); }

  /* Labor/inventory section labels */
  .slabel { font-size: 9px; }

  /* Account tab two-column layout */
  .form-grid { grid-template-columns: 1fr; }

  /* Hide less important columns in review cards */
  .card-sub .platform-badge { display: none; }

  /* Account tab — stack all grids */
  .account-top-row { grid-template-columns: 1fr !important; }
  .account-two-col { grid-template-columns: 1fr !important; }

  /* Hero banner — stack on mobile */
  .acct-hero { flex-direction: column; gap: 12px !important; }
  .acct-hero-right { text-align: left !important; }
}
</style>
<script>
function clientUpload(dataType, input) {
  var resultEl = document.getElementById(dataType + '-inline-result');
  if (!input || !input.files || !input.files[0]) return;

  // Show branded loading overlay
  var overlay = document.getElementById('upload-loading-overlay');
  if (overlay) overlay.style.display = 'flex';
  if (resultEl) { resultEl.style.display = 'none'; }

  var form = new FormData();
  form.append('data_type', dataType === 'inventory' ? 'inventory' : 'shifts');
  form.append('csv_file', input.files[0]);

  fetch('/client/upload-data', {method:'POST', body: form})
    .then(function(res) { return res.json(); })
    .then(function(data) {
      if (overlay) overlay.style.display = 'none';
      if (data.ok) {
        if (resultEl) {
          resultEl.style.display = 'inline';
          resultEl.style.color = '#2d6a4f';
          resultEl.textContent = '✓ ' + data.rows + ' rows loaded — refreshing…';
        }
        setTimeout(function() { location.reload(); }, 1200);
      } else {
        if (resultEl) {
          resultEl.style.display = 'inline';
          resultEl.style.color = '#c84b2f';
          resultEl.textContent = '✗ ' + (data.error || 'Upload failed');
        }
      }
    })
    .catch(function() {
      if (overlay) overlay.style.display = 'none';
      if (resultEl) {
        resultEl.style.display = 'inline';
        resultEl.style.color = '#c84b2f';
        resultEl.textContent = '✗ Network error — try again';
      }
    });
}
</script>
</head>
<body>

<!-- Branded upload loading overlay -->
<div id="upload-loading-overlay" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(14,12,10,0.85);align-items:center;justify-content:center;flex-direction:column">
  <div style="text-align:center">
    <div style="font-family:Georgia,serif;font-size:32px;font-weight:400;color:#f0ebe0;margin-bottom:4px">
      Cavnar <span style="color:#c84b2f;font-style:italic">AI</span>
    </div>
    <div style="font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:#7a736a;margin-bottom:32px">Restaurant Intelligence</div>
    <div style="display:flex;gap:10px;justify-content:center;margin-bottom:16px">
      <div style="width:10px;height:10px;border-radius:50%;background:#c84b2f;animation:upulse 1.1s ease-in-out infinite"></div>
      <div style="width:10px;height:10px;border-radius:50%;background:#c84b2f;animation:upulse 1.1s ease-in-out .18s infinite"></div>
      <div style="width:10px;height:10px;border-radius:50%;background:#c84b2f;animation:upulse 1.1s ease-in-out .36s infinite"></div>
    </div>
    <div style="font-size:13px;color:#a09890;letter-spacing:.02em">Loading your data…</div>
  </div>
</div>
<style>
@keyframes upulse{0%,100%{opacity:.2;transform:scale(.75)}50%{opacity:1;transform:scale(1.15)}}
</style>

{% if current_user.is_admin %}
<div style="background:#b7791f;padding:8px 28px;display:flex;align-items:center;justify-content:space-between">
  <span style="font-size:12px;color:white;font-weight:500">
    👁 Viewing as client — {{ restaurant.name }}. This is exactly what they see.
  </span>
  <a href="/admin/stop-viewing" style="font-size:11px;color:rgba(255,255,255,.8);text-decoration:none;padding:3px 10px;border:1px solid rgba(255,255,255,.4);border-radius:4px">
    ← Back to admin
  </a>
</div>
{% endif %}
<header class="hdr">
  <div class="hdr-left">
    <div class="hdr-logo">Cavnar <em>AI</em></div>
    <div class="hdr-restaurant">{{ restaurant.name }}</div>
  </div>
  <div class="hdr-right">
    <div style="display:flex;align-items:center;gap:8px">
      {% if current_user.is_admin %}
      <img src="data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAUDBAQEAwUEBAQFBQUGBwwIBwcHBw8LCwkMEQ8SEhEPERETFhwXExQaFRERGCEYGh0dHx8fExciJCIeJBweHx7/2wBDAQUFBQcGBw4ICA4eFBEUHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh7/wAARCABEAEQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCiZ7n1QfhTGluj1dfyrTbTwOsimnx6crcBzmvxD21NH2XJIxtkxGS4/KkEMp/j/St8aSP+eh/SnDSVPSf+VaUpyq39nFv0TZEmo/E7HPGF+7n8qDbsed5/KulXRN3AnGf92po/DFzJwkyn6qaieI5HaWj9GCXNsck1sSPvn8qz1trrzZ/tPmFc/ufIKDjnruH0/WvQv+EM1TqjQn6kiq9x4L1sDKwRP/uyCtqWL5Hf80TKDZxDW0Ixtk1I+vywDFFdQ/hXXVbH9myn6Yoro+vx7RI9i+7L08UsMZeRYVUdyDVa71S00yLfeTWkI/vSvtH8+a5LU/Fni2fSriKbRGLMoVSto+c5615fBZ6t4m1wrfS3JlkbHzozMTnoFAz+Ar0cnyOlJOriWnbZL9f8jPF4qcXyQPbR478GCBIn1Kzkudx3ySXD+X7AKqjgfWtfTtVs9QXdp13ptyP+ndkY/l1rj/Dvwo8NW9o114i1K9tEj++9zbPbxr/wJhj8zXUW3w/8JPA0/hWxXVJLdtj3IuFjjR8dDIx698LmvuqFWdFKlSpW8krHkTw8Zp1J1Fbve5eLOp5Vh36VJDeTROJIZirD8j7EVhX9n4o0dUJutLCs2BE900uB9dua2bCJ7q2kS9glS4RMM0ERdQxHAUn73UGtsTXppOliYW7pruZYehKpapQldd15HcaDeQakmI3kSdRl4y2fxB7it9Lc7PvH8RmvLPAOpMniKCK7BBiJMsm0qoj5Usw/g+hr2G68mG3EhZVTGd27jFfmeaYCnhq37v4Xqv8AI+gw9Wc4e+tTP+yMeQQaKxLzxJZQ3DRre8D0GRRXi/uzqXMcXdyabc20ttNOzRSoUcAsCQRg8jkUvhnRNi2dzYfbpBaS+XE8UkQKgHIAzgjjrnP1r02XwNpzji0H/fOKw9c0j/hFwl4kTR2TNiVuqo3QZ9M9M+tfU5Rl2JwFa8vhf6HFHF0Kz5W9zH8S20csEkF99oS3nlRrh5pvNOAwbaFXI5IAPPT1q5NLbjUjdW8ylpUCy7osRyY6HqPmHY+nFZ11bQ3mpC9XUJLiFH81reQAAAg8bsdjznFbNhq2i3brbou92GMursoPTPQdzX2ixVStU5py16BDD0KNPlpw0J5LVZyqPc2yc5HlwZJ/Esak+zR28TpGWYsxdnI5ZuP8B+VZGvLodncLPPzNkEIkhCDBzwM9T0+lZWreKrr/AFdvCLiSUlY0XOWOOOB2FRUmnJuWrGmlFJKxZspLWG91G7kkigX5d/mEBSuMHOe/0rlLjxBq+qTPBZrMYixEY5OF7ew4rUPhnUtR0+3muUVrpblZJI85ATHP6gfnXpGg+Dze2EF3AFt9wy22LP4cmvh87q1K9RUIU+Zx16vf0R0wnGnDmk7I+e7/AO1xXckdy8nmKecmivWfEPw1urnV55vOVgx4PT9KK8WOY0YpKej6mvs09UzsHku7eaCJbHXZXmOFBnUfjzU8U8c+rDRtQsb0rNGRIk8yujL3BHevmH4e6j8WrXRTZwgTaeqbbeDV3bEY6/uxncPx4r1T4d+ItQ0qRLzXLB0uSmx7e3fzQW9QzEkCv1JUXCWrPnFl2KmrxpsteIfBV9YXN3L4Yklgt40ZtlxcJKgA52jHzL7ZzXJQN4xknVYo7VXHH3Sc/wCNb99d31xq9/fKkUYvMkrgnbn098cZrX0O4tY4UivEZgON+Oa5vYzT0Pcp5fiYQ/eK/pqzmrXwhrd/KJdammkz0VFCAf1rtvD/AIXg09S8NssbsMFjy2PrWrBrmkQRhfPlfA4zGxNMn8U2oGLa1nlP+1hB/WumNKxksHiZOyg/y/MddWrxRusZKh42jY+gIxmmaLr97oaiG7kkkXbtAj7n1welZN9r1/cZChIAf7gyfzNZMjs2S24sepJ5qHg1Jt7XPSpZXUlG1V6dtzr/APhIL/UWa4jXcu4rnp0/nRXIpqOqwxJDb3RjijUKq+Sp4HvRXhVOEssqzc507t7u7/zN/qdWOkWrf15D3RRtAGNxwag8tWkOR+VFFfVSPVZI6iNUCgDIyaegAGcdaKKSFEUeuBUqAdaKKoscQPSmFVx0oooExhUZ6UUUUAf/2Q==" style="width:28px;height:28px;border-radius:50%;object-fit:cover;border:1.5px solid #2a2520" alt="Will Cavnar">
      {% endif %}
      <span class="hdr-user">{{ current_user.username }}</span>
    </div>
    <a href="/logout" class="logout-btn">Sign out</a>
  </div>
</header>
{% if show_welcome %}
<div id="welcome-banner" style="background:linear-gradient(135deg,#1a1410,#2a1f1a);border-bottom:2px solid var(--ember);padding:12px 28px;display:flex;align-items:center;justify-content:space-between;gap:16px">
  <div>
    <div style="color:white;font-size:13px;font-weight:600;margin-bottom:2px">Welcome to Cavnar <span style="color:var(--ember);font-style:italic">AI</span> 👋</div>
    <div style="color:#a09890;font-size:12px;line-height:1.5">Your dashboard is live. Sample data is shown until Will connects your real data — usually within 24 hours. Questions? <a href="mailto:will@cavnar.ai" style="color:var(--ember)">will@cavnar.ai</a></div>
  </div>
  <button onclick="dismissWelcome()" style="background:transparent;border:1px solid #3a3530;color:#7a736a;padding:5px 12px;border-radius:6px;cursor:pointer;font-size:11px;white-space:nowrap;font-family:'DM Sans',sans-serif">Got it ✕</button>
</div>
{% endif %}
<div style="background:var(--ink);padding:0 28px">
  <nav style="display:flex;gap:0">
    {% if mod_reviews %}<button class="tab {{'active' if mod_reviews}}" id="tab-reviews" onclick="switchTab('reviews',this)">Reviews <span class="badge">{{rstats.total}}</span></button>{% endif %}
    {% if mod_labor %}<button class="tab {{'active' if not mod_reviews and mod_labor}}" id="tab-labor" onclick="switchTab('labor',this)">Labor</button>{% endif %}
    {% if mod_inventory %}<button class="tab {{'active' if not mod_reviews and not mod_labor and mod_inventory}}" id="tab-inventory" onclick="switchTab('inventory',this)">Inventory</button>{% endif %}
    {% if mod_marketing %}<button class="tab {{'active' if not mod_reviews and not mod_labor and not mod_inventory and mod_marketing}}" id="tab-marketing" onclick="switchTab('marketing',this)">Marketing</button>{% endif %}
    {% if restaurant.google_place_id and mod_reviews and mod_labor and mod_inventory and mod_marketing %}<button class="tab" id="tab-competitor" onclick="switchTab('competitor',this)">Intel</button>{% endif %}
    <button class="tab {{'active' if not mod_reviews and not mod_labor and not mod_inventory and not mod_marketing}}" onclick="switchTab('account',this)" style="margin-left:auto">Account</button>
  </nav>
</div>

<!-- REVIEWS -->
<div class="panel {{'active' if mod_reviews}}" id="panel-reviews">
  <!-- New reviews notification banner -->
  <div id="new-reviews-banner" style="display:none;background:#1a1410;color:#f0ebe0;padding:10px 16px;border-radius:6px;margin-bottom:12px;font-size:13px;cursor:pointer;display:none;align-items:center;justify-content:space-between" onclick="window.location.reload()">
    <span id="new-reviews-text"></span>
    <span style="font-size:11px;color:#c84b2f;font-weight:600">Click to refresh →</span>
  </div>
  {% if not restaurant.reviews_live and not restaurant.gmb_refresh_token %}
  <div style="background:#fff8e6;border:1px solid #f0c040;border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:#8a6a00;display:flex;align-items:center;gap:8px">
    <span>⚠</span><span><strong>Sample data</strong> — example reviews showing how the dashboard works. Your live Google and Yelp reviews will appear here automatically once connected.</span>
  </div>
  {% elif restaurant.gmb_refresh_token and not restaurant.reviews_live %}
  <div style="background:#f0faf4;border:1px solid #a7d7b8;border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:#2d6a4f;display:flex;align-items:center;gap:8px">
    <span>✓</span><span><strong>Google Business connected</strong> — new reviews will sync automatically. Sample reviews shown below until your first live review comes in.</span>
  </div>
  {% endif %}
  <div class="stat-row">
    <div class="stat {{'ok' if rstats.avg_rating >= 4.5 else ('warn' if rstats.avg_rating >= 3.5 else 'hi')}}"><div class="stat-n">{{rstats.avg_rating}}</div><div class="stat-l">Avg rating</div></div>
    <div class="stat"><div class="stat-n">{{rstats.total}}</div><div class="stat-l">Total</div></div>
    <div class="stat ok"><div class="stat-n">{{rstats.positive}}</div><div class="stat-l">Positive</div></div>
    <div class="stat warn"><div class="stat-n">{{rstats.neutral}}</div><div class="stat-l">Neutral</div></div>
    <div class="stat hi"><div class="stat-n">{{rstats.negative}}</div><div class="stat-l">Negative</div></div>
    <div class="stat hi"><div class="stat-n">{{rstats.urgent}}</div><div class="stat-l">Urgent</div></div>
    <div class="stat warn"><div class="stat-n">{{rstats.awaiting_approval}}</div><div class="stat-l">To approve</div></div>
    <div class="stat {{'ok' if restaurant.reviews_live or restaurant.gmb_refresh_token else 'warn'}}">
      <div class="stat-n" style="font-size:14px;margin-top:4px">{{'Live' if restaurant.reviews_live else ('Connected' if restaurant.gmb_refresh_token else 'Demo')}}</div>
      <div class="stat-l">Review source</div>
    </div>
  </div>

  <div class="insight" style="margin-bottom:14px"><div class="insight-lbl">Cavnar AI Review Intelligence</div><div class="insight-text insight-loading" id="review-insight">Loading analysis…</div></div>

  {% set rrate = rstats.response_rate %}
  {% set rrate_label = 'Excellent' if rrate >= 70 else ('Strong' if rrate >= 40 else ('On Track' if rrate >= 15 else 'Below Average')) %}
  {% set rrate_color = '#2d6a4f' if rrate >= 70 else ('#6fcf97' if rrate >= 40 else ('#ef9f27' if rrate >= 15 else '#c0392b')) %}
  <div class="card" style="padding:14px 16px;margin-bottom:14px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--ink3)">Response rate vs industry benchmark</div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:13px;font-weight:700;color:{{rrate_color}}">{{rrate}}%</span>
        <span style="background:{{rrate_color}};color:white;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;letter-spacing:.5px">{{rrate_label}}</span>
      </div>
    </div>
    <div style="position:relative;height:10px;background:var(--paper3);border-radius:5px;overflow:hidden">
      <div style="position:absolute;left:0;top:0;height:100%;width:{% if rrate > 0 %}{{[rrate, 100]|min}}%{% else %}2px{% endif %};background:{{rrate_color}};border-radius:5px;transition:width .4s"></div>
      <div style="position:absolute;left:15%;top:-2px;height:14px;width:2px;background:#ef9f27;opacity:.7" title="15% — typical independent"></div>
      <div style="position:absolute;left:40%;top:-2px;height:14px;width:2px;background:#6fcf97;opacity:.7" title="40% — strong"></div>
      <div style="position:absolute;left:70%;top:-2px;height:14px;width:2px;background:#2d6a4f;opacity:.7" title="70% — excellent"></div>
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:5px;font-size:10px;color:var(--ink3)">
      <span>0%</span>
      <span style="color:#ef9f27;font-weight:600">▲ 15% avg independent</span>
      <span style="color:#6fcf97;font-weight:600">▲ 40% strong</span>
      <span style="color:#2d6a4f;font-weight:600">▲ 70% excellent</span>
      <span>100%</span>
    </div>
    <div style="margin-top:6px;font-size:11px;color:var(--ink3)">{{rstats.posted}} of {{rstats.total}} reviews responded to — restaurants that respond see <strong style="color:var(--ink)">35% higher return rates</strong> and a 3.1% sales lift can mean <strong style="color:var(--ink)">$125k/yr</strong> for a casual dining unit</div>
  </div>
  {% if platform_breakdown and platform_breakdown|length > 1 %}
  <div class="card" style="padding:14px 16px;margin-bottom:14px">
    <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--ink3);margin-bottom:10px">Platform breakdown</div>
    <div style="display:grid;grid-template-columns:{% for p in platform_breakdown %}1fr{% if not loop.last %} {% endif %}{% endfor %};gap:12px">
    {% for p in platform_breakdown %}
      {% set plat_col = '#4285f4' if p.platform == 'google' else '#d32323' %}
      {% set rating_col = '#2d6a4f' if p.avg_rating >= 4.5 else ('#ef9f27' if p.avg_rating >= 3.5 else '#c0392b') %}
      <div style="background:var(--paper);border:1px solid var(--paper3);border-radius:8px;padding:12px 14px">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
          <span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:{{plat_col}}">{{p.platform|title}}</span>
          <span style="font-size:10px;color:var(--ink3)">{{p.total}} review{{'s' if p.total != 1}}</span>
        </div>
        <div style="font-size:26px;font-weight:800;color:{{rating_col}};letter-spacing:-1px;line-height:1">{{p.avg_rating}} <span style="font-size:14px;color:var(--ink3)">★</span></div>
        <div style="display:flex;gap:10px;margin-top:8px;font-size:11px">
          <span style="color:#2d6a4f">▲ {{p.positive}} positive</span>
          <span style="color:#c0392b">▼ {{p.negative}} negative</span>
        </div>
      </div>
    {% endfor %}
    </div>
  </div>
  {% endif %}

  {% if top_issues %}
  <div class="card" style="padding:14px 16px;margin-bottom:14px">
    <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--ink3);margin-bottom:10px">Top mentioned topics — last 90 days</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px">
    {% set max_count = top_issues[0].count %}
    {% for issue in top_issues %}
      {% set pct = (issue.count / max_count * 100)|int %}
      {% set col = '#c0392b' if issue.count >= 4 else ('#ef9f27' if issue.count >= 2 else '#6b7280') %}
      <div style="display:flex;align-items:center;gap:8px;background:var(--paper);border:1px solid var(--paper3);border-radius:8px;padding:6px 12px;min-width:140px;flex:1">
        <div style="flex:1">
          <div style="font-size:12px;font-weight:600;color:var(--ink)">{{issue.label}}</div>
          <div style="height:4px;background:var(--paper3);border-radius:2px;margin-top:4px;overflow:hidden">
            <div style="height:100%;width:{{pct}}%;background:{{col}};border-radius:2px"></div>
          </div>
        </div>
        <span style="font-size:13px;font-weight:700;color:{{col}};min-width:24px;text-align:right">{{issue.count}}</span>
      </div>
    {% endfor %}
    </div>
  </div>
  {% endif %}

  <div class="toolbar">
    <div class="search-wrap">
      <svg class="search-ico" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
      <input class="search-input" id="rsearch" placeholder="Search reviews…" value="{{rsearch}}" onkeydown="if(event.key==='Enter')filterReviews()">
    </div>
    <div class="filter-pills">
      <button class="fpill {{'active' if rfilter=='all'}}" onclick="setRF('all',this)">All</button>
      <button class="fpill {{'active-red' if rfilter=='urgent'}}" onclick="setRF('urgent',this)">Urgent</button>
      <button class="fpill {{'active' if rfilter=='pending'}}" onclick="setRF('pending',this)">To approve</button>
      <button class="fpill {{'active' if rfilter=='negative'}}" onclick="setRF('negative',this)">Negative</button>
      <button class="fpill {{'active' if rfilter=='positive'}}" onclick="setRF('positive',this)">Positive</button>
    </div>
    <span class="count-lbl">{{reviews|length}} review{{'s' if reviews|length!=1}}</span>
    <button class="btn btn-skip" style="margin-left:auto" onclick="exportReviews()">Export CSV ↓</button>
  </div>
  {% if reviews %}
  {% set colors=['#c84b2f','#2d6a4f','#b7791f','#1a56cc','#6b4fa0','#1e7a8c'] %}
  {% for r in reviews %}
  {% set col=colors[loop.index0%colors|length] %}
  <div class="card {{'urgent' if r.urgency=='high'}} {{'posted' if r.response_status=='posted'}} {{'approved' if r.response_status=='approved'}}" id="rc-{{r.id}}" data-platform="{{r.platform}}" data-yelp-id="{{restaurant.yelp_business_id or ''}}">
    {% if r.urgency=='high' %}<div class="ubanner">⚠ Needs immediate attention</div>{% endif %}
    <div class="card-hd">
      <div class="avatar" style="background:{{col}}">{{r.author[0].upper() if r.author else "?"}}</div>
      <div class="card-meta">
        <div class="card-author">{{r.author|e}}</div>
        <div class="card-sub">
          <span class="stars">{% for i in range(5) %}{{('★' if i<r.rating else '☆')}}{% endfor %}</span>
          <span class="pbadge {{'pg' if r.platform=='google' else 'py'}}">{{r.platform}}</span>
          {% if r.review_date %}<span>{{r.review_date[:10]}}</span>{% endif %}
        </div>
      </div>
      <span class="schip {{'sp' if r.sentiment=='positive' else ('sn' if r.sentiment=='negative' else 'su')}}">{{r.sentiment or 'neutral'}}</span>
    </div>
    <div class="card-body">
      <div class="rtext">{{r.text|e}}</div>
      {% if r.categories %}<div class="cats">{% for c in r.categories %}<span class="cat">{{c.replace('_',' ')}}</span>{% endfor %}</div>{% endif %}
      {% if r.draft_response %}
      <div class="draft-box" id="draft-box-{{r.id}}">
        <div class="draft-lbl">Suggested response</div>
        <div class="draft-txt" id="draft-txt-{{r.id}}">{{r.draft_response|e}}</div>
        <div class="draft-actions" id="draft-actions-{{r.id}}">
          {% if r.response_status=='posted' %}
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:11px;color:#1a56cc;font-weight:600;background:#e8f0fe;border:1px solid #c5d8f8;padding:3px 8px;border-radius:4px">✓ Live on {{r.platform|title}}</span>
            </div>
          {% elif r.response_status=='approved' %}
            <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
              <span style="font-size:11px;color:var(--green);font-weight:600;background:#f0fdf4;border:1px solid #b7dfca;padding:3px 8px;border-radius:4px">✓ Approved</span>
              <span style="font-size:11px;color:var(--ink3)">{% if r.platform=='google' %}Auto-posting to Google or click below{% else %}Copy and post to {{r.platform|title}} manually{% endif %}</span>
            </div>
            <div style="display:flex;gap:6px;margin-top:6px">
              <button class="btn btn-skip" onclick="skipR({{r.id}})">Edit</button>
              {% if r.platform != 'google' %}
              <button class="btn" style="background:#e8f0fe;color:#1a56cc;border:1px solid #c5d8f8;font-size:11px" onclick="markPosted({{r.id}},this)">✓ Mark as posted</button>
              {% endif %}
            </div>
          {% elif r.response_status=='skipped' %}
            <button class="btn btn-approve" onclick="approveR({{r.id}})">✓ Approve</button>
            <button class="btn btn-skip" onclick="openEditor({{r.id}})">Edit response</button>
            <button class="btn btn-skip" onclick="regenDraft({{r.id}})">↻ Regenerate</button>
          {% else %}
            <button class="btn btn-approve" onclick="approveR({{r.id}})">✓ Approve</button>
            <button class="btn btn-skip" onclick="skipR({{r.id}})">Skip</button>
          {% endif %}
        </div>
        <!-- Response editor (hidden by default) -->
        <div class="response-editor" id="editor-{{r.id}}" style="display:none;margin-top:10px">
          <textarea id="editor-text-{{r.id}}" style="width:100%;padding:8px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--ink);background:white;resize:vertical;min-height:90px;outline:none" placeholder="Write your own response…">{{r.draft_response|e}}</textarea>
          <div style="display:flex;gap:6px;margin-top:6px">
            <button class="btn btn-approve" onclick="saveDraft({{r.id}})">Save & approve</button>
            <button class="btn btn-skip" onclick="regenDraft({{r.id}})">↻ Regenerate AI draft</button>
            <button class="btn btn-skip" onclick="closeEditor({{r.id}})">Cancel</button>
          </div>
        </div>
      </div>
      {% elif r.response_status != 'posted' %}
      <!-- No draft yet — show write/generate buttons -->
      <div class="draft-box" id="draft-box-{{r.id}}" style="background:var(--paper)">
        <div class="draft-lbl" style="color:var(--ink3)">No response drafted yet</div>
        <div class="draft-actions" style="margin-top:8px">
          <button class="btn btn-approve" onclick="regenDraft({{r.id}})">Generate AI response</button>
          <button class="btn btn-skip" onclick="openEditor({{r.id}})">Write my own</button>
        </div>
        <div class="response-editor" id="editor-{{r.id}}" style="display:none;margin-top:10px">
          <textarea id="editor-text-{{r.id}}" style="width:100%;padding:8px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--ink);background:white;resize:vertical;min-height:90px;outline:none" placeholder="Write your response here…"></textarea>
          <div style="display:flex;gap:6px;margin-top:6px">
            <button class="btn btn-approve" onclick="saveDraft({{r.id}})">Save & approve</button>
            <button class="btn btn-skip" onclick="closeEditor({{r.id}})">Cancel</button>
          </div>
        </div>
      </div>
      {% endif %}
    </div>
  </div>
  {% endfor %}
  {% else %}
  <div class="no-data">No reviews match this filter.</div>
  {% endif %}
</div>

<!-- LABOR -->
<div class="panel {{'active' if not mod_reviews and mod_labor}}" id="panel-labor">
  {% if not labor.is_live %}
  <div style="background:#fff8e6;border:1px solid #f0c040;border-radius:8px;padding:14px 16px;margin-bottom:16px">
    <div style="font-size:12px;color:#8a6a00;font-weight:600;margin-bottom:8px">⚠ Showing sample data — upload your shift CSV to see your real numbers</div>
    <div style="font-size:11px;color:#8a6a00;margin-bottom:10px;line-height:1.6">
      <strong>How to export:</strong>&nbsp;
      <span>Toast: Reports → Labor → Timesheets → Export CSV</span> &nbsp;·&nbsp;
      <span>Square: Dashboard → Team → Timecards → Export</span> &nbsp;·&nbsp;
      <span>Lightspeed: Reports → Employees → Time Clock → Export</span>
    </div>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <label style="display:inline-flex;align-items:center;gap:8px;background:#c84b2f;color:white;padding:7px 14px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer">
        📂 Upload shifts CSV
        <input type="file" accept=".csv" style="display:none" id="shifts-inline-input" onchange="clientUpload('shifts', this)">
      </label>
      <span id="shifts-inline-result" style="font-size:12px;color:#2d6a4f;display:none"></span>
    </div>
  </div>
  {% else %}
  <div style="background:#f0faf4;border:1px solid #a7d7b8;border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:#2d6a4f;display:flex;align-items:center;gap:8px">
    <span>✓</span><span><strong>Showing your real data</strong> — upload a new CSV anytime to update.</span>
    <label style="margin-left:auto;display:inline-flex;align-items:center;gap:6px;background:#c84b2f;color:white;padding:5px 12px;border-radius:5px;font-size:11px;font-weight:600;cursor:pointer">
      Update CSV <input type="file" accept=".csv" style="display:none" id="shifts-inline-input" onchange="clientUpload('shifts', this)">
    </label>
  </div>
  {% endif %}

  <!-- Hero metric — dollar gap -->
  <div id="labor-gap-banner" style="background:var(--ink);border-radius:var(--r);padding:20px 24px;margin-bottom:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
    <div>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:6px">
        <div style="font-size:10px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3)">Monthly labor cost vs target</div>
        {% if labor.date_range and labor.date_range.start %}
        <div style="font-size:10px;color:var(--ink3)" id="labor-period">
          Data period loading…
        </div>
        {% endif %}
      </div>
      <div style="display:flex;align-items:baseline;gap:12px;flex-wrap:wrap">
        <div>
          <span id="gap-current-pct" style="font-family:'DM Serif Display',serif;font-size:48px;color:var(--paper);line-height:1">{{labor.overall_labor_pct}}%</span>
          <span style="font-size:13px;color:var(--ink3);margin-left:6px">current</span>
        </div>
        <div style="color:var(--ink3);font-size:20px">→</div>
        <div>
          <span style="font-family:'DM Serif Display',serif;font-size:32px;color:#6fcf97;line-height:1">30%</span>
          <span style="font-size:13px;color:var(--ink3);margin-left:4px">target</span>
        </div>
      </div>
      <div id="gap-dollar" style="font-size:13px;color:var(--ember2);margin-top:6px;font-weight:500">Loading cost gap…</div>
    </div>
    <div style="text-align:right">
      <div id="gap-amount" style="font-family:'DM Serif Display',serif;font-size:36px;color:var(--ember2);line-height:1">—</div>
      <div style="font-size:11px;color:var(--ink3);margin-top:4px">estimated monthly overspend</div>
      <button onclick="downloadSchedule(this)" style="margin-top:12px;padding:9px 18px;background:var(--ember);color:white;border:none;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:600;cursor:pointer;transition:background .15s" onmouseover="this.style.background='#a83d25'" onmouseout="this.style.background='var(--ember)'">
        Download optimized schedule ↓
      </button>
    </div>
  </div>

  <!-- Stats row -->
  {% if restaurant.last_fetched_at %}
  <div style="font-size:11px;color:var(--ink3);margin-bottom:10px">
    Last updated: {{restaurant.last_fetched_at[:10]}}
  </div>
  {% endif %}
  <div class="stat-row">
    <div class="stat"><div class="stat-n">${{labor.total_sales|int|format_num}}</div><div class="stat-l">Revenue (2 wks)</div></div>
    <div class="stat warn"><div class="stat-n">${{labor.total_labor_cost|int|format_num}}</div><div class="stat-l">Labor cost (2 wks)</div></div>
    <div class="stat {{'hi' if labor.overall_labor_pct>32 else 'ok'}}"><div class="stat-n">{{labor.overall_labor_pct}}%</div><div class="stat-l">Labor %</div></div>
    <div class="stat hi"><div class="stat-n">{{labor.overstaffed_days|length}}</div><div class="stat-l">Overstaffed days</div></div>
    <div class="stat warn"><div class="stat-n">{{labor.overtime_risk|length}}</div><div class="stat-l">Overtime risk</div></div>
  </div>

  <!-- AI insight -->
  <div class="insight"><div class="insight-lbl">Cavnar AI Consultant</div><div class="insight-text insight-loading" id="labor-insight">Loading analysis…</div></div>

  <!-- Two col: overstaffed table + bar chart -->
  <div class="two-col">
    <div>
      <div class="slabel">Overstaffed days — where the money is going</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Date</th><th>Day</th><th>Sales</th><th>Labor cost</th><th>Labor %</th><th>Over target</th></tr></thead>
        <tbody>
        {% for d in labor.overstaffed_days %}
        <tr>
          <td>{{d.date}}</td>
          <td style="font-weight:500">{{d.day}}</td>
          <td>${{d.sales|int|format_num}}</td>
          <td>${{d.labor_cost|format_num}}</td>
          <td><span class="pill {{'pill-red' if d.labor_pct>35 else 'pill-amber'}}">{{d.labor_pct}}%</span></td>
          {% set diff = (d.labor_pct - (labor.labor_target|default(30.0)))|round(1) %}
          {% if diff > 0 %}
          <td style="color:var(--red);font-size:11px;font-weight:500">+{{diff}}% over</td>
          {% else %}
          <td style="color:var(--green);font-size:11px;font-weight:500">{{diff}}% under ✓</td>
          {% endif %}
        </tr>
        {% else %}
        <tr><td colspan="6" style="color:var(--ink3);font-style:italic;padding:10px">No overstaffed days — great work!</td></tr>
        {% endfor %}
        </tbody>
      </table></div>

      {% if labor.overtime_risk %}
      <div class="slabel" style="margin-top:14px">Overtime alerts</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Employee</th><th>Hours that week</th><th>Week</th><th>Status</th></tr></thead>
        <tbody>
        {% for emp in labor.overtime_risk %}
        <tr>
          <td style="font-weight:500">{{emp.employee}}</td>
          <td>{{emp.hours}}h</td>
          <td style="font-size:11px;color:var(--ink3)">{{emp.week}}</td>
          <td>
            {% if emp.status == "overtime" %}
              <span class="pill pill-red">Overtime — review pay</span>
            {% else %}
              <span class="pill pill-amber">Near limit</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
        </tbody>
      </table></div>
      {% endif %}
    </div>

    <div>
      <div class="slabel">Labor % by day of week</div>
      <div class="card" style="padding:16px">
        <div class="day-bars" id="day-bars"></div>
        <div style="display:flex;justify-content:space-around;font-size:9px;color:var(--ink3);margin-top:3px">
          {% for d in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"] %}<span>{{d}}</span>{% endfor %}
        </div>
        <div style="margin-top:8px;display:flex;gap:12px;font-size:10px;color:var(--ink3)">
          <span><span style="color:var(--red)">■</span> Over 32%</span>
          <span><span style="color:#ef9f27">■</span> 28–32%</span>
          <span><span style="color:#6fcf97">■</span> Under 28%</span>
        </div>
      </div>

      <div class="slabel" style="margin-top:14px">Understaffed days</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Date</th><th>Day</th><th>Sales</th><th>Labor %</th></tr></thead>
        <tbody>
        {% for d in labor.understaffed_days %}
        <tr>
          <td>{{d.date}}</td><td style="font-weight:500">{{d.day}}</td>
          <td>${{d.sales|int|format_num}}</td>
          <td><span class="pill pill-green">{{d.labor_pct}}%</span></td>
        </tr>
        {% else %}
        <tr><td colspan="4" style="color:var(--ink3);font-style:italic;padding:10px">None flagged</td></tr>
        {% endfor %}
        </tbody>
      </table></div>
    </div>
  </div>
  <!-- Labor trend chart -->
  <div class="slabel" style="margin-top:16px">Labor % trend — last 4 weeks</div>
  <div class="card" style="padding:16px">
    <div id="labor-trend-bars" style="display:flex;align-items:flex-end;gap:12px;height:80px;margin-bottom:6px">
      <div style="color:var(--ink3);font-size:12px;font-style:italic">Loading trend data…</div>
    </div>
    <div id="labor-trend-labels" style="display:flex;gap:12px;font-size:10px;color:var(--ink3)"></div>
    <div style="margin-top:8px;display:flex;gap:12px;font-size:10px;color:var(--ink3)">
      <span><span style="color:var(--red)">■</span> Over 32%</span>
      <span><span style="color:#ef9f27">■</span> 28–32%</span>
      <span><span style="color:#6fcf97">■</span> Under 28%</span>
      <span style="margin-left:auto;color:var(--ink3)">Target: 30%</span>
    </div>
  </div>
</div>

<!-- INVENTORY -->
<div class="panel {{'active' if not mod_reviews and not mod_labor and mod_inventory}}" id="panel-inventory">
  {% if not inv.is_live %}
  <div style="background:#fff8e6;border:1px solid #f0c040;border-radius:8px;padding:14px 16px;margin-bottom:16px">
    <div style="font-size:12px;color:#8a6a00;font-weight:600;margin-bottom:8px">⚠ Showing sample data — upload your inventory CSV to see your real numbers</div>
    <div style="font-size:11px;color:#8a6a00;margin-bottom:10px;line-height:1.6">
      <strong>How to export:</strong>&nbsp;
      <span>Toast: Inventory → Items → Export CSV</span> &nbsp;·&nbsp;
      <span>Square: Items → Inventory → Export</span> &nbsp;·&nbsp;
      <span>Other: Any CSV with item name, quantity, and unit cost works</span>
    </div>
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <label style="display:inline-flex;align-items:center;gap:8px;background:#2d6a4f;color:white;padding:7px 14px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer">
        📂 Upload inventory CSV
        <input type="file" accept=".csv" style="display:none" id="inventory-inline-input" onchange="clientUpload('inventory', this)">
      </label>
      <span id="inventory-inline-result" style="font-size:12px;color:#2d6a4f;display:none"></span>
    </div>
  </div>
  {% else %}
  <div style="background:#f0faf4;border:1px solid #a7d7b8;border-radius:6px;padding:8px 14px;margin-bottom:12px;font-size:12px;color:#2d6a4f;display:flex;align-items:center;gap:8px">
    <span>✓</span><span><strong>Showing your real data</strong> — upload a new CSV anytime to update.</span>
    <label style="margin-left:auto;display:inline-flex;align-items:center;gap:6px;background:#2d6a4f;color:white;padding:5px 12px;border-radius:5px;font-size:11px;font-weight:600;cursor:pointer">
      Update CSV <input type="file" accept=".csv" style="display:none" id="inventory-inline-input" onchange="clientUpload('inventory', this)">
    </label>
  </div>
  {% endif %}
  <div style="background:{{inv.banner_gradient}};border-radius:10px;padding:16px 20px;margin-bottom:14px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
    <div>
      <div style="font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#f4a4a4;margin-bottom:4px">Projected annual food waste cost</div>
      <div style="font-size:32px;font-weight:800;color:#f87171;letter-spacing:-1px">${{inv.annual_waste_projection|int|format_num}}</div>
      <div style="font-size:12px;color:#f4a4a4;margin-top:3px">Based on this week — ${{inv.monthly_waste_projection|int|format_num}}/mo projected</div>
    </div>
    <div style="text-align:right">
      <div style="font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:#a8d5b5;margin-bottom:4px">Recoverable with better ordering</div>
      <div style="font-size:28px;font-weight:800;color:#6fcf97;letter-spacing:-1px">${{inv.annual_recoverable|int|format_num}}<span style="font-size:14px;font-weight:600;color:#a8d5b5">/yr</span></div>
      <div style="font-size:12px;color:#a8d5b5;margin-top:3px">${{inv.recoverable_monthly|int|format_num}}/mo recoverable</div>
    </div>
  </div>
  <div class="stat-row">
    <div class="stat hi"><div class="stat-n">${{inv.total_waste_cost_week|format_num}}</div><div class="stat-l">Waste/week</div></div>
    <div class="stat hi"><div class="stat-n">${{inv.monthly_waste_projection|int|format_num}}</div><div class="stat-l">Projected/mo</div></div>
    <div class="stat {% if inv.waste_items|length == 0 %}ok{% else %}warn{% endif %}"><div class="stat-n">{{inv.waste_items|length}}</div><div class="stat-l">Waste items</div></div>
    <div class="stat {% if inv.critical_low|length == 0 %}ok{% else %}hi{% endif %}"><div class="stat-n">{{inv.critical_low|length}}</div><div class="stat-l">Critical low</div></div>
    <div class="stat"><div class="stat-n">${{inv.total_stock_value|int|format_num}}</div><div class="stat-l">Inventory value</div></div>
  </div>

  <div class="card" style="padding:14px 16px;margin-bottom:14px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:var(--ink3)">Waste rate vs industry benchmark</div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:13px;font-weight:700;color:{{inv.benchmark_color}}">{{inv.waste_rate_pct}}%</span>
        <span style="background:{{inv.benchmark_color}};color:white;font-size:10px;font-weight:700;padding:2px 8px;border-radius:20px;letter-spacing:.5px">{{inv.benchmark_label}}</span>
      </div>
    </div>
    <div style="position:relative;height:10px;background:var(--paper3);border-radius:5px;overflow:hidden">
      <div style="position:absolute;left:0;top:0;height:100%;width:{% if inv.waste_rate_pct > 0 %}{{[inv.waste_rate_pct * 5, 100]|min}}%{% else %}2px{% endif %};background:{{inv.benchmark_color}};border-radius:5px;transition:width .4s"></div>
      <div style="position:absolute;left:20%;top:-2px;height:14px;width:2px;background:#2d6a4f;opacity:.7" title="4% target"></div>
      <div style="position:absolute;left:25%;top:-2px;height:14px;width:2px;background:#6fcf97;opacity:.7" title="5% target"></div>
    </div>
    <div style="display:flex;justify-content:space-between;margin-top:5px;font-size:10px;color:var(--ink3)">
      <span>0%</span>
      <span style="color:#2d6a4f;font-weight:600">▲ 4-5% target</span>
      <span>{{inv.benchmark_detail}}</span>
      <span>20%+</span>
    </div>
  </div>

  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
    <div style="font-size:13px;font-weight:600;color:var(--ink)">
      Week of {{inv.week_start}} – {{inv.week_end}}
    </div>
    <div style="font-size:11px;color:var(--ink3)">
      Last updated: {{inv.last_updated}}
    </div>
  </div>
  <div class="insight"><div class="insight-lbl">Cavnar AI Food Cost Analysis</div><div class="insight-text insight-loading" id="inv-insight">Loading analysis…</div></div>

  <div class="slabel" style="margin-top:16px">Waste cost trend — last 6 weeks</div>
  <div class="card" style="padding:14px 16px 10px">
    <div id="inv-trend-bars" style="display:flex;align-items:flex-end;gap:10px;height:80px;margin-bottom:6px">
      <div style="color:var(--ink3);font-size:12px;font-style:italic">Loading trend data…</div>
    </div>
    <div id="inv-trend-labels" style="display:flex;gap:10px;font-size:10px;color:var(--ink3)"></div>
  </div>

  <div class="two-col">
    <div>
      <div class="slabel">Top waste offenders</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Item</th><th>Wasted</th><th>Cost</th><th>%</th></tr></thead>
        <tbody>{% for item in inv.waste_items %}<tr>
          <td><strong>{{item.item}}</strong></td><td>{{item.waste_last_week|int if item.waste_last_week % 1 == 0 else item.waste_last_week|round(1)}} {{item.unit}}</td>
          <td><span class="pill pill-red">${{"%.2f"|format(item.waste_cost)}}</span></td><td>{{item.waste_pct}}%</td>
        </tr>{% else %}<tr><td colspan="4" style="color:#2d6a4f;font-style:italic;padding:12px;text-align:center">
          ✓ No significant waste flagged this week — great job.
        </td></tr>{% endfor %}</tbody></table></div>
    </div>
    <div>
      <div class="slabel">Overstocked</div>
      <div class="card"><table class="tbl">
        <thead><tr><th>Item</th><th>Stock</th><th>Par</th><th>Tied-up $</th></tr></thead>
        <tbody>{% for item in inv.overstock %}<tr>
          <td><strong>{{item.item}}</strong></td>
          <td>{{item.current_stock|int}}{% if item.unit %} {{item.unit}}{% endif %}</td>
          <td>{{item.par_level|int}}{% if item.unit %} {{item.unit}}{% endif %}</td>
          <td><span class="pill pill-amber">${{"%.2f"|format(item.overstock_cost)}}</span></td>
        </tr>{% else %}<tr><td colspan="4" style="color:#2d6a4f;font-style:italic;padding:12px;text-align:center">✓ Nothing overstocked this week.</td></tr>{% endfor %}</tbody></table></div>
    </div>
  </div>

  <div class="slabel">Order list — recommended quantities</div>
  {% if inv.critical_low or inv.reorder_soon or inv.order_reduction %}
  <div class="card"><table class="tbl">
    <thead><tr><th>Item</th><th>Status</th><th>Order qty</th><th>Last order</th><th>Savings</th></tr></thead>
    <tbody>
    {% for item in inv.critical_low %}<tr>
      <td><strong>{{item.item}}</strong></td>
      <td><span class="pill pill-red">{{item.days_remaining}}d — urgent</span></td>
      <td><strong>{% if item.suggested_order_qty == 0 %}<span style="color:var(--ink3)">skip</span>{% else %}{{item.suggested_order_qty}}{% if item.unit %} {{item.unit}}{% endif %}{% endif %}</strong></td>
      <td style="color:var(--ink3)">{{item.last_order_qty|int}}</td>
      <td>{% if item.savings_vs_last > 0 %}<span style="color:#2d6a4f;font-weight:600">↓ ${{item.savings_vs_last}}</span>{% elif item.savings_vs_last < 0 %}<span style="color:var(--red)">↑ ${{"{:.2f}".format(item.savings_vs_last * -1)}}</span>{% else %}<span style="color:var(--ink3)">—</span>{% endif %}</td>
    </tr>{% endfor %}
    {% for item in inv.reorder_soon %}<tr>
      <td><strong>{{item.item}}</strong></td>
      <td><span class="pill pill-amber">{{item.days_remaining}}d — order soon</span></td>
      <td><strong>{% if item.suggested_order_qty == 0 %}<span style="color:var(--ink3)">skip</span>{% else %}{{item.suggested_order_qty}}{% if item.unit %} {{item.unit}}{% endif %}{% endif %}</strong></td>
      <td style="color:var(--ink3)">{{item.last_order_qty|int}}</td>
      <td>{% if item.savings_vs_last > 0 %}<span style="color:#2d6a4f;font-weight:600">↓ ${{item.savings_vs_last}}</span>{% elif item.savings_vs_last < 0 %}<span style="color:var(--red)">↑ ${{"{:.2f}".format(item.savings_vs_last * -1)}}</span>{% else %}<span style="color:var(--ink3)">—</span>{% endif %}</td>
    </tr>{% endfor %}
    {% for item in inv.order_reduction %}<tr>
      <td><strong>{{item.item}}</strong></td>
      <td><span class="pill" style="background:#e8f4f0;color:#2d6a4f">reduce order</span></td>
      <td><strong>{% if item.suggested_order_qty == 0 %}<span style="color:var(--ink3)">skip</span>{% else %}{{item.suggested_order_qty}}{% if item.unit %} {{item.unit}}{% endif %}{% endif %}</strong></td>
      <td style="color:var(--ink3)">{{item.last_order_qty|int}}</td>
      <td><span style="color:#2d6a4f;font-weight:600">↓ ${{item.savings_vs_last}}</span></td>
    </tr>{% endfor %}
    </tbody>
  </table></div>
  {% else %}
  <div class="card" style="padding:14px 16px;color:#2d6a4f;font-style:italic;font-size:13px">✓ Your ordering looks well-calibrated this week — no adjustments needed.</div>
  {% endif %}
</div>

<!-- MARKETING -->
<div class="panel {{'active' if not mod_reviews and not mod_labor and not mod_inventory and mod_marketing}}" id="panel-marketing">

  <!-- Instagram & Facebook connect — matches Google Business card style -->
  <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:14px">
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:12px">Instagram &amp; Facebook — Post directly from dashboard</div>
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
      <div>
        {% if restaurant.ig_token %}
        <div style="display:flex;align-items:center;gap:8px;font-size:13px">
          <span style="color:var(--green);font-size:16px">✓</span>
          <div>
            <div style="font-weight:600;color:var(--ink)">Instagram &amp; Facebook connected</div>
            <div style="font-size:11px;color:var(--ink3)">Generate content and post directly — no copy/paste</div>
          </div>
        </div>
        {% else %}
        <div>
          <div style="font-size:13px;font-weight:500;color:var(--ink);margin-bottom:2px">Connect Instagram &amp; Facebook</div>
          <div style="font-size:11px;color:var(--ink3)">Post generated content directly — no copy/paste needed</div>
        </div>
        {% endif %}
      </div>
      <div style="display:flex;gap:8px">
        {% if restaurant.ig_token %}
        <button onclick="disconnectInstagram()" class="btn btn-skip" style="font-size:11px">Disconnect</button>
        {% else %}
        <button onclick="igConnect()" class="btn btn-approve" style="font-size:12px;padding:7px 16px">
          Connect Instagram →
        </button>
        {% endif %}
      </div>
    </div>
  </div>

  <div style="background:#f0f4fa;border:1px solid #b3c5e0;border-radius:6px;padding:10px 14px;margin-bottom:10px;font-size:12px;color:#2d4a6a;line-height:1.6">
    <strong>How this works:</strong> Pick a content type, add a topic or occasion, and hit Generate.
    Copy the result straight to Instagram, your email tool, or Google Business Profile.
    The more specific your topic, the better the output.
  </div>
  <div style="background:#fdf8f4;border:1px solid var(--paper3);border-radius:6px;padding:8px 14px;margin-bottom:14px;font-size:12px;color:var(--ink3);line-height:1.5">
    The AI writes in your restaurant's voice. If something doesn't sound right or you want to update your brand voice,
    <a href="mailto:will@cavnar.ai?subject=Update my marketing voice profile — {{restaurant.name}}" style="color:var(--ember)">email Will</a> and it'll be updated within one business day.
  </div>
  <div class="slabel">Content type</div>
  <div class="ct-grid">{% for ct in ctypes %}
    <div class="ct-btn {{'selected' if loop.first}}" data-type="{{ct.id}}" onclick="selectCt('{{ct.id}}',this)">
      <div class="ct-label">{{ct.label}}</div><div class="ct-desc">{{ct.description}}</div>
    </div>{% endfor %}
  </div>
  <div class="topic-row">
    <input class="topic-input" id="mktopic" placeholder="Topic or occasion — e.g. new spring menu, Sunday brunch special…">
    <button class="btn-primary" onclick="genContent()">Generate ↗</button>
  </div>
  <div class="output-box" id="mkoutput" style="color:var(--ink3);font-style:italic">Select a type and click Generate.</div>
  <div id="sms-counter" style="display:none;font-size:11px;margin-top:4px;color:var(--ink3)">
    <span id="sms-char-count">0</span>/160 characters
    <span id="sms-over" style="color:var(--red);display:none"> — over limit, trim before sending</span>
  </div>
  <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
    <button class="btn-secondary" onclick="navigator.clipboard.writeText(document.getElementById('mkoutput').textContent).then(()=>toast('Copied'))">Copy</button>
    <button class="btn-secondary" onclick="genContent()">Regenerate</button>
    <button class="btn-primary" id="ig-post-btn" onclick="postToInstagram()" style="display:none">Post to Instagram ↗</button>
    <button class="btn-primary" id="fb-post-btn" onclick="postToFacebook()" style="display:none;background:#1877f2">Post to Facebook ↗</button>
  </div>

  <div style="margin-top:24px;background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
      <div class="slabel" style="margin:0">Content calendar</div>
      <div style="display:flex;gap:6px">
        <button class="btn-secondary" style="font-size:10px;padding:5px 10px" onclick="loadCal()">Generate week ↗</button>
        <button class="btn-secondary" style="font-size:10px;padding:5px 10px" id="cal-download-btn" onclick="downloadCal()" style="display:none">Download CSV ↓</button>
      </div>
    </div>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
      <span id="cal-week-range" style="font-size:11px;color:var(--ink3);font-weight:500"></span>
    </div>
    <div class="cal-grid" id="cal-grid"><div class="no-data" style="grid-column:1/-1;padding:20px">Click "Generate week" for content ideas.</div></div>
  </div>

</div>

<!-- COMPETITOR INTEL -->
<div class="panel" id="panel-competitor">
  <div style="background:linear-gradient(135deg,#0d1b2a,#1a2d40);border-radius:var(--r);padding:20px 24px;margin-bottom:20px;border:1px solid #1e3a52">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div>
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#4a9eca;margin-bottom:6px">Cavnar AI · Competitor Intelligence</div>
        <div style="font-family:'DM Serif Display',serif;font-size:20px;color:#e8f4fd">What your neighbors are doing</div>
      </div>

    </div>
  </div>

  {% if competitor_data %}
    <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:18px 20px;margin-bottom:16px" id="intel-insight-card">
      {{ competitor_data.insight | format_intel }}
    </div>
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:10px">Nearby competitors</div>
    <div style="display:flex;flex-direction:column;gap:10px" id="comp-cards-static">
      {% for c in competitor_data.competitors %}
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:14px 16px">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
          <div style="font-weight:600;font-size:13px">{{c.name}}</div>
          <div style="font-size:12px;color:#f59e0b">{{c.rating}}★ <span style="color:var(--ink3)">{{c.review_count}} reviews</span></div>
        </div>
        <div style="font-size:11px;color:var(--ink3);margin-bottom:8px">{{c.vicinity}}</div>
        {% for r in c.reviews[:2] %}
        <div style="font-size:11px;color:var(--ink3);padding:6px 0;border-top:1px solid var(--paper3);line-height:1.5">
          <span style="color:{{'#16a34a' if r.rating >= 4 else '#dc2626'}}">★</span>
          {{r.text[:120]}}{{'...' if r.text|length > 120 else ''}}
        </div>
        {% endfor %}
      </div>
      {% endfor %}
    </div>
    {% if competitor_updated_at %}
    {% set d = competitor_updated_at[:10].split('-') %}
    <div style="font-size:11px;color:var(--ink3);margin-top:12px">Last updated: {{d[1]|int}}/{{d[2]|int}}/{{d[0][2:]}}</div>
    {% endif %}
  {% else %}
  <div style="background:var(--paper2);border-radius:var(--r);padding:24px;text-align:center">
    <div style="font-size:13px;color:var(--ink3);margin-bottom:12px">No competitor data yet. Click Refresh to analyze your neighborhood.</div>
    <button onclick="refreshCompetitorIntel(this)" style="background:#4a9eca;color:white;border:none;padding:9px 20px;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;cursor:pointer">Fetch competitor data</button>
  </div>
  {% endif %}
  <div id="comp-refresh-result" style="margin-top:12px;font-size:12px;display:none"></div>
</div>


<!-- ACCOUNT -->
<div class="panel {{'active' if not mod_reviews and not mod_labor and not mod_inventory and not mod_marketing}}" id="panel-account">

  <!-- Hero consultant banner -->
  <div style="background:var(--ink);border-radius:var(--r);padding:20px 24px;margin-bottom:24px;display:flex;align-items:center;gap:20px" class="acct-hero">
    <div style="width:60px;height:60px;border-radius:50%;flex-shrink:0;box-shadow:0 0 0 3px #c84b2f55,0 0 20px 6px #c84b2f22;overflow:hidden;background:#1a1410">
      <img src="/static/will.png" style="width:100%;height:110%;object-fit:cover;object-position:center 20%">
    </div>
    <div style="flex:1">
      <div style="font-family:'DM Serif Display',serif;font-size:18px;color:var(--paper);margin-bottom:2px">Will Cavnar</div>
      <div style="font-size:12px;color:#7a736a;margin-bottom:10px">Founder, Cavnar AI · Your dedicated restaurant intelligence consultant</div>
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <a href="mailto:will@cavnar.ai" style="font-size:12px;color:var(--ember);text-decoration:none;font-weight:600">will@cavnar.ai</a>
        <span style="color:#2a2520">·</span>
        <span style="font-size:12px;color:#5a5450">Same-day response</span>
        <span style="color:#2a2520">·</span>
        <a href="https://calendly.com/will-cavnar/30min" target="_blank" style="font-size:12px;color:#7a736a;text-decoration:none">Book a call</a>
      </div>
    </div>
    <div style="text-align:right;flex-shrink:0" class="acct-hero-right">
      <div style="font-size:10px;color:#4a4540;text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px">Restaurant</div>
      <div style="font-size:14px;font-weight:600;color:var(--paper)">{{restaurant.name}}</div>
    </div>
  </div>

  <!-- Three column top row -->
  <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:14px" class="account-top-row">

    <!-- Account info -->
    <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:12px">Account</div>
      <table style="font-size:13px;width:100%">
        <tr><td style="color:var(--ink3);padding:4px 0;width:80px">Email</td><td style="font-weight:500;font-size:12px">{{restaurant.owner_email}}</td></tr>
        <tr><td style="color:var(--ink3);padding:4px 0">Username</td><td style="font-weight:500">{{current_user.username}}</td></tr>
      </table>
    </div>

    <!-- Billing -->
    <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px" id="billing-card">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:12px">Billing</div>
      <div id="billing-loading" style="font-size:13px;color:var(--ink3)">Loading…</div>
      <div id="billing-content" style="display:none">
        <div style="margin-bottom:8px">
          <div style="font-size:10px;color:var(--ink3);margin-bottom:2px">Next charge</div>
          <div style="font-size:15px;font-weight:600;color:var(--ink)" id="billing-next-prominent">—</div>
        </div>
        <div style="font-size:10px;color:var(--ink3);margin-bottom:2px">Amount</div>
        <div style="font-size:15px;font-weight:600;color:var(--ink);margin-bottom:8px" id="billing-amount-prominent">—</div>
        <div style="font-size:12px;color:var(--ink3);margin-bottom:4px" id="billing-status"></div>
        <div style="font-size:12px;color:var(--ink3);margin-bottom:10px" id="billing-pm"></div>
        <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
          <a id="billing-portal-link" href="#" target="_blank" style="font-size:12px;color:var(--ember);text-decoration:none;font-weight:600">Manage payment →</a>
          <a id="billing-invoice-link" href="#" target="_blank" style="font-size:12px;color:var(--ink3);text-decoration:none;font-weight:500">View invoice history →</a>
        </div>
      </div>
      <div id="billing-no-sub" style="display:none;font-size:12px;color:var(--ink3)">
        No active subscription. <a href="mailto:will@cavnar.ai" style="color:var(--ember)">Contact Will</a>
      </div>
    </div>

    <!-- Setup status -->
    <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:12px">Setup status</div>
      {% if mod_reviews %}
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
        {% if restaurant.reviews_live %}<span style="color:var(--green)">✓</span><span>Reviews live</span>
        {% else %}<span style="color:#ef9f27">○</span><span style="color:var(--ink3)">Reviews pending</span>{% endif %}
      </div>
      {% endif %}
      {% if mod_labor %}
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
        {% if labor.is_live %}<span style="color:var(--green)">✓</span><span>Labor live</span>
        {% else %}<span style="color:#ef9f27">○</span><span style="color:var(--ink3)">Labor pending</span>{% endif %}
      </div>
      {% endif %}
      {% if mod_inventory %}
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
        {% if inv.is_live %}<span style="color:var(--green)">✓</span><span>Inventory live</span>
        {% else %}<span style="color:#ef9f27">○</span><span style="color:var(--ink3)">Inventory pending</span>{% endif %}
      </div>
      {% endif %}
      {% if mod_marketing %}
      <div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">
        <span style="color:var(--green)">✓</span><span>Marketing ready</span>
      </div>
      {% endif %}
    </div>
  </div>

  <!-- Google Business Connect -->
  {% if mod_reviews %}
  <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:14px">
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:12px">Google Business — Auto-post replies</div>
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px">
      <div>
        {% if restaurant.gmb_refresh_token %}
        <div style="display:flex;align-items:center;gap:8px;font-size:13px">
          <span style="color:var(--green);font-size:16px">✓</span>
          <div>
            <div style="font-weight:600;color:var(--ink)">Google Business connected</div>
            <div style="font-size:11px;color:var(--ink3)">Approved responses post automatically to Google</div>
          </div>
        </div>
        {% else %}
        <div>
          <div style="font-size:13px;font-weight:500;color:var(--ink);margin-bottom:2px">Connect Google Business</div>
          <div style="font-size:11px;color:var(--ink3)">Approved responses will auto-post — no more copy/paste</div>
        </div>
        {% endif %}
      </div>
      <div style="display:flex;gap:8px">
        {% if restaurant.gmb_refresh_token %}
        <button onclick="gmbDisconnect()" class="btn btn-skip" style="font-size:11px">Disconnect</button>
        {% else %}
        <button onclick="gmbConnect()" class="btn btn-approve" style="font-size:12px;padding:7px 16px">
          Connect Google Business →
        </button>
        {% endif %}
      </div>
    </div>
  </div>
  {% endif %}

  <!-- Two column second row -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px" class="account-two-col">

    <!-- What's included -->
    <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:12px">What's included</div>
      {% if mod_reviews %}
      <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--paper3)">
        <span style="font-size:14px">⭐</span>
        <div style="flex:1">
          <div style="font-size:12px;font-weight:600">Review Intelligence</div>
          <div style="font-size:11px;color:var(--ink3)">AI drafts responses — you approve</div>
        </div>
        <span style="font-size:10px;font-weight:600;color:var(--green);background:var(--green-bg);padding:2px 7px;border-radius:20px">Active</span>
      </div>
      {% endif %}
      {% if mod_labor %}
      <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--paper3)">
        <span style="font-size:14px">📊</span>
        <div style="flex:1">
          <div style="font-size:12px;font-weight:600">Labor Optimizer</div>
          <div style="font-size:11px;color:var(--ink3)">Weekly labor cost analysis</div>
        </div>
        <span style="font-size:10px;font-weight:600;color:var(--green);background:var(--green-bg);padding:2px 7px;border-radius:20px">Active</span>
      </div>
      {% endif %}
      {% if mod_inventory %}
      <div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--paper3)">
        <span style="font-size:14px">📦</span>
        <div style="flex:1">
          <div style="font-size:12px;font-weight:600">Inventory Control</div>
          <div style="font-size:11px;color:var(--ink3)">Food cost & waste analysis</div>
        </div>
        <span style="font-size:10px;font-weight:600;color:var(--green);background:var(--green-bg);padding:2px 7px;border-radius:20px">Active</span>
      </div>
      {% endif %}
      {% if mod_marketing %}
      <div style="display:flex;align-items:center;gap:10px;padding:6px 0">
        <span style="font-size:14px">📣</span>
        <div style="flex:1">
          <div style="font-size:12px;font-weight:600">Marketing Autopilot</div>
          <div style="font-size:11px;color:var(--ink3)">AI content in your voice</div>
        </div>
        <span style="font-size:10px;font-weight:600;color:var(--green);background:var(--green-bg);padding:2px 7px;border-radius:20px">Active</span>
      </div>
      {% endif %}
      {% if not mod_reviews and not mod_labor and not mod_inventory and not mod_marketing %}
      <div style="font-size:12px;color:var(--ink3)">No modules active. Contact Will.</div>
      {% endif %}
    </div>

    <!-- Change password + digest -->
    <div style="display:flex;flex-direction:column;gap:14px">
      {% if mod_reviews %}
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:10px">Weekly digest email</div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <select id="digest-day-select" style="padding:6px 8px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px">
            {% for d in ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"] %}
            <option value="{{d}}" {{"selected" if restaurant.digest_day==d}}>{{d|title}}</option>
            {% endfor %}
          </select>
          <select id="digest-enabled-select" style="padding:6px 8px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px">
            <option value="1" {{"selected" if restaurant.digest_enabled}}>Enabled</option>
            <option value="0" {{"selected" if not restaurant.digest_enabled}}>Disabled</option>
          </select>
          <button onclick="saveDigestDay()" style="padding:6px 14px;background:var(--ember);color:white;border:none;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:600;cursor:pointer">Save</button>
          <span id="digest-save-status" style="font-size:11px;display:none"></span>
        </div>
      </div>
      {% endif %}
      <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:10px">Change password</div>
        <div style="display:flex;flex-direction:column;gap:8px">
          <input class="form-input" type="password" id="pw-current" placeholder="Current password" style="font-size:12px">
          <input class="form-input" type="password" id="pw-new" placeholder="New password (min 8 chars)" style="font-size:12px">
          <input class="form-input" type="password" id="pw-confirm" placeholder="Confirm new password" style="font-size:12px">
          <button class="btn-primary" onclick="changePassword()" style="font-size:12px;padding:8px 16px;width:fit-content">Update password</button>
          <div id="pw-status" style="font-size:11px;margin-top:2px;display:none"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Full width bottom row -->
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px" class="account-two-col">

    <!-- Support -->
    <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:10px">Support</div>
      <p style="font-size:13px;color:var(--ink2);line-height:1.6;margin-bottom:12px">Questions, data requests, or anything not working — reach out directly.</p>
      <a href="mailto:will@cavnar.ai" style="display:inline-block;background:var(--ember);color:white;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:12px;font-weight:600">Email Will →</a>
    </div>

    <!-- Refer a restaurant -->
    <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:10px">Refer a restaurant</div>
      <div style="font-size:12px;color:var(--ink2);line-height:1.6;margin-bottom:10px">Know another owner? Send an intro — get <strong>one free month ($300)</strong> if they sign up.</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <input type="text" id="referral-name" placeholder="Restaurant name" style="flex:1;padding:7px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;outline:none">
          <input type="email" id="referral-email" placeholder="Owner email" style="flex:1;padding:7px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;outline:none">
        </div>
        <textarea id="referral-note" rows="2" placeholder="Optional personal note…" style="padding:7px 10px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;outline:none;resize:vertical"></textarea>
        <div style="display:flex;align-items:center;gap:8px">
          <button onclick="sendReferral()" style="background:var(--ember);color:white;border:none;padding:7px 16px;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:600;cursor:pointer">Send referral</button>
          <span id="referral-status" style="font-size:11px;display:none"></span>
        </div>
      </div>
    </div>
  </div>

  <div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:16px;margin-bottom:14px">
    <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:10px">Cancel subscription</div>
    <p style="font-size:13px;color:var(--ink2);line-height:1.6;margin-bottom:12px">No cancellation fees. Cancel before your next billing date to avoid the next charge.</p>
    <a href="mailto:will@cavnar.ai?subject=Cancel%20my%20Cavnar%20AI%20subscription&body=Hi%20Will%2C%0A%0AI%20would%20like%20to%20cancel%20my%20Cavnar%20AI%20subscription%20for%20{{restaurant.name}}.%0A%0APer%20the%2030-day%20notice%20policy%2C%20I%20understand%20my%20account%20will%20remain%20active%20through%20the%20end%20of%20my%20current%20billing%20period%20and%20for%2030%20days%20after%20this%20notice." style="display:inline-block;padding:8px 16px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:500;border:1px solid var(--paper3);color:var(--ink3)">Request cancellation</a>
    <p style="font-size:11px;color:var(--ink3);margin-top:6px">Cancellations require 30 days written notice. Your account stays active through the end of your current billing period.</p>
  </div>

</div>
<div class="toast" id="toast"></div>

<script>
function openEditor(id) {
  document.getElementById('editor-'+id).style.display='block';
  document.getElementById('draft-actions-'+id).style.display='none';
}
function closeEditor(id) {
  document.getElementById('editor-'+id).style.display='none';
  document.getElementById('draft-actions-'+id).style.display='flex';
}
function regenDraft(id) {
  var txtEl = document.getElementById('draft-txt-'+id);
  var editorEl = document.getElementById('editor-text-'+id);
  if (txtEl) txtEl.textContent = 'Generating new response…';
  fetch('/api/regenerate-draft/'+id, {method:'POST'}).then(function(r){return r.json();}).then(function(data){
    if (data.ok) {
      if (txtEl) txtEl.textContent = data.draft;
      if (editorEl) editorEl.value = data.draft;
      document.getElementById('draft-actions-'+id).innerHTML =
        '<button class="btn btn-approve" onclick="approveR('+id+')">✓ Approve</button>' +
        '<button class="btn btn-skip" onclick="skipR('+id+')">Skip</button>';
      document.getElementById('draft-actions-'+id).style.display='flex';
      document.getElementById('editor-'+id).style.display='none';
      toast('New draft generated');
    } else {
      if (txtEl) txtEl.textContent = 'Error generating — try again';
      toast('Error: ' + (data.error||'unknown'));
    }
  }).catch(function(){ toast('Network error — try again'); });
}
function saveDraft(id) {
  var editorEl = document.getElementById('editor-text-'+id);
  var draft = editorEl ? editorEl.value.trim() : '';
  if (!draft) { toast('Response cannot be empty'); return; }
  fetch('/api/save-draft/'+id, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({draft:draft})
  }).then(function(r){return r.json();}).then(function(sd){
    if (!sd.ok) { toast('Save failed'); return; }
    fetch('/approve/'+id, {method:'POST'}).then(function(r){return r.json();}).then(function(ad){
      if (ad.ok) {
        var txtEl = document.getElementById('draft-txt-'+id);
        if (txtEl) txtEl.textContent = draft;
        document.getElementById('editor-'+id).style.display='none';
        document.getElementById('draft-actions-'+id).innerHTML =
          '<span class="btn btn-approved">✓ Approved</span>';
        document.getElementById('draft-actions-'+id).style.display='flex';
        toast('Response saved and approved');
      }
    });
  }).catch(function(){ toast('Network error — try again'); });
}
// Global CSRF-aware fetch wrapper
const _csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
const _fetch = (url, opts={}) => {
  opts.headers = opts.headers || {};
  if(opts.method && opts.method !== 'GET') opts.headers['X-CSRF-Token'] = _csrf;
  return fetch(url, opts);
};

function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2600)}
function disconnectInstagram(){
  if(!confirm('Disconnect Instagram & Facebook? You will need to reconnect to post directly.')) return;
  fetch('/api/instagram-disconnect',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){ toast('Instagram & Facebook disconnected'); setTimeout(()=>location.reload(),800); }
    else { toast('Error disconnecting — try again'); }
  });
}

function igConnect(){
  const popup = window.open('/instagram/connect','ig_connect','width=600,height=700,left=200,top=100');
  window.addEventListener('message', function handler(e){
    if(!e.data || !e.data.ig) return;
    window.removeEventListener('message', handler);
    if(e.data.ig === 'connected'){
      toast('Instagram & Facebook connected ✓');
      setTimeout(()=>location.reload(), 1000);
    } else {
      toast('Connection failed: ' + (e.data.msg || 'unknown error'));
    }
  });
}

function gmbConnect(){
  const popup = window.open('/auth/google/connect','gmb_connect','width=500,height=600,left=200,top=100');
  window.addEventListener('message', function handler(e){
    if(!e.data || !e.data.gmb) return;
    window.removeEventListener('message', handler);
    if(e.data.gmb === 'connected'){
      toast('Google Business connected ✓');
      setTimeout(()=>location.reload(), 1000);
    } else {
      toast('Connection failed: ' + (e.data.msg || 'unknown error'));
    }
  });
}

function gmbDisconnect(){
  if(!confirm('Disconnect Google Business? Responses will no longer auto-post.')) return;
  fetch('/auth/google/disconnect',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){ toast('Google Business disconnected'); setTimeout(()=>location.reload(),800); }
  });
}

function loadReviewInsight(){
  reviewInsightLoaded=true;
  fetch('/api/review-insight').then(function(r){return r.json();}).then(function(d){
    var el=document.getElementById('review-insight');
    if(!el)return;
    el.innerHTML=d.insight||'Analysis unavailable.';
    el.classList.remove('insight-loading');
  }).catch(function(){
    var el=document.getElementById('review-insight');
    if(el){el.textContent='Analysis unavailable — check back shortly.';el.classList.remove('insight-loading');}
  });
}
function switchTab(n,btn){
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('panel-'+n).classList.add('active');btn.classList.add('active');
  if(n==='reviews'&&!reviewInsightLoaded){loadReviewInsight();}
  if(n==='labor'&&!laborLoaded){loadLaborInsight();}
  if(n==='inventory'&&!invLoaded)loadInvInsight();
  if(n==='labor'){renderBars();loadLaborTrend();}
  if(n==='account')loadBillingInfo();
  history.replaceState(null,null,'#'+n);
  fetch('/api/log-activity',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tab:n})});
}
// Restore active tab from hash on page load
document.addEventListener('DOMContentLoaded', function(){
  var hash = window.location.hash.replace('#','');
  var btn = hash ? document.getElementById('tab-'+hash) : null;
  if(btn){ switchTab(hash, btn); }
  else if(document.getElementById('panel-reviews') && document.getElementById('panel-reviews').classList.contains('active')){
    loadReviewInsight();
  }
});
let rfilter='{{rfilter}}';
function setRF(f,btn){rfilter=f;document.querySelectorAll('.fpill').forEach(p=>p.classList.remove('active','active-red'));btn.classList.add(f==='urgent'?'active-red':'active');filterReviews()}
function filterReviews(){const q=document.getElementById('rsearch').value;window.location='/?filter='+rfilter+'&search='+encodeURIComponent(q)}
function approveR(id){fetch('/approve/'+id,{method:'POST'}).then(r=>r.json()).then(d=>{
  if(d.ok){
    const card = document.getElementById('rc-'+id);
    card.classList.add('approved');
    card.classList.remove('urgent');
    if(d.auto_posted){
      document.querySelector('#rc-'+id+' .draft-actions').innerHTML='<span style="font-size:11px;color:var(--green);font-weight:500">✓ Posted to Google</span>';
      toast('Response approved and posted to Google ✓ — now live');
    } else {
      const _plat = document.getElementById('rc-'+id) ? document.getElementById('rc-'+id).dataset.platform : '';
      const _markBtn = _plat === 'yelp'
        ? '<button class="btn" style="background:#e8f0fe;color:#1a56cc;border:1px solid #c5d8f8;font-size:11px;margin-left:6px" onclick="markPosted('+id+',this)">📋 Copy &amp; open Yelp</button>'
        : '<button class="btn" style="background:#e8f0fe;color:#1a56cc;border:1px solid #c5d8f8;font-size:11px;margin-left:6px" onclick="markPosted('+id+',this)">Mark as posted</button>';
      document.querySelector('#rc-'+id+' .draft-actions').innerHTML='<span class="btn btn-approved">✓ Approved</span>'+_markBtn;
      const platform = card.dataset.platform || 'google';
      if(platform === 'google'){
        document.querySelector('#rc-'+id+' .draft-actions').innerHTML=
          '<span class="btn btn-approved">✓ Approved</span>' +
          '<span style="font-size:11px;color:var(--ink3);margin-left:6px">Saved — will post to Google when GBP is connected</span>';
        toast('Response approved and saved ✓');
      } else {
        toast('Response approved — copy and post to ' + platform + ' manually');
      }
    }
  }
})}

function markPosted(id, btn){
  // For Yelp reviews: copy response to clipboard and open Yelp
  const card = document.getElementById('rc-'+id);
  const platform = card ? card.dataset.platform : '';
  const draftEl = document.getElementById('draft-txt-'+id);
  const draftText = draftEl ? draftEl.textContent.trim() : '';

  if(platform === 'yelp'){
    if(draftText){
      navigator.clipboard.writeText(draftText).then(function(){
        toast('Response copied — opening Yelp to paste it ✓');
      }).catch(function(){ toast('Opening Yelp...'); });
    }
    // Open Yelp business owner portal so they can paste and respond
    window.open('https://business.yelp.com', '_blank');
  }

  fetch('/api/mark-posted/'+id,{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){
      const actions = document.getElementById('draft-actions-'+id);
      if(actions) actions.innerHTML='<span style="font-size:11px;color:var(--green);font-weight:500">✓ Posted</span>';
      if(platform !== 'yelp') toast('Marked as posted');
    }
  });
}

function skipR(id){
  fetch('/skip/'+id,{method:'POST'}).then(function(r){return r.json();}).then(function(d){
    if(d.ok){
      var actions = document.getElementById('draft-actions-'+id);
      if(actions) {
        actions.innerHTML =
          '<button class="btn btn-approve" onclick="approveR('+id+')">✓ Approve</button>' +
          '<button class="btn btn-skip" onclick="openEditor('+id+')">Edit response</button>' +
          '<button class="btn btn-skip" onclick="regenDraft('+id+')">↻ Regenerate</button>';
        actions.style.display='flex';
      }
      toast('Skipped — edit or regenerate below');
    }
  });
}
const dowData={{labor.dow_summary|tojson}};
const laborDateRange={{labor.date_range|tojson if labor.date_range else 'null'}};
(function(){
  const elPeriod=document.getElementById('labor-period');
  if(!elPeriod||!laborDateRange||!laborDateRange.start)return;
  function fmt(d){
    const p=d.split('-');
    return parseInt(p[1])+'/'+parseInt(p[2])+'/'+p[0].slice(2);
  }
  elPeriod.textContent='Data: '+fmt(laborDateRange.start)+' — '+fmt(laborDateRange.end)+' ('+laborDateRange.days+' days)';
})();
function renderBars(){
  const days=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
  const vals=days.map(d=>dowData[d]||0);
  const filled=vals.filter(v=>v>0);
  if(!filled.length)return;
  const dataMin=Math.max(0,Math.min(...filled)-8);
  const dataMax=Math.max(...filled)+5;
  const range=dataMax-dataMin||1;
  const c=document.getElementById('day-bars');
  if(!c)return;
  const maxH=72;
  c.innerHTML=days.map(d=>{
    const pct=dowData[d]||0;
    const h=pct>0?Math.max(6,Math.round(((pct-dataMin)/range)*maxH)):0;
    const col=pct>32?'var(--red)':pct>=28?'#ef9f27':'#6fcf97';
    const lbl=pct>0?pct+'%':'';
    return`<div class="day-bar-wrap" style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:2px">
      <span style="font-size:9px;color:${col};font-weight:600;line-height:1">${lbl}</span>
      <div style="width:75%;height:${h}px;background:${col};border-radius:3px 3px 0 0" title="${d}: ${pct}%"></div>
    </div>`;
  }).join('');
}
let laborLoaded=false,invLoaded=false,reviewInsightLoaded=false;
function loadLaborInsight(){
  laborLoaded=true;
  fetch('/api/labor-insight').then(r=>r.json()).then(d=>{
    const elLaborInsight=document.getElementById('labor-insight');
    elLaborInsight.innerHTML=d.insight||'Analysis unavailable.';
    elLaborInsight.classList.remove('insight-loading');
  }).catch(e=>{
    const elLaborErr=document.getElementById('labor-insight');
    elLaborErr.textContent='Analysis unavailable — check back shortly.';
    elLaborErr.classList.remove('insight-loading');
    const elLaborErr2=document.getElementById('labor-insight');
    elLaborErr2.textContent='Analysis unavailable — check back shortly.';
    elLaborErr2.classList.remove('insight-loading');
  });
  // Load dollar gap
  fetch('/api/labor-gap').then(r=>r.json()).then(d=>{
    const gapEl = document.getElementById('gap-amount');
    const msgEl = document.getElementById('gap-dollar');
    const pctEl = document.getElementById('gap-current-pct');
    const target = {{labor_target|default(30.0)}};
    if(!d || d.ok === false) {
      gapEl.textContent = '—';
      msgEl.textContent = 'Unable to calculate gap. Upload shift data to see this.';
      return;
    }
    if(d.over_target && d.monthly_gap > 0) {
      gapEl.textContent = '$' + Math.round(d.monthly_gap).toLocaleString();
      gapEl.style.color = 'var(--ember2)';
      msgEl.textContent = 'You are at ' + d.current_pct + '% vs your ' + target + '% target \u2014 that gap is costing around $' + Math.round(d.monthly_gap).toLocaleString() + '/mo. An optimized schedule can help close it.';
      pctEl.style.color = '#ef9f27';
    } else {
      gapEl.textContent = 'On target ✓';
      gapEl.style.color = '#6fcf97';
      msgEl.textContent = 'Your labor is at ' + d.current_pct + '% \u2014 at or below your ' + target + '% target. Great work. Keep an eye on individual days that spike.';
      pctEl.style.color = '#6fcf97';
    }
  }).catch(e=>{
    document.getElementById('gap-amount').textContent='—';
    document.getElementById('gap-dollar').textContent='Unable to load gap data.';
  });
}
async function downloadSchedule(btn) {
  btn.textContent = 'Generating… (30 sec)';
  btn.disabled = true;
  try {
    const res = await fetch('/api/download-schedule');
    const contentType = res.headers.get('content-type') || '';
    if(contentType.includes('json')) {
      const data = await res.json();
      btn.textContent = data.error || 'Error — try again';
      setTimeout(()=>{btn.textContent='Download optimized schedule ↓';btn.disabled=false;},4000);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'optimized_schedule.csv';
    a.click();
    btn.textContent = '✓ Downloaded';
    setTimeout(()=>{btn.textContent='Download optimized schedule ↓';btn.disabled=false;},3000);
  } catch(e) {
    btn.textContent = 'Error — try again';
    setTimeout(()=>{btn.textContent='Download optimized schedule ↓';btn.disabled=false;},4000);
  }
}
function loadInvInsight(){
  invLoaded=true;
  fetch('/api/inv-insight').then(function(r){return r.json();}).then(function(d){
    var elInvInsight=document.getElementById('inv-insight');
    elInvInsight.innerHTML=d.insight||'Analysis unavailable.';
    elInvInsight.classList.remove('insight-loading');
  }).catch(function(){
    var elInvErr=document.getElementById('inv-insight');
    elInvErr.textContent='Analysis unavailable — check back shortly.';
    elInvErr.classList.remove('insight-loading');
  });
  loadInvTrend();
}
function loadInvTrend(){
  var container=document.getElementById('inv-trend-bars');
  var labels=document.getElementById('inv-trend-labels');
  if(!container)return;
  fetch('/api/inv-trend').then(function(r){return r.json();}).then(function(data){
    if(!data.weeks||!data.weeks.length){
      container.innerHTML='<div style="color:var(--ink3);font-size:12px;font-style:italic">No history yet — waste trend will appear after your first upload.</div>';
      if(labels)labels.innerHTML='';
      return;
    }
    var maxWaste=0;
    for(var i=0;i<data.weeks.length;i++){if(data.weeks[i].waste>maxWaste)maxWaste=data.weeks[i].waste;}
    maxWaste=Math.max(maxWaste,50);
    var html='';
    var lblHtml='';
    var avg=0;
    for(var i=0;i<data.weeks.length;i++){avg+=data.weeks[i].waste;}
    avg=data.weeks.length>0?avg/data.weeks.length:0;
    for(var i=0;i<data.weeks.length;i++){
      var w=data.weeks[i];
      var h=Math.max(6,Math.round((w.waste/maxWaste)*72));
      var isLast=(i===data.weeks.length-1);
      var col=w.waste>avg*1.15?'var(--red)':w.waste<avg*0.85?'#6fcf97':'#ef9f27';
      if(isLast)col='var(--ember)';
      html+='<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:2px">';
      html+='<span style="font-size:10px;color:'+col+';font-weight:600">$'+w.waste.toFixed(0)+'</span>';
      html+='<div style="width:80%;height:'+h+'px;background:'+col+';border-radius:3px 3px 0 0;opacity:'+(isLast?'1':'0.75')+'" title="Week of '+w.week_end+': $'+w.waste+'"></div>';
      html+='</div>';
      lblHtml+='<span style="flex:1;text-align:center">'+w.label+'</span>';
    }
    container.innerHTML=html;
    if(labels)labels.innerHTML=lblHtml;
  }).catch(function(){
    if(container)container.innerHTML='<div style="color:var(--ink3);font-size:12px;font-style:italic">Trend data unavailable.</div>';
  });
}
let selCt='{{ctypes[0].id if ctypes}}';
function selectCt(id,el){selCt=id;document.querySelectorAll('.ct-btn').forEach(b=>b.classList.remove('selected'));el.classList.add('selected')}
function genContent(fromCalendar){
  const topic=document.getElementById('mktopic').value.trim();
  if(!topic){toast('Enter a topic');return;}
  const box=document.getElementById('mkoutput');
  box.style.fontStyle='italic';
  box.style.color='var(--ink3)';
  box.textContent='Generating…';
  fetch('/api/generate-content',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type:selCt,topic,from_calendar:!!fromCalendar})})
    .then(r=>r.json())
    .then(d=>{
      box.style.fontStyle='normal';
      box.style.color='var(--ink2)';
      box.textContent=d.content||'Generation failed — try again.';
      const isSms=document.querySelector('.ct-btn.selected')?.dataset?.type==='loyalty_nudge';
      const counter=document.getElementById('sms-counter');
      if(isSms){
        const charCount=(d.content||'').length;
        document.getElementById('sms-char-count').textContent=charCount;
        document.getElementById('sms-over').style.display=charCount>160?'inline':'none';
        counter.style.display='block';
      } else { counter.style.display='none'; }
    })
    .catch(e=>{
      box.style.color='var(--red)';
      box.style.fontStyle='italic';
      box.textContent='Content generation unavailable — check back shortly.';
    });
}
function downloadCal(){
  const ideas = window._calIdeas;
  if(!ideas || !ideas.length){ toast('Generate the calendar first'); return; }
  const rows = [['Day','Platform','Content Idea','Type']];
  ideas.forEach(function(i){
    rows.push([
      (i.day||'').replace(/,/g,' '),
      (i.platform||'').replace(/,/g,' '),
      (i.angle||'').replace(/,/g,' '),
      (i.type||'').replace(/,/g,' ')
    ]);
  });
  const csv = rows.map(function(r){ return r.map(function(c){ return '"'+c+'"'; }).join(','); }).join(String.fromCharCode(10));
  const blob = new Blob([csv], {type:'text/csv'});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'content_calendar.csv';
  a.click();
  URL.revokeObjectURL(url);
}

function loadCal(){const g=document.getElementById('cal-grid');g.innerHTML='<div class="no-data" style="grid-column:1/-1;padding:16px">Generating…</div>';fetch('/api/content-calendar').then(r=>r.json()).then(d=>{if(!d.ideas||!d.ideas.length){g.innerHTML='<div class="no-data" style="grid-column:1/-1">Could not generate.</div>';return}const calDownBtn=document.getElementById('cal-download-btn');
  if(calDownBtn) calDownBtn.style.display='inline-block';
  // Show week range header
  const weekRange = d.ideas[0] && d.ideas[0].week_range;
  const rangeEl = document.getElementById('cal-week-range');
  if(rangeEl && weekRange) rangeEl.textContent = 'Week of ' + weekRange;
  g.innerHTML=d.ideas.map((i,idx)=>{
    window._calIdeas=window._calIdeas||[];
    window._calIdeas[idx]=i;
    const dateLabel = i.date ? `<span style="font-size:10px;color:var(--ink3);font-weight:400">${i.date}</span>` : '';
    return `<div class="cal-card"><div class="cal-day-name" style="display:flex;align-items:center;justify-content:space-between">${i.day}${dateLabel}</div><div class="cal-platform" style="font-size:10px;color:var(--ink3);margin:2px 0 4px">${i.platform||''}</div><div style="font-size:12px;line-height:1.5">${i.angle||''}</div><button data-idx="${idx}" onclick="generateFromCalIdx(this.dataset.idx)" style="margin-top:8px;padding:4px 10px;font-size:10px;font-weight:600;background:var(--ember);color:white;border:none;border-radius:4px;cursor:pointer;font-family:'DM Sans',sans-serif;width:100%">Generate →</button></div>`;
  }).join('')})}
function generateFromCalIdx(idx) {
  const i = window._calIdeas && window._calIdeas[idx];
  if (!i) return;
  generateFromCal(i.type || 'instagram_post', i.angle || '');
}
function generateFromCal(type, topic) {
  document.querySelectorAll('.ct-btn').forEach(b=>{
    if(b.dataset.type===type) { b.click(); }
  });
  document.getElementById('mktopic').value = topic;
  document.getElementById('mkoutput').scrollIntoView({behavior:'smooth', block:'nearest'});
  genContent(true);
}
async function saveDigestDay() {
  const day     = document.getElementById('digest-day-select').value;
  const enabled = document.getElementById('digest-enabled-select').value;
  const status  = document.getElementById('digest-save-status');
  const res = await fetch('/api/update-digest-day', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({day, enabled: parseInt(enabled)})
  });
  const data = await res.json();
  status.style.display='inline';
  if(data.ok) {
    status.style.color='var(--green)'; status.textContent='✓ Saved';
    document.getElementById('digest-day-current').textContent=day.charAt(0).toUpperCase()+day.slice(1);
    setTimeout(()=>{status.style.display='none';},2500);
  } else { status.style.color='var(--red)'; status.textContent='Save failed'; }
}
function loadBillingInfo() {
  fetch('/api/billing-info').then(r=>r.json()).then(d=>{
    document.getElementById('billing-loading').style.display='none';
    if(!d.ok || d.status==='inactive') {
      document.getElementById('billing-no-sub').style.display='block'; return;
    }
    document.getElementById('billing-content').style.display='block';
    const statusMap={active:'Active',trialing:'Trial period — first charge on day 31',past_due:'⚠ Payment past due',canceled:'Canceled'};
    document.getElementById('billing-status').textContent=statusMap[d.status]||d.status;
    // Prominent next charge
    const nextDate = d.trial_end ? 'Trial ends '+d.trial_end : (d.next_date||'—');
    document.getElementById('billing-next-prominent').textContent=nextDate;
    document.getElementById('billing-amount-prominent').textContent=d.amount||'—';
    document.getElementById('billing-pm').textContent=d.payment_method||'—';
    if(d.portal_url){
      document.getElementById('billing-portal-link').href=d.portal_url;
      document.getElementById('billing-invoice-link').href=d.portal_url;
    } else {
      document.getElementById('billing-portal-link').style.display='none';
      document.getElementById('billing-invoice-link').style.display='none';
    }
  }).catch(()=>{document.getElementById('billing-loading').textContent='Billing info unavailable.';});
}


function loadLaborTrend(){
  var container=document.getElementById('labor-trend-bars');
  var labels=document.getElementById('labor-trend-labels');
  if(!container)return;
  fetch('/api/labor-trend').then(function(r){return r.json();}).then(function(data){
    if(!data.weeks||!data.weeks.length){
      container.innerHTML='<div style="color:var(--ink3);font-size:12px;font-style:italic">No shift data available yet.</div>';
      return;
    }
    var maxPct=0;
    for(var i=0;i<data.weeks.length;i++){if(data.weeks[i].pct>maxPct)maxPct=data.weeks[i].pct;}
    maxPct=Math.max(maxPct,35);
    var html='';
    var lblHtml='';
    for(var i=0;i<data.weeks.length;i++){
      var w=data.weeks[i];
      var h=Math.max(6,Math.round((w.pct/maxPct)*72));
      var col=w.pct>32?'var(--red)':w.pct>=28?'#ef9f27':'#6fcf97';
      html+='<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:2px">';
      html+='<span style="font-size:10px;color:'+col+';font-weight:600">'+w.pct+'%</span>';
      html+='<div style="width:80%;height:'+h+'px;background:'+col+';border-radius:3px 3px 0 0"></div>';
      html+='</div>';
      lblHtml+='<span style="flex:1;text-align:center">'+w.label+'</span>';
    }
    container.innerHTML=html;
    if(labels)labels.innerHTML=lblHtml;
  }).catch(function(){
    container.innerHTML='<div style="color:var(--ink3);font-size:12px;font-style:italic">Trend data unavailable.</div>';
  });
}
function exportReviews(){window.location='/api/export-reviews';}
async function sendReferral(){
  var name = document.getElementById('referral-name').value.trim();
  var email = document.getElementById('referral-email').value.trim();
  var note = document.getElementById('referral-note').value.trim();
  var status = document.getElementById('referral-status');
  if(!name || !email){status.style.display='inline';status.style.color='var(--red)';status.textContent='Enter restaurant name and email';return;}
  status.style.display='inline';status.style.color='var(--ink3)';status.textContent='Sending…';
  var res = await fetch('/api/send-referral',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,email:email,note:note})});
  var data = await res.json();
  if(data.ok){
    status.style.color='var(--green)';status.textContent='Referral sent!';
    document.getElementById('referral-name').value='';
    document.getElementById('referral-email').value='';
    document.getElementById('referral-note').value='';
  } else {
    status.style.color='var(--red)';status.textContent=data.error||'Failed to send';
  }
}
function loadCompetitorIntel(){
  var loading=document.getElementById('comp-loading');
  var comp=document.getElementById('comp-content');
  var empty=document.getElementById('comp-empty');
  if(!loading)return;
  loading.style.display='block';comp.style.display='none';empty.style.display='none';
  fetch('/api/competitor-intel').then(function(r){return r.json();}).then(function(d){
    loading.style.display='none';
    if(!d.ok||!d.data){
      // No data yet - auto-trigger refresh
      loading.textContent='Fetching competitor data for the first time...';
      fetch('/api/refresh-competitor-intel',{method:'POST'}).then(function(r){return r.json();}).then(function(d2){
        if(d2.ok){setTimeout(function(){loadCompetitorIntel();},500);}
        else{loading.style.display='none';empty.style.display='block';}
      }).catch(function(){loading.style.display='none';empty.style.display='block';});
      return;
    }
    comp.style.display='block';
    var insight=document.getElementById('comp-insight');
    if(insight)insight.textContent=d.data.insight||'';
    var updated=document.getElementById('comp-updated');
    if(updated&&d.updated_at)updated.textContent='Last updated: '+d.updated_at.split(' ')[0];
    var cards=document.getElementById('comp-cards');
    if(!cards)return;
    var html='';
    var comps=d.data.competitors||[];
    for(var i=0;i<comps.length;i++){
      var c=comps[i];
      var stars='';
      for(var s=0;s<5;s++)stars+=s<Math.round(c.rating)?'<span style="color:#f59e0b">&#9733;</span>':'<span style="color:#d1d5db">&#9733;</span>';
      var revHtml='';
      var revs=c.reviews||[];
      for(var j=0;j<Math.min(revs.length,2);j++){
        var rv=revs[j];
        var rCol=rv.rating>=4?'#16a34a':'#dc2626';
        var rTxt=rv.text.length>120?rv.text.substring(0,120)+'...':rv.text;
        revHtml+='<div style="font-size:11px;color:var(--ink3);padding:6px 0;border-top:1px solid var(--paper3);line-height:1.5"><span style="color:'+rCol+'">&#9733;</span> '+rTxt+'</div>';
      }
      html+='<div style="background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:14px 16px">'
        +'<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">'
        +'<div style="font-weight:600;font-size:13px">'+c.name+'</div>'
        +'<div>'+stars+' <span style="font-size:12px;color:var(--ink3)">'+c.rating+'</span></div>'
        +'</div>'
        +'<div style="font-size:11px;color:var(--ink3);margin-bottom:8px">'+c.vicinity+' &middot; '+c.review_count+' reviews</div>'
        +revHtml+'</div>';
    }
    cards.innerHTML=html;
  }).catch(function(){
    loading.style.display='none';empty.style.display='block';
  });
}
function refreshCompetitorIntel(btn){
  // Show branded loading overlay
  var overlay = document.getElementById('upload-loading-overlay');
  var overlayMsg = document.getElementById('upload-loading-message');
  if(overlay){
    if(overlayMsg) overlayMsg.textContent = 'Analyzing your competitors...';
    overlay.style.display = 'flex';
  }
  btn.textContent='Refreshing...';btn.disabled=true;
  fetch('/api/refresh-competitor-intel',{method:'POST'}).then(function(r){return r.json();}).then(function(d){
    btn.textContent='Refresh';btn.disabled=false;
    if(overlay) overlay.style.display = 'none';
    if(d.ok){toast('Competitor data updated');setTimeout(function(){window.location.href=window.location.pathname+'?tab=competitor';},800);}
    else{toast('Error: '+(d.error||'failed'));}
  }).catch(function(){
    btn.textContent='Refresh';btn.disabled=false;
    if(overlay) overlay.style.display = 'none';
    toast('Request failed');
  });
}
function checkTabParam(){
  var params=new URLSearchParams(window.location.search);
  var tab=params.get('tab');
  if(tab){
    var btn=document.getElementById('tab-'+tab);
    if(btn){switchTab(tab,btn);}
  }
}
window.addEventListener('load',checkTabParam);
function dismissWelcome(){
  const b=document.getElementById('welcome-banner');
  if(b) b.style.display='none';
  fetch('/api/dismiss-welcome', {method:'POST'});
}
function changePassword(){
  const cur=document.getElementById('pw-current').value;
  const nw=document.getElementById('pw-new').value;
  const conf=document.getElementById('pw-confirm').value;
  const st=document.getElementById('pw-status');
  if(nw!==conf){st.style.display='block';st.style.color='var(--red)';st.textContent='Passwords do not match';return}
  if(nw.length<8){st.style.display='block';st.style.color='var(--red)';st.textContent='Password must be at least 8 characters';return}
  fetch('/api/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current:cur,new_password:nw})}).then(r=>r.json()).then(d=>{st.style.display='block';if(d.ok){st.style.color='var(--green)';st.textContent='Password updated';document.getElementById('pw-current').value='';document.getElementById('pw-new').value='';document.getElementById('pw-confirm').value='';}else{st.style.color='var(--red)';st.textContent=d.error||'Update failed'}})}

</script>
</body>
</html>"""

CLIENT_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ restaurant.name }} — Settings</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="shortcut icon" href="/favicon.ico">
<meta name="theme-color" content="#0e0c0a">
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#0e0c0a;--ink2:#3a3530;--ink3:#7a736a;--paper:#f7f4ef;--paper2:#edeae3;--paper3:#e0dbd0;--ember:#c84b2f;--green:#2d5a3d;--green-bg:#eaf2ed;--r:8px}
body{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);font-size:14px}
.hdr{background:var(--ink);height:54px;display:flex;align-items:center;padding:0 28px;justify-content:space-between}
.hdr-logo{font-family:'DM Serif Display',serif;font-size:16px;color:var(--paper)}
.hdr-logo em{color:#e8956a;font-style:italic}
.back-btn{font-size:11px;color:var(--ink3);text-decoration:none;padding:5px 10px;border:1px solid #2a2520;border-radius:4px}
.container{max-width:800px;margin:0 auto;padding:32px 24px}
.page-title{font-family:'DM Serif Display',serif;font-size:24px;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--ink3);margin-bottom:28px}
.section-card{background:white;border:1px solid var(--paper3);border-radius:var(--r);margin-bottom:20px;overflow:hidden}
.section-hdr{background:var(--ink);padding:12px 18px}
.section-title{font-size:13px;font-weight:500;color:var(--paper);letter-spacing:.02em}
.section-body{padding:18px 20px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group.full{grid-column:1/-1}
label{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3)}
input,select,textarea{padding:9px 12px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--ink);background:white;outline:none;transition:border .15s;width:100%}
input:focus,select:focus,textarea:focus{border-color:var(--ember)}
textarea{resize:vertical;min-height:60px}
.hint{font-size:10px;color:var(--ink3);margin-top:3px;line-height:1.4}
.status-row{display:flex;align-items:center;gap:10px;padding:10px 12px;border:1px solid var(--paper3);border-radius:6px;background:var(--paper2)}
.status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.dot-live{background:var(--green)}
.dot-sample{background:#f59e0b}
.status-text{font-size:12px;color:var(--ink2);flex:1}
.toggle{width:40px;height:22px;background:var(--paper3);border-radius:11px;cursor:pointer;position:relative;transition:background .2s;border:none;flex-shrink:0}
.toggle.on{background:var(--green)}
.toggle::after{content:'';position:absolute;width:16px;height:16px;border-radius:50%;background:white;top:3px;left:3px;transition:left .2s;box-shadow:0 1px 3px rgba(0,0,0,.2)}
.toggle.on::after{left:21px}
.save-bar{display:flex;align-items:center;gap:12px;margin-top:24px}
.btn-save{background:var(--ember);color:white;padding:10px 24px;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;border:none;cursor:pointer;transition:background .15s}
.btn-save:hover{background:#a83d25}
.btn-data{background:white;color:var(--ink2);padding:10px 18px;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:500;border:1px solid var(--paper3);text-decoration:none;transition:all .15s}
.btn-data:hover{background:var(--paper2)}
.save-status{font-size:12px;display:none}
.save-ok{color:var(--green)}
.save-err{color:var(--ember)}
.action-menu{position:relative;display:inline-block}
.action-menu-btn{padding:4px 10px;border-radius:4px;border:1px solid var(--paper3);background:white;font-family:'DM Sans',sans-serif;font-size:11px;font-weight:500;cursor:pointer;color:var(--ink2);transition:all .15s;white-space:nowrap}
.action-menu-btn:hover{background:var(--ink);color:white;border-color:var(--ink)}

</style>
</head>
<body>
<header class="hdr">
  <div class="hdr-logo">Cavnar <em>AI</em> <span style="font-family:'DM Sans',sans-serif;font-size:12px;color:var(--ink3);font-weight:400;margin-left:8px">/ Client Settings</span></div>
  <a href="/admin" class="back-btn">← Back to admin</a>
</header>

<div class="container">
  <div class="page-title">{{ restaurant.name }}</div>
  <div class="page-sub">Configure all settings for this client's dashboard.</div>

  <!-- Basic info -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Basic information</div></div>
    <div class="section-body">
      <div class="form-grid">
        <div class="form-group">
          <label>Restaurant name</label>
          <input type="text" id="name" value="{{ restaurant.name }}">
        </div>
        <div class="form-group">
          <label>Owner email</label>
          <input type="email" id="owner_email" value="{{ restaurant.owner_email }}">
        </div>

        <div class="form-group">
          <label>Owner / GM name</label>
          <input type="text" id="owner_name" value="{{ restaurant.owner_name or '' }}" placeholder="e.g. Sarah">
          <div class="hint">Used to personalize AI reports and communications</div>
        </div>
        <div class="form-group">
          <label>Owner phone number</label>
          <input type="text" id="owner_phone" value="{{ restaurant.owner_phone or '' }}" placeholder="(312) 555-0100">
        </div>
        <div class="form-group">
          <label>Location group (multi-location)</label>
          <input type="text" id="location_group" value="{{ restaurant.location_group or '' }}" placeholder="e.g. Syrup">
          <div class="hint">Group name shared across all locations of the same brand</div>
        </div>
        <div class="form-group">
          <label>Location name</label>
          <input type="text" id="location_name" value="{{ restaurant.location_name or '' }}" placeholder="e.g. Lincoln Park">
        </div>
        <div class="form-group">
          <label>Sign-off name (for emails & responses)</label>
          <input type="text" id="sign_off_name" value="{{ restaurant.sign_off_name or '' }}" placeholder="e.g. Sarah, or The Maplewood Team">
        </div>
      </div>
    </div>
  </div>

  <!-- Platform IDs -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Review platforms</div></div>
    <div class="section-body">
      <div class="form-grid">
        <div class="form-group">
          <label>Google Place ID</label>
          <input type="text" id="google_place_id" value="{{ restaurant.google_place_id or '' }}" placeholder="ChIJ...">
          <div class="hint">Found in Google Maps URL or Google Business Profile</div>
        </div>
        <div class="form-group">
          <label>Yelp Business ID</label>
          <input type="text" id="yelp_business_id" value="{{ restaurant.yelp_business_id or '' }}" placeholder="restaurant-name-chicago">
          <div class="hint">The slug at the end of the Yelp business URL</div>
        </div>
      </div>
      <div style="margin-top:14px">
        <label style="display:block;margin-bottom:6px">Review data status</label>
        <div class="status-row">
          <div class="status-dot {{'dot-live' if restaurant.gmb_refresh_token else 'dot-sample'}}"></div>
          <div class="status-text">
            {{'Pulling live reviews from Google Business' if restaurant.gmb_refresh_token else 'Using sample review data — connect Google Business Profile in the Account tab to go live'}}
          </div>
        </div>
      </div>
      <div style="margin-top:14px">
        <div class="form-grid">
          <div class="form-group">
            <label>Weekly digest day</label>
            <select id="digest_day">
              {% for day in ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"] %}
              <option value="{{ day }}" {{"selected" if restaurant.digest_day == day}}>{{ day|title }}</option>
              {% endfor %}
            </select>
            <div class="hint">Owner receives their weekly review summary on this day at 8am</div>
          </div>
          <div class="form-group">
            <label>Weekly digest email</label>
            <select id="digest_enabled">
              <option value="1" {{"selected" if restaurant.digest_enabled}}>Enabled — send automatically</option>
              <option value="0" {{"selected" if not restaurant.digest_enabled}}>Disabled</option>
            </select>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Labor settings -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Labor settings</div></div>
    <div class="section-body">
      <div class="form-grid">
        <div class="form-group">
          <label>Blended hourly rate (wages + taxes + benefits)</label>
          <input type="number" id="hourly_rate" value="{{ restaurant.hourly_rate or 26.0 }}" min="10" max="60" step="0.50">
          <div class="hint">Used to calculate actual labor cost from hours worked. Industry average $22–28/hr blended.</div>
        </div>
        <div class="form-group">
          <label>Labor % target</label>
          <input type="number" id="labor_target_pct" value="{{ restaurant.labor_target_pct or 30.0 }}" min="15" max="50" step="0.5">
          <div class="hint">Default is 30%. Adjust if this restaurant has a different target (e.g. fine dining may run 35%, fast casual may target 25%).</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Staff scheduling constraints -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Staff scheduling constraints</div></div>
    <div class="section-body">
      <p style="font-size:13px;color:var(--ink2);margin-bottom:14px;line-height:1.6">
        Add constraints per staff member — fixed days, availability limits, guaranteed hours.
        These apply automatically every time an optimized schedule is generated.
      </p>
      {% if staff_notes %}
      <div style="margin-bottom:14px">
        {% for note in staff_notes %}
        <div style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;margin-bottom:6px">
          <div style="flex:1">
            <div style="font-weight:600;font-size:13px">{{note.employee_name}}</div>
            <div style="font-size:12px;color:var(--ink3);margin-top:2px">{{note.notes}}</div>
          </div>
          <button onclick="deleteNote({{note.id}})"
            style="font-size:10px;padding:3px 8px;border-radius:4px;border:1px solid #f5c6c2;background:white;color:#c0392b;cursor:pointer;font-family:'DM Sans',sans-serif">
            Remove
          </button>
        </div>
        {% endfor %}
      </div>
      {% endif %}
      <div class="form-grid">
        <div class="form-group">
          <label>Employee name</label>
          <input type="text" id="staff-name" placeholder="e.g. Marcus G.">
        </div>
        <div class="form-group">
          <label>Constraint</label>
          <input type="text" id="staff-constraint" placeholder="e.g. Always Fri/Sat/Sun, never before 5pm">
        </div>
      </div>
      <button class="btn-save" style="padding:9px 16px;margin-top:4px" onclick="addStaffNote()">Add constraint</button>
      <div style="font-size:11px;color:var(--ink3);margin-top:8px;line-height:1.6">
        Examples: "Always works Mon/Wed/Fri" · "Can't work after 9pm" · "Guaranteed 30h/week" · "Part-time, max 20h"
      </div>
      <div style="font-size:12px;margin-top:8px;display:none" id="staff-note-result"></div>
    </div>
  </div>

  <!-- Marketing settings -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Marketing profile</div></div>
    <div class="section-body">
      <div style="font-size:12px;color:var(--ink3);line-height:1.6;margin-bottom:12px">
        This profile shapes how the AI writes content for this restaurant. The more detail here, the better the output.
      </div>
      <div class="form-grid">
        <div class="form-group">
          <label>Neighborhood</label>
          <input type="text" id="neighborhood" value="{{ restaurant.neighborhood or '' }}" placeholder="e.g. Lincoln Park, Chicago">
        </div>
        <div class="form-group">
          <label>Restaurant vibe</label>
          <input type="text" id="vibe" value="{{ restaurant.vibe or '' }}" placeholder="e.g. warm neighborhood bistro, serious about food">
        </div>
        <div class="form-group full">
          <label>Known for</label>
          <input type="text" id="known_for" value="{{ restaurant.known_for or '' }}" placeholder="e.g. short rib pasta, brunch, house-baked bread, craft cocktails">
        </div>
        <div class="form-group full">
          <label>Brand voice notes</label>
          <textarea id="voice_notes" rows="2" placeholder="e.g. genuine and warm, a little witty, never corporate, speaks like a person not a brand">{{ restaurant.voice_notes or '' }}</textarea>
        </div>
        <div class="form-group">
          <label>Never say (words/phrases to avoid)</label>
          <input type="text" id="never_say" value="{{ restaurant.never_say or '' }}" placeholder="e.g. culinary journey, indulge, delightful">
          <div class="hint">Comma-separated — AI will never use these</div>
        </div>
        <div class="form-row-wide">
          <label class="form-label">Skip these holidays in marketing content</label>
          <input type="text" id="skip_holidays" value="{{ restaurant.skip_holidays or '' }}" placeholder="e.g. St. Patrick's Day, Halloween, Cinco de Mayo">
          <div class="hint">Comma-separated — AI will not suggest these holidays in content calendar or marketing copy</div>
        </div>
        <div class="form-row-wide">
          <label class="form-label">Custom competitors <span style="font-weight:400;color:var(--ink3);font-size:11px">(Google Place IDs)</span></label>
          <input type="text" id="custom_competitors" value="{{ restaurant.custom_competitors or '' }}" placeholder="e.g. ChIJabc123, ChIJxyz789">
          <div class="hint">Comma-separated Google Place IDs — these will always be included in competitor intel regardless of proximity. <a href="https://developers.google.com/maps/documentation/places/web-service/place-id" target="_blank" style="color:var(--ember)">How to find a Place ID</a></div>
        </div>
        <div class="form-row-wide">
          <label class="form-label">Menu &amp; current specials</label>
          <textarea id="menu_notes" rows="4" placeholder="Key menu items, signature dishes, current specials — e.g. Known for: short rib pasta, truffle fries, brunch cocktails. Current specials: bottomless brunch Sat/Sun 10am-2pm">{{ restaurant.menu_notes or '' }}</textarea>
          <div class="hint">AI uses this to generate accurate, specific marketing content. Update when menu or specials change.</div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:6px;flex-wrap:wrap">
            {% if restaurant.google_place_id %}
            <button type="button" onclick="refreshMenuFromGoogle()" style="background:none;border:1px solid var(--paper3);color:var(--ink3);padding:4px 12px;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;font-family:'DM Sans',sans-serif">↻ Refresh from Google Places</button>
            {% endif %}
            <button type="button" onclick="fetchMenuFromUrl()" style="background:none;border:1px solid var(--paper3);color:var(--ink3);padding:4px 12px;border-radius:4px;font-size:11px;font-weight:600;cursor:pointer;font-family:'DM Sans',sans-serif">↻ Fetch from menu URL</button>
            <label style="cursor:pointer;background:none;border:1px solid var(--paper3);color:var(--ink3);padding:4px 12px;border-radius:4px;font-size:11px;font-weight:600;font-family:'DM Sans',sans-serif">
              📄 Upload menu PDF
              <input type="file" id="menu-pdf-input" accept=".pdf" style="display:none" onchange="uploadMenuPDF(this)">
            </label>
          </div>
          <span id="menu-refresh-status" style="font-size:11px;color:var(--ink3);display:block;margin-top:4px"></span>
          <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
            <input type="text" id="menu_url" value="{{ restaurant.menu_url or '' }}" placeholder="https://restaurant.com/menu" style="flex:1;font-size:12px">
            <span style="font-size:11px;color:var(--ink3);white-space:nowrap">Menu URL</span>
          </div>
          <div class="hint" style="margin-top:2px">Paste their menu URL and click ↻ Fetch, or upload a PDF above</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Inventory settings -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Inventory settings</div></div>
    <div class="section-body">
      <div class="form-grid">
        <div class="form-group">
          <label>POS system</label>
          <select id="pos_system">
            <option value="">Unknown / not set</option>
            {% for pos in ['Toast','Square','Lightspeed','Aloha / NCR','Clover','Revel','TouchBistro','Other / Manual'] %}
            <option value="{{ pos }}" {{'selected' if restaurant.pos_system == pos}}>{{ pos }}</option>
            {% endfor %}
          </select>
          <div class="hint">Used to plan direct POS integration for automated data pulls</div>
        </div>
        <div class="form-group">
          <label>Inventory update frequency</label>
          <select id="inventory_frequency">
            <option value="weekly" {{'selected' if (restaurant.inventory_frequency or 'weekly') == 'weekly'}}>Weekly</option>
            <option value="biweekly" {{'selected' if restaurant.inventory_frequency == 'biweekly'}}>Every 2 weeks</option>
            <option value="monthly" {{'selected' if restaurant.inventory_frequency == 'monthly'}}>Monthly</option>
          </select>
          <div class="hint">How often to request a fresh data export from this client</div>
        </div>
        <div class="form-group full">
          <label>Inventory export instructions</label>
          <textarea id="inventory_notes" rows="3" placeholder="e.g. Client exports from Toast — go to Back Office → Menu → Items → Export. Ask Sarah every Monday morning.">{{ restaurant.inventory_notes or '' }}</textarea>
          <div class="hint">Notes for yourself on how to get data from this client each week</div>
        </div>
        <div class="form-group">
          <label>Food cost target %</label>
          <input type="number" id="food_cost_target" value="{{ restaurant.food_cost_target or 30 }}" min="10" max="50" step="1">
          <div class="hint">Target food cost as % of revenue (typically 28–35%)</div>
        </div>
        <div class="form-group">
          <label>Last inventory update</label>
          <input type="text" value="{{ restaurant.inventory_updated_at[:10] if restaurant.inventory_updated_at else 'Never' }}" disabled style="background:var(--paper2);color:var(--ink3)">
          <div class="hint">Updated automatically when you upload new data</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Module access -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Module access</div></div>
    <div class="section-body">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        {% for mod_id, label, price in [
          ("reviews","Review Intelligence","$300/mo"),
          ("labor","Labor Optimizer","$300/mo"),
          ("inventory","Inventory Control","$300/mo"),
          ("marketing","Marketing Autopilot","$300/mo")
        ] %}
        <label style="display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;cursor:pointer;font-weight:400;letter-spacing:0;text-transform:none">
          <input type="checkbox" id="setting-mod-{{mod_id}}"
            {% if mod_id == "reviews" and restaurant.module_reviews %}checked
            {% elif mod_id == "labor" and restaurant.module_labor %}checked
            {% elif mod_id == "inventory" and restaurant.module_inventory %}checked
            {% elif mod_id == "marketing" and restaurant.module_marketing %}checked
            {% endif %}
            style="width:14px;height:14px;accent-color:#c84b2f">
          <div>
            <div style="font-size:13px;font-weight:500;color:var(--ink)">{{label}}</div>
            <div style="font-size:11px;color:var(--ink3)">{{price}}</div>
          </div>
        </label>
        {% endfor %}
      </div>
      <div style="font-size:11px;color:var(--ink3);line-height:1.6">
        <strong>Pricing:</strong> $500 setup per module · $300/mo per module<br>
        2 modules = $1,000 + $600/mo &nbsp;·&nbsp; 3 modules = $1,500 + $900/mo &nbsp;·&nbsp; 4 modules = $2,000 + $1,200/mo<br>
        Checked modules appear as tabs in the client dashboard. Save to apply.
      </div>
    </div>
  </div>

  <!-- Billing status -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Billing & status</div></div>
    <div class="section-body">
      <div class="form-grid">
        <div class="form-group">
          <label>Billing status</label>
          <select id="billing_status">
            {% for s in ["trial","active","paused","churned"] %}
            <option value="{{ s }}" {{"selected" if restaurant.billing_status == s}}>{{ s|title }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="form-group">
          <label>Internal notes (private — not visible to client)</label>
          <textarea id="internal_notes" style="min-height:52px" placeholder="e.g. Signed up May 2026, starter module, on Toast POS, prefers texts">{{ restaurant.internal_notes or "" }}</textarea>
        </div>
      </div>
    </div>
  </div>

  <!-- Password reset -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Reset client password</div></div>
    <div class="section-body">
      <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
        <div class="form-group" style="flex:1;min-width:200px">
          <label>New temporary password</label>
          <input type="text" id="new-password" placeholder="Leave blank to auto-generate">
        </div>
        <div style="display:flex;align-items:center;gap:8px;padding-bottom:2px">
          <input type="checkbox" id="send-reset-email" checked style="width:14px;height:14px;accent-color:#c84b2f">
          <label style="font-size:12px;color:var(--ink2);letter-spacing:0;text-transform:none;font-weight:400">Email new password to owner</label>
        </div>
        <button class="btn-save" style="padding:9px 16px;white-space:nowrap" onclick="resetPassword()">Reset password</button>
      </div>
      <div style="font-size:12px;margin-top:8px;display:none" id="reset-status"></div>
    </div>
  </div>

  <!-- Test email triggers -->
  <div class="section-card">
    <div class="section-hdr"><div class="section-title">Test email triggers</div></div>
    <div class="section-body">
      <p style="font-size:13px;color:var(--ink2);line-height:1.6;margin-bottom:14px">
        Send a test email to this client's address to verify delivery and appearance.
      </p>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button class="btn-save" style="padding:9px 16px;background:#2d6a4f" onclick="sendTestDigest()">
          Send test digest email
        </button>
        <button class="btn-save" style="padding:9px 16px;background:#b7791f" onclick="sendTestUrgent()">
          Send test urgent alert
        </button>
        <button class="btn-save" style="padding:9px 16px;background:#c84b2f" onclick="resendWelcome()" id="resend-welcome-btn">
          Resend welcome email
        </button>
        {% if restaurant.ig_token %}
        <button class="btn-save" style="padding:9px 16px;background:#1877f2" onclick="refreshIgToken()">
          Refresh Instagram tokens
        </button>
        {% endif %}
      </div>
      <div style="font-size:12px;margin-top:10px;display:none" id="test-email-status"></div>
    </div>
  </div>

  <div class="save-bar">
    <button class="btn-save" onclick="saveSettings()">Save all settings</button>
    <a href="/admin/client-data/{{ restaurant.id }}" class="btn-data">Manage data →</a>
    <span class="save-status" id="save-status"></span>
  </div>
</div>

<script>
let reviewsLive = {{ 'true' if restaurant.gmb_refresh_token else 'false' }};

// Poll for new reviews every 5 minutes when on reviews tab
(function() {
  let _knownTotal = {% if rstats is defined %}{{ rstats.total }}{% else %}0{% endif %};
  let _knownPending = {% if rstats is defined %}{{ rstats.awaiting_approval }}{% else %}0{% endif %};
  let _pollActive = false;

  function checkNewReviews() {
    if(document.hidden) return; // don't poll when tab is hidden
    fetch('/api/review-count')
      .then(r => r.json())
      .then(d => {
        const newTotal = d.total || 0;
        const newPending = d.pending || 0;
        const newUrgent = d.urgent || 0;
        if(newTotal > _knownTotal || newPending > _knownPending) {
          const added = newTotal - _knownTotal;
          const banner = document.getElementById('new-reviews-banner');
          const txt = document.getElementById('new-reviews-text');
          if(banner && txt) {
            const urgentNote = newUrgent > 0 ? ` (${newUrgent} urgent ⚠)` : '';
            txt.textContent = added > 0
              ? `${added} new review${added > 1 ? 's' : ''} available${urgentNote}`
              : `New review activity — click to refresh${urgentNote}`;
            banner.style.display = 'flex';
          }
        }
      })
      .catch(() => {}); // silently ignore errors
  }

  // Start polling every 5 minutes
  if(reviewsLive) {
    setInterval(checkNewReviews, 5 * 60 * 1000);
  }
})();



async function resetPassword() {
  const btn = event.target;
  const status = document.getElementById('reset-status');
  const pw = document.getElementById('new-password').value.trim();
  const sendEmail = document.getElementById('send-reset-email').checked;
  btn.textContent = 'Resetting…'; btn.disabled = true;
  const res = await fetch('/admin/reset-password-by-restaurant/{{ restaurant.id }}', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: pw, send_email: sendEmail})
  });
  const data = await res.json();
  status.style.display = 'block';
  if (data.ok) {
    status.style.color = 'var(--green)';
    status.textContent = '✓ Password reset to: ' + data.password + (sendEmail ? ' — email sent' : '');
  } else {
    status.style.color = 'var(--ember)';
    status.textContent = data.error || 'Reset failed';
  }
  btn.textContent = 'Reset password'; btn.disabled = false;
}

async function addStaffNote() {
  const name = document.getElementById('staff-name').value.trim();
  const notes = document.getElementById('staff-constraint').value.trim();
  const result = document.getElementById('staff-note-result');
  if(!name || !notes) { result.style.display='block'; result.style.color='var(--ember)'; result.textContent='Enter both a name and constraint'; return; }
  const form = new FormData();
  form.append('employee_name', name);
  form.append('notes', notes);
  const res = await fetch('/admin/staff-notes/{{restaurant.id}}', {method:'POST', body:form});
  const data = await res.json();
  if(data.ok) {
    result.style.display='block'; result.style.color='var(--green)';
    result.textContent='✓ Saved';
    setTimeout(()=>location.reload(), 800);
  } else {
    result.style.display='block'; result.style.color='var(--ember)';
    result.textContent=data.error||'Save failed';
  }
}
async function deleteNote(noteId) {
  const res = await fetch('/admin/staff-notes/'+noteId+'/delete', {method:'POST'});
  const data = await res.json();
  if(data.ok) location.reload();
}
async function uploadMenuPDF(input){
  const file = input.files[0];
  if(!file) return;
  const status = document.getElementById('menu-refresh-status');
  status.textContent = 'Extracting menu from PDF...'; status.style.color = 'var(--ink3)';
  const form = new FormData();
  form.append('pdf', file);
  try {
    const res = await fetch('/admin/upload-menu-pdf/{{ restaurant.id }}', {method:'POST', body: form});
    const d = await res.json();
    if(d.ok){
      document.getElementById('menu_notes').value = d.menu_notes;
      status.textContent = '✓ Menu extracted from PDF — review and save';
      status.style.color = '#2d6a4f';
    } else {
      status.textContent = d.error || 'Could not extract menu from PDF';
      status.style.color = '#c84b2f';
    }
  } catch(e) {
    status.textContent = 'Upload failed';
    status.style.color = '#c84b2f';
  }
  input.value = '';
}

async function fetchMenuFromUrl(){
  const urlInput = document.getElementById('menu_url');
  const status = document.getElementById('menu-refresh-status');
  const url = urlInput ? urlInput.value.trim() : '';
  if(!url){ status.textContent = 'Enter a menu URL first'; status.style.color = '#c84b2f'; return; }
  const btn = event.target;
  btn.disabled = true; btn.textContent = '↻ Fetching...';
  status.textContent = ''; 
  try {
    const res = await fetch('/admin/fetch-menu-from-url/{{ restaurant.id }}', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url})
    });
    const d = await res.json();
    if(d.ok){
      document.getElementById('menu_notes').value = d.menu_notes;
      status.textContent = '✓ Menu extracted — review and save';
      status.style.color = '#2d6a4f';
    } else {
      status.textContent = d.error || 'Could not extract menu from that URL — fill in manually';
      status.style.color = '#7a736a';
    }
  } catch(e) {
    status.textContent = 'Request failed';
    status.style.color = '#c84b2f';
  }
  btn.disabled = false; btn.textContent = '↻ Fetch from menu URL';
}

async function refreshMenuFromGoogle(){
  const btn = event.target;
  const status = document.getElementById('menu-refresh-status');
  btn.disabled = true; btn.textContent = '↻ Fetching...';
  status.textContent = '';
  try {
    const res = await fetch('/admin/refresh-menu-notes/{{ restaurant.id }}', {method:'POST'});
    const d = await res.json();
    if(d.ok){
      document.getElementById('menu_notes').value = d.menu_notes;
      status.textContent = d.message || '✓ Menu data updated';
      status.style.color = '#2d6a4f';
    } else {
      status.textContent = d.error || 'No menu data found — try PDF upload or enter manually';
      status.style.color = '#7a736a';
    }
  } catch(e) {
    status.textContent = 'Request failed';
    status.style.color = '#c84b2f';
  }
  btn.disabled = false; btn.textContent = '↻ Refresh from Google Places';
}

async function resendWelcome(){
  const btn = document.getElementById('resend-welcome-btn');
  const status = document.getElementById('test-email-status');
  if(!confirm('This will reset the client password and send them a new welcome email with their credentials. Continue?')) return;
  btn.textContent = 'Sending...'; btn.disabled = true;
  status.style.display = 'block'; status.textContent = 'Sending welcome email...'; status.style.color = 'var(--ink3)';
  try {
    const res = await fetch('/admin/resend-welcome/{{ restaurant.id }}', {method:'POST'});
    const data = await res.json();
    if(data.ok){
      status.style.color = '#2d6a4f';
      status.textContent = '\u2713 Welcome email sent to ' + data.email + ' with a new temporary password.';
    } else {
      status.style.color = '#c84b2f';
      status.textContent = 'Error: ' + (data.error || 'failed');
    }
  } catch(e) {
    status.style.color = '#c84b2f';
    status.textContent = 'Request failed';
  }
  btn.textContent = 'Resend welcome email'; btn.disabled = false;
}

async function sendTestDigest() {
  const status = document.getElementById('test-email-status');
  status.style.display = 'block';
  status.style.color = 'var(--ink3)';
  status.textContent = 'Sending digest…';
  const res = await fetch('/admin/test-digest/{{ restaurant.id }}', {method:'POST'});
  const data = await res.json();
  status.style.color = data.ok ? 'var(--green)' : 'var(--ember)';
  status.textContent = data.ok ? '✓ Digest sent to ' + data.email : 'Error: ' + (data.error || 'failed');
}
async function sendTestUrgent() {
  const status = document.getElementById('test-email-status');
  status.style.display = 'block';
  status.style.color = 'var(--ink3)';
  status.textContent = 'Sending urgent alert…';
  const res = await fetch('/admin/test-urgent/{{ restaurant.id }}', {method:'POST'});
  const data = await res.json();
  status.style.color = data.ok ? 'var(--green)' : 'var(--ember)';
  status.textContent = data.ok ? '✓ Urgent alert sent to ' + data.email : 'Error: ' + (data.error || 'failed');
}
async function refreshIgToken() {
  const status = document.getElementById('test-email-status');
  status.style.display = 'block';
  status.style.color = 'var(--ink3)';
  status.textContent = 'Refreshing tokens…';
  const res = await fetch('/admin/refresh-ig-token/{{ restaurant.id }}', {method:'POST'});
  const data = await res.json();
  status.style.color = data.ok ? 'var(--green)' : 'var(--ember)';
  status.textContent = data.ok ? '✓ Tokens refreshed — new expiry: ' + data.expires : 'Error: ' + (data.error || 'failed');
}
async function saveSettings() {
  const btn = document.querySelector('.btn-save');
  const status = document.getElementById('save-status');
  btn.textContent = 'Saving…'; btn.disabled = true;
  const payload = {
    name:            document.getElementById('name').value,
    owner_email:     document.getElementById('owner_email').value,
    owner_name:      document.getElementById('owner_name').value,
    owner_phone:     document.getElementById('owner_phone').value,
    location_group:       document.getElementById('location_group').value,
    location_name:        document.getElementById('location_name').value,
    inventory_frequency:  document.getElementById('inventory_frequency') ? document.getElementById('inventory_frequency').value : 'weekly',
    inventory_notes:      document.getElementById('inventory_notes') ? document.getElementById('inventory_notes').value : '',
    food_cost_target:     document.getElementById('food_cost_target') ? document.getElementById('food_cost_target').value : 30,
    digest_day:      document.getElementById('digest_day').value,
    digest_enabled:  parseInt(document.getElementById('digest_enabled').value),
    pos_system:      document.getElementById('pos_system').value,
    sign_off_name:   document.getElementById('sign_off_name').value,
    google_place_id: document.getElementById('google_place_id').value,
    yelp_business_id:document.getElementById('yelp_business_id').value,
    reviews_live:    {{ 1 if restaurant.gmb_refresh_token else 0 }},
    neighborhood:    document.getElementById('neighborhood').value,
    known_for:       document.getElementById('known_for').value,
    vibe:            document.getElementById('vibe').value,
    voice_notes:     document.getElementById('voice_notes').value,
    never_say:       document.getElementById('never_say').value,
    skip_holidays:   document.getElementById('skip_holidays').value,
    custom_competitors: document.getElementById('custom_competitors').value,
    menu_notes:      document.getElementById('menu_notes').value,
    menu_url:        document.getElementById('menu_url').value,
    hourly_rate:        parseFloat(document.getElementById('hourly_rate').value),
    labor_target_pct:   parseFloat(document.getElementById('labor_target_pct').value) || 30.0,
    billing_status:  document.getElementById('billing_status').value,
    internal_notes:  document.getElementById('internal_notes').value,
    module_reviews:  document.getElementById('setting-mod-reviews').checked ? 1 : 0,
    module_labor:    document.getElementById('setting-mod-labor').checked ? 1 : 0,
    module_inventory:document.getElementById('setting-mod-inventory').checked ? 1 : 0,
    module_marketing:document.getElementById('setting-mod-marketing').checked ? 1 : 0,
  };
  const res = await fetch(window.location.pathname, {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  const data = await res.json();
  status.style.display = 'inline';
  if (data.ok) {
    status.className = 'save-status save-ok';
    status.textContent = '✓ Saved';
    setTimeout(() => status.style.display='none', 3000);
  } else {
    status.className = 'save-status save-err';
    status.textContent = data.error || 'Save failed';
  }
  btn.textContent = 'Save all settings'; btn.disabled = false;
}
</script>
</body>
</html>"""

CLIENT_DATA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ restaurant.name }} — Data Setup</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="shortcut icon" href="/favicon.ico">
<meta name="theme-color" content="#0e0c0a">
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#0e0c0a;--ink2:#3a3530;--ink3:#7a736a;--paper:#f7f4ef;--paper2:#edeae3;--paper3:#e0dbd0;--ember:#c84b2f;--green:#2d5a3d;--green-bg:#eaf2ed;--amber:#b7791f;--amber-bg:#fef9ec;--r:8px}
body{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);font-size:14px}
.hdr{background:var(--ink);height:54px;display:flex;align-items:center;padding:0 28px;justify-content:space-between}
.hdr-logo{font-family:'DM Serif Display',serif;font-size:16px;color:var(--paper)}
.hdr-logo em{color:#e8956a;font-style:italic}
.hdr-right{display:flex;align-items:center;gap:12px}
.back-btn{font-size:11px;color:var(--ink3);text-decoration:none;padding:5px 10px;border:1px solid #2a2520;border-radius:4px}
.container{max-width:860px;margin:0 auto;padding:32px 24px}
.page-title{font-family:'DM Serif Display',serif;font-size:24px;margin-bottom:4px}
.page-sub{font-size:13px;color:var(--ink3);margin-bottom:28px}
.module-card{background:white;border:1px solid var(--paper3);border-radius:var(--r);margin-bottom:20px;overflow:hidden}
.module-hdr{padding:16px 20px;border-bottom:1px solid var(--paper3);display:flex;align-items:center;justify-content:space-between}
.module-title{font-weight:600;font-size:15px}
.module-status{font-size:11px;font-weight:500;padding:3px 10px;border-radius:20px}
.status-live{background:var(--green-bg);color:var(--green)}
.status-sample{background:var(--amber-bg);color:var(--amber)}
.module-body{padding:20px}
.slabel{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--ink3);margin-bottom:8px}
.tabs{display:flex;gap:4px;margin-bottom:16px}
.mtab{padding:6px 14px;border-radius:6px;border:1px solid var(--paper3);font-size:12px;cursor:pointer;background:white;font-family:'DM Sans',sans-serif;transition:all .15s}
.mtab.active{background:var(--ink);color:white;border-color:var(--ink)}
.tab-content{display:none}
.tab-content.active{display:block}
.upload-zone{border:2px dashed var(--paper3);border-radius:var(--r);padding:28px;text-align:center;cursor:pointer;transition:all .2s;position:relative}
.upload-zone:hover{border-color:var(--ember);background:var(--paper2)}
.upload-zone input{position:absolute;inset:0;opacity:0;cursor:pointer;width:100%;height:100%}
.upload-icon{font-size:28px;margin-bottom:8px}
.upload-label{font-size:13px;font-weight:500;color:var(--ink2);margin-bottom:4px}
.upload-sub{font-size:11px;color:var(--ink3)}
.pos-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
.pos-card{background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;padding:10px 12px;cursor:pointer;transition:all .15s}
.pos-card:hover{border-color:var(--ember)}
.pos-name{font-weight:600;font-size:12px;margin-bottom:3px}
.pos-steps{font-size:11px;color:var(--ink3);line-height:1.5}
.textarea{width:100%;padding:10px 12px;border:1px solid var(--paper3);border-radius:6px;font-family:monospace;font-size:11px;color:var(--ink);background:white;outline:none;resize:vertical;min-height:140px}
.textarea:focus{border-color:var(--ember)}
.btn-primary{background:var(--ember);color:white;padding:9px 20px;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:600;border:none;cursor:pointer;transition:background .15s}
.btn-primary:hover{background:#a83d25}
.btn-primary:disabled{background:var(--ink3);cursor:default}
.result-msg{font-size:12px;margin-top:10px;padding:8px 12px;border-radius:6px;display:none}
.result-ok{background:var(--green-bg);color:var(--green);border:1px solid #b7dfca}
.result-err{background:#fdf0ef;color:var(--ember);border:1px solid #f5c6c2}
.current-data{background:var(--paper2);border-radius:6px;padding:12px 14px;font-size:12px;color:var(--ink2);margin-bottom:14px}
.format-box{background:var(--ink);border-radius:6px;padding:12px 14px;font-family:monospace;font-size:11px;color:rgba(250,248,245,.8);overflow-x:auto;margin-top:8px;line-height:1.6}
</style>
</head>
<body>
<header class="hdr">
  <div class="hdr-logo">Cavnar <em>AI</em> <span style="font-family:'DM Sans',sans-serif;font-size:12px;color:var(--ink3);font-weight:400;margin-left:8px">/ Data Setup</span></div>
  <div class="hdr-right">
    <a href="/admin" class="back-btn">← Back to admin</a>
  </div>
</header>

<div class="container">
  <div class="page-title">{{ restaurant.name }}</div>
  <div class="page-sub">Set up real data for this client's labor and inventory modules.</div>

  <!-- LABOR MODULE -->
  <div class="module-card">
    <div class="module-hdr">
      <div class="module-title">Labor & Scheduling</div>
      <span class="module-status {{'status-live' if data.get('shifts_source') in ('upload','manual') else 'status-sample'}}">
        {{'Live data' if data.get('shifts_source') in ('upload','manual') else 'Using sample data'}}
      </span>
    </div>
    <div class="module-body">
      {% if data.get('shifts_csv') %}
      <div class="current-data">
        ✓ Real data loaded — uploaded {{ data.get('updated_at','')[:10] }} via {{ data.get('shifts_source','upload') }}
      </div>
      {% endif %}

      <div class="slabel">Upload method</div>
      <div class="tabs">
        <button class="mtab active" onclick="switchTab('shifts','upload',this)">Upload CSV</button>
        <button class="mtab" onclick="switchTab('shifts','paste',this)">Paste data</button>
        <button class="mtab" onclick="switchTab('shifts','guide',this)">POS export guide</button>
      </div>

      <div class="tab-content active" id="shifts-upload">
        <div style="background:var(--paper2);border-radius:var(--r);padding:12px 14px;margin-bottom:12px;font-size:12px;color:var(--ink2);line-height:1.7">
          <strong style="color:var(--ink1)">How to export your shift data:</strong><br>
          <span style="color:var(--ember)">Toast:</span> Reports → Labor → Timesheets → Export CSV &nbsp;·&nbsp;
          <span style="color:var(--ember)">Square:</span> Dashboard → Team → Timecards → Export &nbsp;·&nbsp;
          <span style="color:var(--ember)">Lightspeed:</span> Reports → Employees → Time Clock → Export
        </div>
        <div style="margin-bottom:8px">
          <a href="/client/sample-template/shifts" download="sample_shifts_template.csv" style="font-size:12px;color:var(--ember);text-decoration:none;font-weight:600">📥 Download sample shifts template</a>
          <span style="font-size:11px;color:var(--ink3);margin-left:8px">Required columns: date, employee, actual_hours, sales</span>
        </div>
        <div class="upload-zone" id="shifts-drop">
          <input type="file" accept=".csv" onchange="handleFile('shifts', this)">
          <div class="upload-icon">📂</div>
          <div class="upload-label">Drop CSV file here or click to browse</div>
          <div class="upload-sub">Exported from Toast, Square, Lightspeed, or any POS</div>
        </div>
        <div id="shifts-upload-name" style="font-size:12px;color:var(--green);margin-top:8px;display:none"></div>
        <button class="btn-primary" style="margin-top:12px" onclick="uploadData('shifts','upload')" id="shifts-upload-btn" disabled>Upload shifts data</button>
        <div class="result-msg" id="shifts-upload-result"></div>
      </div>

      <div class="tab-content" id="shifts-paste">
        <div class="slabel">Required CSV columns</div>
        <div class="format-box">date,day,shift,employee,role,scheduled_hours,actual_hours,sales_that_day</div>
        <div style="margin:10px 0 6px;font-size:11px;color:var(--ink3)">Paste your data below:</div>
        <textarea class="textarea" id="shifts-paste-content" placeholder="date,day,shift,employee,role,scheduled_hours,actual_hours,sales_that_day&#10;2026-05-01,Thursday,dinner,Maria G.,server,5,5.5,3200&#10;..."></textarea>
        <button class="btn-primary" style="margin-top:10px" onclick="uploadData('shifts','manual')">Save shifts data</button>
        <div class="result-msg" id="shifts-paste-result"></div>
      </div>

      <div class="tab-content" id="shifts-guide">
        <div class="slabel">How to export from your POS</div>
        <div class="pos-grid">
          <div class="pos-card">
            <div class="pos-name">🍞 Toast</div>
            <div class="pos-steps">Reports → Labor → Timesheets → Export CSV<br>Date range: last 2-4 weeks</div>
          </div>
          <div class="pos-card">
            <div class="pos-name">⬛ Square</div>
            <div class="pos-steps">Dashboard → Reports → Team → Timecards → Export</div>
          </div>
          <div class="pos-card">
            <div class="pos-name">⚡ Lightspeed</div>
            <div class="pos-steps">Reports → Staff → Time Tracking → Export CSV</div>
          </div>
          <div class="pos-card">
            <div class="pos-name">🔷 Aloha</div>
            <div class="pos-steps">Manager → Reports → Labor Detail → Export</div>
          </div>
          <div class="pos-card">
            <div class="pos-name">🔶 Clover</div>
            <div class="pos-steps">Reporting → Employees → Time Cards → Export</div>
          </div>
          <div class="pos-card">
            <div class="pos-name">📋 Manual/Other</div>
            <div class="pos-steps">Use the Paste tab and enter data in the required format</div>
          </div>
        </div>
        <div style="font-size:12px;color:var(--ink3);margin-top:4px">
          After exporting, the column names may differ — use the Paste tab to reformat into the required columns if needed.
        </div>
      </div>
    </div>
  </div>

  <!-- INVENTORY MODULE -->
  <div class="module-card">
    <div class="module-hdr">
      <div class="module-title">Inventory & Food Waste</div>
      <span class="module-status {{'status-live' if data.get('inventory_source') in ('upload','manual') else 'status-sample'}}">
        {{'Live data' if data.get('inventory_source') in ('upload','manual') else 'Using sample data'}}
      </span>
    </div>
    <div class="module-body">
      {% if data.get('inventory_csv') %}
      <div class="current-data">
        ✓ Real data loaded — uploaded {{ data.get('updated_at','')[:10] }} via {{ data.get('inventory_source','upload') }}
      </div>
      {% endif %}

      <div style="background:#f0faf4;border:1px solid #a7d7b8;border-radius:6px;padding:10px 14px;margin-bottom:12px;font-size:12px;color:#2d6a4f;line-height:1.6">
        <strong>How this works:</strong> You manage inventory data on behalf of your client.
        Ask them to export a weekly CSV from their POS or inventory system (Toast, Square, Sysco, etc.)
        and email/send it to you. Upload it here and their dashboard updates automatically.
        Clients with no existing system get the template below to fill out manually each week.
      </div>
      <div style="margin-bottom:14px">
        <a href="/admin/inventory-template" download
           style="display:inline-block;padding:7px 14px;background:white;border:1px solid var(--paper3);border-radius:6px;font-size:12px;font-weight:600;color:var(--ink2);text-decoration:none">
          ⬇ Download CSV template
        </a>
        <span style="font-size:11px;color:var(--ink3);margin-left:8px">Send this to clients who don't have an inventory system</span>
      </div>
      <div class="slabel">Upload method</div>
      <div class="tabs">
        <button class="mtab active" onclick="switchTab('inv','upload',this)">Upload CSV</button>
        <button class="mtab" onclick="switchTab('inv','paste',this)">Paste data</button>
        <button class="mtab" onclick="switchTab('inv','guide',this)">Format guide</button>
      </div>

      <div class="tab-content active" id="inv-upload">
        <div style="background:var(--paper2);border-radius:var(--r);padding:12px 14px;margin-bottom:12px;font-size:12px;color:var(--ink2);line-height:1.7">
          <strong style="color:var(--ink1)">How to export your inventory data:</strong><br>
          <span style="color:var(--ember)">Toast:</span> Inventory → Items → Export CSV &nbsp;·&nbsp;
          <span style="color:var(--ember)">Square:</span> Items → Inventory → Export &nbsp;·&nbsp;
          <span style="color:var(--ember)">Other:</span> Any CSV with item name, quantity, unit cost columns works
        </div>
        <div style="margin-bottom:8px">
          <a href="/client/sample-template/inventory" download="sample_inventory_template.csv" style="font-size:12px;color:var(--ember);text-decoration:none;font-weight:600">📥 Download sample inventory template</a>
          <span style="font-size:11px;color:var(--ink3);margin-left:8px">Required columns: item, current_stock, par_level, unit_cost, waste_last_week</span>
        </div>
        <div class="upload-zone">
          <input type="file" accept=".csv" onchange="handleFile('inv', this)">
          <div class="upload-icon">📦</div>
          <div class="upload-label">Drop inventory CSV here or click to browse</div>
          <div class="upload-sub">Exported from your inventory system or POS</div>
        </div>
        <div id="inv-upload-name" style="font-size:12px;color:var(--green);margin-top:8px;display:none"></div>
        <button class="btn-primary" style="margin-top:12px" onclick="uploadData('inv','upload')" id="inv-upload-btn" disabled>Upload inventory data</button>
        <div class="result-msg" id="inv-upload-result"></div>
      </div>

      <div class="tab-content" id="inv-paste">
        <div class="slabel">Required CSV columns</div>
        <div class="format-box">item,category,unit,par_level,current_stock,unit_cost,avg_daily_usage,last_ordered,last_order_qty,waste_last_week</div>
        <div style="margin:10px 0 6px;font-size:11px;color:var(--ink3)">Paste your data below:</div>
        <textarea class="textarea" id="inv-paste-content" placeholder="item,category,unit,par_level,current_stock,unit_cost,avg_daily_usage,last_ordered,last_order_qty,waste_last_week&#10;Salmon fillet,protein,lb,20,18,14.50,3.2,2026-05-01,30,5.0&#10;..."></textarea>
        <button class="btn-primary" style="margin-top:10px" onclick="uploadData('inv','manual')">Save inventory data</button>
        <div class="result-msg" id="inv-paste-result"></div>
      </div>

      <div class="tab-content" id="inv-guide">
        <div class="slabel">Column reference</div>
        <div class="format-box">item            — Item name (e.g. "Salmon fillet")
category        — protein / dairy / produce / dry / beverage
unit            — lb / oz / unit / case / liter / qt
par_level       — Your target stock level
current_stock   — What you have right now
unit_cost       — Cost per unit in dollars
avg_daily_usage — Average units used per day
last_ordered    — Date of last order (YYYY-MM-DD)
last_order_qty  — Units ordered last time
waste_last_week — Units wasted in the last 7 days</div>
        <div style="font-size:12px;color:var(--ink3);margin-top:10px">
          Most inventory systems can export a product list. You may need to add the waste_last_week column manually based on your waste log.
        </div>
      </div>
    </div>
  </div>
</div>



<script>
const restaurantId = {{ restaurant.id }};
const fileData = {shifts: null, inv: null};

function switchTab(module, tab, btn) {
  const prefix = module === 'shifts' ? 'shifts' : 'inv';
  document.querySelectorAll(`#${prefix}-upload, #${prefix}-paste, #${prefix}-guide`).forEach(el => el.classList.remove('active'));
  document.getElementById(`${prefix}-${tab}`).classList.add('active');
  btn.closest('.tabs').querySelectorAll('.mtab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function handleFile(module, input) {
  const file = input.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = e => {
    fileData[module] = e.target.result;
    const nameEl = document.getElementById(module + '-upload-name');
    nameEl.textContent = '✓ ' + file.name + ' (' + (file.size/1024).toFixed(1) + ' KB)';
    nameEl.style.display = 'block';
    document.getElementById(module + '-upload-btn').disabled = false;
  };
  reader.readAsText(file);
}

async function uploadData(module, source) {
  const dataType = module === 'shifts' ? 'shifts' : 'inventory';
  const resultId = module + '-' + (source === 'upload' ? 'upload' : 'paste') + '-result';
  const resultEl = document.getElementById(resultId);

  const form = new FormData();
  form.append('data_type', dataType);
  form.append('source', source);

  if (source === 'upload') {
    const fileInput = document.querySelector('#' + module + '-drop input[type=file]');
    if (!fileInput || !fileInput.files[0]) {
      showResult(resultEl, false, 'No file selected'); return;
    }
    form.append('csv_file', fileInput.files[0]);
  } else {
    // paste/manual — wrap as a blob so the server receives it as csv_file
    const csvContent = document.getElementById(module + '-paste-content').value;
    if (!csvContent.trim()) { showResult(resultEl, false, 'No data entered'); return; }
    form.append('csv_file', new Blob([csvContent], {type:'text/csv'}), 'data.csv');
  }

  // Use client route (works for both admins and regular clients)
  const res = await fetch('/client/upload-data', {method:'POST', body: form});
  const data = await res.json();
  if (data.ok) {
    showResult(resultEl, true, '✓ ' + data.rows + ' rows loaded. Refreshing dashboard...');
    setTimeout(() => location.reload(), 1800);
  } else {
    showResult(resultEl, false, data.error || 'Upload failed');
  }
}

function showResult(el, ok, msg) {
  el.style.display = 'block';
  el.className = 'result-msg ' + (ok ? 'result-ok' : 'result-err');
  el.textContent = msg;
}


</script>
</body>
</html>"""

ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cavnar AI — Admin</title>
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="shortcut icon" href="/favicon.ico">
<meta name="theme-color" content="#0e0c0a">
<link href="https://fonts.googleapis.com/css2?family=DM+Serif+Display:ital@0;1&family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{--ink:#0e0c0a;--ink2:#3a3530;--ink3:#7a736a;--paper:#f7f4ef;--paper2:#edeae3;--paper3:#e0dbd0;--ember:#c84b2f;--green:#2d6a4f;--green-bg:#eaf4ee;--r:8px}
body{font-family:'DM Sans',sans-serif;background:var(--paper);color:var(--ink);font-size:14px}
.hdr{background:var(--ink);height:54px;display:flex;align-items:center;padding:0 28px;justify-content:space-between}
.hdr-logo{font-family:'DM Serif Display',serif;font-size:16px;color:var(--paper)}
.hdr-logo em{color:#e8956a;font-style:italic}
.hdr-badge{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;padding:3px 8px;background:var(--ember);color:white;border-radius:4px;margin-left:10px}
.logout-btn{font-size:11px;color:var(--ink3);text-decoration:none;padding:5px 10px;border:1px solid #2a2520;border-radius:4px}
.container{max-width:900px;margin:0 auto;padding:32px 24px}
.section-title{font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);margin-bottom:10px}
.card{background:white;border:1px solid var(--paper3);border-radius:var(--r);padding:20px;margin-bottom:20px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group.full{grid-column:1/-1}
label{font-size:10px;font-weight:600;letter-spacing:.07em;text-transform:uppercase;color:var(--ink3)}
input,select{padding:9px 12px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:13px;color:var(--ink);background:white;outline:none;transition:border .15s;width:100%}
input:focus,select:focus{border-color:var(--ember)}
.btn{padding:9px 18px;border-radius:6px;border:none;font-family:'DM Sans',sans-serif;font-size:12px;font-weight:600;cursor:pointer;transition:all .15s}
.btn-primary{background:var(--ember);color:white}
.btn-primary:hover{background:#a83d25}
.tbl{width:100%;border-collapse:collapse;font-size:13px}
.tbl th{text-align:left;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--ink3);padding:8px 12px;border-bottom:1px solid var(--paper3);background:var(--paper2)}
.tbl td{padding:10px 12px;border-bottom:1px solid var(--paper3);overflow:visible}
.tbl tr:last-child td{border-bottom:none}
.badge-active{background:var(--green-bg);color:var(--green);font-size:10px;padding:2px 7px;border-radius:20px;font-weight:500}
.status-msg{padding:10px 14px;border-radius:6px;font-size:13px;margin-top:10px;display:none}
.status-ok{background:var(--green-bg);color:var(--green);border:1px solid #b7dfca}
.status-err{background:#fdf0ef;color:var(--ember);border:1px solid #f5c6c2}
.action-menu{position:relative;display:inline-block}
.action-menu-btn{padding:5px 12px;border-radius:4px;border:1px solid var(--paper3);background:white;font-family:'DM Sans',sans-serif;font-size:11px;font-weight:500;cursor:pointer;color:var(--ink2);white-space:nowrap}
.action-menu-btn:hover{background:var(--ink);color:white;border-color:var(--ink)}
.action-dropdown{display:none;position:absolute;right:0;top:calc(100% + 4px);background:white;border:1px solid var(--paper3);border-radius:6px;box-shadow:0 6px 20px rgba(14,12,10,.15);min-width:175px;z-index:9999}
.action-dropdown.open{display:block}
.action-item{display:block;width:100%;box-sizing:border-box;text-align:left;padding:9px 14px;font-family:'DM Sans',sans-serif;font-size:12px;color:var(--ink2);background:white;border:none;border-bottom:1px solid var(--paper3);cursor:pointer;white-space:nowrap}
.action-item:last-child{border-bottom:none}
.action-item:hover{background:var(--paper2);color:var(--ink)}
.action-item-danger{color:#c0392b}
.action-item-danger:hover{background:#fdf0ef;color:#c0392b}
.action-item-success{color:var(--green)}
.action-item-success:hover{background:var(--green-bg);color:var(--green)}
.group-filter-btn{padding:3px 10px;border-radius:20px;border:1px solid var(--paper3);background:white;font-family:'DM Sans',sans-serif;font-size:11px;cursor:pointer;color:var(--ink2);transition:all .15s}
.group-filter-btn:hover,.group-filter-btn.active{background:var(--ink);color:white;border-color:var(--ink)}
</style>
</head>
<body>
<header class="hdr">
  <div style="display:flex;align-items:center">
    <div class="hdr-logo">Cavnar <em>AI</em></div>
    <span class="hdr-badge">Admin</span>
  </div>
  <a href="/logout" class="logout-btn">Sign out</a>
</header>
<div class="container">

  <!-- MRR Stats Bar -->
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:24px">
    <div style="background:var(--ink);border-radius:var(--r);padding:14px 16px">
      <div style="font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Active Clients</div>
      <div style="font-family:'DM Serif Display',serif;font-size:28px;color:var(--paper)">{{users|selectattr('is_active')|selectattr('is_admin','equalto',0)|list|length}}</div>
    </div>
    <div style="background:var(--ink);border-radius:var(--r);padding:14px 16px">
      <div style="font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">MRR</div>
      <div style="font-family:'DM Serif Display',serif;font-size:28px;color:#6fcf97">${{mrr|default(0)|int|format_num}}</div>
    </div>
    <div style="background:var(--ink);border-radius:var(--r);padding:14px 16px">
      <div style="font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Annual Run Rate</div>
      <div style="font-family:'DM Serif Display',serif;font-size:28px;color:#6fcf97">${{(mrr|default(0)*12)|int|format_num}}</div>
    </div>
    <div style="background:var(--ink);border-radius:var(--r);padding:14px 16px">
      <div style="font-size:10px;color:var(--ink3);text-transform:uppercase;letter-spacing:.1em;margin-bottom:4px">Contracts Signed</div>
      <div style="font-family:'DM Serif Display',serif;font-size:28px;color:var(--ember)">{{users|selectattr('contract_status','equalto','signed')|selectattr('is_admin','equalto',0)|list|length}} / {{users|selectattr('is_active')|selectattr('is_admin','equalto',0)|list|length}}</div>
    </div>
  </div>

  <div class="section-title">Create new client account</div>
  <div class="card">
    <div class="form-grid">
      <div class="form-group"><label>Restaurant name</label><input type="text" id="r-name" placeholder="Maplewood Kitchen"></div>
      <div class="form-group"><label>Owner email</label><input type="email" id="r-email" placeholder="owner@restaurant.com"></div>
      <div class="form-group"><label>Dashboard username</label><input type="text" id="u-username" placeholder="maplewoodkitchen"></div>
      <div class="form-group"><label>Owner / GM name</label><input type="text" id="r-owner-name" placeholder="e.g. Sarah"></div>
      <div class="form-group">
        <label>Temporary password</label>
        <div style="display:flex;gap:6px;align-items:center">
          <input type="text" id="u-password" placeholder="Click Generate →" style="flex:1">
          <button type="button" onclick="genPassword()"
            style="padding:9px 12px;background:#c84b2f;color:white;border:none;border-radius:6px;font-family:'DM Sans',sans-serif;font-size:11px;font-weight:600;cursor:pointer;white-space:nowrap;flex-shrink:0">
            Generate
          </button>
          <button type="button" onclick="copyPassword()" id="copy-pw-btn"
            style="padding:9px 12px;background:white;color:var(--ink2);border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:11px;font-weight:500;cursor:pointer;white-space:nowrap;flex-shrink:0">
            Copy
          </button>
        </div>
      </div>
      <div class="form-group"><label>Owner phone number</label><input type="text" id="r-phone" placeholder="(312) 555-0100"></div>
      <div class="form-group"><label>Location group (optional)</label><input type="text" id="r-group" placeholder="e.g. Syrup" list="existing-groups"><datalist id="existing-groups">{% for g in location_groups %}<option value="{{g}}">{% endfor %}</datalist></div>
      <div class="form-group"><label>Location name (optional)</label><input type="text" id="r-location" placeholder="e.g. Lincoln Park"></div>
      <div class="form-group"><label>Google Place ID (optional)</label><input type="text" id="r-google" placeholder="ChIJ..."></div>
      <div class="form-group"><label>Yelp Business ID (optional)</label><input type="text" id="r-yelp" placeholder="restaurant-name-chicago"></div>
      <div class="form-group full"><label>Owner voice notes (for AI drafting)</label><input type="text" id="r-voice" placeholder="Warm, casual tone. Always invite guests back. Never sound corporate."></div>
      <div class="form-group full">
        <label>Modules (check all that apply)</label>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px">
          <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;cursor:pointer;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none">
            <input type="checkbox" id="mod-reviews" value="reviews" checked style="width:14px;height:14px;accent-color:#c84b2f">
            Review Intelligence — $300/mo
          </label>
          <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;cursor:pointer;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none">
            <input type="checkbox" id="mod-labor" value="labor" style="width:14px;height:14px;accent-color:#c84b2f">
            Labor Optimizer — $300/mo
          </label>
          <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;cursor:pointer;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none">
            <input type="checkbox" id="mod-inventory" value="inventory" style="width:14px;height:14px;accent-color:#c84b2f">
            Inventory Control — $300/mo
          </label>
          <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;background:var(--paper2);border:1px solid var(--paper3);border-radius:6px;cursor:pointer;font-size:13px;font-weight:400;letter-spacing:0;text-transform:none">
            <input type="checkbox" id="mod-marketing" value="marketing" style="width:14px;height:14px;accent-color:#c84b2f">
            Marketing Autopilot — $300/mo
          </label>
        </div>

      </div>
    </div>
    <div style="background:#f0faf4;border:1px solid #a7d7b8;border-radius:6px;padding:8px 14px;margin-top:14px;font-size:12px;color:#2d6a4f">
      ✓ Contract sent automatically on creation. Payment link and welcome email sent automatically after client signs.
    </div>
    <button class="btn btn-primary" style="margin-top:12px" onclick="createClient()">Create client account</button>
    <div class="status-msg" id="create-status"></div>
  </div>

  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
    <div class="section-title" style="margin-bottom:0">Active client accounts</div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
      <input type="text" id="client-search" placeholder="Search clients…" oninput="searchClients(this.value)"
        style="padding:6px 12px;border:1px solid var(--paper3);border-radius:6px;font-family:'DM Sans',sans-serif;font-size:12px;width:200px;outline:none">
      {% if location_groups %}
      <span style="font-size:11px;color:var(--ink3)">Filter:</span>
      <button onclick="filterGroup('')" class="group-filter-btn active" id="filter-all">All</button>
      {% for g in location_groups %}
      <button onclick="filterGroup('{{g}}')" class="group-filter-btn" id="filter-{{g|replace(' ','-')}}">{{g}}</button>
      {% endfor %}
      {% endif %}
    </div>
  </div>
  <div style="display:flex;flex-direction:column;gap:10px">
      {% for user in users %}
      <div class="client-row card" data-group="{{user.location_group or ''}}" style="padding:14px 18px">

        <!-- Top row: name + health + actions -->
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:10px">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <strong style="font-size:14px">{{user.restaurant_name}}</strong>
            {% if user.location_group %}<span style="font-size:11px;color:var(--ink3)">{{user.location_group}}{% if user.location_name %} · {{user.location_name}}{% endif %}</span>{% endif %}
            {% if user.internal_notes %}<span title="{{user.internal_notes}}" style="cursor:help;font-size:10px;background:var(--amber-bg);color:var(--amber);padding:1px 6px;border-radius:10px;font-weight:500">note</span>{% endif %}

            <!-- Health pill -->
            {% set hc = {'green':'#2d6a4f','amber':'#b7791f','red':'#c84b2f'} %}
            {% set hbg = {'green':'#eaf4ee','amber':'#fef9ec','red':'#fef2f2'} %}
            {% set hl = user.health or 'amber' %}
            <span style="font-size:10px;font-weight:700;padding:2px 9px;border-radius:20px;background:{{hbg[hl]}};color:{{hc[hl]}}">{{hl|upper}}</span>

            <!-- Billing -->
            {% set bc = {'trial':'#b7791f','active':'#2d6a4f','paused':'#6b7280','churned':'#c0392b'} %}
            <span style="font-size:10px;font-weight:500;padding:2px 8px;border-radius:20px;background:{% if user.billing_status=='active' %}var(--green-bg){% elif user.billing_status=='trial' %}var(--amber-bg){% else %}#f3f4f6{% endif %};color:{{bc.get(user.billing_status,'#6b7280')}}">{{(user.billing_status or 'trial')|title}}</span>

            <!-- Contract -->
            {% if not user.is_admin %}
              {% if user.contract_status == 'signed' %}<span style="font-size:10px;color:#2d6a4f;font-weight:600">✓ Signed</span>
              {% elif user.contract_status == 'sent' %}<span style="font-size:10px;color:#b7791f;font-weight:600">⏳ Contract pending</span>
              {% else %}<span style="font-size:10px;color:#9ca3af">No contract</span>{% endif %}
            {% endif %}

            <!-- Pending reviews -->
            {% if user.pending_reviews > 0 %}
            <span style="font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px;background:{% if user.pending_reviews > 5 %}#fef2f2{% else %}#fef9ec{% endif %};color:{% if user.pending_reviews > 5 %}#c84b2f{% else %}#b7791f{% endif %}">
              {{user.pending_reviews}} reviews pending
            </span>
            {% endif %}
          </div>

          <!-- Actions -->
          {% if not user.is_admin %}
          <div class="action-menu" id="menu-wrap-{{user.id}}" style="flex-shrink:0">
            <button class="action-menu-btn" onclick="toggleMenu({{user.id}})">Actions ▾</button>
            <div class="action-dropdown" id="menu-{{user.id}}">
              <button class="action-item" onclick="window.location='/admin/client-settings/{{user.restaurant_id}}'">Settings</button>
              <button class="action-item" onclick="window.location='/admin/client-data/{{user.restaurant_id}}'">Manage data</button>
              <button class="action-item" onclick="window.location='/admin/view-as/{{user.restaurant_id}}'">View as client</button>
              {% if user.is_active %}
              <div class="action-divider"></div>
              <button class="action-item" onclick="resendPayment({{user.restaurant_id}},'{{user.email}}','{{user.billing_status}}');closeMenu({{user.id}})">Resend payment link</button>
              <button class="action-item" onclick="resendContract({{user.restaurant_id}});closeMenu({{user.id}})">Resend contract</button>
              <button class="action-item" onclick="fetchReviewsNow({{user.restaurant_id}});closeMenu({{user.id}})">Fetch reviews now</button>
              <button class="action-item" onclick="seedReviews({{user.restaurant_id}});closeMenu({{user.id}})">Seed sample reviews</button>
              <div class="action-divider"></div>
              <button class="action-item action-item-danger" onclick="deactivateClient({{user.id}},'{{user.restaurant_name}}');closeMenu({{user.id}})">Deactivate</button>
              {% else %}
              <div class="action-divider"></div>
              <button class="action-item action-item-success" onclick="reactivateClient({{user.id}},'{{user.restaurant_name}}');closeMenu({{user.id}})">Reactivate</button>
              {% endif %}
            </div>
          </div>
          {% endif %}
        </div>

        <!-- Detail row + usage toggle -->
        <div style="display:flex;flex-wrap:wrap;gap:16px;font-size:11px;color:var(--ink3);border-top:1px solid var(--paper2);padding-top:8px;margin-top:2px;align-items:center">
          <span><strong style="color:var(--ink2)">User:</strong> {{user.username}}</span>
          <span><strong style="color:var(--ink2)">Email:</strong> {{user.email}}</span>
          {% if user.phone %}<span><strong style="color:var(--ink2)">Phone:</strong> {{user.phone}}</span>{% endif %}
          <span><strong style="color:var(--ink2)">Modules:</strong>
            {% set mods = [] %}
            {% if user.module_reviews %}{% set _ = mods.append('Reviews') %}{% endif %}
            {% if user.module_labor %}{% set _ = mods.append('Labor') %}{% endif %}
            {% if user.module_inventory %}{% set _ = mods.append('Inventory') %}{% endif %}
            {% if user.module_marketing %}{% set _ = mods.append('Marketing') %}{% endif %}
            {{ mods|join(', ') if mods else 'None' }}
          </span>
          <span><strong style="color:var(--ink2)">Last login:</strong> {% if user.last_login and user.last_login|length >= 10 %}{% set d=user.last_login[:10].split('-') %}{{d[1]|int}}/{{d[2]|int}}/{{d[0][2:]}}{% else %}Never{% endif %}</span>
          {% if user.last_active_tab %}<span><strong style="color:var(--ink2)">Last tab:</strong> {{user.last_active_tab}}</span>{% endif %}
          <span><strong style="color:var(--ink2)">Reviews fetched:</strong> {{user.last_fetched_at or 'Never'}}</span>
          <span><strong style="color:var(--ink2)">Account:</strong> {% if user.is_active %}<span style="color:#2d6a4f">Active</span>{% else %}<span style="color:#9ca3af">Inactive</span>{% endif %}</span>
          {% if not user.is_admin %}
          <button onclick="toggleUsage({{user.restaurant_id}}, this)" style="font-size:10px;color:var(--ember);background:none;border:none;cursor:pointer;padding:0;font-weight:500;margin-left:auto">Show usage ▾</button>
          {% endif %}
        </div>
        {% if not user.is_admin %}
        <div id="usage-{{user.restaurant_id}}" style="display:none;margin-top:8px;padding-top:8px;border-top:1px solid var(--paper2)">
          <div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--ink3);margin-bottom:6px">30-day usage</div>
          <div id="usage-data-{{user.restaurant_id}}" style="font-size:12px;color:var(--ink3)">Loading...</div>
        </div>
        {% endif %}

      </div>
      {% else %}
      <div class="card" style="padding:16px;color:var(--ink3);font-style:italic">No clients yet — create one above.</div>
      {% endfor %}
  </div>
</div>

<script>
function filterGroup(group) {
  document.querySelectorAll('.group-filter-btn').forEach(b=>b.classList.remove('active'));
  const activeBtn = group ? document.getElementById('filter-'+group.replace(/ /g,'-')) : document.getElementById('filter-all');
  if(activeBtn) activeBtn.classList.add('active');
  document.querySelectorAll('.client-row').forEach(row=>{
    if(!group || row.dataset.group===group) row.style.display='';
    else row.style.display='none';
  });
}
function genPassword() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789';
  let pw = '';
  for(let i=0;i<10;i++) pw += chars[Math.floor(Math.random()*chars.length)];
  document.getElementById('u-password').value = pw;
  const btn = document.getElementById('copy-pw-btn');
  btn.textContent = 'Copy';
  btn.style.color = 'var(--ink2)';
}
function copyPassword() {
  const pw = document.getElementById('u-password').value;
  if(!pw) { return; }
  navigator.clipboard.writeText(pw).then(() => {
    const btn = document.getElementById('copy-pw-btn');
    btn.textContent = '✓ Copied';
    btn.style.color = 'var(--green)';
    setTimeout(() => { btn.textContent = 'Copy'; btn.style.color = 'var(--ink2)'; }, 2000);
  });
}
function toggleUsage(rid, btn){
  const div = document.getElementById('usage-'+rid);
  if(!div) return;
  if(div.style.display === 'none'){
    div.style.display = 'block';
    btn.textContent = 'Hide usage ▴';
    const dataDiv = document.getElementById('usage-data-'+rid);
    if(dataDiv.textContent === 'Loading...'){
      fetch('/admin/api/client-usage/'+rid).then(r=>r.json()).then(d=>{
        if(!d.ok){ dataDiv.textContent = 'No data yet.'; return; }
        const tabs = d.tab_counts || {};
        const events = d.event_counts || {};
        let html = '';
        if(Object.keys(tabs).length){
          html += '<div style="margin-bottom:6px"><strong style="color:var(--ink2)">Tab views:</strong> ';
          html += Object.entries(tabs).sort((a,b)=>b[1]-a[1]).map(([k,v])=>k+' ('+v+')').join(' · ');
          html += '</div>';
        }
        if(events.login) html += '<span style="margin-right:12px">🔑 Logins: <strong>'+events.login+'</strong></span>';
        if(events.review_approved) html += '<span style="margin-right:12px">✓ Approvals: <strong>'+events.review_approved+'</strong></span>';
        if(events.csv_upload) html += '<span>📤 CSV uploads: <strong>'+events.csv_upload+'</strong></span>';
        if(!html) html = 'No activity logged yet.';
        dataDiv.innerHTML = html;
      }).catch(()=>{ dataDiv.textContent = 'Error loading usage.'; });
    }
  } else {
    div.style.display = 'none';
    btn.textContent = 'Show usage ▾';
  }
}

function toast(msg){
  let t = document.getElementById('admin-toast');
  if(!t){
    t = document.createElement('div');
    t.id = 'admin-toast';
    t.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#0e0c0a;color:white;padding:10px 20px;border-radius:8px;font-size:13px;z-index:9999;opacity:0;transition:opacity .3s;pointer-events:none';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.opacity = '1';
  setTimeout(()=>{ t.style.opacity = '0'; }, 2600);
}

function toggleMenu(id) {
  event.stopPropagation();
  const menu = document.getElementById('menu-'+id);
  const isOpen = menu.classList.contains('open');
  document.querySelectorAll('.action-dropdown').forEach(m => m.classList.remove('open'));
  if(!isOpen) menu.classList.add('open');
}
function closeMenu(id) {
  setTimeout(() => {
    const menu = document.getElementById('menu-'+id);
    if(menu) menu.classList.remove('open');
  }, 100);
}
document.addEventListener('click', function(e) {
  if(!e.target.closest('.action-menu')) {
    document.querySelectorAll('.action-dropdown').forEach(m => m.classList.remove('open'));
  }
});
async function createClient() {
  const btn = document.querySelector('.btn-primary');
  const status = document.getElementById('create-status');
  btn.textContent = 'Creating…'; btn.disabled = true;
  const payload = {
    restaurant_name: document.getElementById('r-name').value,
    owner_email:     document.getElementById('r-email').value,
    username:        document.getElementById('u-username').value,
    password:        document.getElementById('u-password').value,
    google_place_id: document.getElementById('r-google').value,
    yelp_business_id:document.getElementById('r-yelp').value,
    owner_name:      document.getElementById('r-owner-name') ? document.getElementById('r-owner-name').value : '',
    voice_notes:     document.getElementById('r-voice').value,
    owner_phone:     document.getElementById('r-phone').value,
    location_group:  document.getElementById('r-group') ? document.getElementById('r-group').value : '',
    location_name:   document.getElementById('r-location') ? document.getElementById('r-location').value : '',
    module_reviews:  document.getElementById('mod-reviews').checked ? 1 : 0,
    module_labor:    document.getElementById('mod-labor').checked ? 1 : 0,
    module_inventory:document.getElementById('mod-inventory').checked ? 1 : 0,
    module_marketing:document.getElementById('mod-marketing').checked ? 1 : 0,

  };
  try {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 20000);
    const res = await fetch('/admin/create-client', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
      signal: controller.signal
    });
    clearTimeout(timeout);
    const data = await res.json();
    status.style.display = 'block';
    if (data.ok) {
      status.className = 'status-msg status-ok';
      let msg = '✓ Client created — username: ' + payload.username;
      if (data.docusign_skipped) msg += '. (DocuSign unavailable — send contract manually)';
      status.textContent = msg;
      setTimeout(() => location.reload(), 1800);
    } else {
      status.className = 'status-msg status-err';
      status.textContent = data.error || 'Something went wrong';
    }
  } catch(e) {
    status.style.display = 'block';
    status.className = 'status-msg status-err';
    if(e.name === 'AbortError') {
      status.textContent = 'Request timed out — check Railway logs and try again';
    } else {
      status.textContent = 'Request failed: ' + e.message;
    }
  }
  btn.textContent = 'Create client account'; btn.disabled = false;
}
function searchClients(query) {
  const q = query.toLowerCase();
  document.querySelectorAll('.client-row').forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = (!q || text.includes(q)) ? '' : 'none';
  });
}

async function resendContract(restaurantId) {
  const btn = event.target;
  btn.textContent = 'Sending…'; btn.disabled = true;
  const res = await fetch('/admin/resend-contract/' + restaurantId, {method:'POST'});
  const data = await res.json();
  if (data.ok) {
    btn.textContent = '✓ Sent';
    setTimeout(() => { btn.textContent = 'Resend contract'; btn.disabled = false; }, 3000);
  } else {
    btn.textContent = 'Error: ' + (data.error || 'failed');
    btn.disabled = false;
  }
}

async function resendPayment(restaurantId, email, billing) {
  const btn = event.target;
  btn.textContent = 'Sending…'; btn.disabled = true;
  const res = await fetch('/admin/resend-payment/' + restaurantId, {method:'POST'});
  const data = await res.json();
  if (data.ok) {
    btn.textContent = '✓ Sent';
    btn.style.background = '#eaf2ed';
    setTimeout(() => { btn.textContent = 'Resend payment'; btn.disabled = false; }, 3000);
  } else {
    btn.textContent = 'Error';
    btn.disabled = false;
    console.error(data.error);
  }
}

async function fetchReviewsNow(rid){
  if(!confirm('Fetch latest reviews from Google/Yelp for this restaurant?')) return;
  const btn = event.target;
  btn.textContent = 'Fetching...'; btn.disabled = true;
  try {
    const controller = new AbortController();
    const timeout = setTimeout(()=>controller.abort(), 30000);
    const res = await fetch('/admin/fetch-reviews/'+rid, {method:'POST', signal:controller.signal});
    clearTimeout(timeout);
    const data = await res.json();
    if(data.ok){
      const n = data.new_reviews || data.new_count || 0;
      toast('Fetched ' + n + ' new reviews ✓' + (data.errors&&data.errors.length?' ('+data.errors.length+' warnings)':''));
    } else {
      toast('Error: ' + (data.error||'unknown'));
    }
  } catch(e) {
    if(e.name==='AbortError') toast('Timed out — try again or check Railway logs');
    else toast('Fetch failed: '+e.message);
  }
  btn.textContent = 'Fetch reviews now'; btn.disabled = false;
}

async function seedReviews(restaurantId) {
  const btn = event.target;
  btn.textContent = 'Seeding…'; btn.disabled = true;
  const res = await fetch('/admin/seed-reviews/' + restaurantId, {method:'POST'});
  const data = await res.json();
  if (data.ok) {
    btn.textContent = '✓ Seeded ' + data.seeded + ' reviews';
    setTimeout(() => { btn.textContent = 'Seed reviews'; btn.disabled = false; }, 3000);
  } else {
    btn.textContent = 'Error'; btn.disabled = false;
  }
}

async function deactivateClient(id, name) {
  const btn = event.target;
  btn.textContent = 'Deactivating...';
  btn.disabled = true;
  try {
    const res = await fetch('/admin/deactivate-client/' + id, {method:'POST', headers:{'Content-Type':'application/json'}});
    const data = await res.json();
    if (data.ok) { location.reload(); }
    else { btn.textContent = 'Error'; console.error(data); }
  } catch(e) { btn.textContent = 'Error'; console.error(e); }
}
async function reactivateClient(id, name) {
  const btn = event.target;
  btn.textContent = 'Reactivating...';
  btn.disabled = true;
  try {
    const res = await fetch('/admin/reactivate-client/' + id, {method:'POST', headers:{'Content-Type':'application/json'}});
    const data = await res.json();
    if (data.ok) { location.reload(); }
    else { btn.textContent = 'Error'; console.error(data); }
  } catch(e) { btn.textContent = 'Error'; console.error(e); }
}

async function addStaffNote() {
  const name = document.getElementById('staff-name').value.trim();
  const notes = document.getElementById('staff-constraint').value.trim();
  const result = document.getElementById('staff-note-result');
  if(!name || !notes) { showResult(result, false, 'Enter both a name and constraint'); return; }
  const form = new FormData();
  form.append('employee_name', name);
  form.append('notes', notes);
  const res = await fetch('/admin/staff-notes/' + restaurantId, {method:'POST', body: form});
  const data = await res.json();
  if(data.ok) {
    showResult(result, true, '✓ Constraint saved');
    setTimeout(() => location.reload(), 1000);
  } else {
    showResult(result, false, data.error || 'Save failed');
  }
}
async function deleteNote(noteId) {
  const res = await fetch('/admin/staff-notes/' + noteId + '/delete', {method:'POST'});
  const data = await res.json();
  if(data.ok) location.reload();
}
</script>
  <!-- Email Log -->
  <div style="max-width:860px;margin:0 auto">
  <div style="display:flex;align-items:center;justify-content:space-between;margin:12px 0 8px">
    <div class="section-title" style="margin-bottom:0">Email log</div>
    <span style="font-size:11px;color:var(--ink3)">Last 50 emails sent</span>
  </div>
  <div class="card" style="padding:0;overflow:auto;margin-bottom:32px">
    <table class="tbl">
      <thead><tr><th>Time</th><th>Client</th><th>Type</th><th>To</th></tr></thead>
      <tbody>
      {% for log in email_log %}
      <tr>
        <td style="font-size:11px;color:var(--ink3);white-space:nowrap">
          {% if log.sent_at and log.sent_at|length >= 10 %}{% set d=log.sent_at[:10].split('-') %}{{d[1]|int}}/{{d[2]|int}}/{{d[0][2:]}}{% else %}—{% endif %}
          <span style="color:var(--paper3)"> · </span>{% set hr=log.sent_at[11:13]|int %}{% set mn=log.sent_at[14:16] %}{% set ampm='am' if hr < 12 else 'pm' %}{% set hr12=hr if hr <= 12 else hr-12 %}{% set hr12=12 if hr12==0 else hr12 %}{{hr12}}:{{mn}}{{ampm}}
        </td>
        <td style="font-size:12px">{{log.restaurant_name or '—'}}</td>
        <td>
          {% set type_colors = {'welcome':'#2d6a4f','payment':'#b7791f','contract':'#1a56cc','digest':'#7a736a'} %}
          <span style="font-size:10px;font-weight:600;padding:2px 8px;border-radius:20px;
            background:{{'#eaf4ee' if log.email_type=='welcome' else ('#fef9ec' if log.email_type=='payment' else ('#e8f0fe' if log.email_type=='contract' else '#f3f4f6'))}};
            color:{{type_colors.get(log.email_type,'#6b7280')}}">
            {{log.email_type|title}}
          </span>
        </td>
        <td style="font-size:12px;color:var(--ink3)">{{log.to_email}}</td>
      </tr>
      {% else %}
      <tr><td colspan="4" style="color:var(--ink3);font-style:italic;padding:16px">No emails logged yet.</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  </div>

  <!-- Activity Feed -->
  <div style="max-width:860px;margin:0 auto 32px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin:12px 0 8px">
      <div class="section-title" style="margin-bottom:0">Activity feed</div>
      <span style="font-size:11px;color:var(--ink3)">Recent client actions</span>
    </div>
    <div class="card" style="padding:0;overflow:auto">
      <table class="tbl">
        <thead><tr><th>Time</th><th>Client</th><th>Action</th></tr></thead>
        <tbody>
        {% if activity_feed %}
          {% for item in activity_feed %}
          <tr>
            <td style="font-size:11px;color:var(--ink3);white-space:nowrap">
              {% if item.ts %}
                {% if item.ts and item.ts|length >= 10 %}{% set d=item.ts[:10].split('-') %}{{d[1]|int}}/{{d[2]|int}}/{{d[0][2:]}}{% else %}—{% endif %}
                <span style="color:var(--paper3)"> · </span>
                {% set hr=item.ts[11:13]|int %}{% set mn=item.ts[14:16] %}
                {% set ampm='am' if hr < 12 else 'pm' %}
                {% set hr12=hr if hr<=12 else hr-12 %}{% set hr12=12 if hr12==0 else hr12 %}
                {{hr12}}:{{mn}}{{ampm}} CT
              {% else %}—{% endif %}
            </td>
            <td style="font-size:12px">{{item.restaurant or '—'}}</td>
            <td>
              <span style="font-size:12px;color:{{item.color or '#6b7280'}}">{{item.action}}</span>
            </td>
          </tr>
          {% endfor %}
        {% else %}
          <tr><td colspan="3" style="color:var(--ink3);font-style:italic;padding:16px">No activity yet.</td></tr>
        {% endif %}
        </tbody>
      </table>
    </div>
  </div>
</div>

<footer style="background:var(--ink);padding:14px 28px;display:flex;align-items:center;justify-content:space-between">
  <span style="font-size:11px;color:#4a4540">© 2026 Cavnar AI LLC</span>
  <div style="display:flex;gap:16px;align-items:center">
    <a href="https://cavnar.ai/privacy" target="_blank" style="font-size:11px;color:#4a4540;text-decoration:none">Privacy Policy</a>
    <a href="https://cavnar.ai" target="_blank" style="font-size:11px;color:#4a4540;text-decoration:none">cavnar.ai</a>
  </div>
</footer>
</body>
</html>"""


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
        return render_template_string("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reset Password — Cavnar AI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',sans-serif;background:#f5f3f0;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.card{background:white;border-radius:12px;padding:40px;width:100%;max-width:380px;border:1px solid #e5e0db}
.logo{font-size:20px;font-weight:600;margin-bottom:28px;color:#0e0c0a}
.logo em{color:#c84b2f;font-style:italic}
h2{font-size:16px;font-weight:600;margin-bottom:8px;color:#0e0c0a}
p{font-size:13px;color:#7a736a;line-height:1.6;margin-bottom:20px}
label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:#4a4540;display:block;margin-bottom:6px}
input{width:100%;padding:10px 12px;border:1px solid #e5e0db;border-radius:8px;font-family:'DM Sans',sans-serif;font-size:13px;outline:none;margin-bottom:14px}
input:focus{border-color:#c84b2f}
.btn{width:100%;padding:11px;background:#c84b2f;color:white;border:none;border-radius:8px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;cursor:pointer}
.back{display:block;text-align:center;margin-top:16px;font-size:12px;color:#7a736a;text-decoration:none}
.success{background:#f0faf4;border:1px solid #a7d7b8;border-radius:8px;padding:14px;font-size:13px;color:#2d6a4f;text-align:center;margin-bottom:16px}
</style></head><body>
<div class="card">
  <div class="logo">Cavnar <em>AI</em></div>
  {% if sent %}
  <div class="success">✓ If that email is in our system, a reset link is on its way. Check your inbox.</div>
  <a href="/login" class="back">Back to sign in</a>
  {% else %}
  <h2>Reset your password</h2>
  <p>Enter your account email and we'll send you a reset link.</p>
  <form method="POST">
    <label>Email address</label>
    <input type="email" name="email" placeholder="you@restaurant.com" required autofocus>
    <button type="submit" class="btn">Send reset link</button>
  </form>
  <a href="/login" class="back">Back to sign in</a>
  {% endif %}
</div>
</body></html>""", sent=sent)

    # POST — send reset email
    ip = _get_client_ip()
    if _is_rate_limited(ip):
        return render_template_string("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Rate Limited</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:'DM Sans',sans-serif;background:#f5f3f0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:white;border-radius:12px;padding:40px;width:100%;max-width:380px;border:1px solid #e5e0db;text-align:center}
.logo{font-size:20px;font-weight:600;margin-bottom:24px;color:#0e0c0a}.logo em{color:#c84b2f;font-style:italic}
p{font-size:13px;color:#7a736a;margin-bottom:16px}.back{font-size:12px;color:#7a736a;text-decoration:none}</style></head>
<body><div class="card"><div class="logo">Cavnar <em>AI</em></div>
<p>Too many attempts. Please wait a few minutes before trying again.</p>
<a href="/forgot-password" class="back">← Back</a></div></body></html>"""), 429
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
        return render_template_string("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reset Password — Cavnar AI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'DM Sans',sans-serif;background:#f5f3f0;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.card{background:white;border-radius:12px;padding:40px;width:100%;max-width:380px;border:1px solid #e5e0db}
.logo{font-size:20px;font-weight:600;margin-bottom:28px;color:#0e0c0a}
.logo em{color:#c84b2f;font-style:italic}
h2{font-size:16px;font-weight:600;margin-bottom:8px;color:#0e0c0a}
p{font-size:13px;color:#7a736a;line-height:1.6;margin-bottom:20px}
label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:#4a4540;display:block;margin-bottom:6px}
input{width:100%;padding:10px 12px;border:1px solid #e5e0db;border-radius:8px;font-family:'DM Sans',sans-serif;font-size:13px;outline:none;margin-bottom:14px}
input:focus{border-color:#c84b2f}
.btn{width:100%;padding:11px;background:#c84b2f;color:white;border:none;border-radius:8px;font-family:'DM Sans',sans-serif;font-size:13px;font-weight:600;cursor:pointer}
.back{display:block;text-align:center;margin-top:16px;font-size:12px;color:#7a736a;text-decoration:none}
.error{background:#fef2f2;border:1px solid #f5c6c2;border-radius:8px;padding:14px;font-size:13px;color:#c84b2f;margin-bottom:16px}
</style></head><body>
<div class="card">
  <div class="logo">Cavnar <em>AI</em></div>
  {% if not valid %}
  <div class="error">This reset link has expired or is invalid. <a href="/forgot-password" style="color:#c84b2f">Request a new one.</a></div>
  {% else %}
  <h2>Choose a new password</h2>
  <p>Must be at least 8 characters.</p>
  <form method="POST" id="resetForm">
    <label>New password</label>
    <input type="password" name="password" id="pw" placeholder="Min 8 characters" minlength="8" required>
    <label>Confirm password</label>
    <input type="password" name="confirm" id="cpw" placeholder="Confirm password" minlength="8" required>
    <div id="pw-err" style="font-size:12px;color:#c84b2f;margin-bottom:10px;display:none">Passwords don't match.</div>
    <button type="submit" class="btn">Set new password</button>
  </form>
  <script>
  document.getElementById('resetForm').onsubmit=function(e){
    if(document.getElementById('pw').value!==document.getElementById('cpw').value){
      e.preventDefault();
      document.getElementById('pw-err').style.display='block';
    }
  };
  </script>
  {% endif %}
  <a href="/login" class="back">Back to sign in</a>
</div>
</body></html>""", valid=valid)

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
            return render_template_string(LOGIN_HTML,
                error="Too many failed attempts. Please wait 5 minutes and try again.")
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        user = verify_password(username, password)
        if not user:
            _record_failed_attempt(ip)
            return render_template_string(LOGIN_HTML, error="Invalid username or password")
        _clear_attempts(ip)
        token = create_session(user["id"])
        next_url = request.args.get("next", "/admin" if user["is_admin"] else "/")
        resp = make_response(redirect(next_url))
        _on_railway = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
        resp.set_cookie("session_token", token, max_age=30*24*3600,
                        httponly=True, secure=_on_railway, samesite="Lax")
        return resp
    import secrets as _sec2
    csrf2 = _sec2.token_hex(16)
    resp2 = make_response(render_template_string(LOGIN_HTML, error=None))
    resp2.set_cookie("csrf_token", csrf2, httponly=True, samesite="Lax")
    return resp2

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
    try:
        labor = analyse_shifts_for_restaurant(rid)
    except Exception as e:
        print(f"Labor analysis error: {e}")
        labor = {"is_live":False,"total_labor_cost":0,"total_sales":0,"overall_labor_pct":0,
                 "overstaffed_days":[],"understaffed_days":[],"overtime_risk":[],
                 "dow_summary":{},"potential_savings":0,"labor_target":30.0,
                 "by_day":{},"employee_hours":{}}
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

    import secrets as _sec
    csrf_token = request.cookies.get('csrf_token') or _sec.token_hex(16)
    return render_template_string(DASHBOARD_HTML,
        show_welcome=show_welcome,
        csrf_token=csrf_token,
        current_user=current_user, restaurant=restaurant,
        rstats=rstats, reviews=reviews, rfilter=rfilter, rsearch=rsearch, top_issues=top_issues, platform_breakdown=platform_breakdown,
        labor=labor, inv=inv, ctypes=CONTENT_TYPES,
        mod_reviews=int(restaurant.module_reviews or 0),
        mod_labor=int(restaurant.module_labor or 0),
        mod_inventory=int(restaurant.module_inventory or 0),
        mod_marketing=int(restaurant.module_marketing or 0),
        now=datetime.now().strftime("%b %d, %Y"),
        viewing_as=current_user.get("is_admin", 0),
        labor_target=float(restaurant.labor_target_pct or 30.0) if restaurant else 30.0,
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
    # Auto-post to Google if connected and this is a Google review with a review_name
    try:
        from gmb import post_reply, is_connected
        conn = get_conn()
        row = conn.execute(
            "SELECT platform, draft_response, review_name FROM reviews WHERE id=? AND restaurant_id=?",
            (rid, current_user["restaurant_id"])
        ).fetchone()
        conn.close()
        if row and row["platform"] == "google" and row["review_name"] and row["draft_response"]:
            if is_connected(current_user["restaurant_id"]):
                result = post_reply(current_user["restaurant_id"], row["review_name"], row["draft_response"])
                if result["ok"]:
                    from models import mark_posted
                    mark_posted(rid)
                    return jsonify(ok=True, auto_posted=True)
                else:
                    print(f"[GMB] Auto-post failed for review {rid}: {result['error']}")
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
        prompt = f"""You are a restaurant reputation consultant reviewing this week's review data.
Restaurant: {rest_name}
{name_line}
Today: {today_str}

Review snapshot:
- Total reviews: {rstats['total']} | Avg rating: {rstats['avg_rating']} stars
- Sentiment: {rstats['positive']} positive, {rstats['neutral']} neutral, {rstats['negative']} negative
- Urgent (need immediate attention): {rstats['urgent']}
- Response rate: {rstats['response_rate']}% ({rstats['posted']} of {rstats['total']} responded to)
- Top mentioned topics (90 days): {issues_str}
- {wow_str}
- Recent urgent review excerpts: {urgent_texts}

Write a short review intelligence summary. Rules:
- No markdown, no bullet points, no bold, no asterisks
- No headers or labels
- Plain flowing prose, 3-5 sentences max
- Friendly and direct — like a trusted advisor
- Always use $ signs for dollar amounts
- Open with the most important signal this week (urgent reviews, rating trend, or response rate gap)
- If urgent reviews exist, address what they mention specifically
- If response rate is below 40%, mention the opportunity
- Close with one specific, actionable thing they should do today
- Never generic — always tied to the actual data"""

        msg = _client_ri.messages.create(
            model=os.getenv("CLAUDE_MODEL","claude-haiku-4-5-20251001"),
            max_tokens=350,
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
        insight = get_claude_insights(analysis, restaurant_name=name, owner_name=owner, restaurant_id=current_user["restaurant_id"])
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

# ── Admin routes ──────────────────────────────────────────────────────────────

@app.route("/api/regenerate-draft/<int:review_id>", methods=["POST"])
@login_required
def regenerate_draft(review_id, current_user):
    if _is_api_rate_limited(_get_client_ip()):
        return jsonify(ok=False, error="Too many requests. Please slow down."), 429
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
    from auth import update_last_login
    update_last_login(current_user["id"])
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
    from models import ensure_columns as _ec, init_email_log as _iel, init_onboarding_emails as _ioe
    _ec()
    _iel()
    _ioe()
    print("DB init OK")
except Exception as _e:
    print(f"DB init error: {_e}")

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

if __name__ == "__main__":
    init_db()
    init_auth()
    from models import init_staff_notes, ensure_columns, init_email_log, init_onboarding_emails
    init_staff_notes()
    ensure_columns()
    init_email_log()
    init_onboarding_emails()

    # Start background scheduler for digests and review fetching
    from scheduler import start_scheduler
    start_scheduler()

    # Create your admin account if it doesn't exist
    from models import create_restaurant, Restaurant, get_conn as gc
    conn = gc()
    existing_admin = conn.execute(
        "SELECT id FROM users WHERE username=?", (ADMIN_USERNAME,)
    ).fetchone()
    conn.close()

    if not existing_admin:
        admin_pw = os.getenv("ADMIN_PASSWORD", "changeme123")
        # Admin gets restaurant_id=1 (create a placeholder if needed)
        conn = gc()
        r = conn.execute("SELECT id FROM restaurants LIMIT 1").fetchone()
        conn.close()
        if not r:
            rid = create_restaurant(Restaurant(
                name="Cavnar AI Admin",
                owner_email="will@cavnar.ai",
            ))
        else:
            rid = r[0]
        create_user(rid, ADMIN_USERNAME, "will@cavnar.ai",
                    admin_pw, is_admin=True)
        # Set admin billing status to internal so it never affects MRR/client stats
        from models import get_conn as _gc2
        _c = _gc2()
        _c.execute("UPDATE restaurants SET billing_status='internal' WHERE id=?", (rid,))
        _c.commit(); _c.close()
        print(f"\n  Admin account created: {ADMIN_USERNAME} (password set from env)\n")

    # Create Ryan's test client account if it doesn't exist
    conn = gc()
    ryan_exists = conn.execute(
        "SELECT id FROM users WHERE email=?", ("ryancavnar@gmail.com",)
    ).fetchone()
    conn.close()

    if not ryan_exists:
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
        sample_reviews = [
            ("google", "rev_ryan_001", 5, "Absolutely incredible dinner. The Chilean sea bass melted in my mouth and our server was phenomenal. Best waterfront dining in Melbourne FL by far.", "positive", "Jennifer M."),
            ("google", "rev_ryan_002", 2, "Waited 40 minutes past our reservation. The prime rib was overcooked and came out cold. Manager never came by to check on us. Disappointed for the price point.", "negative", "David K."),
            ("yelp",   "rev_ryan_003", 5, "Celebrated my anniversary here. The filet mignon and lobster tail combo was perfect. Sunset views from the patio are absolutely stunning. Will be back every year.", "positive", "Sarah T."),
            ("google", "rev_ryan_004", 4, "Great happy hour on the outdoor patio — firecracker shrimp and craft cocktails were excellent. Service was a bit slow but the food made up for it.", "neutral",  "Mike R."),
            ("yelp",   "rev_ryan_005", 1, "Food was cold, service was rude, and the lobster bisque tasted like it came from a can. For these prices I expected much better. Will not return.", "negative", "Amanda L."),
            ("google", "rev_ryan_006", 5, "The Chart House Cut prime rib is legendary. Been coming here for 10 years and it never disappoints. Lagoon views at sunset are unmatched in Brevard County.", "positive", "Robert H."),
            ("google", "rev_ryan_007", 3, "Hit or miss experience. The tuna tartare appetizer was excellent but my Mac Nut Mahi came out overcooked. Staff was friendly though.", "neutral",  "Lisa C."),
            ("yelp",   "rev_ryan_008", 5, "Best restaurant on the lagoon hands down. The mud pie dessert is a must. Our server Danny made the whole evening special with his knowledge of the menu.", "positive", "Tom W."),
        ]
        from zoneinfo import ZoneInfo as _ZI_r
        from datetime import datetime as _dt_r
        _now_r = _dt_r.now(_ZI_r('America/Chicago')).strftime('%Y-%m-%dT%H:%M:%S')
        for platform, ext_id, rating, text, sentiment, name in sample_reviews:
            _conn_r.execute("""
                INSERT OR IGNORE INTO reviews
                (restaurant_id, platform, external_id, author, rating, text, sentiment,
                 fetched_at, review_date, response_status, processed, review_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ryan_rid, platform, ext_id, name, rating, text, sentiment,
                    _now_r, _now_r, 'pending', 1, name))
        _conn_r.commit()
        _conn_r.close()

        # Analyse Ryan's reviews so categories/sentiment are populated (needed for top issues)
        try:
            from analyser import analyse_pending
            analyse_pending(ryan_rid, limit=50)
            print("  Ryan's reviews analysed.\n")
        except Exception as _ae:
            print(f"  Analyse error: {_ae}")

        # Draft responses for Ryan's reviews
        try:
            from drafter import draft_pending
            draft_pending(ryan_rid, limit=50)
            print("  Ryan's reviews seeded and drafted.\n")
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

    print(f"\n  Hosted dashboard → http://localhost:{PORT}")
    print(f"  Admin panel      → http://localhost:{PORT}/admin\n")
    app.run(host="0.0.0.0", port=PORT, debug=False)
